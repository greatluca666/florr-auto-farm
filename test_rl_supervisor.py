"""谁在控的测试。全部注入, 不碰游戏也不截屏。"""
import pytest

from rl_supervisor import FarmingSupervisor


class Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t

    def tick(self, dt=0.1):
        self.t += dt


def sup(**kw):
    c = kw.pop("clock", None) or Clock()
    calls = {"position": 0, "start_screen": 0, "path_back": []}

    def position():
        calls["position"] += 1
        return kw.pop("_pos", None) if False else pos_box[0]

    def start_screen():
        calls["start_screen"] += 1
        return menu_box[0]

    def path_back(p):
        calls["path_back"].append(p)
        c.tick(path_secs[0])          # 寻路是真的要花几秒, 时钟得跟着走
        return path_ok[0]

    pos_box = [kw.pop("pos", (100, 100))]
    menu_box = [kw.pop("menu", False)]
    path_ok = [kw.pop("path_back_ok", True)]
    path_secs = [kw.pop("path_secs", 0.0)]
    # sleep 必须推动假时钟 —— AFK 那段是"等到弹窗解完"的阻塞循环, 时钟不走的话
    # 超时永远到不了, 测试不是变红而是**挂死**。
    s = FarmingSupervisor(position=position, in_area=kw.pop("in_area", lambda p: True),
                          path_back=path_back, on_start_screen=start_screen,
                          clock=c, sleep=lambda dt: c.tick(dt), **kw)
    s._t = c
    s._calls = calls
    s._pos_box = pos_box
    s._menu_box = menu_box
    s._path_ok = path_ok
    return s


# ── 节流 ─────────────────────────────────────────────────────────────────────

def test_first_tick_always_checks():
    """刚从脚本手里接过来, 不知道人在哪 —— 不能先放一秒再说。"""
    s = sup()
    assert s.control() == "policy"
    assert s._calls["position"] == 1


def test_the_expensive_checks_are_throttled():
    """位置和菜单判定都要截屏, 一次几十毫秒。每拍都做的话 10Hz 根本跑不起来 ——
    策略一拍的总预算才 100ms。"""
    s = sup(check_interval=1.0)
    s.control()
    for _ in range(9):
        s._t.tick(0.1)
        assert s.control() == "policy"
    assert s._calls["position"] == 1          # 这 10 拍只查了一次
    s._t.tick(0.2)
    s.control()
    assert s._calls["position"] == 2


def test_afk_is_polled_every_tick():
    """AFK 那个检查只读日志文件, 不截屏 —— 便宜, 所以不跟着节流走。"""
    calls = [0]

    def paused():
        calls[0] += 1
        return calls[0] <= 2          # 头两次说"在弹窗", 之后解完

    s = sup(afk_paused=paused, afk_poll=0.1)
    assert s.control() == "script"
    assert s._calls["position"] == 0          # 没去截屏
    assert s.control() == "policy"


def test_afk_blocks_until_the_popup_is_solved():
    """**原地等到解完, 不是只标一下就放行。** 只标不等的话这一拍虽然被环境截断了,
    但动作已经发出去了 —— 弹窗那几十秒里策略每拍都在推鼠标, 正好跟解 AFK 的那个
    工具抢光标。规则战斗那边一直是 sleep+continue, 一点输入都不发。"""
    n = [0]

    def paused():
        n[0] += 1
        return n[0] <= 5

    s = sup(afk_paused=paused, afk_poll=0.2)
    assert s.control() == "script"
    assert n[0] >= 5                          # 一直问到它说解完
    assert s.stats()["afk_waits"] == 1


