"""config.json 的 map 名 -> 一条"进场路线".

改之前一个 map 名 = 一张寻路图, 因为沙漠/海洋都能在 florr 标题页的生态区选择器
里直接选中、点开始就落在那张图上。蚁穴不行 —— 标题页**没有蚁穴格**, 唯一的路是

    标题页选花园 -> 进花园 -> 跑到花园里一个固定的蚁穴洞口 -> 踩上去自动传送

所以蚁穴这一个 map 名要对应两个阶段、两张寻路图。这里把「一个 map 名 -> (去哪个
生态区的服务器, 依次经过哪几张图)」抽成一张表, 让 main 的主循环对单图/多图一视同仁。

纯数据 + 纯函数: 不 import cv2 / pyautogui / GUI / cdp_bridge, 好单测。
"""
from dataclasses import dataclass


# 蚁穴洞口在 maps/garden.png 坐标系(300x300)里的位置。
#
# 2026-09-15 实机标定(1920x1080 客户端): 花园局内跑 `python capture_map.py garden`
# 生成 maps/garden.png + debug_garden_raw.png, 在**原色**那张上点洞口读出来的。
# 两张图来自同一次 utils.get_map(), 坐标系完全一致。
#
# 洞口在小地图上是个亮绿色圆点, 灰度 179 —— 低于 preprocess_map 的 200 阈值, 默认
# 会被当成墙。寻路目标是墙的话 lazy_theta_star 直接规划失败, 所以 maps/garden.png
# 必须是这样生成的(否则这个坐标指向一堵墙):
#
#     python capture_map.py garden --portal 133,234
#
# 花园里这样的绿点一共 7 个, 只有这一个是蚁穴入口(2026-09-15 用户确认), 另外 6 个
# 通向别处。--portal 只打通点名的那一个, 其余留着当墙 —— 踩上传送点是会把人传走
# 的, 留成墙寻路才会绕开, 不然 bot 去蚁穴的半路上就被拽到别的地方了。
# test_map_routes 里两条回归用例分别盯着"这个必须可走"和"那 6 个必须是墙"。
# None = 未标定 -> route_for("anthell").is_calibrated() 为 False -> main 在 worker
# 启动时就明说"洞口坐标未标定"并退出, 不会静默按错坐标乱跑。
ANTHELL_PORTAL = (133, 234)

# 踩上去就传送(用户确认), 所以到达判定取洞口 ±这么多像素的小方框(地图坐标系),
# 不要求精确落在那一个像素上。lazy_theta_pathing 本来就用"进 area 就算到"判定。
#
# 这个容差框比florr真正触发传送要求的精度松(实机复盘 2026-09-16: 角色蹭到框边
# 被判"到了", 但传送没真的发生) —— 300x300小地图截图+resize+颜色匹配这条链路
# 本身就有损。真正的精度提升来自下面的 ANTHELL_PORTAL_WORLD(canvas 钩子直接读
# 的世界坐标, 不经过这层量化), 这个像素容差框继续留着当"退化"路径的判据, 两边
# 一起判才推进(main._run_entry_route), 不是互相替代。
#
# 不能收得比 5 还紧: main.move_to_position()(lazy_theta_pathing 内层走每一步
# 用的那个)自己的"到达"判据是"距离目标 < 5 个地图像素"(欧氏距离, 硬编码,
# 通用于所有寻路, 不是这一条路线专属), 跟这里的容差框是"每个轴分别 <= tolerance"
# (正方形, 不是圆)。tolerance 比 5 小时, move_to_position 会在一个欧氏距离已经
# < 5 但某个轴分量超过 tolerance 的点上认为"到了"(比如目标 (133,234), 落在
# (134,231): 距离 3.16 < 5, 但 y 轴上差 3), 而 lazy_theta_pathing 外层的
# if_in_area(target_box) 检查不认这个点 —— 两边判据对不上, 外层会不断重新
# 规划一条"从这个点到目标"的1步路径、move_to_position 又立刻宣称"到了"、
# 外层再复查还是不在框里, 无限循环(实机复现 2026-09-19: 卡在这个死循环里,
# 连下面的 canvas 复查/精确贴近逻辑都没机会跑起来)。tolerance=5 的正方形能
# 完整包住 move_to_position 那个半径5的圆(dist<5 ⟹ |dx|<5 且 |dy|<5), 消除
# 这个不可能三角。
PORTAL_TOLERANCE = 5

