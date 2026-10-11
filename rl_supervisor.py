"""刷怪区里谁在控 —— 策略只管打, 出区/AFK/菜单仍归脚本。

后端每拍调一次 control(), 这里回一个字符串: "policy" 就继续让策略打, 别的都让环境
把这一段截断(那些拍不是策略干的, 不能进 rollout)。

## 为什么要节流

脚本那几个检查全都要截屏: get_player_position 扫小地图、on_start_screen 比像素。
一次几十毫秒, 每拍都做的话 10Hz 根本跑不起来 —— 而策略的一拍预算总共才 100ms。
所以位置/菜单这类按 check_interval 节流(默认 1 秒), 中间那些拍直接放给策略。

节流不会漏掉要紧的事:
  * 死亡**在**这里管, 而且**就地复活, 不结束这一局**(见 control() 里的长注释 ——
    结束这一局会让 sb3 把没收满的 rollout 整个丢掉, 而被丢掉的恰好是死得快的那些局)
  * 出区最多晚 1 秒发现。花朵一秒走不出多远, 而且出区本来就只是"该寻路回去"而不是
    "出事了"
  * AFK 弹窗那个检查只是读一个日志文件, 不截屏, 所以每拍都做 —— 而且发现之后是
    **原地等到解完**, 不是只标一下就放行(见 control() 里的说明)

## 阻塞是有意的

path_back() 要跑寻路, 几秒起步。它就在 control() 里同步跑完 —— 这一拍本来就不是
策略的, 环境会截断; 下一拍的 dt 会很大, 奖励那边判 stalled 直接作废。比起让策略
在"人正在被拖回去"的那几秒里继续学, 这样干净。
"""
import time

# 这些是"这一轮刷怪该结束了, 交回上层"的理由; control() 回这些值时 runner 要停。
STOP_REASONS = ("dead", "menu", "lost")