def test_afk_wait_has_a_ceiling():
    """弹窗一直不解(解题工具挂了/日志卡住)的话, 不能就这么无限等下去 —— 上面那个
    deadline 才有机会把这一局收掉。

    **问到第 100 次就抛**, 不是靠"跑太久"来判定: 上限没了的话这条测试会挂死而不是
    变红, 而挂死的测试什么也证明不了(变异验证时实测如此)。
    """
    n = [0]

    def paused():
        n[0] += 1
        if n[0] > 100:
            raise AssertionError("没有上限, 一直在等 AFK")
        return True

    s = sup(afk_paused=paused, afk_max_wait=5.0, afk_poll=0.5)
    assert s.control() == "script"
    assert n[0] <= 12                         # 5.0 / 0.5 = 10 轮左右


def test_the_throttle_restarts_after_an_afk_wait():
    """弹窗可能挡了几十秒。拿等之前的时间当基准的话, 回来第一拍立刻又截一次屏。"""
    n = [0]
    s = sup(afk_paused=lambda: n.__setitem__(0, n[0] + 1) or n[0] <= 3,
            afk_poll=1.0, check_interval=1.0)
    s.control()
    before = s._calls["position"]
    s.control()
    assert s._calls["position"] == before


# ── 出区 ─────────────────────────────────────────────────────────────────────

def test_out_of_area_paths_back_and_is_not_policy_control():
    """被拖回刷怪区的那几秒不是策略干的 —— 算进 rollout 的话, 它会以为自己学会了
    长途奔袭。"""
    s = sup(in_area=lambda p: False, pos=(9, 9))
    assert s.control() == "script"
    assert s._calls["path_back"] == [(9, 9)]


def test_path_back_failure_ends_the_round():
    s = sup(in_area=lambda p: False, path_back_ok=False)
    assert s.control() == "lost"
    assert s.stop_reason == "lost"


def test_the_throttle_restarts_after_pathing_back():
    """寻路要跑好几秒。拿跑之前的时间当基准的话, 回来第一拍立刻又查一次, 白截一次屏。"""
    s = sup(in_area=lambda p: False, check_interval=1.0, path_secs=5.0)
    s.control()                               # 这一下里面的寻路花了 5 秒
    assert s._calls["position"] == 1
    s.control()
    assert s._calls["position"] == 1          # 还在节流窗口里, 没再查


# ── 读不到位置 ───────────────────────────────────────────────────────────────

def test_one_missed_position_is_not_fatal():
    """截屏偶尔读不到位置是常事(掉帧/画面闪)。一次就放弃, 一晚上要断几十回。"""
    s = sup(max_misses=5)
    s._pos_box[0] = None
    assert s.control() == "script"
    assert s.stop_reason is None


def test_enough_missed_positions_ends_the_round():
    s = sup(max_misses=3, check_interval=0.0)
    s._pos_box[0] = None
    assert [s.control() for _ in range(3)] == ["script", "script", "lost"]


def test_a_good_read_clears_the_miss_streak():
    s = sup(max_misses=3, check_interval=0.0)
    s._pos_box[0] = None
    s.control()
    s.control()
    s._pos_box[0] = (50, 50)
    assert s.control() == "policy" and s.misses == 0
    s._pos_box[0] = None
    assert s.control() == "script"            # 重新从 1 开始数, 不是直接到 3
    assert s.stop_reason is None


# ── 终止 ─────────────────────────────────────────────────────────────────────

def test_menu_ends_the_round():
    s = sup(menu=True)
    assert s.control() == "menu" and s.stop_reason == "menu"


def test_a_stop_reason_sticks():
    """停了就别再让策略动 —— 后面那些拍, 人可能已经在死亡结算画面上了。"""
    s = sup(menu=True, check_interval=0.0)
    s.control()
    s._menu_box[0] = False
    assert s.control() == "menu"
    assert s._calls["position"] == 0           # 也不再白截屏


def test_stats_carry_what_happened():
    s = sup()
    s.control()
    assert s.stats()["checks"] == 1 and s.stats()["last_position"] == (100, 100)