# 蚁穴洞口的绝对世界坐标(florr 内部真实使用的浮点坐标, 不是300x300小地图缩放后
# 的量化坐标) —— 2026-09-17 用 debug_canvas_portal.py 从 canvas 绘制记录里直接
# 反解出来的, 不是估的:
#   - 小地图上这个洞口画成一个 #7EEF6D 绿点, 反解公式 (x-m[4])/m[0] 直接给出
#     世界坐标, 不用先截图缩放再颜色匹配.
#   - 主视图里同一个世界坐标处能看到一组同心圆(半径按9递减, 最外层是渐变填充)
#     ——跟"带光效的洞口"这个视觉描述吻合, 独立印证了这不是别的什么东西.
#   - 交叉验证: 花园里一共有7个这样的绿点(对应7个minimap坐标), 把这次反解出来
#     的7个世界坐标换算回300x300小地图空间(减去 minimap_capture_region() 的
#     偏移), 跟当年纯像素分析找出来的7个坐标逐一对应、误差<0.3像素 —— 其中
#     换算出来的(133.2,234.1)跟 ANTHELL_PORTAL=(133,234)几乎完全重合, 确认
#     这就是同一个洞口, 不是另外6个通向别处的.
# None = 还没标定 -> 精度提升这条路径不生效, main 退化成只用 PORTAL_TOLERANCE
# 那套(现有行为, 不会更差)。
ANTHELL_PORTAL_WORLD = (27392.0, 48896.0)

# canvas 世界坐标判"还没到"的半径(世界单位) —— 没有在实机做过专门的精度收紧
# 实验, 只是从这次标定时的观测数据(玩家在这个距离外能看到洞口发光效果, 光效
# 半径约100世界单位)倒推出一个明显够用的量: 偏大的代价只是多重试几次(有
# ENTRY_ROUTE_TIMEOUT 兜底, 不危险), 偏小才会漏判"其实还没到"——所以宁可估大。
PORTAL_WORLD_HOLD_BACK_RADIUS = 150.0

# 密道(Shortcuts, florr 2025-12-15 加入游戏): 贴着墙画的隐藏通道, 玩家靠近才
# 显形, 平时看起来就是一堵普通墙。capture_map.py 截的静态图截不到"靠近显形"
# 这个动态效果, preprocess_map 二值化时跟真墙没有任何区别 —— 跟 ANTHELL_PORTAL
# 踩过的坑同一类(传送点在没打通前也被当墙), 修法也一样: 标定出坐标, 生成地图
# 时强制打通成可走(见 utils.preprocess_map 的 open_shortcuts_at)。
#
# 用户反馈(2026-09-20): "地图里其实还有密道可以走", 要求把已知密道也加进来。
# 花园这两条不是实机标定的(手上没有能现场量坐标的实机环境), 坐标来自
# florr.io Fandom Wiki 的 Shortcuts 词条(official-florrio.fandom.com/
# wiki/Shortcuts)用户投稿的标注小地图截图("The two garden shortcuts"),
# 通过下面这套流程换算成这份代码用的 300x300 地图坐标, 不是肉眼估的:
#   1. wiki 那张标注图跟 debug_garden_raw.png 是同一张花园小地图, 花园上那 7 个
#      传送点绿斑(ANTHELL_PORTAL + test_map_routes._OTHER_PORTALS 那 6 个)在
#      两张图上都找得到、一一对应。
#   2. 拿这 7 对对应点做仿射最小二乘拟合(纯缩放+平移, 无旋转), 7 点残差全部
#      <0.8 像素(wiki 图坐标系下) —— 说明这就是同一张图整体缩放+裁切了一点
#      边距, 不是巧合凑出来的映射。
#   3. wiki 图上红线标注的两条密道位置, 代入这个变换算出对应的 300x300 地图
#      坐标, 取标注范围的外接矩形 —— 落点全部准确压在 maps/garden.png 现有的
#      墙(0)像素上, 跟"密道在静态图里就是墙"这个预期吻合, 交叉验证了变换没算错。
# 矩形格式: (x0, y0, x1, y1), 含边界, 300x300 地图坐标系.
GARDEN_SHORTCUTS = [
    (28, 127, 68, 139),    # 出生点左边那条(wiki 原话: "one just to the left of spawn")
    (179, 82, 211, 115),   # 水晶房右边那条(wiki 原话: "the right of the Crystal Room")
]

