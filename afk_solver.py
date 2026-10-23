"""自己解 florr 的挂机检测弹窗(AFK check): 识别管道 -> 规划路线 -> 鼠标把球拖到终点。

为什么自己解: 2026-10-18 的 15 份录像里 florr-auto-afk 每次都在弹窗出现 31~43 秒后才"发现"它, 解题本身
又要 13~24 秒, 而弹窗只活 ~58 秒 —— 6 次里 3 次赶不上, 被踢回标题页丢一条命(2026-10-15 工厂两次、
10-18 蚁穴两次)。我们在画布上第一拍就看见弹窗字了, 自己 1~2 秒解掉。

弹窗长什么样(2026-10-15 / 10-18 的 64 张真实截图): 一块深色半透明的圆角框, 左上「挂机检测」「将圆球拖动到终点」
两行字(画布文字, 钩子能记下); 框里一条**浅灰色管道**(折线描边, 圆头, 每次形状不同: 弯钩 / 螺旋 / 之字),
管道一头有一颗发光的**球**(绿或青色), 另一头是一个**暗色小圆环**(终点)。位置每次不同, 要把球顺着管道拖到
圆环上。钩子(canvas_hook.js)只记弧形填充 / 血条色描边 / 文字, 管道那条折线描边被它过滤掉了, 所以路径只能从
截图里认: 球 = 饱和的绿/青色圆斑; 管道 = 以球旁边的颜色为准的区域生长; 终点 = 管道区域里一个小而圆的"洞"。

所有长度都按球半径 r 折算, 截图缩小 / UI 缩放不同都能用。
"""
import heapq
import math
import time

import cv2
import numpy as np

# 球: 发光圆, 饱和度高、亮; 颜色不限(见 find_ball)。
BALL_MIN_SAT = 110
BALL_MIN_VAL = 170
BALL_MIN_AREA = 25            # 像素(1024x768 的半尺寸截图上球只有 ~30 像素; 1920x1080 半尺寸约 130~170)

TUBE_TOL = 30                 # 管道颜色容差(各通道最大差)
HOLE_END_REACH = 3.0          # 端头尖离环心(洞)这么近(球半径)才算"这头有环"
CENTER_WEIGHT = 15.0          # 路线贴管道中线的力度(代价 = 1 + CENTER_WEIGHT/(1+离墙距离²)); 真实截图上扫过:
                              # 6 -> 15 让线段离管道边缘的最小余量 0.44r -> 0.56r, 中位 0.72r -> 0.82r
MIN_ROUTE_R = 2.0             # 球到终点的路线至少这么长(单位: 球半径), 再短就是球已经在环上了(不用拖)
                              # 管道被怪 / 特效盖住切成两段时只认到球所在的短段, 路线会很短 —— 宁可不拖





# 弹窗框相对画布上「挂机检测」标题文字位置 (tx, ty)(文字记录的 x, y)的范围, UI 缩放 1.0(1920x1080 全屏)。
# 从 2026-10-15 / 10-18 的 64 张真实截图量出来: 半尺寸截图上框是 [标题条左上角 loc 的 (-5,-8) ~ (+132,+170)],
# 10-15 工厂那两次的画布文字坐标 (599.5,267.8)(787.8,204.2) 对上截图里 loc*2 + (51.7, 28)。框约 274x356。
POPUP_BOX_FROM_TITLE = (-62.0, -44.0, 212.0, 312.0)
POPUP_TITLE_TEXT = "挂机检测"


def popup_box(title_xy, ui_scale=1.0):
    """画布标题文字位置 -> 弹窗框 (x0, y0, x1, y1)(页面 / 截图像素)。ui_scale 取文字记录里变换矩阵的缩放 m[0]。"""
    tx, ty = title_xy
    l, t, r, b = POPUP_BOX_FROM_TITLE
    return (tx + l * ui_scale, ty + t * ui_scale, tx + r * ui_scale, ty + b * ui_scale)


BALL_MIN_CIRC = 0.6           # 球斑的圆度下限(4πA/P²); 弹窗里别的饱和亮斑(受击数字、光效)多半不圆
BALL_R_BASE = 14.0            # UI 缩放 1.0(1920x1080)时球的半径; 给了 r_hint 就按它的 0.55~1.8 倍筛面积


