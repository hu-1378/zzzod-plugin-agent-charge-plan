"""特训目标页、以及滚动操作的共用入口。

## 区域定义在哪

区域坐标写在插件自己的 `screen_info/compendium_agent_plan.yml` 里（画面名
`快捷手册-特训目标`），按项目规范「截图区域在 yml 里声明，不硬编码在 Python 中」。

不直接加进主仓的 `compendium.yml` 是因为：主仓已经有 `screen_name=快捷手册` 这个画面，
`screen_loader` 遇到重名会跳过后注册的那个，插件覆盖不了它。所以用全局唯一的画面名
单独注册一份。页面导航仍然走主仓的 `快捷手册-训练` 画面路线。
"""

from typing import TYPE_CHECKING

from one_dragon.base.geometry.point import Point
from one_dragon.base.geometry.rectangle import Rect

if TYPE_CHECKING:
    from zzz_od.context.zzz_context import ZContext


#: 插件自己注册的画面名（区域容器，不参与 goto 导航）
COMPENDIUM_SCREEN: str = '快捷手册-特训目标'
#: 主仓的画面名，导航时用它
COMPENDIUM_TRAINING_SCREEN: str = '快捷手册-训练'

#: 训练 TAB 左侧分类里「特训目标」那一格（选中时是蓝底）
AREA_LEFT_CAT_TARGET: str = '区域-分类-特训目标'
#: 主区顶部角色卡上的代理人名字（白色粗体）
AREA_AGENT_NAME: str = '区域-特训目标角色名'
#: 角色卡上的「查看攻略」按钮
AREA_VIEW_GUIDE: str = '按钮-查看攻略'
#: 角色卡上的「更换特训目标」按钮
AREA_CHANGE_TARGET: str = '按钮-更换特训目标'
#: 主区整块条目列表（一次 OCR 后按 y 聚成行）
AREA_PLAN_LIST: str = '区域-特训目标清单'


def get_area_rect(ctx: 'ZContext', screen_name: str, area_name: str) -> Rect:
    """取一个区域的矩形。

    Args:
        ctx: 一条龙上下文，用来访问 screen_loader
        screen_name: 画面名称
        area_name: 区域名称

    Returns:
        区域矩形

    Raises:
        LookupError: 画面对应的 yml 没加载到，或者 yml 里没写这个区域
    """
    area = ctx.screen_loader.get_area(screen_name, area_name)
    if area is None:
        raise LookupError(
            f'取不到区域 {screen_name}.{area_name}：'
            f'插件的 screen_info 没加载到，或者 yml 里漏了这个区域'
        )
    return area.rect


def scroll_rect(
    ctx: 'ZContext',
    rect: Rect,
    direction: str = 'down',
    start_ratio: float = 0.84,
    end_ratio: float = 0.16,
    duration: float = 0.9,
) -> None:
    """在一个矩形范围内按住拖动来滚动。

    算法和参数含义照搬项目标准的 `one_dragon.base.screen.screen_utils.scroll_area`，
    区别只是这里接受 `Rect` 而不是 `ScreenArea`（有些调用点手里只有矩形）。

    Args:
        ctx: 一条龙上下文
        rect: 要滚动的区域
        direction: `down` 表示看下面的内容（手势往上划），`up` 反之
        start_ratio: 起点在这块区域的纵向比例，0 是顶部、1 是底部
        end_ratio: 终点比例
        duration: 拖动全程耗时（秒）。调大 = 滑得慢，更容易控制距离
    """
    height: int = rect.height
    start_ratio = max(0.0, min(1.0, start_ratio))
    end_ratio = max(0.0, min(1.0, end_ratio))
    y_start: int = rect.y1 + int(height * start_ratio)
    y_end: int = rect.y1 + int(height * end_ratio)
    x: int = rect.center.x

    if direction == 'up':
        # 往上翻：手势从下往上
        start = Point(x, y_end)
        end = Point(x, y_start)
    else:
        # 往下翻：手势从上往下拖
        start = Point(x, y_start)
        end = Point(x, y_end)

    # 起终点重合时说明这块区域高度是 0，挪一个像素避免「没滚动」
    if start.y == end.y:
        if start.y >= rect.center.y:
            end = Point(end.x, max(rect.y1 + 1, end.y - 1))
        else:
            end = Point(end.x, min(rect.y2 - 1, end.y + 1))

    ctx.controller.drag_to(start=start, end=end, duration=duration)