# 蚁穴的 3 条密道 —— 直接从 florr 自己的地图文件算出来的, 不是估的.
#
# 数据源: https://florr.io/static/maps/ant_hell.tmj (florr 服务端用的 Tiled 地图,
# ashish.top 的地图查看器画的也是它) + /static/tiles/*.svg (每种瓦片的形状).
#   - "dirt" 瓦片层 = 会碰撞的墙. 瓦片不是整格方块: dirt_l / dirt_tl / dirt_tri
#     是半格/角/三角的弯曲轮廓, 带翻转标志(TMX 规范: 先 x/y 对调, 再水平、垂直翻).
#   - "shortcut" 瓦片层(属性 collisions=false) = 密道上盖的树根贴图 root_*. 小地图
#     把它当墙画, 所以截图生成的 maps/anthell.png 里密道是墙.
#   - 真正能走的密道 = 树根贴图底下、dirt 墙轮廓之外的那部分.
#
# 换算到 300x300 小地图坐标:
#   1. 按 SVG 轮廓把整张蚁穴的墙渲染出来, 跟 debug_anthell_raw.png 的小地图整图
#      配准: 起点用 3 个传送点(to_garden/to_desert/to_jungle, 就是
#      test_map_routes._ANTHELL_PORTALS 那 3 个绿点)解的仿射变换, 再用 ECC 精修 ——
#      相关系数 0.995, 精修只挪了不到 0.1 像素, 墙的像素一致率 95.5% (剩下是小地图
#      抗锯齿/阈值的边缘差异, 不是错位). 同样的渲染把翻转约定换成"先翻后对调",
#      一致率掉到 93%, 说明翻转也解对了.
#   2. 取"小地图画成墙 + 树根贴图覆盖 + dirt 墙覆盖 < 50%"的像素 —— 3 条共 90 个.
#      每条都是 2~3 像素宽的弯通道, 两头接主迷宫, 走它能把路程从 113~147 像素缩到
#      11~12 像素.
#
# 用户反馈(2026-09-26)"密道还是有问题, 为什么不是精准的": 上一版把"有树根贴图的
# 格子"整格当可走, 还往外取整扩成 3~4 像素的方块 —— 共打通 274 个像素, 按上面的
# 渲染其中 169 个是真墙, 寻路会以为能从石头里穿过去.
# 格式同 GARDEN_SHORTCUTS: (x0, y0, x1, y1) 含边界; 这里每个元素是同一行的一段像素.
ANTHELL_SHORTCUTS = [
    # ant_hell.tmj shortcut 物件 id 331 (世界约 17168,16824 起)
    (83, 84, 87, 84), (83, 85, 87, 85), (86, 86, 93, 86), (86, 87, 93, 87),
    (89, 88, 89, 88), (91, 88, 93, 88), (91, 89, 93, 89), (91, 90, 93, 90),
    # id 330 (世界约 20776,22160 起)
    (100, 107, 100, 107), (100, 108, 104, 108), (100, 109, 104, 109),
    (103, 110, 109, 110), (103, 111, 109, 111),
    # id 332 (世界约 15616,37608 起)
    (85, 178, 86, 178), (84, 179, 86, 179), (76, 180, 76, 180), (84, 180, 86, 180),
    (76, 181, 85, 181), (76, 182, 85, 182),
]


@dataclass(frozen=True)
class Stage:
    """路线里的一段: 在 map_name 这张图上走到 walk_to。

    walk_to 为 None = 最后一段, 不用走, 到这张图就开始按时块配的 location /
    farming_area 刷怪。

    walk_to_world: walk_to 对应的绝对世界坐标(canvas 钩子能读到的那种), 没有
    就是 None —— 只有蚁穴洞口这一段现在有. main._run_entry_route 拿它做一层
    比 target_box 更精确的"是不是真的到了"复查, 读不到(钩子没装上/画面没在动/
    这段本来就没有世界坐标)就跳过这层, 只用 target_box 那套现有判据.
    """
    map_name: str
    walk_to: tuple | None
    walk_to_world: tuple | None = None

    def target_box(self, tolerance=PORTAL_TOLERANCE):
        """walk_to 周围的到达判定框, 形状直接喂给 utils.if_in_area /
        lazy_theta_pathing(location, [box])。"""
        x, y = self.walk_to
        return [(x - tolerance, y - tolerance), (x + tolerance, y + tolerance)]


@dataclass(frozen=True)
class Route:
    """一个 config map 名的完整进场路线。

    server_biome: 给 utils.select_biome_on_title() 和 utils.switch_server() 的
        生态区 key(= server_lookup.BIOME_INDEX 的 key)。蚁穴填 garden 不是
        ant_hell —— 见模块文档。
    stages: 从进场到刷怪依次经过的图, 最后一个是刷怪那张。
    """
    server_biome: str
    stages: tuple

    @property
    def final_map(self):
        """刷怪阶段那张图 —— 时块里的 location / farming_area 就活在它的坐标系里。"""
        return self.stages[-1].map_name

    def stage_for(self, map_name):
        """人现在在 map_name 这张图上时该走哪一段。不在这条路线上 -> None。"""
        for stage in self.stages:
            if stage.map_name == map_name:
                return stage
        return None

    def is_calibrated(self):
        """这条路线能不能跑: 每一个要走到的点都得有坐标。
        单图路线没有要走的点, 恒 True; 蚁穴在 ANTHELL_PORTAL 填上之前是 False。"""
        return all(stage.walk_to is not None for stage in self.stages[:-1])


def _routes():
    """每次现建, 不做模块级常量 —— 这样测试 monkeypatch ANTHELL_PORTAL 之后
    立刻生效(frozen dataclass 一旦建好就捕获了当时的值)。表只有三条, 重建的
    开销可以忽略, 而 route_for() 的调用点也就 worker 启动和每轮一次。"""
    return {
        "desert": Route("desert", (Stage("desert", None),)),
        "ocean": Route("ocean", (Stage("ocean", None),)),
        "anthell": Route("garden", (Stage("garden", ANTHELL_PORTAL, ANTHELL_PORTAL_WORLD),
                                    Stage("anthell", None))),
    }


def route_for(map_name):
    """config.json 的 map 名 -> Route。未知名回退 desert —— 跟
    server_lookup.biome_key_for_map() 同样的容错(调用方已经保证传的是
    app_config._VALID_MAPS 之一, 这里只是多一层不炸)。"""
    routes = _routes()
    return routes.get(map_name, routes["desert"])
