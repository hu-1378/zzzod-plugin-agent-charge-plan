"""特训目标相关画面的区域坐标。

## 为什么坐标写在这里，而不是主仓的 screen_info

这些区域（角色名、查看攻略、更换特训目标、清单区、左侧分类）本该加进主仓
`assets/game_data/screen_info/compendium.yml`。但改主仓要走 PR 评审流程，
本插件选择**完全不动主仓**，把需要的坐标写成本文件的常量。

## 取值顺序

先问主仓的 `screen_loader`：如果以后这些区域合进了主仓，自动用主仓的（更准）。
取不到再用本文件的兜底值。

区域名故意起得和主仓风格一致，方便以后整体搬进 `compendium.yml`。
"""

from typing import TYPE_CHECKING

from one_dragon.base.geometry.point import Point
from one_dragon.base.geometry.rectangle import Rect

if TYPE_CHECKING:
    from zzz_od.context.zzz_context import ZContext


#: 画面对应主仓 `assets/game_data/screen_info/compendium.yml` 的 screen_name
COMPENDIUM_SCREEN: str = '快捷手册'
#: 训练 TAB 对应的画面（主仓 `compendium_training.yml` 的 screen_name），特训目标页在这个 TAB 下
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

#: 兜底坐标，格式 (left, top, right, bottom)，基准 1920x1080
_FALLBACK_RECT: dict[str, tuple[int, int, int, int]] = {
    AREA_LEFT_CAT_TARGET: (164, 325, 429, 432),
    AREA_AGENT_NAME: (500, 265, 860, 350),
    AREA_VIEW_GUIDE: (940, 315, 1240, 390),
    AREA_CHANGE_TARGET: (1235, 310, 1580, 395),
    AREA_PLAN_LIST: (470, 410, 1675, 975),
}


def get_area_rect(ctx: 'ZContext', screen_name: str, area_name: str) -> Rect:
    """取一个区域的矩形。

    主仓 screen_info 里定义了就用主仓的，否则用本文件的兜底坐标。

    Args:
        ctx: 一条龙上下文，用来访问 screen_loader
        screen_name: 画面名称
        area_name: 区域名称

    Returns:
        区域矩形

    Raises:
        KeyError: 主仓没有该区域，本文件也没有兜底坐标
    """
    area = ctx.screen_loader.get_area(screen_name, area_name)
    if area is not None:
        return area.rect

    fallback = _FALLBACK_RECT.get(area_name)
    if fallback is None:
        raise KeyError(f'没有为 {screen_name}.{area_name} 准备兜底坐标')
    left, top, right, bottom = fallback
    return Rect(left, top, right, bottom)


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
    区别只是这里接受 `Rect` 而不是 `ScreenArea`——特训目标页的清单区可能是本文件里的
    兜底坐标，没有对应的 ScreenArea 对象。

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