class FarmingSupervisor:
    """所有外部动作都靠注入 —— 这样整套判定逻辑能在不碰游戏的情况下测。

    position:        () -> (x, y) | None      小地图坐标, 截屏
    in_area:         (pos) -> bool            纯判断
    path_back:       (pos) -> bool            寻路回刷怪区, 回是否成功
    afk_paused:      () -> bool               读日志, 便宜, 每拍都调
    on_start_screen: () -> bool               截屏
    on_death_screen: () -> bool               截屏
    yield_input:     () -> None                交出输入(松键、光标压回花朵)。做任何
                                              阻塞的事之前调 —— 见 rl_backend.release
    revive:          () -> bool               就地复活(点继续/点开始/寻路回刷怪区),
                                              回是否成功。给 None 表示不会复活, 那就
                                              像以前一样把这一局交回上层
    max_revives:     一次死亡最多复活几回。复活后走回刷怪区的半路上又死了(死亡画面还在)
                     就再来一回; 别的原因失败不重来
    max_misses:      连续几次读不到位置就认输(截屏偶尔失败是常事, 一次就放弃太脆)
    max_jump:        两次检查之间最多能挪多远(小地图单位)。超了就当读错, 复读一次,
                     两次对得上才认
    """

    def __init__(self, *, position, in_area, path_back, afk_paused=None,
                 on_start_screen=None, on_death_screen=None, revive=None,
                 yield_input=None, check_interval=1.0, max_revives=3,
                 max_misses=5, max_jump=80.0, afk_max_wait=180.0, afk_poll=0.2,
                 clock=time.monotonic, sleep=time.sleep):
        self._position = position
        self._in_area = in_area
        self._path_back = path_back
        self._afk_paused = afk_paused or (lambda: False)
        self._on_start_screen = on_start_screen or (lambda: False)
        self._on_death_screen = on_death_screen or (lambda: False)
        self._revive = revive
        self.yield_input = yield_input or (lambda: None)
        self.deaths = 0
        self.max_revives = max_revives
        self.check_interval = check_interval
        self.max_misses = max_misses
        self.max_jump = max_jump
        self.afk_max_wait = afk_max_wait
        self.afk_poll = afk_poll
        self.clock = clock
        self.sleep = sleep
        self.afk_waits = 0
        self.stop_reason = None
        self.reason = None
        self.last_position = None
        self.misses = 0
        self.checks = 0
        self.rejected_jumps = 0
        self._next_check = -1e18      # 第一拍就查一次: 刚交接过来, 不知道人在哪

    def _due(self):
        now = self.clock()
        if now < self._next_check:
            return False
        self._next_check = now + self.check_interval
        return True

    def control(self):
        """这一拍谁在控。"policy" 以外的值都会让环境截断这一段。"""
        if self.stop_reason:
            return self.stop_reason
        if self._afk_paused():
            # **原地等到弹窗解完, 不是只标一下就放行。** 只标不等的话, 这一拍虽然被
            # 环境截断了, 但动作已经发出去了 —— 弹窗那几十秒里策略每拍都在推鼠标,
            # 正好跟解 AFK 的那个工具抢光标。规则战斗那边一直是 sleep+continue,
            # 一点输入都不发; 这里要一样。
            self.reason = "afk"
            self.afk_waits += 1
            self.yield_input()      # 弹窗期间别占着键和光标
            end = self.clock() + self.afk_max_wait
            while self._afk_paused() and self.clock() < end:
                self.sleep(self.afk_poll)
            # 等完了重设节流基准 —— 弹窗可能挡了很久, 拿等之前的时间当基准会让
            # 回来第一拍立刻又截一次屏。
            self._next_check = self.clock() + self.check_interval
            return "script"
        if not self._due():
            self.reason = None
            return "policy"

        self.checks += 1
        if self._on_death_screen():
            return self._handle_death()
        if self._on_start_screen():
            self.stop_reason = self.reason = "menu"
            return "menu"

        pos = self._confirmed_position()
        if pos is None:
            # 截屏偶尔读不到位置是常事(掉帧/画面闪), 连着好几次才是真出事了。
            self.misses += 1
            self.reason = "no-position"
            if self.misses >= self.max_misses:
                self.stop_reason = self.reason = "lost"
                return "lost"
            return "script"
        self.misses = 0
        self.last_position = pos

        if not self._in_area(pos):
            self.reason = "out-of-area"
            self.yield_input()      # 寻路期间 shift 还按着的话花朵是减速的, 一路卡住
            if not self._path_back(pos):
                # **寻路失败最常见的原因, 就是它自己在半路上撞见了死亡结算画面** ——
                # lazy_theta_pathing 每次循环开头都查那两个画面, 查到就交回上层。
                # 实机(2026-09-24)一份 29 局的日志里 12 局是这么结束的, 其中 4 局
                # `梯度更新: 0 次`: 判 "lost" 的话这一局没有死亡惩罚、不复活, 那批
                # 没收满的 rollout 被 sb3 整个丢掉 —— 丢掉的又正好是"怎么会死"的
                # 证据。走死亡那条路才对, 它管发惩罚也管就地复活。
                if self._on_death_screen():
                    return self._handle_death()
                self.stop_reason = self.reason = "lost"
                return "lost"
            # 寻路刚跑完, 别拿跑之前的时间当基准 —— 否则回来第一拍立刻又查一次。
            self._next_check = self.clock() + self.check_interval
            return "script"

        self.reason = None
        return "policy"

    def _handle_death(self):
        """死亡这一拍怎么处理 —— 发惩罚(靠回 "dead")并尽量就地复活。

        两个入口: 仲裁器自己查到死亡画面; 以及 path_back 失败之后复查发现是死亡
        画面(lazy_theta_pathing 在半路上先撞见它了)。
        """
        # 死亡必须由这里发现: 死亡结算画面上血条没了, 后端从画面解不出玩家, 环境
        # 那条 hp<=0 的判据永远不成立。
        #
        # **能复活就地复活, 不结束这一局。** 用户实机指出的问题: 一局结束就意味着
        # learn() 返回, 而没收满的那批 rollout 被 sb3 整个丢弃 —— 被丢掉的恰好是
        # "死得快"的那些局。实测 25 局里只有 3 局进了训练, 其余 22 局全是几十步就
        # 死的。训练数据因此被系统性筛成"活得久的样本", 而"怎么会死"的证据成建制
        # 地扔掉。那它当然学不会活下来。
        #
        # 复活之后这一拍仍然回 "dead": 环境照样终止并发死亡惩罚, 那一步**留在
        # 缓冲区里**会被训练到; sb3 自己会 reset 接着收同一批 rollout。
        self.deaths += 1
        self.reason = "dead"
        if self._revive is None:
            self.stop_reason = "dead"
            return "dead"
        self.yield_input()      # 复活要点按钮 + 寻路, 几十秒起步
        # 走回刷怪区的半路上又死了 = revive 回 False 而死亡画面还在。实机(2026-09-24)
        # 20 轮日志里 9 局 "lost" 全是这样, 7 局 `梯度更新: 0 次`。那是脚本走路时死的,
        # 不是"复活不了" —— 再点一次继续接着走。
        ok = False
        for _ in range(self.max_revives):
            ok = self._revive()
            if ok or not self._on_death_screen():
                break
        self._next_check = self.clock() + self.check_interval
        if not ok:
            self.stop_reason = self.reason = "lost"
            return "lost"
        self.last_position = None      # 复活点跟死亡点无关, 别拿它做跨度判断
        return "dead"

    def _confirmed_position(self):
        """读一次位置; 跨度离谱就复读一次, 两次对得上才认。

        实机(2026-09-24): 刚确认在 (17,56), 一秒后读出 (249,293) —— 花朵不可能一秒跑
        250 个单位。仲裁器拿这个坏读数当真, 花 11 秒规划一条"回刷怪区"的路(lazy theta*
        从地图另一头起算), 规划完才发现人早死了。连着四轮各烧掉一分钟。

        坏读数不会连着出现两次相同的值, 所以复读就能筛掉; 而真的跨了一大步(传送/重连
        落点变了)两次会对得上, 照常放行。代价是多截一次屏, 只在跨度离谱时才付。
        """
        pos = self._position()
        if pos is None or self.last_position is None:
            return pos
        if self._dist(pos, self.last_position) <= self.max_jump:
            return pos
        again = self._position()
        if again is not None and self._dist(again, pos) <= self.max_jump:
            return again          # 两次一致 -> 是真跨了一大步
        self.rejected_jumps += 1
        return None               # 当成"这次没读到", 走 miss 那条路

    @staticmethod
    def _dist(a, b):
        return ((a[0] - b[0]) ** 2 + (a[1] - b[1]) ** 2) ** 0.5

    def stats(self):
        return {"checks": self.checks, "misses": self.misses, "afk_waits": self.afk_waits,
                "deaths": self.deaths,
                "rejected_jumps": self.rejected_jumps,
                "stop_reason": self.stop_reason, "last_position": self.last_position}