def find_ball(img, roi=None, r_hint=None):
    """球的 (cx, cy, r)(img 像素坐标)。roi=(x0, y0, x1, y1) 限定在弹窗框里(别把框外的绿草地当球); 找不到返回 None。

    **不按颜色认**: 球的颜色每个弹窗不一样 —— 10-15 / 10-18 的 64 张截图里只有绿 / 青, 10-18 夜里 1024x768 那份
    是黄色, 按"绿到青"写死就认不出(三次都 no_ball)。弹窗里(框叠在世界上, 世界的东西被半透明框压暗)画在最上面的
    又饱和又亮的圆斑只有球: 标题字是白的、管道和环是灰的、框底是深棕。所以按"饱和 + 亮 + 圆 + 大小对得上"找;
    r_hint(球的预期半径, 像素)给了就把面积限在 0.55~1.8 倍, 多个候选取最圆的。"""
    x0, y0, x1, y1 = _clip_roi(img, roi)
    hsv = cv2.cvtColor(img[y0:y1, x0:x1], cv2.COLOR_BGR2HSV)
    m = ((hsv[..., 1] >= BALL_MIN_SAT) & (hsv[..., 2] >= BALL_MIN_VAL)).astype(np.uint8)
    n, lab, st, cen = cv2.connectedComponentsWithStats(m)
    best = None
    for i in range(1, n):
        area = int(st[i, cv2.CC_STAT_AREA])
        if area < BALL_MIN_AREA:
            continue
        if r_hint is not None:
            lo, hi = math.pi * (0.55 * r_hint) ** 2, math.pi * (1.8 * r_hint) ** 2
            if not lo <= area <= hi:
                continue
        cnts, _ = cv2.findContours((lab == i).astype(np.uint8), cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        per = cv2.arcLength(max(cnts, key=cv2.contourArea), True) if cnts else 0
        circ = 4 * math.pi * area / (per * per) if per > 0 else 0
        if circ < BALL_MIN_CIRC:
            continue
        score = (circ, area)
        if best is None or score > best[0]:
            best = (score, i, area)
    if best is None:
        return None
    _, i, area = best
    cx, cy = cen[i]
    return (float(cx + x0), float(cy + y0), math.sqrt(area / math.pi))


def _clip_roi(img, roi):
    h, w = img.shape[:2]
    if roi is None:
        return 0, 0, w, h
    x0, y0, x1, y1 = roi
    return max(0, int(x0)), max(0, int(y0)), min(w, int(x1)), min(h, int(y1))


def tube_mask(img, ball, roi=None):
    """以球旁边的颜色为准, 区域生长出管道的二值图(0/255)。管道是浅灰半透明白叠在深色框底上, 颜色随背景略变,
    所以不用固定阈值: 先取球周围一圈里**灰色**(低饱和 —— 青色球的光晕会把旁边染色, 不能算)且最亮的那批像素的
    中位色当"管道色", 长出一遍; 再用长出来的管道里离球远(>2r, 不受光晕影响)的像素重新估色, 再长一遍。
    容差是**相对**的: 取管道色与框底色(roi 里的中位色)反差的一半左右 —— 蚁穴那种棕色底上管道只比底亮一点,
    固定容差会漏进整个框底。长出来的区域碰到 roi 边缘 = 漏了(真管道离框边至少有一圈边距), 收紧容差重来。"""
    cx, cy, r = ball
    h, w = img.shape[:2]
    rx0, ry0, rx1, ry1 = _clip_roi(img, roi)
    yy, xx = np.mgrid[0:h, 0:w]
    d = np.hypot(xx - cx, yy - cy)
    hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)
    color = None
    for lo, hi in ((1.15, 1.7), (1.7, 2.8)):                 # 光晕大 / 管道窄时, 圈里灰色像素太少就往外一圈
        ring = (d >= lo * r) & (d <= hi * r) & (hsv[..., 1] < 70)
        pix = img[ring].astype(np.float32)
        if len(pix) >= 12:
            bright = pix.max(axis=1)
            color = np.median(pix[bright >= np.percentile(bright, 55)], axis=0)
            break
    if color is None:
        return None
    roi_pix = img[ry0:ry1, rx0:rx1].reshape(-1, 3).astype(np.float32)
    bg = np.median(roi_pix, axis=0)
    k = max(1, int(round(r / 3)))
    kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (2 * k + 1, 2 * k + 1))
    ci = (int(round(cy)), int(round(cx)))

    def grow(color, tol):
        diff = np.abs(img.astype(np.float32) - color).max(axis=2)
        m = (diff <= tol).astype(np.uint8)
        m[d <= 1.05 * r] = 1                                   # 球本身也算管道
        m = cv2.morphologyEx(m, cv2.MORPH_CLOSE, kernel)
        n, lab, st, cen = cv2.connectedComponentsWithStats(m, connectivity=8)
        li = lab[ci]
        if li == 0:
            return None
        return ((lab == li).astype(np.uint8)) * 255

    def leaked(mask):
        ys, xs = np.nonzero(mask)
        pad = 2
        return (len(xs) == 0 or xs.min() <= rx0 + pad or ys.min() <= ry0 + pad
                or xs.max() >= rx1 - 1 - pad or ys.max() >= ry1 - 1 - pad)

    mask = None
    for pass_ in range(2):
        contrast = float(np.abs(color - bg).max())
        tol0 = float(np.clip(0.5 * contrast, 8.0, TUBE_TOL))
        mask = None
        for shrink in (1.0, 0.7, 0.5):
            m = grow(color, tol0 * shrink)
            if m is None:
                return None
            if not leaked(m):
                mask = m
                break
        if mask is None:
            return None
        far = (mask > 0) & (d > 2.0 * r)
        if pass_ == 0 and far.sum() >= 30:
            color = np.median(img[far].astype(np.float32), axis=0)   # 用管道里干净的部分重新估色
    return _fill_small_holes(mask, r)


