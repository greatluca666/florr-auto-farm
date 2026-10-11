"""刷怪奖励 —— 纯状态机, 不碰 CDP, 不依赖 Gym。

设计(用户 2026-09-21 拍板):

  每步立即   Δ自身血量 × hp_weight、存活 +survival、死亡 +death
  击杀结算   稀有度权重 × (托管的累计掉血 + kill_bonus)
  掉落落地   Δ花瓣数 × 稀有度权重

**托管结算(escrow)**是这里的核心, 也是最容易写错的一块: 打掉的血不当场发分, 先挂账;
只有游戏自己的击杀计数器真的 +1 才兑现; 怪跑了/被别人补刀/一直没死 -> 挂账作废。
这样别人抢走的怪, 你砍的那几刀一分不得 —— 多人抢怪的归因问题自然消失。

**挂账按稀有度归类, 不按物种。** 权重只跟稀有度有关, 同稀有度的挂账兑现谁都一样, 所以
不需要把 canvas 解出的中文怪名映射到计数器里的英文 sid —— 那层映射本身就是一个坑
(物种表按图分流、别名表只覆盖部分生态)。少一个能出错的地方。

**代价, 明写在这**: 奖励因此是稀疏的 —— 只有击杀那一刻才结算。用户选择了不做行为克隆
冷启动、从随机初始化直接在线训练, 所以前期信号会很弱。为了让"学不动"和"步数不够"能分
得开, step() 的 info 里逐项拆开了每一项的贡献, 训练时直接记日志看。
"""
import math

# 稀有度权重: (档位+1)/10。不用 rank/9 —— 那样普通怪恒为 0 分, 而刷怪区绝大多数是普通怪,
# 策略会学成完全无视它们。跟 farm_baseline.py 用的是同一套, 两边数字可直接比。
MAX_RANK = 9


def rarity_weight(rank):
    if rank is None or rank < 0:
        return 0.0
    return (min(rank, MAX_RANK) + 1) / 10.0


# 逐帧认怪: 上一帧里**同名**、离它最近、且在这个距离内的那只就是它。
#
# 原来是按 20 单位的格子取整当编号 —— 怪一跨格线编号就变, 那一拍的掉血读不到。实测
# 一只每拍走 6 单位的怪, 20 拍里编号换了 6 次, 三成的拍白丢, 而且一声不响。
#
# 40: 每拍位移(7.4Hz)实测不到 10 单位, 留足余量; 又小于两只同名怪挨在一起时的中心距
# (怪会互相推开, 实测半径 15~32, 中心距至少 30 上下) —— 真挨得那么近时, 按距离贪心
# 配对也会先把各自最近的那个配上。
MATCH_RADIUS = 40.0


