"""特训目标页的数据结构、OCR 解析规则和页面判定。

这个文件只放「不点屏幕」的东西：

- 数据结构：一条特训目标 `AgentPlanItem`、一屏结果 `AgentPlanSnapshot`、
  更换弹窗里的一格代理人 `DialogAgent`
- 把 OCR 文字块拼成上面这些结构的纯函数（方便单独测试，不需要游戏）
- 两个颜色判据：左侧分类「特训目标」是不是蓝色高亮、更换弹窗里哪一格是黄色选中框

真正点屏幕的动作在 `goto_agent_plan.py` / `read_agent_plan_list.py` /
`change_agent_plan_target.py` 里。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import numpy as np
from cv2.typing import MatLike

from one_dragon.base.geometry.point import Point
from one_dragon.base.geometry.rectangle import Rect
from one_dragon.utils import str_utils
from one_dragon.utils.log_utils import log
from zzz_od.game_data.agent import AgentEnum

from . import screen_areas

if TYPE_CHECKING:
    from numpy import ndarray

    from zzz_od.context.zzz_context import ZContext


# ============================= 解析规则里的常数 =============================

#: 分组标题（代理人 / 技能 / 驱动盘，灰小字）文字框左边缘，相对清单区左边缘的横坐标上限。
#: 实测分组标题缩进约 23px，条目名字缩进约 65px，取中间值 45 分开两者
GROUP_HEADER_MAX_X_OFFSET: int = 45

#: 电量文本（20-100 / 40 / 60）所在列，相对清单区左边缘的横坐标下限。实测在 967 左右
CHARGE_MIN_X_OFFSET: int = 900

#: 进度文本（129/290）所在列的横坐标范围。实测左边缘在 432~453
PROGRESS_X_OFFSET_RANGE: tuple[int, int] = (350, 800)

#: 「前往」按钮上的字
GO_BUTTON_TEXT: str = '前往'

#: 清单里出现过的分组标题。OCR 会把「技能」读成「按能」这类样子，靠这个列表纠正回来
GROUP_NAME_HINT_LIST: list[str] = ['代理人', '技能', '驱动盘']

#: 分组标题猜名字的相似度。只有三个候选，而且三者之间没有共同的汉字，所以放得比较松
GROUP_NAME_MATCH_CUTOFF: float = 0.45

#: 电量文本：20-100 这种区间
_CHARGE_RANGE_PATTERN = re.compile(r'^\d{1,3}[-~－～—]\d{1,3}$')
#: 电量文本：40 / 60 这种单个数
_CHARGE_SINGLE_PATTERN = re.compile(r'^\d{2,3}$')
#: 进度文本：129/290。斜杠可能被识别成各种样子
_PROGRESS_PATTERN = re.compile(r'^\d{1,4}[/／|I]\d{1,4}$')
#: 纯数字。进度那一条被裁掉一半时，斜杠可能被识别成数字（实测 153/203 变成 1537203）
_DIGIT_ONLY_PATTERN = re.compile(r'^\d{1,8}$')

#: 两条同名的特训目标合并时要求的相似度。
#: 名字一般 4~6 个字，OCR 错一个字相似度还有 0.67，所以不能卡太严，否则同一屏会重复记两条
MERGE_NAME_CUTOFF: float = 0.65

# ============================= 颜色判据里的常数 =============================

#: 左侧分类选中时的蓝色（BGR 三个通道的判断边界）。实测选中格约 70% 像素是蓝色，未选中约 0%
_HIGHLIGHT_BLUE_MIN: int = 170
_HIGHLIGHT_GREEN_MIN: int = 80
_HIGHLIGHT_GREEN_MAX: int = 170
_HIGHLIGHT_RED_MAX: int = 120
_HIGHLIGHT_BLUE_RED_GAP: int = 90
#: 蓝色像素占区域的比例达到这个值就算选中
HIGHLIGHT_BLUE_MIN_RATIO: float = 0.3

#: 弹窗里选中格的黄色描边（BGR 判断边界）。实测描边约 RGB(230, 215, 80)
_FRAME_RED_MIN: int = 180
_FRAME_GREEN_MIN: int = 160
_FRAME_BLUE_MAX: int = 120
_FRAME_RED_BLUE_GAP: int = 90

#: 找黄色描边时，把代理人网格区域四周多放开多少像素（描边画在格子外沿，会被网格区域切掉）
SELECTED_FRAME_PAD: int = 40
#: 描边竖边至少连续多少像素。实测约 200，格子里的稀有度角标（S）只有 30 左右
SELECTED_FRAME_MIN_VERTICAL_RUN: int = 80
#: 描边横边至少要占竖边之间宽度的多少比例。实测描边有圆角，160/185 ≈ 0.86
SELECTED_FRAME_HORIZONTAL_RUN_RATIO: float = 0.6
#: 选中格子至少多宽多高，用来挡掉误检
SELECTED_FRAME_MIN_WIDTH: int = 120
SELECTED_FRAME_MIN_HEIGHT: int = 150

# ============================= 更换弹窗的解析规则 =============================

#: 代理人名字框的最小高度。名字大约 26~35px 高，网格里的杂字（如「已过」）只有 13px
DIALOG_NAME_MIN_HEIGHT: int = 18
#: 点代理人时，从名字框中心往上挪多少像素（头像在名字上方）
DIALOG_NAME_CLICK_OFFSET: int = 80
#: 弹窗里找目标代理人时要求的相似度。名字 3 个字、OCR 错一个还有 0.67，所以卡 0.6；
#: 真点错了后面「确认切换结果」会拿页面上的角色名再核对一次
DIALOG_NAME_MATCH_CUTOFF: float = 0.6
#: 弹窗里的按钮文字，不是代理人名字
_DIALOG_NOT_AGENT_TEXT_LIST: list[str] = ['特训方案', '清除目标', '更换特训目标', '取消', '确认']
#: Lv.60 这种等级文字，要丢掉
_LEVEL_PATTERN = re.compile(r'^lv\.', re.IGNORECASE)


# ============================= 数据结构 =============================


@dataclass
class TextBlock:
    """一块 OCR 出来的文字和它在整张截图上的位置。"""

    text: str
    rect: Rect


@dataclass
class AgentPlanItem:
    """特训目标清单里的一条。"""

    #: 所属分组名：代理人 / 技能 / 驱动盘
    group_name: str
    #: 条目名，例如「调查员记录」「代理人技能」「猎人与忠犬」
    name: str
    #: 右侧电量文本，三种形态：20-100（实战模拟室，按张卡算）/ 40（专业挑战室）/ 60（区域巡防、恶名狩猎）
    charge_text: str = ''
    #: 进度文本，例如 129/290。驱动盘分组没有进度条，这里就是 None
    progress_text: str | None = None
    #: 进度里已完成的数。文本解析不出来时是 None
    progress_current: int | None = None
    #: 进度里要求的数。文本解析不出来时是 None
    progress_total: int | None = None
    #: 条目名字框的中心点，代表这一条在屏幕上的位置
    center: Point = field(default_factory=lambda: Point(0, 0))
    #: 「前往」按钮的中心点。这一条被裁掉一半、按钮没识别到时是 None
    go_button_center: Point | None = None
    #: 从「代理人技能」左侧芯片图标的颜色判出来的属性（电属性 / 物理属性 …）。
    #: 只有那一条会去取色，其他条目恒为 None；取不到或认不出也是 None
    icon_element: str | None = None

    @property
    def is_finished(self) -> bool:
        """进度是否已经满了。没有进度条的条目（驱动盘分组）一律算没满。"""
        if self.progress_current is None or self.progress_total is None:
            return False
        return self.progress_current >= self.progress_total


@dataclass
class AgentPlanSnapshot:
    """一整个特训目标页读下来的结果。"""

    #: 顶部角色卡上的代理人名字
    agent_name: str
    #: 清单里的全部条目，按阅读顺序（从上到下）
    item_list: list[AgentPlanItem] = field(default_factory=list)


@dataclass
class DialogAgent:
    """更换特训目标弹窗里的一格代理人。"""

    #: 代理人名字（OCR 文本，不查枚举，游戏里可能有项目里没有的角色）
    name: str
    #: 名字文字框
    name_rect: Rect

    def click_point(self) -> Point:
        """要点的位置：名字框中心往上挪一点，因为头像在名字上面。"""
        return Point(self.name_rect.center.x, self.name_rect.center.y - DIALOG_NAME_CLICK_OFFSET)


# ============================= OCR =============================


#: 白色文字的 BGR 范围（项目里 color_range 走 cv2.inRange，通道顺序是 BGR）。
#:
#: ⚠️ 实机对比过：拿它过滤代理人弹窗反而更差——颜色过滤会抹掉文字的抗锯齿边缘，
#: OCR 识别率明显下降（留档截图：不过滤读到 9 个全对，170~200 各档只读到 6~12 个且有错字）。
#: 保留这个常量是给「灰底浅色文字」这类场景备用，读代理人名字不要用它。
WHITE_TEXT_COLOR_RANGE: list[list[int]] = [[170, 170, 170], [255, 255, 255]]


def read_text_blocks(
    ctx: ZContext,
    screen: MatLike,
    area: Rect,
    color_range: list[list[int]] | None = None,
) -> list[TextBlock]:
    """把一个区域里的文字全部 OCR 出来，坐标换算成整张截图上的坐标。

    Args:
        ctx: 一条龙上下文
        screen: 游戏截图
        area: 要识别的区域

    Returns:
        文字块列表，未排序
    """
    if color_range is not None:
        # 先按颜色过滤再 OCR：只保留符合范围的像素，杂色文字直接不会进识别
        result_list = ctx.ocr_service.get_ocr_result_list(
            screen, color_range=color_range, rect=area, crop_first=True
        )
        block_list_with_color: list[TextBlock] = []
        for item in result_list:
            if not item.data:
                continue
            rect = item.rect
            # crop_first=True 时识别的是裁剪图，坐标要加回区域左上角
            rect.add_offset(area.left_top)
            block_list_with_color.append(TextBlock(text=item.data, rect=rect))
        return block_list_with_color

    result_map = ctx.ocr.crop_and_run_ocr(screen, area)
    block_list: list[TextBlock] = []
    for text, match_result_list in result_map.items():
        for match_result in match_result_list:
            rect = match_result.rect
            rect.add_offset(area.left_top)
            block_list.append(TextBlock(text=text, rect=rect))
    return block_list


def read_agent_name(ctx: ZContext, screen: MatLike) -> str:
    """读顶部角色卡上的代理人名字。读不到返回空字符串。

    Args:
        ctx: 一条龙上下文
        screen: 游戏截图

    Returns:
        代理人名字，读不到就是空字符串
    """
    area = screen_areas.get_area_rect(
        ctx, screen_areas.COMPENDIUM_SCREEN, screen_areas.AREA_AGENT_NAME
    )
    block_list = read_text_blocks(ctx, screen, area)
    return pick_agent_name(block_list)


def pick_agent_name(block_list: list[TextBlock]) -> str:
    """从角色名区域的文字块里挑出名字：取含中文且最长的那块。

    Args:
        block_list: 角色名区域的文字块

    Returns:
        代理人名字，挑不到就是空字符串
    """
    name: str = ''
    for block in block_list:
        text = _normalize_text(block.text)
        if not str_utils.with_chinese(text):
            continue
        if len(text) > len(name):
            name = text
    return name


# ============================= 清单解析 =============================


def parse_plan_items(block_list: list[TextBlock], area: Rect) -> list[AgentPlanItem]:
    """把清单区的 OCR 文字块拼成条目列表。

    拼法：按 y 从上往下走一遍，遇到名字就开一条新条目，后面的电量、进度、前往按钮
    都算这一条的。滚到一半被裁掉的行只有零散数字、没有名字，会被自动丢掉。

    Args:
        block_list: 清单区的文字块（整张截图的坐标）
        area: 清单区矩形，用来算文字框相对左边缩进了多少

    Returns:
        条目列表，按阅读顺序
    """
    item_list: list[AgentPlanItem] = []
    group_name: str = ''
    current: AgentPlanItem | None = None

    for block in sorted(block_list, key=lambda b: (b.rect.center.y, b.rect.x1)):
        kind = classify_text_block(block, area)
        if kind == 'group':
            group_name = normalize_group_name(block.text)
            current = None
        elif kind == 'name':
            current = AgentPlanItem(
                group_name=group_name,
                name=_normalize_text(block.text),
                center=block.rect.center,
            )
            item_list.append(current)
        elif kind == 'charge' and current is not None:
            current.charge_text = _normalize_text(block.text)
        elif kind == 'progress' and current is not None:
            current.progress_text = _normalize_text(block.text)
            done, total = parse_progress_text(current.progress_text)
            current.progress_current = done
            current.progress_total = total
        elif kind == 'go' and current is not None:
            current.go_button_center = block.rect.center

    return [item for item in item_list if _is_real_plan_item(item)]


def _is_real_plan_item(item: AgentPlanItem) -> bool:
    """是不是一条真条目。

    特训目标页的每一条都带电量（`20-100` / `40` / `60`），所以「名字很短 + 没读到电量」
    基本是界面上的滚动箭头、图标、装饰文字被 OCR 当成了条目名（实测出现过一条叫 `A` 的假条目）。
    只在这种两个条件同时成立时才丢，电量单独读失败的真条目还能留下。

    Args:
        item: 解析出来的候选条目

    Returns:
        True 表示像真条目
    """
    if item.charge_text:
        return True
    return len(item.name.strip()) >= 3


def fill_icon_element(screen: ndarray, item_list: list[AgentPlanItem]) -> None:
    """给「按属性分副本」的条目补上图标属性。

    只有「代理人技能」这一条需要：它的图标颜色代表属性。其他条目左边的图标是材料图标，
    颜色代表稀有度，取色会误判（实测把经验书判成了火属性）。

    Args:
        screen: 整屏截图（BGR）
        item_list: 解析出来的条目，原地写 `icon_element`
    """
    from . import chip_element

    for item in item_list:
        if not chip_element.is_element_entry(item.name):
            continue
        element, detail = chip_element.read_chip_element(
            screen, (item.center.x, item.center.y)
        )
        if detail:
            log.info('芯片图标取色：%s -> %s（%s）', item.name, element, detail)
        item.icon_element = element


def classify_text_block(block: TextBlock, area: Rect) -> str:
    """判断一块文字属于清单里的哪种东西。

    Args:
        block: 文字块
        area: 清单区矩形

    Returns:
        group（分组标题）/ name（条目名）/ charge（电量）/ progress（进度）/ go（前往按钮）/ other（不认识）
    """
    text = _normalize_text(block.text)
    if not text:
        return 'other'

    if str_utils.find_by_lcs(GO_BUTTON_TEXT, text, percent=0.5) and len(text) <= 4:
        return 'go'

    x_offset = block.rect.x1 - area.x1

    if _CHARGE_RANGE_PATTERN.match(text) and x_offset >= CHARGE_MIN_X_OFFSET:
        return 'charge'
    if _CHARGE_SINGLE_PATTERN.match(text) and x_offset >= CHARGE_MIN_X_OFFSET:
        return 'charge'

    in_progress_column = (
        PROGRESS_X_OFFSET_RANGE[0] <= x_offset <= PROGRESS_X_OFFSET_RANGE[1]
    )
    if in_progress_column and _PROGRESS_PATTERN.match(text):
        return 'progress'
    if in_progress_column and _DIGIT_ONLY_PATTERN.match(text):
        return 'progress'

    if x_offset < GROUP_HEADER_MAX_X_OFFSET:
        return 'group'
    if text in GROUP_NAME_HINT_LIST:
        return 'group'

    if str_utils.with_chinese(text):
        return 'name'
    if _DIGIT_ONLY_PATTERN.match(text):
        return 'other'
    return 'name'


def normalize_group_name(text: str) -> str:
    """把分组标题的 OCR 文本对到已知的三个分组名上。

    OCR 常把「技能」读成「按能」这类样子，直接拿去用的话后面没法映射成副本。
    对不上三个已知分组就原样返回，让上层按未知分组处理。

    Args:
        text: OCR 出来的分组标题

    Returns:
        纠正后的分组名
    """
    name = _normalize_text(text)
    if not name:
        return ''
    idx = str_utils.find_best_match_by_difflib(
        name, GROUP_NAME_HINT_LIST, cutoff=GROUP_NAME_MATCH_CUTOFF
    )
    if idx is None:
        return name
    return GROUP_NAME_HINT_LIST[idx]


def parse_progress_text(text: str | None) -> tuple[int | None, int | None]:
    """把 129/290 这样的进度文本拆成两个数。拆不出来返回 (None, None)。

    Args:
        text: 进度文本

    Returns:
        (已完成, 要求总数)
    """
    if text is None:
        return None, None
    normalized = _normalize_text(text)
    match = _PROGRESS_PATTERN.match(normalized)
    if match is None:
        return None, None
    parts = re.split(r'[/／|I]', normalized)
    if len(parts) != 2:
        return None, None
    try:
        return int(parts[0]), int(parts[1])
    except ValueError:
        return None, None


def merge_item_list(
    old_list: list[AgentPlanItem], new_list: list[AgentPlanItem]
) -> tuple[list[AgentPlanItem], int]:
    """把新读到的一屏合并进已经收到的总表里。

    同名条目不重复添加，只把这次多读到的信息（电量、进度、按钮位置）补上去。

    Args:
        old_list: 之前累计的条目
        new_list: 这一屏读到的条目

    Returns:
        (合并后的条目列表, 这次新增了几条)。新增 0 条说明已经滚到底了
    """
    merged: list[AgentPlanItem] = list(old_list)
    name_list: list[str] = [item.name for item in merged]
    new_count: int = 0

    for item in new_list:
        idx = str_utils.find_in_list_with_fuzzy(item.name, name_list, cutoff=MERGE_NAME_CUTOFF)
        if idx is None:
            merged.append(item)
            name_list.append(item.name)
            new_count += 1
            continue
        _fill_item(merged[idx], item)

    return merged, new_count


def _fill_item(target: AgentPlanItem, source: AgentPlanItem) -> None:
    """把 source 里有、target 里没有的信息补到 target 上。

    Args:
        target: 已经存在的条目
        source: 这一屏重新读到的同一个条目
    """
    if not target.charge_text and source.charge_text:
        target.charge_text = source.charge_text
    if target.progress_current is None and source.progress_current is not None:
        target.progress_current = source.progress_current
        target.progress_total = source.progress_total
        target.progress_text = source.progress_text
    elif target.progress_text is None and source.progress_text is not None:
        target.progress_text = source.progress_text
    if not target.group_name and source.group_name:
        target.group_name = source.group_name
    target.center = source.center
    if source.go_button_center is not None:
        target.go_button_center = source.go_button_center


# ============================= 更换弹窗解析 =============================


#: 代理人弹窗网格的列数与行数（实测 6 列 2 行，一屏 12 格）
DIALOG_GRID_COLUMNS: int = 6
DIALOG_GRID_ROWS: int = 2

#: 项目代理人表里的全部名字，用来校验补读到的东西靠不靠谱
_KNOWN_AGENT_NAMES: frozenset[str] = frozenset(
    member.value.agent_name for member in AgentEnum
)


def dialog_grid_cell(block: TextBlock, area: Rect) -> tuple[int, int] | None:
    """文本块落在第几行第几列的格子里。

    Args:
        block: 文本块
        area: 代理人网格区域

    Returns:
        (行, 列) 从 0 开始；落在网格外返回 None
    """
    if area.width <= 0 or area.height <= 0:
        return None

    cx, cy = block.rect.center.x, block.rect.center.y
    offset_x = cx - area.x1
    offset_y = cy - area.y1
    if offset_x < 0 or offset_y < 0 or offset_x >= area.width or offset_y >= area.height:
        return None

    cell_w = area.width / DIALOG_GRID_COLUMNS
    cell_h = area.height / DIALOG_GRID_ROWS
    return int(offset_y // cell_h), int(offset_x // cell_w)


def parse_dialog_agents_by_grid(
    block_list: list[TextBlock], area: Rect
) -> list[DialogAgent]:
    """按 6x2 格子解析代理人名字，一格最多出一个。

    这样即使 OCR 把某个名字切成两半、或者把格子外面的装饰文字也读了出来，
    也不会多出一个不存在的角色：碎片会被归到同一格，装饰文字归不进任何格子。

    Args:
        block_list: 网格区的文本块
        area: 代理人网格区域

    Returns:
        代理人列表，按「先左右后上下」的顺序
    """
    cell_map: dict[tuple[int, int], list[TextBlock]] = {}
    for block in block_list:
        cell = dialog_grid_cell(block, area)
        if cell is None:
            continue
        cell_map.setdefault(cell, []).append(block)

    agent_list: list[DialogAgent] = []
    for cell in sorted(cell_map.keys()):
        candidates = [block for block in cell_map[cell] if is_dialog_agent_name(block)]
        if not candidates:
            continue
        best = _pick_cell_name(candidates)
        agent_list.append(
            DialogAgent(name=_normalize_text(best.text), name_rect=best.rect)
        )
    return agent_list


def _pick_cell_name(candidates: list[TextBlock]) -> TextBlock:
    """从同一格的多段文字里挑一个当名字。

    实测同一格会读到「好 | 雅 | Lv.60」这种：弹窗背景上有「特训方案 / 清除目标」等
    半透明大字，被压暗后 OCR 会读出碎片（日、好、A、办、EXN…），和真名字落在同一格。

    挑选顺序：
    1. 项目代理人表里有的（真名字一定能对上）
    2. 表里都没有时按文字高度，完整名字比碎片高
    3. 一样高就按长度（多字名字比单字碎片可信）

    不在这里做模糊匹配：新角色本来就不在表里，硬配到老角色上会把人认错。

    Args:
        candidates: 同一格里被认为是名字的文本块

    Returns:
        挑中的那个
    """
    in_table = [
        block for block in candidates
        if _normalize_text(block.text).strip('「」') in _KNOWN_AGENT_NAMES
    ]
    pool = in_table if in_table else candidates
    return max(pool, key=lambda block: (block.rect.height, len(_normalize_text(block.text))))


def parse_dialog_agents(block_list: list[TextBlock]) -> list[DialogAgent]:
    """把更换弹窗里代理人网格的 OCR 文字块拼成代理人列表。

    丢弃规则：Lv.60 这种等级文字、按钮文字、以及方格里去不掉的角标杂字（高度太矮）。

    Args:
        block_list: 网格区域的文字块

    Returns:
        代理人列表，按阅读顺序
    """
    agent_list: list[DialogAgent] = []
    for block in sorted(block_list, key=lambda b: (b.rect.center.y, b.rect.x1)):
        if not is_dialog_agent_name(block):
            continue
        agent_list.append(DialogAgent(name=_normalize_text(block.text), name_rect=block.rect))
    return agent_list


def is_dialog_agent_name(block: TextBlock) -> bool:
    """这块文字是不是一个代理人名字。

    Args:
        block: 文字块

    Returns:
        是代理人名字就是 True
    """
    text = _normalize_text(block.text)
    if not text:
        return False
    if _LEVEL_PATTERN.match(text):
        return False
    if text in _DIALOG_NOT_AGENT_TEXT_LIST:
        return False
    if block.rect.height < DIALOG_NAME_MIN_HEIGHT:
        return False
    if _DIGIT_ONLY_PATTERN.match(text):
        return False
    return str_utils.with_chinese(text)


#: 放大重识别时，名字框向外扩的像素（把被裁掉的笔画含进来）
ZOOM_NAME_PADDING: int = 6
#: 放大倍率。名字本身只有 20~30px 高，放大 2 倍识别率明显更好
ZOOM_NAME_SCALE: float = 2.0


def refine_dialog_name(ctx: ZContext, screen: MatLike, agent: DialogAgent) -> str:
    """把名字框放大后重新识别一次，用于纠正单字错字。

    实测「雅」在第二行被底部滚动条挡掉下半截时会读成「好」，而「好」不是任何角色。
    放大重识别能救回来一部分。

    Args:
        ctx: 一条龙上下文
        screen: 整屏截图
        agent: 网格解析出来的代理人（带名字框）

    Returns:
        重新识别到的名字；识别不出来就返回原名字
    """
    import cv2

    rect = agent.name_rect
    x1 = max(0, int(rect.x1) - ZOOM_NAME_PADDING)
    y1 = max(0, int(rect.y1) - ZOOM_NAME_PADDING)
    x2 = int(rect.x2) + ZOOM_NAME_PADDING
    y2 = int(rect.y2) + ZOOM_NAME_PADDING
    if x2 <= x1 or y2 <= y1:
        return agent.name

    crop = screen[y1:y2, x1:x2]
    if crop.size == 0:
        return agent.name

    zoomed = cv2.resize(
        crop, None, fx=ZOOM_NAME_SCALE, fy=ZOOM_NAME_SCALE,
        interpolation=cv2.INTER_CUBIC,
    )
    text = _normalize_text(ctx.ocr.run_ocr_single_line(zoomed))
    if not text:
        return agent.name
    if _LEVEL_PATTERN.match(text) or _DIGIT_ONLY_PATTERN.match(text):
        return agent.name
    return text


#: 补读空格子时，名字区域在格子里的相对位置（左、上、右、下，0~1）。
#: 实测格子布局：头像占上半，名字在格子的 61%~72% 处（下面还有一行 Lv.60），左右各留 8% 边
CELL_NAME_RATIO: tuple[float, float, float, float] = (0.08, 0.56, 0.92, 0.76)
#: 补读时的放大倍率。格子里的字本来就小，放 3 倍再识别
MISSING_NAME_ZOOM: float = 3.0


def read_missing_cell_names(
    ctx: ZContext,
    screen: MatLike,
    area: Rect,
    block_list: list[TextBlock] | None = None,
) -> list[DialogAgent]:
    """把网格里「没读到名字」的格子单独裁出来放大重识别。

    整屏 OCR 偶尔会漏掉某些格子（实测「柳」那一格只检测到 `Lv.60`，名字整个没出现）。
    格子布局是固定的 6x2，所以可以直接按位置裁出名字区域重识别，补回漏掉的。

    Args:
        ctx: 一条龙上下文
        screen: 整屏截图
        area: 代理人网格区域
        block_list: 已经识别过的文本块；不传就自己再识别一次

    Returns:
        补读到的代理人列表（只包含原本没读到的格子）
    """
    import cv2

    if area.width <= 0 or area.height <= 0:
        return []

    # 先看哪些格子已经有名字了（调用方已经识别过就直接用，省一次 OCR）
    if block_list is None:
        block_list = read_text_blocks(ctx, screen, area)
    filled_cells = {
        dialog_grid_cell(block, area) for block in block_list
        if is_dialog_agent_name(block)
    }

    cell_w = area.width / DIALOG_GRID_COLUMNS
    cell_h = area.height / DIALOG_GRID_ROWS
    found: list[DialogAgent] = []

    for row in range(DIALOG_GRID_ROWS):
        for col in range(DIALOG_GRID_COLUMNS):
            if (row, col) in filled_cells:
                continue

            cell_x = area.x1 + col * cell_w
            cell_y = area.y1 + row * cell_h
            left_ratio, top_ratio, right_ratio, bottom_ratio = CELL_NAME_RATIO
            x1 = int(cell_x + cell_w * left_ratio)
            y1 = int(cell_y + cell_h * top_ratio)
            x2 = int(cell_x + cell_w * right_ratio)
            y2 = int(cell_y + cell_h * bottom_ratio)
            if x2 <= x1 or y2 <= y1:
                continue

            crop = screen[y1:y2, x1:x2]
            if crop.size == 0:
                continue
            zoomed = cv2.resize(
                crop, None, fx=MISSING_NAME_ZOOM, fy=MISSING_NAME_ZOOM,
                interpolation=cv2.INTER_CUBIC,
            )
            text = _normalize_text(ctx.ocr.run_ocr_single_line(zoomed))
            if not text or _LEVEL_PATTERN.match(text) or _DIGIT_ONLY_PATTERN.match(text):
                continue
            if not str_utils.with_chinese(text):
                continue
            # 只收表里有的名字：被遮挡的格子会读出「方表」「清」这类碎片，宁可漏也别错
            if text.strip('「」') not in _KNOWN_AGENT_NAMES:
                log.info('格子 (行%d, 列%d) 补读到「%s」，不在代理人表里，丢弃', row, col, text)
                continue

            found.append(
                DialogAgent(
                    name=text,
                    name_rect=Rect(x1, y1, x2, y2),
                )
            )
            log.info('格子 (行%d, 列%d) 整屏没读到名字，补读得到「%s」', row, col, text)

    return found


#: 名字区域相对 Lv. 文本的位置：同宽、左右各扩 6px、往上 34px
LEVEL_NAME_PAD_X: int = 6
LEVEL_NAME_ABOVE: int = 34
#: 按 Lv. 反推裁出来的名字要放大多少倍再识别
LEVEL_NAME_ZOOM: float = 3.0


def read_names_by_level_anchor(
    ctx: ZContext, screen: MatLike, block_list: list[TextBlock],
) -> list[DialogAgent]:
    """用「Lv.60」这类文本反推名字位置，再单独裁出来识别。

    ## 为什么这么做

    格子里的名字只有 20~30px 高，整屏 OCR 直接读容易出问题：
    - 名字被切成一半（「照」读成「昭」）
    - 整个名字没检测到（「柳」那格只读到 Lv.60）
    - 背景上「特训方案 / 清除目标」这些半透明大字被压暗后读出碎片，
      和真名字落在同一格（「雅」旁边多个「好」）

    但每格的 `Lv.XX` 都能稳定读到。名字就在它正上方 34px 左右、宽度基本对齐，
    按这个位置裁出来放大 3 倍单独识别，实测一屏 12 个全对。

    Args:
        ctx: 一条龙上下文
        screen: 整屏截图
        block_list: 网格区的文本块（用来找 Lv.）

    Returns:
        代理人列表，按 Lv. 的阅读顺序
    """
    import cv2

    agent_list: list[DialogAgent] = []
    seen: set[str] = set()
    for block in sorted(block_list, key=lambda item: (item.rect.center.y, item.rect.x1)):
        if not _LEVEL_PATTERN.match(_normalize_text(block.text)):
            continue

        level_rect = block.rect
        x1 = max(0, int(level_rect.x1) - LEVEL_NAME_PAD_X)
        y1 = max(0, int(level_rect.y1) - LEVEL_NAME_ABOVE)
        x2 = int(level_rect.x2) + LEVEL_NAME_PAD_X
        y2 = int(level_rect.y1) + 1
        if x2 <= x1 or y2 <= y1:
            continue

        crop = screen[y1:y2, x1:x2]
        if crop.size == 0:
            continue
        zoomed = cv2.resize(
            crop, None, fx=LEVEL_NAME_ZOOM, fy=LEVEL_NAME_ZOOM,
            interpolation=cv2.INTER_CUBIC,
        )
        text = _normalize_text(ctx.ocr.run_ocr_single_line(zoomed))
        if not text or _LEVEL_PATTERN.match(text) or _DIGIT_ONLY_PATTERN.match(text):
            continue
        if text in seen:
            continue
        seen.add(text)
        agent_list.append(DialogAgent(name=text, name_rect=Rect(x1, y1, x2, y2)))

    return agent_list


def find_dialog_agent(block_list: list[TextBlock],
                      target_name: str) -> DialogAgent | None:
    """在网格里找目标代理人。

    Args:
        block_list: 网格区域的文字块
        target_name: 要找的代理人名字

    Returns:
        找到的代理人，找不到就是 None
    """
    agent_list = parse_dialog_agents(block_list)
    name_list = [agent.name for agent in agent_list]
    idx = str_utils.find_in_list_with_fuzzy(
        target_name, name_list, cutoff=DIALOG_NAME_MATCH_CUTOFF
    )
    if idx is None:
        return None
    return agent_list[idx]


def agent_name_similar(current: str, target: str) -> bool:
    """两个代理人名字是不是同一个人。

    按 OCR 文本比，不查项目里的角色枚举（游戏可能有枚举里没有的新角色）。
    目标名常常是简称（比如只写「安东」），所以包含关系也算命中。

    Args:
        current: 画面上读到的名字
        target: 想要的名字

    Returns:
        认为是同一个人就是 True
    """
    a = _normalize_agent_name(current)
    b = _normalize_agent_name(target)
    if not a or not b:
        return False
    if a == b or a in b or b in a:
        return True
    return str_utils.find_best_match_by_difflib(a, [b], cutoff=0.6) is not None


# ============================= 颜色判据 =============================


def is_blue_highlight(screen: MatLike, rect: Rect) -> bool:
    """这个区域是不是被蓝色高亮（左侧分类选中时的样子）。

    Args:
        screen: 游戏截图（BGR）
        rect: 区域

    Returns:
        蓝色像素够多就是 True
    """
    part = screen[rect.y1:rect.y2, rect.x1:rect.x2]
    if part.size == 0:
        return False
    blue = part[:, :, 0].astype(int)
    green = part[:, :, 1].astype(int)
    red = part[:, :, 2].astype(int)
    mask = (
        (blue > _HIGHLIGHT_BLUE_MIN)
        & (green > _HIGHLIGHT_GREEN_MIN)
        & (green < _HIGHLIGHT_GREEN_MAX)
        & (red < _HIGHLIGHT_RED_MAX)
        & ((blue - red) > _HIGHLIGHT_BLUE_RED_GAP)
    )
    return float(mask.mean()) >= HIGHLIGHT_BLUE_MIN_RATIO


def find_selected_cell_rect(screen: MatLike, area: Rect) -> Rect | None:
    """在代理人网格里找黄色选中框，返回被框住的那一格的位置。

    做法：先按颜色挑出黄色像素，再找长得像描边的竖边和横边——
    竖边要连续 80px 以上（稀有度角标只有 30 左右，会被排除掉）。

    Args:
        screen: 游戏截图（BGR）
        area: 代理人网格区域

    Returns:
        选中格子的矩形，判断不出来就是 None
    """
    height, width = screen.shape[0], screen.shape[1]
    left = max(0, area.x1 - SELECTED_FRAME_PAD)
    top = max(0, area.y1 - SELECTED_FRAME_PAD)
    right = min(width, area.x2 + SELECTED_FRAME_PAD)
    bottom = min(height, area.y2 + SELECTED_FRAME_PAD)

    part = screen[top:bottom, left:right]
    if part.size == 0:
        return None

    red = part[:, :, 2].astype(int)
    green = part[:, :, 1].astype(int)
    blue = part[:, :, 0].astype(int)
    mask = (
        (red > _FRAME_RED_MIN)
        & (green > _FRAME_GREEN_MIN)
        & (blue < _FRAME_BLUE_MAX)
        & ((red - blue) > _FRAME_RED_BLUE_GAP)
    )

    col_list = np.nonzero(
        _has_run(mask, SELECTED_FRAME_MIN_VERTICAL_RUN, axis=0)
    )[0]
    if len(col_list) < 2:
        return None
    x_lo = int(col_list.min())
    x_hi = int(col_list.max())
    cell_width = x_hi - x_lo
    if cell_width < SELECTED_FRAME_MIN_WIDTH:
        return None

    need_run = max(1, int(cell_width * SELECTED_FRAME_HORIZONTAL_RUN_RATIO))
    row_list = np.nonzero(
        _has_run(mask[:, x_lo:x_hi + 1], need_run, axis=1)
    )[0]
    if len(row_list) < 2:
        return None
    y_lo = int(row_list.min())
    y_hi = int(row_list.max())
    if (y_hi - y_lo) < SELECTED_FRAME_MIN_HEIGHT:
        return None

    return Rect(left + x_lo, top + y_lo, left + x_hi, top + y_hi)


def _has_run(mask: np.ndarray, run_length: int, axis: int) -> np.ndarray:
    """每一行（或每一列）里有没有一段长度达到 run_length 的连续 True。

    Args:
        mask: 布尔矩阵
        run_length: 要求的最短连续长度
        axis: 0 表示沿着列方向数（判断每一列），1 表示沿着行方向数（判断每一行）

    Returns:
        每个列（或行）一个布尔值
    """
    work = mask.astype(np.int32)
    if axis == 0:
        work = work.T
    if work.shape[1] < run_length:
        return np.zeros(work.shape[0], dtype=bool)
    zero_row = np.zeros((work.shape[0], 1), dtype=np.int32)
    cum = np.concatenate([zero_row, np.cumsum(work, axis=1)], axis=1)
    hit = (cum[:, run_length:] - cum[:, :-run_length]) == run_length
    return hit.any(axis=1)


def is_agent_plan_page(ctx: ZContext, screen: MatLike) -> bool:
    """现在是不是停在「快捷手册-训练-特训目标」页。

    主判据：左侧分类「特训目标」是蓝色高亮（选中态）。
    兜底判据：角色卡上的「更换特训目标」按钮文字，这个按钮只有这一页有。

    不拿「清单区有内容」当判据——养成方案详情页的同一块位置也有字，会误判成特训目标页。
    没设特训目标时页面是空态的，这时只能靠左侧分类的高亮认出来。

    Args:
        ctx: 一条龙上下文
        screen: 游戏截图

    Returns:
        在特训目标页就是 True
    """
    cat_rect = screen_areas.get_area_rect(
        ctx, screen_areas.COMPENDIUM_SCREEN, screen_areas.AREA_LEFT_CAT_TARGET
    )
    if is_blue_highlight(screen, cat_rect):
        return True

    button_rect = screen_areas.get_area_rect(
        ctx, screen_areas.COMPENDIUM_SCREEN, screen_areas.AREA_CHANGE_TARGET
    )
    return has_text(ctx, screen, button_rect, '更换特训目标')


def has_text(ctx: ZContext, screen: MatLike, area: Rect, target: str,
             lcs_percent: float = 0.6) -> bool:
    """区域内有没有出现某段文字（按最长公共子序列比，容忍 OCR 少量错字）。

    Args:
        ctx: 一条龙上下文
        screen: 游戏截图
        area: 区域
        target: 想找的文字
        lcs_percent: 最短公共子序列要占目标文字长度的比例

    Returns:
        找到就是 True
    """
    for block in read_text_blocks(ctx, screen, area):
        if str_utils.find_by_lcs(target, block.text, percent=lcs_percent):
            return True
    return False


# ============================= 小工具 =============================


def _normalize_text(text: str) -> str:
    """去掉空白字符。OCR 出来的 129 / 290 想当 129/290 用。"""
    return str_utils.remove_whitespace(text)


def _normalize_agent_name(name: str) -> str:
    """代理人名字比较前先去掉空白和间隔号，避免「安东·伊万诺夫」和「安东伊万诺夫」比不上。"""
    text = _normalize_text(name)
    for ch in ('·', '・', '•'):
        text = text.replace(ch, '')
    return text