def test_the_death_screen_ends_the_round_as_dead():
    """原本打算把死亡交给后端(每拍从画面解血量), 实机证明行不通: 死亡结算画面上血条
    没了, 解不出玩家。不在这里查的话, 这一局要一直跑到位置检查连着失败才以 "lost"
    收场 —— 慢, 名字也不对, 而且死亡惩罚一次都发不出去。"""
    s = FarmingSupervisor(position=lambda: (1, 1), in_area=lambda p: True,
                          path_back=lambda p: True, on_death_screen=lambda: True)
    assert s.control() == "dead" and s.stop_reason == "dead"


def test_the_death_check_is_throttled_like_the_rest():
    """它也要截屏 —— 每拍都查的话 10Hz 跑不起来。最多晚 1 秒发现。"""
    calls = []
    s = FarmingSupervisor(position=lambda: (1, 1), in_area=lambda p: True,
                          path_back=lambda p: True, check_interval=1.0,
                          on_death_screen=lambda: calls.append(1) or False,
                          clock=Clock())
    for _ in range(5):
        s.control()
    assert len(calls) == 1


# ── 坏读数防抖 ───────────────────────────────────────────────────────────────

def moving_sup(reads, **kw):
    """位置按 reads 依次吐; 吐完重复最后一个。"""
    q = list(reads)
    calls = []

    def position():
        calls.append(1)
        return q.pop(0) if len(q) > 1 else q[0]

    c = Clock()
    s = FarmingSupervisor(position=position, in_area=kw.pop("in_area", lambda p: True),
                          path_back=kw.pop("path_back", lambda p: True),
                          check_interval=0.0, clock=c, sleep=lambda dt: c.tick(dt), **kw)
    s._reads = calls
    return s


def test_one_absurd_jump_is_not_believed():
    """实机(2026-09-24): 刚确认在 (17,56), 一秒后读出 (249,293)。花朵一秒跑不了 250
    个单位。仲裁器信了这个数, 花 11 秒规划一条从地图另一头回刷怪区的路, 规划完才发现
    人早死了 —— 连着四轮各烧掉一分钟。"""
    s = moving_sup([(17, 56), (249, 293), (18, 57)], in_area=lambda p: p[0] < 100)
    assert s.control() == "policy"             # 建立基准
    assert s.control() == "script"             # 跳变被拒 -> 当成没读到
    assert s.stats()["rejected_jumps"] == 1
    assert s.control() == "policy"


def test_a_rejected_jump_never_triggers_pathing_back():
    """这才是代价所在: path_back 会跑 lazy theta*, 从地图另一头起算要十几秒, 整段
    时间策略是停的。"""
    paths = []
    # 复读读回一个跟坏值完全不同的数 —— 坏读数就是这样, 不会连着出现两次相同的。
    s = moving_sup([(17, 56), (249, 293), (18, 57)],
                   in_area=lambda p: p[0] < 100,
                   path_back=lambda p: paths.append(p) or True)
    s.control()
    s.control()
    assert paths == []


def test_a_real_big_jump_is_accepted_when_it_repeats():
    """传送/重连之后落点是真的变了。复读两次一致就该照常放行, 否则一辈子回不去。"""
    paths = []
    s = moving_sup([(17, 56), (249, 293), (249, 293), (249, 293)],
                   in_area=lambda p: p[0] < 100,
                   path_back=lambda p: paths.append(p) or True)
    s.control()
    s.control()                                 # 第一次: 复读也是 (249,293) -> 认
    assert paths == [(249, 293)]


def test_the_first_reading_has_nothing_to_compare_against():
    s = moving_sup([(200, 200)])
    assert s.control() == "policy"
    assert s._reads == [1]                      # 没白复读


def test_a_small_move_does_not_pay_for_a_second_screenshot():
    """复读要多截一次屏。正常走动时不该付这个。"""
    s = moving_sup([(17, 56), (20, 60), (24, 63)])
    for _ in range(3):
        s.control()
    assert len(s._reads) == 3


# ── 就地复活 ─────────────────────────────────────────────────────────────────