class RewardTracker:
    """跨步维护托管挂账和上一步的血量。一局一个实例, reset() 重来。

    hp_clip: 一步之内血量变化超过这个比例, 判定为解码读错, 这一步不给奖不给罚, 并把基线
             清空(下一步重新建立)。玩家和怪各有这道闸。
    escrow_timeout_s: 挂账多久没再掉血就作废。怪跑出视野/被别人拖走都属于这种。
    attack_range: 只有这个世界距离内的怪掉血才挂账 —— 更远的不可能是你打的。

    **survival 为什么是 0**(2026-09-24 实机 29 局改的, 原值 0.01):
    0.01/步 × 7.4Hz × 300 秒 = **跑满一局白拿 +22**, 而一次击杀实测只值 +1.2
    (从满局反推: 84.4/67、50.0/46、70.3/57)。于是"躲满一局"= 18 次击杀, 零风险 ——
    策略真的学会了这一手: 第 30 轮 2120 步(全场最多)只有 3 杀, 总回报 +22.21 而
    2120×0.01=21.2, 中间 250 秒 `怪中位 0~1`; 第 38 轮击杀从 76 秒起冻在 46 不动,
    一冻 222 秒。更糟的是这种局跑得满、梯度更新最多(33/34 次), 而死得快的局只有
    0~3 次 —— 每个梯度步里"站着不动"被强化的次数高一个数量级。29 局的击杀均值
    从前半程 23.2 掉到后半程 14.1。

    去掉它**不会**让"别死"的压力消失: 死亡时 terminated=True, sb3 bootstrap V=0,
    后面所有回报本来就全部没收 —— 那才是真正的存活压力。survival 是在那之上又白送
    一笔, 而且是唯一一笔"什么都不干也拿得到"的。

    **death 为什么是 -5**(原值 -1.0): -1.0 比一次击杀(+1.2)还便宜, 死亡在账面上
    几乎不算事。-5 ≈ 四次击杀。

    **idle 空转惩罚**(用户 2026-09-25): 周围没怪时策略就停在原地 —— 视野里没怪时所有
    输入都一样, 学不到"该往哪走", 停着不挨打不掉血, 账面零成本。连续 idle_grace_s 秒
    没打到怪(没造成伤害、也没击杀), 之后每秒扣 idle_per_s。0.05/秒: 多空转一分钟
    ≈ -3, 两三次击杀, 比死一次(-5)轻。每一段(reset)重新给宽限 —— 脚本拉回/复活走路
    那几十秒不是策略在空转; 卡顿拍不扣(跨了多久不可信)。
    """

    def __init__(self, *, survival=0.0, hp_weight=1.0, death=-5.0, kill_bonus=1.0,
                 drop_weight=1.0, engage_weight=1.0, attack_range=200.0,
                 hp_clip=0.5, escrow_timeout_s=5.0, idle_grace_s=15.0, idle_per_s=0.05):
        self.survival = survival
        self.hp_weight = hp_weight
        self.death = death
        self.kill_bonus = kill_bonus
        self.drop_weight = drop_weight
        self.engage_weight = engage_weight
        self.attack_range = attack_range
        self.hp_clip = hp_clip
        self.escrow_timeout_s = escrow_timeout_s
        self.idle_grace_s = idle_grace_s
        self.idle_per_s = idle_per_s
        self.reset()

    def reset(self):
        self._prev_hp = None
        # 上一帧认出来的怪: 编号 -> {"name","x","y","hp"}。hp 为 None = 基线作废, 下一帧重建。
        self._tracks = {}
        self._next_id = 0
        # rank -> {怪编号: [累计掉血, 最后一次掉血的时刻]} —— **每只怪一笔**。
        # 原来是每挨一拍记一笔, 击杀只兑现最大的那一拍: 挨 9 拍才死的怪只结算 1/9。
        self._escrow = {}
        self._last_hit_t = None     # 上一次打到怪(伤害/击杀)的时刻; None = 这一段还没开始
        self._prev_t = None

    def _idle(self, t, hit):
        """这一步的空转惩罚(<= 0)。hit: 这一步打到怪了没有。"""
        prev, self._prev_t = self._prev_t, t
        if hit or self._last_hit_t is None:
            self._last_hit_t = t
            return 0.0
        if prev is None or t - self._last_hit_t <= self.idle_grace_s:
            return 0.0
        # 只扣宽限期之后的那部分时间
        start = max(prev, self._last_hit_t + self.idle_grace_s)
        return -self.idle_per_s * max(0.0, t - start)

    # ---------------------------------------------------------------- 内部
    def _track(self, mobs):
        """把这一帧的怪接到上一帧的怪上, 返回 [(编号, 怪, 上一帧的血或 None), ...]。

        按距离贪心配对: 所有"同名且在 MATCH_RADIUS 内"的候选对从近到远, 两边都没被
        占用才配上。配不上的算新出现的怪, 发新编号。
        """
        pairs = []
        for i, mob in enumerate(mobs):
            for tid, prev in self._tracks.items():
                if prev["name"] != mob.get("name"):
                    continue
                d = math.hypot(mob["x"] - prev["x"], mob["y"] - prev["y"])
                if d <= MATCH_RADIUS:
                    pairs.append((d, i, tid))
        pairs.sort()
        matched, used = {}, set()
        for _, i, tid in pairs:
            if i not in matched and tid not in used:
                matched[i] = tid
                used.add(tid)

        out, tracks = [], {}
        for i, mob in enumerate(mobs):
            tid = matched.get(i)
            prev_hp = None
            if tid is None:
                tid = self._next_id
                self._next_id += 1
            else:
                prev_hp = self._tracks[tid]["hp"]
            tracks[tid] = {"name": mob.get("name"), "x": mob["x"], "y": mob["y"],
                           "hp": mob.get("hp")}
            out.append((tid, mob, prev_hp))
        self._tracks = tracks
        return out

    def _accrue(self, t, mobs, player):
        """怪掉血 -> 挂账。返回这一步新挂上去多少(只为诊断, 不是奖励)。"""
        accrued = 0.0
        px, py = player["x"], player["y"]
        for tid, mob, prev in self._track(mobs):
            hp = mob.get("hp")
            if hp is None or prev is None:
                continue
            drop = prev - hp
            if drop <= 0:
                continue
            if drop > self.hp_clip:
                # 跳变太大 = 解码读错, 不是真打中。丢掉基线, 下一步重建。
                self._tracks[tid]["hp"] = None
                continue
            if math.hypot(mob["x"] - px, mob["y"] - py) > self.attack_range:
                continue
            entry = self._escrow.setdefault(mob.get("rarity_rank"), {}).setdefault(tid, [0.0, t])
            entry[0] += drop
            entry[1] = t
            accrued += drop
        return accrued

    def _expire(self, t):
        """超时的挂账作废。返回作废掉多少(诊断用)。

        超时从这只怪**最后一次**掉血算起 —— 一只怪打了 8 秒, 头几拍的血不能先作废。
        """
        lost = 0.0
        for rank, bucket in list(self._escrow.items()):
            for tid, (amount, last_t) in list(bucket.items()):
                if t - last_t > self.escrow_timeout_s:
                    lost += amount
                    del bucket[tid]
            if not bucket:
                self._escrow.pop(rank)
        return lost

    def _settle(self, kill_events):
        """击杀计数器涨了 -> 兑现对应稀有度的挂账。

        同稀有度有多只怪挂着账时, 先兑现**累计掉血最多**的那只 —— 打得最狠的那只最可能就是
        刚死的那只。兑现不了(没挂账, 比如怪是被队友打残你补的最后一刀)也照给 kill_bonus:
        计数器涨了就是你杀的, 这是游戏自己判的, 比我们的挂账可靠。
        """
        total = 0.0
        settled = 0
        for rank, n in kill_events:
            w = rarity_weight(rank)
            bucket = self._escrow.get(rank, {})
            for _ in range(n):
                damage = 0.0
                if bucket:
                    tid = max(bucket, key=lambda k: bucket[k][0])
                    damage = bucket.pop(tid)[0]
                total += w * (self.engage_weight * damage + self.kill_bonus)
                settled += 1
            if not bucket:
                self._escrow.pop(rank, None)
        return total, settled

    def _drops(self, drop_events):
        return self.drop_weight * sum(rarity_weight(r) * n for r, n in drop_events)

    def death_penalty(self):
        """外部(脚本截屏看到死亡结算画面)触发的死亡惩罚。

        为什么需要这条路: 死亡结算画面上血条已经没了, canvas 解不出玩家, 于是 step()
        里 `hp <= 0` 那个判据**永远不会成立** —— 实机(2026-09-24)就是这样, 死了一轮
        又一轮, 一次死亡惩罚都没发出去。"死亡有惩罚"整条设计等于没实现。

        顺带把挂账全部作废: 人都死了, 那些打掉的血不会再有击杀去兑现它们。
        """
        self._escrow.clear()
        self._prev_hp = None
        self._tracks = {}
        return self.death

    def settle_counters(self, kill_events=(), drop_events=()):
        """只结算游戏计数器(击杀兑现挂账 + 掉落), 不碰存活/血量。返回 (reward, info)。

        给"这一拍不归策略"的那些拍用(脚本接管/死亡结算): 后端是**先**读计数器、**再**
        问仲裁器谁在控, 所以这一拍读到的击杀发生在交出控制权之前 —— 是策略打的。
        """
        kill, settled = self._settle(kill_events)
        drop = self._drops(drop_events)
        parts = {"survival": 0.0, "hp": 0.0, "death": 0.0, "kill": kill, "drop": drop,
                 "idle": 0.0}
        return kill + drop, self._info(parts, kill + drop, settled=settled)

    # ---------------------------------------------------------------- 主入口
    def step(self, *, t, player, mobs, kill_events=(), drop_events=(), stalled=False):
        """算这一步的奖励。

        t:            单调时钟(秒)
        player:       {"hp": 0~1 或 None, "x", "y"}; None = 这一帧解不出玩家
        mobs:         [{"x","y","hp","rarity_rank","name"}, ...]
        kill_events:  [(稀有度档位, 个数), ...] 本步击杀计数器的增量
        drop_events:  [(稀有度档位, 个数), ...] 本步花瓣数的增量
        stalled:      这一步跨的真实时间不可信(掉帧/卡顿)

        返回 (reward, info)。info 逐项拆开, 训练时直接记日志 —— 从零开始学时, "学不动"和
        "步数不够"必须分得开, 光看总回报分不出来。
        """
        parts = {"survival": 0.0, "hp": 0.0, "death": 0.0, "kill": 0.0, "drop": 0.0,
                 "idle": 0.0}
        died = player is not None and player.get("hp") is not None and player["hp"] <= 0.0

        if stalled:
            # 这一步跨的时间不明, 存活/掉血/挂账全不算 —— 但死亡是事实, 照罚; 击杀和
            # 掉落是游戏计数器给的, 也是事实, 照结。原来卡顿拍整个作废: 每批 rollout 的
            # 第一拍(刚做完梯度更新)必卡, 仲裁器每秒截屏那一拍也可能卡, 落在这些拍上的
            # 击杀一分不给。
            if died:
                parts["death"] = self.death
            self._prev_hp = None if player is None else player.get("hp")
            # 怪照样认(不然卡一拍所有怪都换新编号, 挂账被劈成两笔), 基线换成这一帧的
            # 读数 —— 只是这一拍掉的血不挂账。超时也不在这拍判: 时间本身不可信。
            self._track(mobs)
            parts["kill"], settled = self._settle(kill_events)
            parts["drop"] = self._drops(drop_events)
            self._prev_t = t                       # 卡顿拍不扣空转, 但时钟往前挪
            if settled:
                self._last_hit_t = t
            reward = sum(parts.values())
            return reward, self._info(parts, reward, stalled=True, died=died, settled=settled)

        parts["survival"] = self.survival

        hp = None if player is None else player.get("hp")
        if hp is not None and self._prev_hp is not None:
            delta = hp - self._prev_hp
            if abs(delta) > self.hp_clip:
                hp = None          # 读错了 —— 清空基线, 这一步不算血量项
            else:
                parts["hp"] = self.hp_weight * delta
        if died:
            parts["death"] = self.death
        self._prev_hp = hp

        accrued, expired = 0.0, 0.0
        if player is not None:
            accrued = self._accrue(t, mobs, player)
            expired = self._expire(t)
        settled_reward, settled_n = self._settle(kill_events)
        parts["kill"] = settled_reward

        parts["drop"] = self._drops(drop_events)
        parts["idle"] = self._idle(t, hit=accrued > 0 or settled_n > 0)

        reward = sum(parts.values())
        return reward, self._info(parts, reward, died=died, accrued=accrued,
                                  expired=expired, settled=settled_n)

    def _info(self, parts, reward, *, stalled=False, died=False, accrued=0.0,
              expired=0.0, settled=0):
        return {
            "reward": reward,
            "parts": parts,
            "stalled": stalled,
            "died": died,
            "escrow_accrued": round(accrued, 4),     # 这步新挂上去的掉血
            "escrow_expired": round(expired, 4),     # 这步超时作废掉的
            "escrow_settled": settled,               # 这步兑现了几笔
            "escrow_open": sum(len(b) for b in self._escrow.values()),
        }
