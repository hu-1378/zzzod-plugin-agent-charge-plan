"""读养成材料的「已有 / 所需」，算出缺口。

## 两条读数路径

### 路径 A（默认，最省事）：养成方案-详情页的「材料预览」栏

    特训目标页 → 点「查看攻略」→ 养成方案-详情 → 右侧「材料预览」栏

这一栏每行是「材料图标 + 进度条 [已有/所需] + 按钮」，进度条上是 `60/159` 这种文本，
意思就是「已有 60 / 所需 159」。一次 OCR 就能拿到这个角色整套养成方案涉及的所有材料。

**代价：这一栏只有图标，没有材料名。** 所以本文件把 `material_name` 留空，
用 `source` 标明取自哪一栏，由上层按图标顺序或其它来源补名字。不在这里瞎猜名字，
猜错会把缺口算到隔壁材料上（初阶 / 高阶 / 特化只差一个字，后果比留空更糟）。

### 路径 B（材料预览读不到时的备选，默认可用）：养成材料计算的明细表

    养成方案-详情 → 「更多方案」→ 养成指南 → 角色名下方「养成材料计算」
    → 弹窗设置页点「确定」→ 结果页「养成材料明细」四列表

这条路能拿到材料名（材料预览栏只有图标），代价是多点几下。所以默认顺序是
「先读材料预览，读不到再走这条路」；不想要这条路就把构造参数的
`allow_dialog_fallback` 传 False，这时材料预览读不到会直接失败，不会乱点。

中间每一步都有独立节点、独立失败状态：哪个画面没到、哪个按钮没找到，都写进 status，
不会静默跳过然后交一份空数据上去。

**注意**：这条路涉及的三页（养成方案详情 / 养成指南 / 养成材料计算弹窗）里，
养成方案详情页和养成材料计算弹窗有留档截图，养成指南页只有一张「方案概览」的截图。
点「更多方案」「养成材料计算」「确定」这几个动作的坐标是照建档写的，**未实机验证**。

## 游戏内 10 分钟延迟（硬约束，别在这文件里做闭环）

结果页自己写着「仓库数据计算存在10分钟左右延迟，请悉知」。刚刷完的材料不会立刻
出现在页面和仓库库存里，所以：

- 本文件只负责「读一次当前快照」，**不实现**「刷一次 → 立刻重读 → 精确判定够了没」。
  那样读到的是旧值，会把已经够的材料判成还差，反复多刷。
- 判定够不够用 `planner/material_goal.py` 的办法：按「已刷次数 × 单次产出」自己累加估算。
- 真要重新校准，等 10 分钟以上再调一次本操作。

## 明细表的读法（路径 B 用）

四列表的列 x 固定、行 y 随滚动变化，所以是「整块 OCR → 按 x 分列 → 按 y 聚行」。
两个坑，都在 `parse_detail_table` 里处理了：

- 行尾「仍需获取」是绿色对勾时那一格**没有文字**，这是「已满足」，不是解析失败。
- 分区标题行（「代理人技能升级」这种）右侧挂着一个丁尼合计（`675,000`，带千分位），
  不能当成某一行材料的缺口。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from one_dragon.base.geometry.rectangle import Rect
from one_dragon.base.operation.operation_edge import node_from
from one_dragon.base.operation.operation_node import operation_node
from one_dragon.base.operation.operation_round_result import OperationRoundResult
from one_dragon.utils import str_utils
from one_dragon.utils.log_utils import log
from zzz_od.operation.zzz_operation import ZOperation

from . import agent_plan_page, screen_areas
from .agent_plan_page import TextBlock
from .goto_agent_plan import GotoAgentPlan

if TYPE_CHECKING:
    from cv2.typing import MatLike

    from zzz_od.context.zzz_context import ZContext


# ============================= 画面与区域名 =============================

#: 养成方案详情页（插件自带 screen_info/agent_guide_overview.yml）
GUIDE_SCREEN: str = '养成方案-详情'
#: 右侧「材料预览」栏：每行「图标 + 已有/所需 + 按钮」。**本文件的主力数据源**
AREA_GUIDE_MATERIAL_PREVIEW: str = '区域-材料预览'
#: 蓝色横幅上的代理人名字
AREA_GUIDE_AGENT_NAME: str = '区域-角色名'
#: 左上角「更多方案」，也是这一页的识别锚点
AREA_GUIDE_MORE_PLAN: str = '按钮-更多方案'

#: 养成指南页（插件自带 screen_info/agent_guide.yml），从养成方案详情页点「更多方案」进来
AGENT_GUIDE_SCREEN: str = '养成指南'
#: 左上角标题，本页的识别锚点
AREA_AGENT_GUIDE_TITLE: str = '标题-养成指南'
#: 代理人名字正下方的「养成材料计算 ▶」，进材料计算弹窗的入口
AREA_AGENT_GUIDE_CALC_BUTTON: str = '按钮-养成材料计算'
#: 粉色横幅上的代理人名字
AREA_AGENT_GUIDE_AGENT_NAME: str = '区域-角色名'
#: 左上角返回按钮，回养成方案详情页（本文件暂不用，留给异常收尾）
AREA_AGENT_GUIDE_BACK: str = '按钮-返回'

#: 养成材料计算弹窗（插件自带 screen_info/material_calc_dialog.yml）
CALC_SCREEN: str = '养成材料计算'
#: 弹窗标题，用来判断弹窗开没开
AREA_CALC_TITLE: str = '标题-养成材料计算'
#: 设置页底部的「确定」，点完同一弹窗内切到结果页
AREA_CALC_CONFIRM: str = '按钮-确定'
#: 结果页二：养成材料明细四列表整块
AREA_CALC_DETAIL_TABLE: str = '区域-明细表'
#: 结果页二的四列（列 x 固定）
AREA_CALC_COL_NAME: str = '列-材料名'
AREA_CALC_COL_REQUIRED: str = '列-所需'
AREA_CALC_COL_OWNED: str = '列-已有'
AREA_CALC_COL_MISSING: str = '列-仍需获取'
#: 结果页一：需要获取的材料图标横排（只有数字或「已满足」，没有材料名）
AREA_CALC_NEED_ICONS: str = '区域-需要获取的材料'

# ============================= 数据来源标记 =============================

#: 来自养成方案详情页的「材料预览」栏（有已有/所需，没有材料名）
SOURCE_GUIDE_PREVIEW: str = 'material_preview'
#: 来自养成材料计算弹窗的「明细表」（有材料名）
SOURCE_CALC_DETAIL: str = 'calc_detail'

# ============================= 解析用的常数 =============================

#: 同一行的文字块 y（中心）相差多少像素以内算一行。实测明细表行距约 76px，同一行内偏差不超过 10px
ROW_Y_TOLERANCE: int = 24

#: 「已有/所需」进度文本。斜杠可能被 OCR 读成全角，先统一再匹配
_PROGRESS_PATTERN = re.compile(r'^(\d{1,9})/(\d{1,9})$')
#: 纯数字（材料个数）。带逗号的是丁尼合计，不算
_COUNT_PATTERN = re.compile(r'^\d{1,9}$')

#: 材料预览栏每行右侧的按钮文字。用来交叉核对「够 / 不够」，也方便排查
PREVIEW_BUTTON_TEXT_LIST: list[str] = ['获取', '养成', '装备', '前往']

#: 明细表表头三列的名字，用来认出「表头行」和「分区标题行」
TABLE_HEADER_KEY_LIST: list[str] = ['所需', '已有']
#: 结果页一里表示「已达标」的文本
ICON_SATISFIED_TEXT: str = '已满足'

#: 点按钮后等界面反应的时间（秒）
CLICK_WAIT: float = 1.2

#: 明细表四列在 `column_block_list` 里的下标记号
COLUMN_NAME: int = 0
COLUMN_REQUIRED: int = 1
COLUMN_OWNED: int = 2
COLUMN_MISSING: int = 3

#: 节点状态：材料预览栏没读到东西，要不要再试弹窗路径
STATUS_PREVIEW_EMPTY: str = '材料预览没读到'
#: 节点状态：已经在养成指南页了
STATUS_GUIDE_READY: str = '已在养成指南页'
#: 节点状态：弹窗已经开着
STATUS_DIALOG_OPEN: str = '弹窗已开'


# ============================= 数据结构 =============================


@dataclass
class MaterialGapItem:
    """一种材料的缺口。"""

    owned: int | None = None
    """已有数量。读不到是 None。"""

    required: int | None = None
    """所需数量（养成方案要求的总数）。读不到是 None。"""

    missing: int | None = None
    """仍需获取。页面上是黄字数字；绿色对勾（已满足）时是 None，看 satisfied。"""

    satisfied: bool = False
    """是否已满足。行尾对勾、或者已有 >= 所需，都算满足。"""

    material_name: str = ''
    """材料名。材料预览栏只有图标没有名字，所以这里通常是空的——留空，不猜。"""

    progress_text: str = ''
    """页面上那一格原文，例如 60/159。排查时看这个最直接。"""

    section_name: str = ''
    """明细表里的分区名，例如 代理人技能升级。材料预览栏没有分区名。"""

    button_text: str = ''
    """同一行右侧的按钮文字（获取 / 养成 / 装备）。只有材料预览栏有。"""

    source: str = SOURCE_GUIDE_PREVIEW
    """这条数据读自哪一栏，见 SOURCE_* 常量。"""

    @property
    def gap(self) -> int | None:
        """还差多少个。已知数字时用它，不知道就是 None。"""
        if self.missing is not None:
            return max(0, self.missing)
        if self.owned is not None and self.required is not None:
            return max(0, self.required - self.owned)
        return None

    @property
    def has_name(self) -> bool:
        """有没有材料名。材料预览栏读出来的是 False。"""
        return self.material_name != ''

    def to_plan_row(self) -> dict[str, str]:
        """转成 `PlanSnapshot.from_rows` 能吃的字典。

        只给「条目名 + 进度文本」两项：条目名是材料名（可能为空，空的那条没法排计划），
        电量文本和单次产出由 `planner/material_goal.py` 按材料表补。
        """
        progress_text = f'{self.owned}/{self.required}' \
            if self.owned is not None and self.required is not None else ''
        return {
            'entry_name': self.material_name,
            'progress_text': progress_text,
        }


@dataclass(frozen=True)
class NeedIconSummary:
    """结果页一「需要获取的材料」的概况。只有图标和数字，拿不到材料名。"""

    gap_count: int = 0
    """带数字的图标个数：有几个材料还缺。"""

    satisfied_count: int = 0
    """显示「已满足」的图标个数。"""

    gap_number_list: tuple[int, ...] = ()
    """每个缺口的数字，按从左到右。顺序和图标顺序一致。"""


@dataclass
class MaterialGapSnapshot:
    """一次材料缺口读数的完整结果。"""

    source: str = ''
    """整份数据来自哪条路径，见 SOURCE_* 常量。"""

    agent_name: str = ''
    """这套养成方案属于哪个代理人。读不到就是空字符串。"""

    item_list: list[MaterialGapItem] = field(default_factory=list)
    """读到的每一种材料，按页面从上到下。"""

    need_icon_summary: NeedIconSummary | None = None
    """结果页一的概况。只走材料预览栏时是 None。"""

    warnings: list[str] = field(default_factory=list)
    """降级提示：没读到名字、数字不全、某行被跳过都在这里。"""

    @property
    def gap_item_list(self) -> list[MaterialGapItem]:
        """还没满足的那些材料。"""
        return [item for item in self.item_list if not item.satisfied]

    @property
    def named_item_list(self) -> list[MaterialGapItem]:
        """有材料名的那些（只有明细表路径才有）。"""
        return [item for item in self.item_list if item.has_name]

    @property
    def unnamed_count(self) -> int:
        """只有图标、没有材料名的条目个数。

        走「材料预览」栏时整栏都是这种：缺口数字能读到，但没法对到材料表换算次数。
        """
        return sum(1 for item in self.item_list if not item.has_name)

    def plan_row_list(self) -> list[dict[str, str]]:
        """转成 `PlanSnapshot.from_rows` 的入参。没名字的条目直接跳过（个数看 `unnamed_count`）。

        本方法不改 `warnings`，可以放心多调几次。现读现用的写法：

            planner = MaterialGoalPlanner()
            goal_list = [
                MaterialOwned(goal=MaterialGoal(name, floor), owned=owned)
                for name, floor, owned in 配置
            ]
            snapshot, warnings = planner.build_snapshot(goal_list)
        """
        return [item.to_plan_row() for item in self.item_list if item.has_name]

    def summary_lines(self) -> list[str]:
        """拼成几行日志。"""
        lines: list[str] = []
        if self.agent_name:
            lines.append(f'代理人 {self.agent_name}（数据来源 {self.source}）')
        for item in self.item_list:
            name = item.material_name or '（无名字，只有图标）'
            gap = item.gap if item.gap is not None else '未知'
            flag = '已满足' if item.satisfied else f'还差 {gap}'
            section = f'{item.section_name} ' if item.section_name else ''
            lines.append(
                f'{section}{name} 已有 {item.owned} 所需 {item.required} {flag}'
                f'（{item.progress_text or item.source}）'
            )
        if self.need_icon_summary is not None:
            summary = self.need_icon_summary
            lines.append(
                f'需要获取的材料：{summary.gap_count} 种还缺 {list(summary.gap_number_list)}，'
                f'{summary.satisfied_count} 种已满足'
            )
        lines.extend(f'[注意] {warning}' for warning in self.warnings)
        return lines


# ============================= 材料预览栏解析 =============================


def parse_preview_lines(
    block_list: list[TextBlock],
) -> tuple[list[MaterialGapItem], list[str]]:
    """把「材料预览」栏的文字块拼成材料缺口。

    只认「数字/数字」这种进度文本。栏目名（材料预览 / 代理人 / 驱动盘）、按钮
    （获取 / 养成 / 装备）、驱动盘图标上的件数（4 / 2）都丢掉。

    Args:
        block_list: 材料预览区域的文字块（整张截图的坐标）

    Returns:
        (材料列表, 降级提示)
    """
    item_list: list[MaterialGapItem] = []
    warnings: list[str] = []

    for row in group_rows(block_list):
        progress: tuple[str, int, int] | None = None
        for block in row:
            text = normalize_text(block.text)
            match = _PROGRESS_PATTERN.match(text)
            if match is not None:
                progress = (text, int(match.group(1)), int(match.group(2)))
                break
        if progress is None:
            continue

        text, owned, required = progress
        button_text = pick_button_text(row)
        # 页面上够不够其实看数字就行；按钮只是个旁证，对不上时记一笔，不改判定
        if button_text and button_text in PREVIEW_BUTTON_TEXT_LIST[2:] and owned < required:
            warnings.append(f'{text} 右侧按钮是「{button_text}」，但已有少于所需，按数字算')

        item_list.append(
            MaterialGapItem(
                owned=owned,
                required=required,
                missing=max(0, required - owned),
                satisfied=owned >= required,
                progress_text=text,
                button_text=button_text,
                source=SOURCE_GUIDE_PREVIEW,
            )
        )

    if not item_list:
        warnings.append(
            '「材料预览」栏一行「已有/所需」都没读到。可能这一栏是空的，'
            '也可能页面没停稳，需要重试或改走弹窗路径（见模块说明）'
        )
    return item_list, warnings


def pick_button_text(row: list[TextBlock]) -> str:
    """从一行文字块里挑出右侧按钮的文字。挑不到返回空字符串。

    Args:
        row: 同一行的文字块

    Returns:
        按钮文字，例如 获取
    """
    for block in row:
        text = normalize_text(block.text)
        if text in PREVIEW_BUTTON_TEXT_LIST:
            return text
    return ''


# ============================= 明细表解析 =============================


def parse_detail_table(
    block_list: list[TextBlock],
    name_rect: Rect,
    required_rect: Rect,
    owned_rect: Rect,
    missing_rect: Rect,
) -> tuple[list[MaterialGapItem], list[str]]:
    """把明细表的文字块拼成材料缺口。

    做法：先按 x 把文字块分到四列，再按 y 聚成行，最后一行一行读。
    行的判定是「所需 / 已有 / 仍需获取 里至少有一个数字」，所以：

    - 表头行（所需 已有 仍需获取）跳过；
    - 分区标题行（代理人技能升级 + 右侧丁尼合计）认成 section_name，不算数据行；
    - 行尾是绿色对勾时「仍需获取」那格没有文字，按「已满足」处理，不算解析失败。

    Args:
        block_list: 明细表区域的文字块（整张截图的坐标）
        name_rect: 「养成材料」列
        required_rect: 「所需」列
        owned_rect: 「已有」列
        missing_rect: 「仍需获取」列

    Returns:
        (材料列表, 降级提示)
    """
    item_list: list[MaterialGapItem] = []
    warnings: list[str] = []
    section_name: str = ''

    for row in group_rows(block_list):
        if is_table_header(row):
            continue

        column_block_list: list[list[TextBlock]] = [[], [], [], []]
        for block in row:
            column_block_list[
                column_of(block.rect, name_rect, required_rect, owned_rect, missing_rect)
            ].append(block)

        name_text = join_text(column_block_list[COLUMN_NAME])
        required = first_count(column_block_list[COLUMN_REQUIRED])
        owned = first_count(column_block_list[COLUMN_OWNED])
        missing = first_count(column_block_list[COLUMN_MISSING])
        missing_text_list = [normalize_text(b.text) for b in column_block_list[COLUMN_MISSING]]
        satisfied_mark = any(ICON_SATISFIED_TEXT in text for text in missing_text_list)
        money_like = any(looks_like_money(text) for text in missing_text_list)

        if required is None and owned is None:
            if name_text and str_utils.with_chinese(name_text):
                # 分区标题行，例如「代理人技能升级」。右侧挂的是丁尼合计，不是缺口
                section_name = name_text
            elif missing is not None and not money_like:
                warnings.append(
                    f'明细表有一行只读到「仍需获取 {missing}」，'
                    f'所需/已有都没读到，已跳过'
                )
            continue

        if missing is not None:
            satisfied = missing <= 0
        elif satisfied_mark:
            satisfied = True
        elif required is not None and owned is not None:
            # 绿色对勾那一格没有文字，用两个数字自己比一下
            satisfied = owned >= required
        else:
            satisfied = False
            warnings.append(
                f'明细表「{name_text or "某一行"}」数字不全'
                f'（所需 {required} / 已有 {owned} / 仍需 {missing}），按未满足处理'
            )

        item_list.append(
            MaterialGapItem(
                owned=owned,
                required=required,
                missing=missing,
                satisfied=satisfied,
                material_name=name_text,
                progress_text=f'{owned}/{required}'
                if owned is not None and required is not None else '',
                section_name=section_name,
                source=SOURCE_CALC_DETAIL,
            )
        )
        if not name_text:
            warnings.append(
                f'明细表有一行（所需 {required} / 已有 {owned}）没读到材料名，'
                f'缺口数字可用，但没法对到材料表'
            )

    if not item_list:
        warnings.append('明细表的四列表一行都没读到，可能页面还没切到结果页')
    return item_list, warnings


def parse_need_icons(block_list: list[TextBlock]) -> NeedIconSummary:
    """读结果页一「需要获取的材料」：几个还缺、几个已满足。

    这一排只有图标 + 数字（或「已满足」），拿不到材料名，只能用来快速判断有没有缺口。

    Args:
        block_list: 「需要获取的材料」区域的文字块

    Returns:
        概况
    """
    gap_number_list: list[int] = []
    satisfied_count: int = 0

    for block in block_list:
        text = normalize_text(block.text)
        if ICON_SATISFIED_TEXT in text:
            satisfied_count += 1
            continue
        if _COUNT_PATTERN.match(text):
            gap_number_list.append(int(text))

    return NeedIconSummary(
        gap_count=len(gap_number_list),
        satisfied_count=satisfied_count,
        gap_number_list=tuple(gap_number_list),
    )


def column_of(
    block_rect: Rect,
    name_rect: Rect,
    required_rect: Rect,
    owned_rect: Rect,
    missing_rect: Rect,
) -> int:
    """这块文字属于哪一列。

    用相邻两列之间的中线当边界（数字常比列区域宽一点，用列区域本身卡会误判）。
    比最右列还靠右的，也算最右列。

    Args:
        block_rect: 文字块矩形
        name_rect: 「养成材料」列
        required_rect: 「所需」列
        owned_rect: 「已有」列
        missing_rect: 「仍需获取」列

    Returns:
        COLUMN_NAME / COLUMN_REQUIRED / COLUMN_OWNED / COLUMN_MISSING
    """
    center_x: int = block_rect.center.x
    if center_x < (name_rect.x2 + required_rect.x1) / 2:
        return COLUMN_NAME
    if center_x < (required_rect.x2 + owned_rect.x1) / 2:
        return COLUMN_REQUIRED
    if center_x < (owned_rect.x2 + missing_rect.x1) / 2:
        return COLUMN_OWNED
    return COLUMN_MISSING


def is_table_header(row: list[TextBlock]) -> bool:
    """这一行是不是表头（养成材料 / 所需 / 已有 / 仍需获取）。

    Args:
        row: 同一行的文字块

    Returns:
        是表头就是 True
    """
    text_list = [normalize_text(block.text) for block in row]
    joined = '|'.join(text_list)
    return all(key in joined for key in TABLE_HEADER_KEY_LIST)


def looks_like_money(text: str) -> bool:
    """像不像丁尼合计（带千分位逗号）。

    分区标题右侧挂的 `675,000` 是这一类材料的丁尼总数，不是某一行材料的缺口。
    """
    return ',' in text or '，' in text


def first_count(block_list: list[TextBlock]) -> int | None:
    """一列里第一个纯数字。

    带逗号的（丁尼合计）、带汉字的（已满足）都不算材料个数，返回 None。

    Args:
        block_list: 同一行、同一列的文字块

    Returns:
        数字；没有就是 None
    """
    for block in block_list:
        text = normalize_text(block.text)
        if _COUNT_PATTERN.match(text):
            return int(text)
    return None


def join_text(block_list: list[TextBlock]) -> str:
    """把一格里拆开的文字块接起来（材料名被 OCR 拆成两段时会用到）。"""
    return ''.join(normalize_text(block.text) for block in block_list)


def group_rows(
    block_list: list[TextBlock], tolerance: int = ROW_Y_TOLERANCE
) -> list[list[TextBlock]]:
    """按 y 把文字块聚成行。

    Args:
        block_list: 文字块
        tolerance: 同一行的 y 允许偏差多少像素

    Returns:
        行列表，从上到下；每行内按 x 从左到右
    """
    rows: list[list[TextBlock]] = []
    current: list[TextBlock] = []
    current_y: float = 0.0

    for block in sorted(block_list, key=lambda b: (b.rect.center.y, b.rect.x1)):
        y = block.rect.center.y
        if not current:
            current = [block]
            current_y = float(y)
            continue
        if abs(y - current_y) <= tolerance:
            current.append(block)
            current_y = (current_y * (len(current) - 1) + y) / len(current)
            continue
        rows.append(sorted(current, key=lambda b: b.rect.x1))
        current = [block]
        current_y = float(y)

    if current:
        rows.append(sorted(current, key=lambda b: b.rect.x1))
    return rows


def normalize_text(text: str) -> str:
    """去掉空白、把全角斜杠统一成半角。"""
    return str_utils.remove_whitespace(text).replace('／', '/')


# ============================= 页面读取 =============================


def read_guide_agent_name(ctx: ZContext, screen: MatLike) -> str:
    """读养成方案详情页蓝色横幅上的代理人名字。读不到返回空字符串。"""
    area = guide_area(ctx, AREA_GUIDE_AGENT_NAME)
    if area is None:
        return ''
    block_list = agent_plan_page.read_text_blocks(ctx, screen, area)
    return agent_plan_page.pick_agent_name(block_list)


def read_guide_preview(ctx: ZContext, screen: MatLike) -> tuple[list[MaterialGapItem], list[str]]:
    """读养成方案详情页右侧的「材料预览」栏。

    Args:
        ctx: 一条龙上下文
        screen: 游戏截图

    Returns:
        (材料列表, 降级提示)。区域没配置时返回空列表并在提示里写清
    """
    area = guide_area(ctx, AREA_GUIDE_MATERIAL_PREVIEW)
    if area is None:
        return [], [f'{GUIDE_SCREEN}.{AREA_GUIDE_MATERIAL_PREVIEW} 区域没配置，读不了材料预览']
    block_list = agent_plan_page.read_text_blocks(ctx, screen, area)
    return parse_preview_lines(block_list)


def read_calc_detail(
    ctx: ZContext, screen: MatLike
) -> tuple[list[MaterialGapItem], NeedIconSummary, list[str]]:
    """读养成材料计算弹窗结果页的两块：四列明细表 + 需要获取的材料。

    Args:
        ctx: 一条龙上下文
        screen: 游戏截图

    Returns:
        (材料列表, 图标概况, 降级提示)
    """
    warnings: list[str] = []
    table_area = calc_area(ctx, AREA_CALC_DETAIL_TABLE)
    name_rect = calc_area(ctx, AREA_CALC_COL_NAME)
    required_rect = calc_area(ctx, AREA_CALC_COL_REQUIRED)
    owned_rect = calc_area(ctx, AREA_CALC_COL_OWNED)
    missing_rect = calc_area(ctx, AREA_CALC_COL_MISSING)
    icon_area = calc_area(ctx, AREA_CALC_NEED_ICONS)

    missing_area_name_list: list[str] = []
    for area_name, area in (
        (AREA_CALC_DETAIL_TABLE, table_area),
        (AREA_CALC_COL_NAME, name_rect),
        (AREA_CALC_COL_REQUIRED, required_rect),
        (AREA_CALC_COL_OWNED, owned_rect),
        (AREA_CALC_COL_MISSING, missing_rect),
    ):
        if area is None:
            missing_area_name_list.append(area_name)
    if missing_area_name_list:
        return [], NeedIconSummary(), [
            f'{CALC_SCREEN} 缺少区域配置：{"、".join(missing_area_name_list)}'
        ]

    item_list, table_warnings = parse_detail_table(
        agent_plan_page.read_text_blocks(ctx, screen, table_area),
        name_rect,
        required_rect,
        owned_rect,
        missing_rect,
    )
    warnings.extend(table_warnings)

    summary = NeedIconSummary()
    if icon_area is not None:
        summary = parse_need_icons(agent_plan_page.read_text_blocks(ctx, screen, icon_area))
    else:
        warnings.append(f'{CALC_SCREEN}.{AREA_CALC_NEED_ICONS} 区域没配置，跳过图标概况')
    return item_list, summary, warnings


def guide_area(ctx: ZContext, area_name: str) -> Rect | None:
    """取养成方案详情页的一个区域。没配置返回 None。"""
    return _area(ctx, GUIDE_SCREEN, area_name)


def agent_guide_area(ctx: ZContext, area_name: str) -> Rect | None:
    """取养成指南页的一个区域。没配置返回 None。"""
    return _area(ctx, AGENT_GUIDE_SCREEN, area_name)


def read_agent_guide_name(ctx: ZContext, screen: MatLike) -> str:
    """读养成指南页粉色横幅上的代理人名字。读不到返回空字符串。"""
    area = agent_guide_area(ctx, AREA_AGENT_GUIDE_AGENT_NAME)
    if area is None:
        return ''
    block_list = agent_plan_page.read_text_blocks(ctx, screen, area)
    return agent_plan_page.pick_agent_name(block_list)


def has_detail_table_header(ctx: ZContext, screen: MatLike) -> bool:
    """明细表表头（所需 / 已有 / 仍需获取）在不在这块画面里。

    只在「一行数据都没读到」时用来分辨是「结果页没出来」还是「结果页出来了但没数据」。

    Args:
        ctx: 一条龙上下文
        screen: 游戏截图

    Returns:
        看到表头就是 True；区域没配置也算 False
    """
    area = calc_area(ctx, AREA_CALC_DETAIL_TABLE)
    if area is None:
        return False
    block_list = agent_plan_page.read_text_blocks(ctx, screen, area)
    return any(is_table_header(row) for row in group_rows(block_list))


def calc_area(ctx: ZContext, area_name: str) -> Rect | None:
    """取养成材料计算弹窗的一个区域。没配置返回 None。"""
    return _area(ctx, CALC_SCREEN, area_name)


def _area(ctx: ZContext, screen_name: str, area_name: str) -> Rect | None:
    """从 screen_loader 取区域矩形。没配置返回 None（不抛异常）。"""
    area = ctx.screen_loader.get_area(screen_name, area_name)
    if area is None:
        return None
    return area.rect


# ============================= 操作 =============================


class ReadMaterialCalc(ZOperation):
    """读材料缺口。结果的 data 是 `MaterialGapSnapshot`。

    顺序：先读养成方案详情页右侧的「材料预览」栏（路径 A，最省事）；
    读不到时（且 `allow_dialog_fallback` 没关）再走
    「更多方案 → 养成指南 → 养成材料计算 → 确定 → 明细表」（路径 B，能拿到材料名）。

    导航链路的失败都是显式的：哪个画面没到、哪个按钮没找到，都写在 status 里。
    """

    def __init__(
        self,
        ctx: ZContext,
        goto_first: bool = True,
        allow_dialog_fallback: bool = True,
    ) -> None:
        """
        构造函数。

        Args:
            ctx: 一条龙上下文
            goto_first: 调用前没在特训目标页时，是否自己先走过去
            allow_dialog_fallback: 材料预览栏读不到时，要不要走「养成指南 → 养成材料计算」
                弹窗那条路。默认开；传 False 时读不到就直接失败，不多点几下
        """
        ZOperation.__init__(self, ctx, op_name='读取材料缺口')

        self.goto_first: bool = goto_first
        """没在特训目标页时是否自己导航过去"""

        self.allow_dialog_fallback: bool = allow_dialog_fallback
        """材料预览读不到时是否试弹窗路径"""

        self.agent_name: str = ''
        """代理人名字，读得到就带上"""

        self.snapshot: MaterialGapSnapshot = MaterialGapSnapshot()
        """正在拼的结果"""

    @operation_node(name='确认特训目标页', is_start_node=True, node_max_retry_times=3)
    def ensure_page(self) -> OperationRoundResult:
        """确认停在特训目标页。允许自己导航过去。"""
        if not self.goto_first and not agent_plan_page.is_agent_plan_page(
            self.ctx, self.last_screenshot
        ):
            # 外层说不用导航，那就直接试着往下走
            return self.round_success(status='跳过导航')
        if agent_plan_page.is_agent_plan_page(self.ctx, self.last_screenshot):
            return self.round_success(status='已在特训目标页')

        op = GotoAgentPlan(self.ctx)
        return self.round_by_op_result(op.execute(), retry_on_fail=True)

    @node_from(from_name='确认特训目标页')
    @operation_node(name='读角色名')
    def read_agent_name(self) -> OperationRoundResult:
        """读特训目标页顶部角色卡上的名字。没设特训目标时读不到，不算失败。"""
        self.agent_name = agent_plan_page.read_agent_name(self.ctx, self.last_screenshot)
        return self.round_success(status=f'当前特训目标 {self.agent_name or "（空）"}')

    @node_from(from_name='读角色名')
    @operation_node(name='点查看攻略', node_max_retry_times=5)
    def click_view_guide(self) -> OperationRoundResult:
        """点角色卡上的「查看攻略」进养成方案详情页。已经在详情页就不点。"""
        if self._is_guide_page():
            return self.round_success(status='已在养成方案详情页')

        area: Rect = screen_areas.get_area_rect(
            self.ctx, screen_areas.COMPENDIUM_SCREEN, screen_areas.AREA_VIEW_GUIDE
        )
        self.ctx.controller.click(area.center)
        return self.round_wait(status='点击查看攻略', wait=CLICK_WAIT)

    @node_from(from_name='点查看攻略')
    @operation_node(name='确认养成方案详情', node_max_retry_times=5)
    def confirm_guide_page(self) -> OperationRoundResult:
        """用「更多方案」按钮确认真的到了养成方案详情页。"""
        if self._is_guide_page():
            guide_name = read_guide_agent_name(self.ctx, self.last_screenshot)
            if guide_name:
                self.agent_name = guide_name
            return self.round_success(status=f'养成方案详情页（{self.agent_name or "角色名没读到"}）')
        return self.round_retry(status='没看到「更多方案」按钮，还不是养成方案详情页', wait=1)

    @node_from(from_name='确认养成方案详情')
    @operation_node(name='读材料预览')
    def read_preview(self) -> OperationRoundResult:
        """读右侧「材料预览」栏的已有/所需。"""
        agent_name = read_guide_agent_name(self.ctx, self.last_screenshot)
        if agent_name:
            self.agent_name = agent_name

        item_list, warnings = read_guide_preview(self.ctx, self.last_screenshot)
        log.info('材料预览栏读到 %d 条，注意 %d 条', len(item_list), len(warnings))

        if item_list:
            snapshot = MaterialGapSnapshot(
                source=SOURCE_GUIDE_PREVIEW,
                agent_name=self.agent_name,
                item_list=item_list,
                warnings=warnings,
            )
            if snapshot.unnamed_count > 0:
                # 这一栏本来就只有图标没有名字，写清楚，别让上层以为数据残缺
                snapshot.warnings.append(
                    f'「材料预览」栏只有图标、没有材料名，{snapshot.unnamed_count} 条材料名留空；'
                    f'缺口数字可用，但要按材料表换算次数得先补上名字（见模块说明）'
                )
            self.snapshot = snapshot
            return self.round_success(status=f'材料预览读到 {len(item_list)} 条', data=snapshot)

        if not self.allow_dialog_fallback:
            self.snapshot = MaterialGapSnapshot(
                source=SOURCE_GUIDE_PREVIEW,
                agent_name=self.agent_name,
                warnings=list(warnings),
            )
            return self.round_fail(
                status='材料预览栏没读到任何「已有/所需」，且 allow_dialog_fallback 关着，'
                '不走「养成指南 → 养成材料计算」那条路；'
                '这条路径要求页面停在养成方案-详情，且右侧「材料预览」栏有内容',
                data=self.snapshot,
            )

        # 注意：这里必须用 round_success 带状态，不能用 round_wait。
        # 框架里 WAIT 只是「本轮不算、重跑同一个节点」，不会沿边跳走，用了会卡死在这个节点
        return self.round_success(status=STATUS_PREVIEW_EMPTY)

    @node_from(from_name='读材料预览', status=STATUS_PREVIEW_EMPTY)
    @operation_node(name='进养成指南', node_max_retry_times=5)
    def goto_agent_guide(self) -> OperationRoundResult:
        """从养成方案详情页点「更多方案」，进养成指南。

        已经在养成指南页（或弹窗已经开着）就不重复点，直接把状态往下传。
        找不到「更多方案」按钮、或者压根不在详情页，最后一次重试后把原因写进 status 失败。
        """
        if self._is_agent_guide_page():
            return self.round_success(status=STATUS_GUIDE_READY)
        if self._is_dialog_open():
            return self.round_success(status=STATUS_DIALOG_OPEN)

        last_retry = self.node_retry_times >= self.node_max_retry_times - 1
        if not self._is_guide_page():
            if last_retry:
                return self.round_fail(
                    status='进不了养成指南：当前不在养成方案详情页，'
                    '没看到「更多方案」按钮（190,22-412,82）'
                )
            return self.round_retry(status='当前不在养成方案详情页，等一下再看', wait=1)

        if last_retry:
            return self.round_fail(
                status='养成方案详情页上没找到「更多方案」按钮（190,22-412,82），点不进养成指南'
            )
        result = self.round_by_find_and_click_area(
            self.last_screenshot,
            GUIDE_SCREEN,
            AREA_GUIDE_MORE_PLAN,
            success_wait=CLICK_WAIT,
            retry_wait=1,
        )
        if result.is_success:
            # 换成自己的状态：点按钮成功时框架给的 status 是区域名，对不上下游那条边，
            # 会对不上就当成「没有下一个节点」，于是带着空数据收工
            return self.round_success(status=STATUS_GUIDE_READY)
        return result

    @node_from(from_name='进养成指南', status=STATUS_GUIDE_READY)
    @operation_node(name='确认养成指南', node_max_retry_times=5)
    def confirm_agent_guide(self) -> OperationRoundResult:
        """用左上角标题「养成指南」确认真的进了这一页。"""
        if self._is_agent_guide_page():
            guide_name = read_agent_guide_name(self.ctx, self.last_screenshot)
            if guide_name:
                self.agent_name = guide_name
            return self.round_success(
                status=f'养成指南页（{self.agent_name or "角色名没读到"}）'
            )
        if self.node_retry_times >= self.node_max_retry_times - 1:
            return self.round_fail(
                status='点了「更多方案」但没进到养成指南：左上角标题「养成指南」没找到'
            )
        return self.round_retry(status='还没看到养成指南标题，等一下再看', wait=1)

    @node_from(from_name='进养成指南', status=STATUS_DIALOG_OPEN)
    @node_from(from_name='确认养成指南')
    @operation_node(name='打开材料计算', node_max_retry_times=5)
    def open_material_calc(self) -> OperationRoundResult:
        """点代理人名字正下方的「养成材料计算」，弹材料计算弹窗。

        弹窗已经开着就不点。找不到那个入口按钮时，最后一次重试把原因写进 status。
        """
        if self._is_dialog_open():
            return self.round_success(status=STATUS_DIALOG_OPEN)
        last_retry = self.node_retry_times >= self.node_max_retry_times - 1
        if not self._is_agent_guide_page():
            if last_retry:
                return self.round_fail(
                    status='当前不在养成指南页（没看到「养成指南」标题 160,20-400,85），'
                    '找不到材料计算入口'
                )
            return self.round_retry(status='当前不在养成指南页，等一下再看', wait=1)
        if last_retry:
            return self.round_fail(
                status='养成指南页上没找到「养成材料计算」入口（区域 820,175-1110,235）'
            )

        result = self.round_by_find_and_click_area(
            self.last_screenshot,
            AGENT_GUIDE_SCREEN,
            AREA_AGENT_GUIDE_CALC_BUTTON,
            success_wait=CLICK_WAIT,
            retry_wait=1,
        )
        if result.is_success:
            # 同上：换成自己的状态，才能走到「打开材料计算 --弹窗已开--> 确认弹窗已开」
            return self.round_success(status=STATUS_DIALOG_OPEN)
        return result

    @node_from(from_name='打开材料计算', status=STATUS_DIALOG_OPEN)
    @operation_node(name='确认弹窗已开', node_max_retry_times=5)
    def confirm_dialog(self) -> OperationRoundResult:
        """用弹窗标题「养成材料计算」确认弹窗真的弹出来了。"""
        if self._is_dialog_open():
            return self.round_success(status=STATUS_DIALOG_OPEN)
        if self.node_retry_times >= self.node_max_retry_times - 1:
            return self.round_fail(
                status='点了「养成材料计算」但弹窗没弹出来：标题「养成材料计算」没找到'
            )
        return self.round_retry(status='还没看到材料计算弹窗标题，等一下再看', wait=1)

    @node_from(from_name='确认弹窗已开', status=STATUS_DIALOG_OPEN)
    @operation_node(name='点确定出结果', node_max_retry_times=5)
    def confirm_calc(self) -> OperationRoundResult:
        """弹窗设置页点「确定」，同一弹窗内切到结果页。"""
        if self.node_retry_times >= self.node_max_retry_times - 1:
            return self.round_fail(
                status='材料计算弹窗里没找到「确定」按钮（区域 975,895-1390,980）'
            )
        return self.round_by_find_and_click_area(
            self.last_screenshot,
            CALC_SCREEN,
            AREA_CALC_CONFIRM,
            success_wait=CLICK_WAIT,
            retry_wait=1,
        )

    @node_from(from_name='点确定出结果')
    @operation_node(name='读明细表')
    def read_detail_table(self) -> OperationRoundResult:
        """读结果页的四列明细表 + 「需要获取的材料」概况。

        一行都没读到时再分一次：看到表头说明结果页出来了、只是没数据；
        连表头都没有说明「确定」没生效，结果页没出来。两种都明确失败，不交空数据。
        """
        item_list, summary, warnings = read_calc_detail(self.ctx, self.last_screenshot)
        self.snapshot = MaterialGapSnapshot(
            source=SOURCE_CALC_DETAIL,
            agent_name=self.agent_name,
            item_list=item_list,
            need_icon_summary=summary,
            warnings=list(warnings),
        )
        log.info(
            '明细表读到 %d 行，需要获取的材料 %d 种还缺、%d 种已满足',
            len(item_list),
            summary.gap_count,
            summary.satisfied_count,
        )
        if not item_list:
            if has_detail_table_header(self.ctx, self.last_screenshot):
                return self.round_fail(
                    status='明细表有表头但没有数据行：可能还没加载完，或表格需要往下滚',
                    data=self.snapshot,
                )
            return self.round_fail(
                status='点了「确定」但结果页没出来：明细表表头「所需 / 已有」没找到',
                data=self.snapshot,
            )
        return self.round_success(status=f'明细表读到 {len(item_list)} 行', data=self.snapshot)

    def _is_guide_page(self) -> bool:
        """现在是不是停在养成方案详情页（靠「更多方案」按钮认）。"""
        if guide_area(self.ctx, AREA_GUIDE_MORE_PLAN) is None:
            return False
        return self.round_by_find_area(
            self.last_screenshot, GUIDE_SCREEN, AREA_GUIDE_MORE_PLAN
        ).is_success

    def _is_agent_guide_page(self) -> bool:
        """现在是不是停在养成指南页（靠左上角标题认）。"""
        if agent_guide_area(self.ctx, AREA_AGENT_GUIDE_TITLE) is None:
            return False
        return self.round_by_find_area(
            self.last_screenshot, AGENT_GUIDE_SCREEN, AREA_AGENT_GUIDE_TITLE
        ).is_success

    def _is_dialog_open(self) -> bool:
        """养成材料计算弹窗现在是不是开着（靠标题认）。"""
        if calc_area(self.ctx, AREA_CALC_TITLE) is None:
            return False
        return self.round_by_find_area(
            self.last_screenshot, CALC_SCREEN, AREA_CALC_TITLE
        ).is_success


def __debug() -> None:
    from zzz_od.context.zzz_context import ZContext

    ctx = ZContext()
    ctx.init_by_config()
    ctx.init_ocr()
    ctx.run_context.start_running()
    op = ReadMaterialCalc(ctx)
    op_result = op.execute()
    log.info('读材料缺口结果 %s', op_result.status)
    snapshot: MaterialGapSnapshot | None = op_result.data
    if snapshot is not None:
        for line in snapshot.summary_lines():
            print(line)


if __name__ == '__main__':
    __debug()