def test_death_with_a_revive_does_not_end_the_round():
    """用户实机指出的问题: 一局结束就意味着 learn() 返回, 而没收满的那批 rollout 被
    sb3 整个丢弃 —— 被丢掉的**恰好是死得快的那些局**(实测 25 局里只有 3 局进了训练)。
    训练数据因此被系统性筛成"活得久的样本", 而"怎么会死"的证据成建制地扔掉。"""
    revived = []
    s = FarmingSupervisor(position=lambda: (1, 1), in_area=lambda p: True,
                          path_back=lambda p: True, on_death_screen=lambda: True,
                          revive=lambda: revived.append(1) or True)
    assert s.control() == "dead"          # 环境照样终止并发死亡惩罚
    assert s.stop_reason is None          # 但这一局没完
    assert revived == [1]
    assert s.stats()["deaths"] == 1


def test_a_failed_revive_hands_the_round_back():
    s = FarmingSupervisor(position=lambda: (1, 1), in_area=lambda p: True,
                          path_back=lambda p: True, on_death_screen=lambda: True,
                          revive=lambda: False)
    assert s.control() == "lost" and s.stop_reason == "lost"


def test_dying_again_on_the_walk_back_revives_again():
    """实机(2026-09-24)一份 20 轮的日志: 9 局 "lost" 全是同一个样子 —— 死了, 就地复活,
    从 (105,31) 走回刷怪区的半路上又死一次, path_back 失败, revive 回 False, 整局结束,
    那批 rollout 被丢掉(9 局里 7 局 `梯度更新: 0 次`)。第二次死是脚本走路时死的, 跟
    "复活不了"是两回事 —— 再点一次继续接着走就是了。"""
    attempts = []

    def revive():
        attempts.append(1)
        return len(attempts) >= 2         # 第一次走到半路又死了

    s = FarmingSupervisor(position=lambda: (1, 1), in_area=lambda p: True,
                          path_back=lambda p: True, on_death_screen=lambda: True,
                          revive=revive)
    assert s.control() == "dead"
    assert s.stop_reason is None and len(attempts) == 2


def test_revive_retries_are_bounded():
    """一直死在路上(比如那条走廊整个被怪堵了)就别无限重来 —— 交回上层, 它会换服。"""
    attempts = []
    s = FarmingSupervisor(position=lambda: (1, 1), in_area=lambda p: True,
                          path_back=lambda p: True, on_death_screen=lambda: True,
                          revive=lambda: attempts.append(1) or False, max_revives=3)
    assert s.control() == "lost"
    assert len(attempts) == 3


def test_a_revive_that_fails_for_another_reason_is_not_retried():
    """没死、只是没走到(寻路超时/卡死) —— 再来一遍也是一样, 直接交回上层。"""
    attempts = []
    screens = iter([True, False])         # 查到死亡 -> 复活失败后复查: 不是死亡画面
    s = FarmingSupervisor(position=lambda: (1, 1), in_area=lambda p: True,
                          path_back=lambda p: True,
                          on_death_screen=lambda: next(screens, False),
                          revive=lambda: attempts.append(1) or False)
    assert s.control() == "lost"
    assert len(attempts) == 1


def test_a_pathing_failure_that_is_really_a_death_goes_down_the_death_path():
    """lazy_theta_pathing 每次循环开头都查死亡/开局画面, 查到就交回上层 —— 于是
    "人在半路上死了"会以 path_back 返回 False 的形式出现, 而不是仲裁器自己查到。
    实机(2026-09-24)一份 29 局的日志里 12 局是这么结束的, 4 局 `梯度更新: 0 次`:
    判 "lost" 的话没有死亡惩罚、不复活, 那批 rollout 被 sb3 整个丢掉。"""
    dead = [False]
    revived = []

    def on_death():
        return dead[0]

    def path_back(pos):
        dead[0] = True                # 走到一半撞见死亡结算画面
        return False

    s = FarmingSupervisor(position=lambda: (99, 99), in_area=lambda p: False,
                          path_back=path_back, on_death_screen=on_death,
                          revive=lambda: revived.append(1) or True)
    assert s.control() == "dead"
    assert s.stop_reason is None          # 复活成功 -> 这一局继续
    assert revived == [1] and s.stats()["deaths"] == 1


