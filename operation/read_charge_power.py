"""读快捷手册资源栏的电量、储蓄电量、以太电池。

## 为什么不用主仓现成的解析

主仓 `ChargePlanApp._parse_battery_charge_ocr` 是下划线私有方法，主仓一改签名插件就碎。
这段解析逻辑只有几十行，复制一份放在插件里更稳，行为与原版一致（同样处理「识别失败返回
(0, 0, 0) 且不抛异常」这个约定）。

## 识别方式

跑主仓的「电量检测」图像分析流水线（按以太电池区域裁剪 + HSV 过滤 + 整栏 OCR），
再把 OCR 出来的数字按「三个字段的固定中心锚点」归位：

- 文本按 `/` 分界取左段（`129/240` -> `129`）
- 每个数字框的中心 x 与三个锚点比，最接近哪个就归到哪个字段
- 同一字段多个候选时取更准的那个；某字段没有候选就补 0
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from one_dragon.utils import str_utils
from one_dragon.utils.log_utils import log

if TYPE_CHECKING:
    from numpy import ndarray

    from zzz_od.context.zzz_context import ZContext


#: 主仓「电量检测」流水线的名字
PIPELINE_NAME: str = '电量检测'

#: 三个字段在资源栏里的固定中心锚点（按裁剪后的坐标量出来的，改布局才需要动）
FIELD_CENTERS: tuple[int, int, int] = (173, 348, 485)


def parse_charge_ocr(ocr_result: dict) -> tuple[int, int, int]:
    """把资源栏的 OCR 结果解析成三个数字。

    Args:
        ocr_result: OCR 结果，形如 `{文本: MatchResultList}`

    Returns:
        (电量, 储蓄电量, 以太电池)。识别不到的字段补 0，全失败返回 (0, 0, 0) 且不抛异常
    """
    items: list[tuple[float, int]] = []
    for text, match_list in (ocr_result or {}).items():
        if match_list is None:
            continue
        for match in match_list:
            value = str_utils.get_positive_digits(text.split('/')[0], None)
            if value is None:
                continue
            items.append((match.x + match.w / 2, value))

    slot_values: list[tuple[float, int] | None] = [None, None, None]
    for x_center, value in items:
        field_idx = min(range(3), key=lambda i: abs(x_center - FIELD_CENTERS[i]))
        current = slot_values[field_idx]
        if current is None or (
            abs(x_center - FIELD_CENTERS[field_idx])
            < abs(current[0] - FIELD_CENTERS[field_idx])
        ):
            slot_values[field_idx] = (x_center, value)

    return (
        slot_values[0][1] if slot_values[0] is not None else 0,
        slot_values[1][1] if slot_values[1] is not None else 0,
        slot_values[2][1] if slot_values[2] is not None else 0,
    )


def read_charge(ctx: ZContext, screen: ndarray) -> tuple[int, int, int]:
    """跑流水线读一次电量。

    Args:
        ctx: 一条龙上下文，用来访问图像分析流水线
        screen: 当前截图（快捷手册的资源栏要停在画面上）

    Returns:
        (电量, 储蓄电量, 以太电池)。流水线出错时记录日志并返回 (0, 0, 0)，由调用方决定怎么降级
    """
    try:
        pipeline_ctx = ctx.cv_service.run_pipeline(PIPELINE_NAME, screen)
    except Exception:
        log.error('跑「电量检测」流水线出错', exc_info=True)
        return 0, 0, 0
    return parse_charge_ocr(pipeline_ctx.ocr_result)