HOLE_MIN_R = 0.3              # 终点圆环内圈(洞)的半径范围(单位: 球半径)
HOLE_MAX_R = 2.0
HOLE_MIN_CIRC = 0.5           # 洞的圆度下限(4πA/P²)


def _fill_small_holes(mask, r):
    """把管道区域里的小洞填实, 同时记下其中又小又圆的洞心(终点圆环的暗色内圈)。返回 (填实后的 mask, [(x, y), ...])。

    填实的原因: 圆环内圈是个洞, 它会让"离墙距离"在环心附近归零, 圆头的圆心就被推到环里面两个球半径的地方。
    螺旋 / 之字围出的大洞(远大于球)不动, 那是真的墙。洞心留着给 find_target 认终点用: 球已经被拖到管道
    中间时, 没法靠"球在哪头"分辨起点和终点, 但环心永远在终点那头。"""
    inv = (mask == 0).astype(np.uint8)
    n, lab, st, cen = cv2.connectedComponentsWithStats(inv, connectivity=4)
    h, w = mask.shape
    out = mask.copy()
    holes = []
    for i in range(1, n):
        x, y, bw, bh, area = st[i]
        if x == 0 or y == 0 or x + bw >= w or y + bh >= h:       # 碰边 = 管道外面的背景
            continue
        if area > math.pi * (2.0 * r) ** 2:
            continue
        out[lab == i] = 255
        rad = math.sqrt(area / math.pi)
        if HOLE_MIN_R * r <= rad <= HOLE_MAX_R * r:
            cnts, _ = cv2.findContours((lab == i).astype(np.uint8), cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
            per = cv2.arcLength(cnts[0], True) if cnts else 0
            if per > 0 and 4 * math.pi * area / (per * per) >= HOLE_MIN_CIRC:
                holes.append((float(cen[i][0]), float(cen[i][1])))
    return out, holes


class _Grid:
    """管道二值图缩到球半径约 3 像素的栅格(Dijkstra 在上面跑才够快), 带离墙距离。"""

    def __init__(self, mask, r):
        self.scale = max(1.0, r / 3.0)
        small = cv2.resize(mask, None, fx=1 / self.scale, fy=1 / self.scale, interpolation=cv2.INTER_AREA)
        self.walk = small >= 128
        self.dt = cv2.distanceTransform(self.walk.astype(np.uint8), cv2.DIST_L2, 3)
        self.h, self.w = self.walk.shape

    def cell(self, p):
        c = (min(self.h - 1, max(0, int(round(p[1] / self.scale)))),
             min(self.w - 1, max(0, int(round(p[0] / self.scale)))))
        if self.walk[c]:
            return c
        ys, xs = np.nonzero(self.walk)
        if len(ys) == 0:
            return None
        i = int(np.argmin((ys - c[0]) ** 2 + (xs - c[1]) ** 2))
        return (int(ys[i]), int(xs[i]))

    def xy(self, c):
        return (c[1] * self.scale, c[0] * self.scale)

    def dijkstra(self, src, cost_dt=True, goal=None):
        """从 src 出发的最短路: 返回 (dist 数组, prev 字典)。cost_dt=True 时离墙越近越贵(路线贴管道中线);
        False 时是纯几何距离(找端头用)。给了 goal 到它就停。"""
        inf = float("inf")
        dist = np.full((self.h, self.w), inf)
        prev = {}
        dist[src] = 0.0
        heap = [(0.0, src)]
        while heap:
            d0, u = heapq.heappop(heap)
            if d0 > dist[u]:
                continue
            if goal is not None and u == goal:
                break
            uy, ux = u
            for dy in (-1, 0, 1):
                for dx in (-1, 0, 1):
                    if dy == 0 and dx == 0:
                        continue
                    vy, vx = uy + dy, ux + dx
                    if not (0 <= vy < self.h and 0 <= vx < self.w) or not self.walk[vy, vx]:
                        continue
                    step = math.hypot(dy, dx)
                    if cost_dt:
                        step *= 1.0 + CENTER_WEIGHT / (1.0 + float(self.dt[vy, vx]) ** 2)
                    nd = d0 + step
                    if nd < dist[vy, vx]:
                        dist[vy, vx] = nd
                        prev[(vy, vx)] = u
                        heapq.heappush(heap, (nd, (vy, vx)))
        return dist, prev

    def farthest(self, dist):
        d = np.where(np.isfinite(dist), dist, -1.0)
        i = int(np.argmax(d))
        return (i // self.w, i % self.w)


def _walk_back(prev, goal, src):
    cells = [goal]
    while cells[-1] != src:
        cells.append(prev[cells[-1]])
    cells.reverse()
    return cells


def find_target(img, mask, ball, holes=()):
    """终点 = 终点圆环的环心。管道只有两个端头(管道区域的"测地直径"两端: 从球出发最远的点 A, 再从 A 出发最远的
    点 B); 圆环那头的端头尖离环心(_fill_small_holes 记下的小而圆的洞)不远。返回 (target_xy, (grid, 规则)) 或
    (None, 原因)。

    只认环, 不靠"球在起点那头, 终点就是另一头"这种推断: 环被怪 / 特效盖住时管道会被截断, 那条推断会选到被截断
    的断口(64 张真实截图里 2 张, 全错)。认不出环就不拖, 下一拍重新截图 —— 怪过一会儿就挪开了。"""
    cx, cy, r = ball
    g = _Grid(mask, r)
    src = g.cell((cx, cy))
    if src is None:
        return None, "球不在管道里"
    d1, _ = g.dijkstra(src, cost_dt=False)
    a = g.farthest(d1)
    d2, _ = g.dijkstra(a, cost_dt=False)
    b = g.farthest(d2)
    if a == b:
        return None, "管道太短"
    if not holes:
        return None, "no_ring"

    def hole_near(cell):
        x, y = g.xy(cell)
        return min(holes, key=lambda hxy: math.hypot(hxy[0] - x, hxy[1] - y))

    ha, hb = hole_near(a), hole_near(b)
    xa, ya = g.xy(a)
    xb, yb = g.xy(b)
    da = math.hypot(ha[0] - xa, ha[1] - ya)
    db = math.hypot(hb[0] - xb, hb[1] - yb)
    reach = HOLE_END_REACH * r
    if min(da, db) > reach:
        return None, "no_ring"
    if da <= reach and db <= reach and abs(da - db) <= 0.5 * r:
        return None, "ambiguous_end"                       # 两头都像有环: 不猜
    return (ha if da < db else hb), (g, "hole")


def plan_route(grid, start, target, r):
    """管道里从 start 到 target 的路线: 带"贴中线"代价的最短路, Douglas-Peucker 化简后按 ~0.8r 重采样。
    返回 [(x, y), ...](原图像素坐标); 走不通返回 None。"""
    s, t = grid.cell(start), grid.cell(target)
    if s is None or t is None:
        return None
    dist, prev = grid.dijkstra(s, cost_dt=True, goal=t)
    if not math.isfinite(dist[t]):
        return None
    cells = _walk_back(prev, t, s)
    pts = np.array([grid.xy(c) for c in cells], dtype=np.float32).reshape(-1, 1, 2)
    simp = cv2.approxPolyDP(pts, max(0.15 * r, 0.5), False).reshape(-1, 2)
    route = [(float(start[0]), float(start[1]))] + [(float(x), float(y)) for x, y in simp[1:-1]] \
        + [(float(target[0]), float(target[1]))]
    return _resample(route, 0.8 * r)


def _resample(route, step):
    out = [route[0]]
    carry = 0.0
    for a, b in zip(route, route[1:]):
        seg = math.hypot(b[0] - a[0], b[1] - a[1])
        if seg == 0:
            continue
        t = step - carry
        while t < seg:
            out.append((a[0] + (b[0] - a[0]) * t / seg, a[1] + (b[1] - a[1]) * t / seg))
            t += step
        carry = seg - (t - step)
    if out[-1] != route[-1]:
        out.append(route[-1])
    return out


def solve_image(img, roi=None, r_hint=None):
    """从一张截图里认出球 / 管道 / 终点, 规划路线。成功返回
    ({"ball": (x, y, r), "end": (x, y), "route": [(x, y), ...], "mask": ...}, "ok"), 认不出返回 (None, 原因)。"""
    ball = find_ball(img, roi, r_hint)
    if ball is None:
        return None, "no_ball"
    got = tube_mask(img, ball, roi)
    if got is None:
        return None, "no_tube"
    mask, holes = got
    target, extra = find_target(img, mask, ball, holes)
    if target is None:
        return None, "no_end:" + str(extra)
    grid, rule = extra
    route = plan_route(grid, (ball[0], ball[1]), target, ball[2])
    if route is None:
        return None, "no_route"
    length = sum(math.hypot(b[0] - a[0], b[1] - a[1]) for a, b in zip(route, route[1:]))
    if length < MIN_ROUTE_R * ball[2]:
        return None, "route_too_short"
    return {"ball": ball, "end": target, "route": route, "mask": mask, "rule": rule, "holes": holes}, "ok"


# ── 读页面 / 拖动 / 编排 ─────────────────────────────────────────────────────────────────

# 画布日志里最新那帧的「挂机检测」标题文字位置(只读, 不清空 —— 跟 afk_watch 的弹窗探测一样别抢索敌的帧), 加上把
# 页面坐标换成鼠标坐标要用的窗口信息。文字记录的 x, y 是画布缓冲像素(= CDP 截图像素), m 是变换矩阵(m[0] 的缩放 =
# UI 缩放)。
POPUP_STATE_JS = """(() => {
  var log = window.__canvasLog || [], best = null, frames = {}, nf = 0, cjk = false;
  for (var i = log.length - 1; i >= 0; i--) {
    var r = log[i];
    if (!frames[r.frame]) { frames[r.frame] = true; nf++; }
    if (r.op !== "text" || !r.text) continue;
    if (/[\u4e00-\u9fff]/.test(String(r.text))) cjk = true;
    if (r.text === "挂机检测" && (best === null || r.frame > best.frame)) best = r;
  }
  return {
    frames: nf, cjk: cjk,
    title: best ? [best.x, best.y, Math.hypot(best.m[0], best.m[1])] : null,
    dpr: window.devicePixelRatio || 1,
    sx: window.screenX, sy: window.screenY, ow: window.outerWidth, oh: window.outerHeight,
    iw: window.innerWidth, ih: window.innerHeight, sw: screen.width, sh: screen.height,
    fs: !!document.fullscreenElement
  };
})()"""

DRAG_SPEED = 260.0            # 拖动速度(截图像素 / 秒): 慢一点, 球如果是缓动跟随就不会抄近路出管道
PRESS_PAUSE = 0.08            # 移到球上 / 按下去 / 到终点之后各停一下(秒)
RELEASE_PAUSE = 0.15
MAX_ATTEMPTS = 3
VERIFY_WAIT = 1.0             # 松手之后等这么久再看弹窗还在不在


def drag_events(route, dpr=1.0, speed=DRAG_SPEED):
    """把路线(截图像素)换成鼠标事件序列 [(type, x_css, y_css, delay_after)], type ∈ move / down / up。
    坐标 / dpr = CSS 像素(CDP 鼠标事件用的); 路线点间隔 ~0.8r, 每步等 间隔/speed 秒。"""
    pts = [(x / dpr, y / dpr) for x, y in route]
    ev = [("move", pts[0][0], pts[0][1], PRESS_PAUSE), ("down", pts[0][0], pts[0][1], PRESS_PAUSE)]
    for a, b in zip(pts, pts[1:]):
        step = math.hypot(b[0] - a[0], b[1] - a[1]) * dpr
        ev.append(("move", b[0], b[1], step / speed))
    ev.append(("up", pts[-1][0], pts[-1][1], 0.0))
    ev[-2] = (ev[-2][0], ev[-2][1], ev[-2][2], ev[-2][3] + RELEASE_PAUSE)    # 走完最后一步, 再停一下才松手
    return ev


def page_to_screen(pt_css, info, pyauto_width=None):
    """页面 CSS 坐标 -> pyautogui 屏幕坐标(OS 鼠标兜底用)。全屏: 窗口左上角就是屏幕左上角; 窗口化: 加上窗口位置和
    浏览器顶栏(outerHeight - innerHeight, 边框两侧对半)。pyautogui 的坐标系可能是物理像素(DPI 感知)也可能是 CSS
    像素, 用 pyautogui.size().width / screen.width 折算(pyauto_width 没给就当 1:1)。"""
    x, y = pt_css
    if info.get("fs") or (info.get("ow") == info.get("iw") and info.get("oh") == info.get("ih")):
        left = top = 0.0
    else:
        side = max(0.0, (info.get("ow", 0) - info.get("iw", 0)) / 2.0)
        left = info.get("sx", 0) + side
        top = info.get("sy", 0) + max(0.0, info.get("oh", 0) - info.get("ih", 0) - side)
    k = (pyauto_width / info["sw"]) if (pyauto_width and info.get("sw")) else 1.0
    return ((left + x) * k, (top + y) * k)


class CdpMouse:
    """一条开着的 CDP 连接, 发 Input.dispatchMouseEvent(受信的真实输入事件, 不需要窗口在前台)。拖动要连发上百个事件,
    一事件一连接太慢, 所以拖动期间独占一条。websocket / 找标签页都走 cdp_bridge 现成的。"""

    def __init__(self, ws=None):
        self._ws = ws
        self._id = 0

    def __enter__(self):
        if self._ws is None:
            import cdp_bridge
            tab = cdp_bridge.find_florr_tab()
            if tab is None:
                raise RuntimeError("找不到 florr.io 标签页(CDP)")
            self._ws = cdp_bridge.websocket.create_connection(tab["webSocketDebuggerUrl"], timeout=5)
        self._focus_emulation()
        return self

    def _focus_emulation(self):
        """让页面以为自己有焦点(document.hasFocus() = true)。2026-10-18 夜的录像里页面有 35% 的拍子「没焦点」(控制
        面板 / 悬浮窗抢了焦点), 有的游戏在失焦时会忽略鼠标输入。这个覆盖只活在这条 CDP 连接上, 关连接自动还原。
        失败(旧 Chrome 没这个命令)不影响拖动。"""
        try:
            self._call("Emulation.setFocusEmulationEnabled", {"enabled": True})
        except Exception:
            pass

    def __exit__(self, *exc):
        try:
            self._ws.close()
        except Exception:
            pass
        return False

    def _call(self, method, params):
        import json
        self._id += 1
        rid = self._id
        self._ws.send(json.dumps({"id": rid, "method": method, "params": params}))
        while True:                                    # 同一条连接上会混进页面事件, 按 id 对上号
            msg = json.loads(self._ws.recv())
            if msg.get("id") == rid:
                if "error" in msg:
                    raise RuntimeError(f"CDP {method} 出错: {msg['error']}")
                return msg.get("result")

    def event(self, kind, x, y):
        params = {"x": float(x), "y": float(y), "button": "none", "buttons": 0, "clickCount": 0}
        if kind == "down":
            params.update(type="mousePressed", button="left", buttons=1, clickCount=1)
        elif kind == "up":
            params.update(type="mouseReleased", button="left", buttons=0, clickCount=1)
        else:
            params["type"] = "mouseMoved"
            if self._held:
                params.update(button="left", buttons=1)
        self._held = kind == "down" or (self._held and kind != "up")
        self._call("Input.dispatchMouseEvent", params)

    _held = False


def run_drag(events, send, sleep):
    """按事件序列发鼠标事件。send(kind, x, y); 出任何错都先松手再抛(别让鼠标键卡在按下状态)。"""
    held = False
    try:
        for kind, x, y, delay in events:
            send(kind, x, y)
            held = kind == "down" or (held and kind != "up")
            if delay > 0:
                sleep(delay)
    except Exception:
        if held:
            try:
                send("up", events[-1][1], events[-1][2])
            except Exception:
                pass
        raise


DEBUG_KEEP = 24               # logs/afk/ 里最多留这么多张图, 再旧的删掉
DEBUG_MARGIN = 24             # 存图时在弹窗框外多留的边(像素)


def debug_dir():
    import os
    import app_config
    return os.path.join(os.path.dirname(app_config.CONFIG_PATH), "logs", "afk")


def annotate(img, roi, out=None, ball=None):
    """带标注的弹窗截图(裁到框附近, 存盘用): 黄框 = 搜索范围, 青圈 = 球, 洋红点 = 终点, 红线 = 路线。
    ASCII 以外的字 cv2 画不出来, 不写字, 原因放文件名里。"""
    h, w = img.shape[:2]
    x0, y0, x1, y1 = [int(round(v)) for v in roi]
    cx0, cy0 = max(0, x0 - DEBUG_MARGIN), max(0, y0 - DEBUG_MARGIN)
    cx1, cy1 = min(w, x1 + DEBUG_MARGIN), min(h, y1 + DEBUG_MARGIN)
    crop = img[cy0:cy1, cx0:cx1].copy()
    off = (cx0, cy0)

    def pt(p):
        return (int(round(p[0] - off[0])), int(round(p[1] - off[1])))

    cv2.rectangle(crop, pt((x0, y0)), pt((x1, y1)), (0, 255, 255), 1)
    if out is not None:
        for a, b in zip(out["route"], out["route"][1:]):
            cv2.line(crop, pt(a), pt(b), (0, 0, 255), 1)
        cv2.drawMarker(crop, pt(out["end"]), (255, 0, 255), cv2.MARKER_DIAMOND, 8, 1)
        ball = ball or out["ball"]
    if ball is not None:
        cv2.circle(crop, pt(ball), max(2, int(round(ball[2]))), (255, 255, 0), 1)
    return crop


def save_debug(name, img):
    """logs/afk/<name> 存一张图, 只留最近 DEBUG_KEEP 张。存盘失败一律吞掉(诊断不能拖累解题)。"""
    import os
    try:
        d = debug_dir()
        os.makedirs(d, exist_ok=True)
        cv2.imwrite(os.path.join(d, name), img)
        names = sorted(n for n in os.listdir(d) if n.endswith(".png"))
        for n in names[:-DEBUG_KEEP]:
            try:
                os.remove(os.path.join(d, n))
            except OSError:
                pass
    except Exception:
        pass


def describe_ball_motion(before, after, end, r):
    """拖完之后球动没动 —— 区分"游戏没接受这次拖动"和"接受了但没拖到位"最直接的证据。"""
    if after is None:
        return "拖完找不到球了(可能已经解开, 也可能被遮住)"
    moved = math.hypot(after[0] - before[0], after[1] - before[1])
    left = math.hypot(after[0] - end[0], after[1] - end[1])
    if moved < 0.5 * r:
        return f"球纹丝没动(位移 {moved:.1f}px < 半个球半径) —— 游戏没接受这次拖动, 鼠标事件没起作用?"
    return f"球从 ({before[0]:.0f},{before[1]:.0f}) 移到 ({after[0]:.0f},{after[1]:.0f}), 位移 {moved:.0f}px, 离终点还有 {left:.0f}px"


class Deps:
    """实机依赖, 测试里整个换掉。"""

    def save_debug(self, name, img):
        save_debug(name, img)

    def read_state(self):
        import cdp_bridge
        return cdp_bridge._eval_value(POPUP_STATE_JS, timeout=3)

    def screenshot(self):
        import cdp_bridge
        data = cdp_bridge.capture_screenshot(timeout=8)
        return cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_COLOR)

    def drag_cdp(self, events):
        with CdpMouse() as m:
            run_drag(events, m.event, time.sleep)

    def drag_os(self, events, info):
        import pyautogui
        width = pyautogui.size().width
        prev_pause, prev_failsafe = pyautogui.PAUSE, pyautogui.FAILSAFE
        pyautogui.PAUSE = 0                    # 每次调用后默认睡 0.1 秒, 拖动要连发上百次
        pyautogui.FAILSAFE = False             # 拖到屏幕角落不要触发安全中止
        try:
            def send(kind, x, y):
                sx, sy = page_to_screen((x, y), info, width)
                if kind == "down":
                    pyautogui.moveTo(sx, sy)
                    pyautogui.mouseDown()
                elif kind == "up":
                    pyautogui.moveTo(sx, sy)
                    pyautogui.mouseUp()
                else:
                    pyautogui.moveTo(sx, sy)
            run_drag(events, send, time.sleep)
        finally:
            pyautogui.PAUSE, pyautogui.FAILSAFE = prev_pause, prev_failsafe

    @staticmethod
    def sleep(seconds):
        time.sleep(seconds)



def popup_state(info):
    """画布日志里弹窗的三态: "present" 有标题字 / "gone" 确认没有(画布在画、有中文字, 就是没有「挂机检测」) /
    "unknown" 判断不了(读不出 / 没装钩子 / 缓冲是空的 —— 不能当成弹窗没了)。"""
    if not isinstance(info, dict):
        return "unknown"
    if info.get("title"):
        return "present"
    if (info.get("frames") or 0) >= 1 and info.get("cjk"):
        return "gone"
    return "unknown"


def plan_from_page(deps):
    """读页面状态 + 截图 -> (规划结果 or None, 原因, 页面信息)。弹窗标题文字不在画布日志里(缓冲刚被清空 / 弹窗没了)
    = (None, "no_popup", info)。"""
    info = deps.read_state()
    state = popup_state(info)
    if state != "present":
        return None, ("no_popup" if state == "gone" else "unknown"), info
    tx, ty, scale = info["title"]
    img = deps.screenshot()
    if img is None:
        return None, "no_screenshot", info
    scale = scale or 1.0
    roi = popup_box((tx, ty), scale)
    out, why = solve_image(img, roi, r_hint=BALL_R_BASE * scale)
    info = dict(info)
    info["_img"], info["_roi"], info["_scale"] = img, roi, scale          # 诊断存图 / 拖完对比球位置用
    return out, why, info


def _debug(deps, name, make):
    """存一张诊断图。deps 没有 save_debug(测试里的假依赖)就跳过; 出任何错都吞掉。"""
    saver = getattr(deps, "save_debug", None)
    if saver is None:
        return
    try:
        saver(name, make())
    except Exception:
        pass


def solve_popup(deps=None, *, max_attempts=MAX_ATTEMPTS, log=print):
    """自己解弹窗。最多 max_attempts 次: 单数次用 CDP 鼠标事件, 双数次用系统鼠标(pyautogui)兜底 —— 不知道游戏的
    拖动只认哪一种输入, 两种都试。每次: 截图识别 -> 拖 -> 等 VERIFY_WAIT 秒 -> 看弹窗字还在不在。
    返回 True(弹窗没了) / False(试完了还在, 或认不出)。认不出环 / 路线不像样就不拖, 等 0.5 秒重新截图再试
    (怪 / 特效盖住的情况过一会儿就变了); 这种空转的重试也算次数, 免得在认不出的弹窗上耗掉 florr-auto-afk 的时间。

    每次尝试都留证据(2026-10-19 用户说"还是不行"但手上只有一行日志, 看不出是没触发 / 认不出 / 拖了没反应):
    日志里写识别参数(搜索框 / UI 缩放 / 球 / 终点 / 规则), 拖完再截一张比较球动没动, 并存带标注的图到 logs/afk/
    (录像 zip 会一起带走)。"""
    import time as _time
    deps = deps or Deps()
    stamp = _time.strftime("%H%M%S")
    for attempt in range(1, max_attempts + 1):
        tag = f"afk_{stamp}_a{attempt}"
        try:
            out, why, info = plan_from_page(deps)
        except Exception as e:
            log(f"🧩 自己解弹窗: 第 {attempt} 次读页面 / 截图出错: {e}")
            deps.sleep(0.5)
            continue
        if why == "no_popup":
            return True                                  # 弹窗已经没了(被谁解掉了 / 被踢了)
        if why == "unknown":
            log(f"🧩 自己解弹窗: 第 {attempt} 次读不到画布状态, 等一下重试")
            deps.sleep(0.5)
            continue
        img, roi, scale = info.get("_img"), info.get("_roi"), info.get("_scale")
        if img is not None and roi is not None:
            where = "ROI=({:.0f},{:.0f},{:.0f},{:.0f}) 截图 {}x{} UI缩放 {:.3f}".format(*roi, img.shape[1], img.shape[0], scale)
        else:
            where = "(没有截图信息)"
        if out is None:
            log(f"🧩 自己解弹窗: 第 {attempt} 次认不出({why}), 不拖, 等一下重试; {where}")
            if img is not None and roi is not None:
                _debug(deps, f"{tag}_fail-{why.replace(':', '-')}.png", lambda: annotate(img, roi))
            deps.sleep(0.5)
            continue
        dpr = float(info.get("dpr") or 1.0)
        events = drag_events(out["route"], dpr)
        method = "CDP" if attempt % 2 == 1 else "系统鼠标"
        length = sum(math.hypot(b[0] - a[0], b[1] - a[1]) for a, b in zip(out["route"], out["route"][1:]))
        bx, by, br = out["ball"]
        log(f"🧩 自己解弹窗: 第 {attempt} 次, {method} 拖球, 路线 {length:.0f}px / {len(events)} 步; {where}; "
            f"球 ({bx:.0f},{by:.0f}) r={br:.1f}, 终点 ({out['end'][0]:.0f},{out['end'][1]:.0f}), 规则 {out['rule']}, dpr {dpr:g}")
        _debug(deps, f"{tag}_plan.png", lambda: annotate(img, roi, out))
        try:
            if attempt % 2 == 1:
                deps.drag_cdp(events)
            else:
                deps.drag_os(events, info)
        except Exception as e:
            log(f"🧩 自己解弹窗: 第 {attempt} 次拖动出错: {e}")
            continue
        deps.sleep(0.3)
        try:                                              # 拖完球动没动: 区分"游戏没接受拖动"和"接受了但没拖到位"
            after_img = deps.screenshot()
            after = find_ball(after_img, roi, BALL_R_BASE * scale)
            log("🧩 自己解弹窗: " + describe_ball_motion(out["ball"], after, out["end"], br))
            _debug(deps, f"{tag}_after.png", lambda: annotate(after_img, roi, None, after))
        except Exception as e:
            log(f"🧩 自己解弹窗: 拖完对比球位置出错: {e}")
        deps.sleep(max(0.0, VERIFY_WAIT - 0.3))
        try:
            gone = popup_state(deps.read_state()) == "gone"
        except Exception:
            gone = False
        if gone:
            log(f"🧩 自己解弹窗: 第 {attempt} 次解开了")
            return True
        log(f"🧩 自己解弹窗: 第 {attempt} 次拖完弹窗还在")
    log("🧩 自己解弹窗: 试完了还没解开, 让 florr-auto-afk 接手")
    return False