def test_a_pathing_failure_that_is_not_a_death_still_ends_the_round():
    """真走不回去(路被墙堵了/预算用完)还是得交回上层 —— 别把每一次寻路失败都
    当成死亡, 那会在没死的时候去点"继续"。"""
    s = FarmingSupervisor(position=lambda: (99, 99), in_area=lambda p: False,
                          path_back=lambda p: False, on_death_screen=lambda: False,
                          revive=lambda: pytest.fail("没死却去复活"))
    assert s.control() == "lost" and s.stop_reason == "lost"


def test_without_a_revive_death_still_ends_the_round():
    """多阶段路线(蚁穴)复活要跑整条进场路线, 那是主循环的活 —— 那种情况不给 revive,
    行为退回原样。"""
    s = FarmingSupervisor(position=lambda: (1, 1), in_area=lambda p: True,
                          path_back=lambda p: True, on_death_screen=lambda: True)
    assert s.control() == "dead" and s.stop_reason == "dead"


def test_revive_resets_the_jump_baseline():
    """复活点跟死亡点无关, 可能在图的另一头。拿死亡前那个位置做跨度判断的话, 复活后
    第一次读数会被当成坏读数拒掉。"""
    c = Clock()
    dead = [True]
    s = FarmingSupervisor(position=lambda: (200, 200), in_area=lambda p: True,
                          path_back=lambda p: True, check_interval=0.0,
                          on_death_screen=lambda: dead[0],
                          revive=lambda: True, clock=c, sleep=lambda dt: c.tick(dt))
    s.last_position = (10, 10)
    s.control()
    assert s.last_position is None
    dead[0] = False
    assert s.control() == "policy"        # 复活后第一次读数直接认


# ── 阻塞之前先把输入交出去 ───────────────────────────────────────────────────

def yielding_sup(**kw):
    yielded = []
    c = Clock()
    s = FarmingSupervisor(position=kw.pop("position", lambda: (1, 1)),
                          in_area=kw.pop("in_area", lambda p: True),
                          path_back=kw.pop("path_back", lambda p: True),
                          yield_input=lambda: yielded.append(1),
                          check_interval=kw.pop("check_interval", 0.0),
                          clock=c, sleep=lambda dt: c.tick(dt), **kw)
    s._yielded = yielded
    return s


def test_input_is_handed_back_before_pathing_back():
    """寻路期间 shift 还按着的话花朵是减速的 —— 实机一路"原地打转14次, 判定卡住"。"""
    s = yielding_sup(in_area=lambda p: False)
    s.control()
    assert s._yielded == [1]


def test_input_is_handed_back_before_reviving():
    s = yielding_sup(on_death_screen=lambda: True, revive=lambda: True)
    s.control()
    assert s._yielded == [1]


def test_input_is_handed_back_before_waiting_out_an_afk_popup():
    n = [0]
    s = yielding_sup(afk_paused=lambda: n.__setitem__(0, n[0] + 1) or n[0] <= 2,
                     afk_poll=0.1)
    s.control()
    assert s._yielded == [1]


def test_input_is_not_handed_back_on_a_normal_tick():
    """正常的一拍不该白松一次键 —— 那会把策略这一拍的动作抹掉。

    check_interval 要给个**非零**值: 0 的话每拍都走"该查了"那条路, 被节流跳过的那条
    分支根本测不到(变异验证时就是这么漏过去的)。
    """
    s = yielding_sup(check_interval=1.0)
    s.control()                   # 第一拍: 真查一次
    for _ in range(5):
        s.control()               # 之后几拍: 被节流跳过
    assert s._yielded == []
