"""扫描「更换特训目标」弹窗里的全部代理人名字。

## 为什么要这个

游戏里同时只能设一个特训目标，想批量养一批代理人就得在这个弹窗里反复切换。
批量配置的第一步是「知道有哪些代理人可选」，所以本文件只干一件事：
把弹窗每一屏的代理人名字读出来攒成完整名单，然后**原样关掉弹窗**。
全程不点任何代理人格子，也不动当前的特训目标。

## 流程（节点顺序）

    确认特训目标页 → 打开更换弹窗 → 确认弹窗已开 → 回到网格顶部
    → 读名字（读一屏 → 往下滚一屏 → 再读，直到连续两屏没有新名字）→ 关掉弹窗

- 读到哪里算完，看的是**名字集合**，不看坐标：滚完之后这一屏的名字全都见过，就算这屏没贡献。
- 「没有新名字」要连续两次才收工。只等一次就收，遇到拖拽没生效（惯性、按到边界）会静默漏人。
- 弹窗关不掉只往 `warnings` 里记一笔，不算这次操作失败——名单已经拿到了。

## 复用了什么

名字解析一律走 `agent_plan_page`，这里不重写 OCR 规则：

- 整块 OCR：`agent_plan_page.read_text_blocks`
- 丢掉 `Lv.60`、稀有度角标 `S`、按钮文字、高度不够的碎字：
  `agent_plan_page.parse_dialog_agents`（内部用 `is_dialog_agent_name` + `DIALOG_NAME_MIN_HEIGHT`）
- 画面名和区域名直接用 `change_agent_plan_target` 里那几个常量，免得两处各写一份字符串

## 未实机验证的点（照坐标写好的，需要跑一遍游戏确认）

- **网格往下滚一屏**：拖拽起止点按「区域-代理人网格」上下边缘内缩 `GRID_SCROLL_MARGIN` 写的，
  和 `change_agent_plan_target` 里找目标时的滚动是同一套写法。滚动距离够不够一屏、
  会不会因为惯性多滚或滚不动，**没有实测**。
- **回到网格顶部**：弹窗刚打开一般在顶部，这一步只是兜底（上一次流程可能把弹窗留在滚到中间的状态）。
- **关掉弹窗后是不是真回到特训目标页**：只核对了「更换特训目标」标题消失，没有核对页面上的其它元素。
- 网格里能看见 6 列 × 2 行（12 格），游戏里代理人比一屏多，所以「读第二屏」整段依赖上面的滚动，
  这一屏的名单是否完整**要实机对一遍**。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from one_dragon.base.geometry.rectangle import Rect
from one_dragon.base.operation.operation_edge import node_from
from one_dragon.base.operation.operation_node import operation_node
from one_dragon.base.operation.operation_round_result import OperationRoundResult
from one_dragon.utils import str_utils
from one_dragon.utils.log_utils import log
from zzz_od.game_data.agent import AgentEnum
from zzz_od.operation.zzz_operation import ZOperation

from . import agent_plan_page, screen_areas
from .change_agent_plan_target import (
    AREA_AGENT_GRID,
    AREA_CLOSE_BUTTON,
    AREA_DIALOG_TITLE,
    CHANGE_TARGET_SCREEN,
)
from .goto_agent_plan import GotoAgentPlan

if TYPE_CHECKING:
    from cv2.typing import MatLike

    from zzz_od.context.zzz_context import ZContext


#: 在网格里滚动时，离网格上下边缘留多少像素（别按到边缘外面去）
GRID_SCROLL_MARGIN: int = 60
#: 「回到网格顶部」最多往上拖几次。一次约一屏，弹窗里最多也就三四屏
BACK_TO_TOP_TIMES: int = 4
#: 往下最多滚几次。防止界面异常时死循环
MAX_SCROLL_TIMES: int = 20

#: 项目代理人表里的全部名字，用来判断 OCR 读出来的名字靠不靠谱
_KNOWN_AGENT_NAMES: frozenset[str] = frozenset(
    member.value.agent_name for member in AgentEnum
)
#: 「本屏没有新名字」连续出现几次就认定读完了
NO_NEW_SCREEN_LIMIT: int = 2
#: 判「是不是同一个人」的相似度。4 字名字 OCR 错 1 个字是 0.75，所以卡 0.75；
#: 不同代理人差得远（比如「安比」和「零号安比」只有 0.67），不会被误合并
NAME_DEDUP_CUTOFF: float = 0.75

#: 点按钮后等界面反应的时间（秒）
CLICK_WAIT: float = 1.2
#: 网格滚动后等画面停稳的时间（秒）
SCROLL_WAIT: float = 1.2
#: 点完「关闭」后等弹窗收起来的时间（秒）
CLOSE_WAIT: float = 1.0
#: 回顶部时每次拖完等一下画面（秒）
SCROLL_SETTLE: float = 0.3
#: 竖向拖拽的起止比例（相对网格高度）。0.75 -> 0.25 相当于一次滚 50% 高度，也就是一行。
#: 只滚一行是为了「重叠」：最下面那行会被滚动条和按钮挡住一半（实测「照」被读成「日」），
#: 滚一屏后同一行会移到中间位置，能被完整读到一次。
SCROLL_START_RATIO: float = 0.75
SCROLL_END_RATIO: float = 0.25
#: 拖拽全程耗时。原来用默认 0.5 秒拖 320px，游戏多半当成了点击，压根没滚
SCROLL_DRAG_SECONDS: float = 0.9

#: 节点状态：名单读完了（滚到没有新名字）
STATUS_NAMES_READ: str = '名单读完'
#: 节点状态：一个名字都没读到
STATUS_NAMES_EMPTY: str = '名单没读到'


@dataclass
class AgentNameList:
    """扫一遍弹窗拿到的代理人名单。"""

    name_list: list[str] = field(default_factory=list)
    """代理人名字，按屏幕上第一次出现的顺序，已经去过重。"""

    warnings: list[str] = field(default_factory=list)
    """降级提示：滚动没生效、弹窗关不掉、名单可能不全都在这里。"""

    @property
    def count(self) -> int:
        """读到几个代理人。"""
        return len(self.name_list)

    def summary_lines(self) -> list[str]:
        """拼成几行日志，方便直接 print 排查。"""
        lines: list[str] = [
            f'{index}. {name}' for index, name in enumerate(self.name_list, start=1)
        ]
        lines.extend(f'[注意] {warning}' for warning in self.warnings)
        return lines


def read_dialog_screen(
    ctx: ZContext, screen: MatLike, area: Rect
) -> tuple[list[str], str]:
    """读一屏里代理人网格的名字，顺带给出这一屏的文字指纹（复用 `agent_plan_page` 的解析规则）。

    每格是「头像 + 稀有度角标 + 名字 + Lv.60」，解析规则在
    `agent_plan_page.parse_dialog_agents` 里：丢掉 Lv. 开头的、按钮文字、
    高度不够的碎字，剩下的按阅读顺序就是名字。

    指纹是这个区域里所有文字按字母序拼起来的字符串，只用来判断
    「滚动之后画面到底动没动」，不参与名字解析。

    Args:
        ctx: 一条龙上下文
        screen: 游戏截图
        area: 代理人网格区域

    Returns:
        (这一屏读到的名字, 文字指纹)。名字可能带同一格在滚动边界上重复读出来的条目
    """
    block_list = agent_plan_page.read_text_blocks(ctx, screen, area)
    agent_list = agent_plan_page.parse_dialog_agents_by_grid(block_list, area)
    # 整屏 OCR 偶尔整个格子都没检测到（实测「柳」），按固定布局裁出来补读一遍
    missing = agent_plan_page.read_missing_cell_names(ctx, screen, area, block_list)
    if missing:
        existing = {agent.name for agent in agent_list}
        agent_list.extend(agent for agent in missing if agent.name not in existing)
    name_list: list[str] = []
    for agent in agent_list:
        name = agent.name
        # 不在项目代理人表里的名字，多半是 OCR 认错了（实测「雅」读成「好」）。
        # 放大重识别一次，能救回来就用新的。
        if name.strip('「」') not in _KNOWN_AGENT_NAMES:
            refined = agent_plan_page.refine_dialog_name(ctx, screen, agent)
            if refined.strip('「」') in _KNOWN_AGENT_NAMES:
                log.info('名字「%s」不在代理人表里，放大重识别得到「%s」', name, refined)
                name = refined
        name_list.append(name)
    fingerprint = '|'.join(sorted(block.text for block in block_list))
    return name_list, fingerprint


def merge_agent_names(
    old_list: list[str],
    new_list: list[str],
    cutoff: float = NAME_DEDUP_CUTOFF,
) -> tuple[list[str], int]:
    """把这一屏的名字合并进已经收到的总名单，去重但保序。

    判重不看位置，只看名字：同一格在滚动边界上会被两屏各读一次，
    OCR 偶尔会差一个字，所以精确比对之后再用 difflib 兜一层。

    Args:
        old_list: 之前累计的名字
        new_list: 这一屏读到的名字
        cutoff: 模糊判重的相似度下限

    Returns:
        (合并后的名单, 这次新增了几个)
    """
    merged: list[str] = list(old_list)
    new_count: int = 0
    for name in new_list:
        if str_utils.find_in_list_with_fuzzy(name, merged, cutoff=cutoff) is not None:
            continue
        merged.append(name)
        new_count += 1
    return merged, new_count


class ScanAgentList(ZOperation):
    """把「更换特训目标」弹窗里的全部代理人名字读一遍。结果的 data 是 `AgentNameList`。

    读完会点弹窗右上角的关闭收尾，不点任何代理人格子，也不改当前的特训目标。
    每个节点失败都带自己的 status，用来分清是卡在「不在特训目标页」「弹窗没开」还是别的地方。
    """

    def __init__(self, ctx: ZContext, goto_first: bool = True) -> None:
        """
        构造函数。

        Args:
            ctx: 一条龙上下文
            goto_first: 调用前没在特训目标页时，是否自己先走过去。外层已经走过了可以传 False
        """
        ZOperation.__init__(self, ctx, op_name='扫描代理人列表')

        self.goto_first: bool = goto_first
        """没在特训目标页时是否自己导航过去"""

        self.name_list: list[str] = []
        """累计扫到的代理人名字，按屏幕上第一次出现的顺序"""

        self.warnings: list[str] = []
        """降级提示"""

        self.scroll_times: int = 0
        """已经往下滚了几次"""

        self.no_new_times: int = 0
        """连续几屏没有新名字"""

        self.same_screen_times: int = 0
        """连续几次滚动后画面一个字都没变（拖拽没生效的迹象）"""

        self.last_fingerprint: str = ''
        """上一屏的文字指纹，用来识别「拖拽没生效」"""

        self.fail_status: str = ''
        """读名字阶段发现的问题。收尾节点用它决定这次操作是成是败"""

    def handle_init(self) -> None:
        """每次执行前把上一次跑留下的中间状态清干净，免得同实例复用时名单串味。"""
        self.name_list = []
        self.warnings = []
        self.scroll_times = 0
        self.no_new_times = 0
        self.same_screen_times = 0
        self.last_fingerprint = ''
        self.fail_status = ''

    @operation_node(name='确认特训目标页', is_start_node=True, node_max_retry_times=3)
    def ensure_page(self) -> OperationRoundResult:
        """确认停在特训目标页；不在就让 `GotoAgentPlan` 走过去。

        弹窗已经开着时不再导航：那时候页面被弹窗盖住，判断不一定准，
        乱点还有可能点到别的东西（`change_agent_plan_target` 也是这么绕开的）。
        """
        if self._is_dialog_open():
            return self.round_success(status='弹窗已经开着，跳过导航')
        if agent_plan_page.is_agent_plan_page(self.ctx, self.last_screenshot):
            return self.round_success(status='已在特训目标页')
        if not self.goto_first:
            return self.round_fail(
                status='不在特训目标页（左侧分类「特训目标」没高亮，也没看到「更换特训目标」按钮），'
                '而调用方要求不自己导航'
            )

        op_result = GotoAgentPlan(self.ctx).execute()
        if op_result.success:
            return self.round_success(status='已进入特训目标页')
        if self.node_retry_times >= self.node_max_retry_times - 1:
            return self.round_fail(status=f'进不了特训目标页：{op_result.status}')
        return self.round_retry(status=f'进不了特训目标页：{op_result.status}', wait=1)

    @node_from(from_name='确认特训目标页')
    @operation_node(name='打开更换弹窗')
    def open_dialog(self) -> OperationRoundResult:
        """点角色卡上的「更换特训目标」。弹窗已经开着就直接过。"""
        if self._is_dialog_open():
            return self.round_success(status='弹窗已开')

        rect: Rect = screen_areas.get_area_rect(
            self.ctx, screen_areas.COMPENDIUM_SCREEN, screen_areas.AREA_CHANGE_TARGET
        )
        self.ctx.controller.click(rect.center)
        return self.round_success(status='点击更换特训目标', wait=CLICK_WAIT)

    @node_from(from_name='打开更换弹窗')
    @operation_node(name='确认弹窗已开', node_max_retry_times=5)
    def confirm_dialog(self) -> OperationRoundResult:
        """用弹窗标题「更换特训目标」确认弹窗真的弹出来了。"""
        if self._is_dialog_open():
            return self.round_success(status='弹窗已开')
        if self.node_retry_times >= self.node_max_retry_times - 1:
            return self.round_fail(
                status='点了「更换特训目标」但弹窗没弹出来：左上角标题「更换特训目标」没找到'
            )
        return self.round_retry(status='还没看到弹窗标题，等一下再看', wait=1)

    @node_from(from_name='确认弹窗已开')
    @operation_node(name='回到网格顶部')
    def back_to_grid_top(self) -> OperationRoundResult:
        """先把网格滚回最上面，保证每次扫描都从第一格开始。

        弹窗刚打开一般在顶部，这一步是兜底：上一次流程可能把弹窗留在滚到中间的状态。
        **未实机验证**：拖不动的时候这一步是空操作，不会报错。
        """
        area = self._grid_area()
        if area is None:
            return self.round_success(status='网格区域没配置，跳过回顶部')

        for _ in range(BACK_TO_TOP_TIMES):
            self._drag_grid(area, downward=False)
            time.sleep(SCROLL_SETTLE)
        return self.round_success(status='网格已回到顶部')

    @node_from(from_name='回到网格顶部')
    @operation_node(name='逐屏读名字', node_max_retry_times=MAX_SCROLL_TIMES + 5)
    def read_names(self) -> OperationRoundResult:
        """读当前这一屏的名字，然后往下滚一屏再读，直到连续两屏都没有新名字。

        结束判据是名字集合，不是位置：滚动后这一屏的名字全都见过，就算这屏没贡献。
        「没有新名字」要连续 `NO_NEW_SCREEN_LIMIT` 次才收工，一次就收会因为
        拖拽没生效（惯性、按到边界）而静默漏人。
        """
        area = self._grid_area()
        if area is None:
            self.fail_status = f'{CHANGE_TARGET_SCREEN}.{AREA_AGENT_GRID} 区域没配置，读不了代理人名字'
            return self.round_success(status=STATUS_NAMES_EMPTY)

        screen_name_list, fingerprint = read_dialog_screen(
            self.ctx, self.last_screenshot, area
        )
        self.name_list, new_count = merge_agent_names(self.name_list, screen_name_list)
        log.info(
            '代理人名单 第 %d 屏读到 %d 个，新增 %d 个，累计 %d 个',
            self.scroll_times + 1,
            len(screen_name_list),
            new_count,
            len(self.name_list),
        )

        # 指纹一样说明这一下拖拽没动画面，和「真的滚到底」要分开记：前者可能漏人
        same_screen = fingerprint == self.last_fingerprint and fingerprint != ''
        self.last_fingerprint = fingerprint

        if new_count == 0:
            self.no_new_times += 1
            self.same_screen_times = self.same_screen_times + 1 if same_screen else 0
            if self.no_new_times >= NO_NEW_SCREEN_LIMIT:
                if self.same_screen_times >= 1:
                    self.warnings.append(
                        '最后两次滚动后画面一个字都没变（拖拽可能没生效或已到底），按读完了处理；'
                        '如果名单明显偏少，先确认网格能不能滚、再调 GRID_SCROLL_MARGIN'
                    )
                return self.round_success(status=STATUS_NAMES_READ)
        else:
            self.no_new_times = 0
            self.same_screen_times = 0

        if self.scroll_times >= MAX_SCROLL_TIMES:
            self.warnings.append(
                f'往下滚了 {MAX_SCROLL_TIMES} 次还没读完，名单可能不全（见 MAX_SCROLL_TIMES）'
            )
            return self.round_success(status=STATUS_NAMES_READ)

        self._drag_grid(area, downward=True)
        self.scroll_times += 1
        return self.round_wait(
            status=f'已读 {len(self.name_list)} 个，往下滚第 {self.scroll_times} 次',
            wait=SCROLL_WAIT,
        )

    @node_from(from_name='逐屏读名字')
    @operation_node(name='关掉弹窗')
    def close_dialog(self) -> OperationRoundResult:
        """收尾：点弹窗右上角的关闭，把界面放回特训目标页。

        **不点任何代理人格子**，也不点「清除目标」，当前特训目标原样保留。
        关不掉不算失败，只往 warnings 里记一笔：名单已经拿到了，关窗是界面卫生问题。

        Returns:
            读到名字就成功（data 是 `AgentNameList`），一个都没读到就失败
        """
        if self._is_dialog_open():
            self._close_dialog()
            # 点完之后重新截一张图再看，否则判断的还是点之前那一帧
            self.screenshot()
            if self._is_dialog_open():
                self.warnings.append(
                    '点了弹窗右上角关闭，但「更换特训目标」弹窗还在，界面可能停在了弹窗上'
                )

        snapshot = self._make_snapshot()
        if self.fail_status:
            return self.round_fail(status=self.fail_status, data=snapshot)
        if not snapshot.name_list:
            return self.round_fail(
                status='一个代理人名字都没读到：网格区域可能没对准，或者弹窗里没有代理人',
                data=snapshot,
            )
        return self.round_success(
            status=f'读到 {snapshot.count} 个代理人，弹窗已关', data=snapshot
        )

    def _make_snapshot(self) -> AgentNameList:
        """把扫到的结果打包成外部要用的结构。"""
        return AgentNameList(name_list=list(self.name_list), warnings=list(self.warnings))

    def _is_dialog_open(self) -> bool:
        """更换弹窗现在是不是开着（靠左上角标题认）。"""
        if self._dialog_area(AREA_DIALOG_TITLE) is None:
            return False
        return self.round_by_find_area(
            screen=self.last_screenshot,
            screen_name=CHANGE_TARGET_SCREEN,
            area_name=AREA_DIALOG_TITLE,
        ).is_success

    def _dialog_area(self, area_name: str) -> Rect | None:
        """取插件自带画面里的一个区域矩形。没配置就是 None。

        Args:
            area_name: 区域名

        Returns:
            区域矩形
        """
        area = self.ctx.screen_loader.get_area(CHANGE_TARGET_SCREEN, area_name)
        if area is None:
            return None
        return area.rect

    def _grid_area(self) -> Rect | None:
        """代理人网格区域。没配置就是 None。"""
        return self._dialog_area(AREA_AGENT_GRID)

    def _drag_grid(self, area: Rect, downward: bool) -> None:
        """在网格里拖一下，让列表往下（或往上）滚。

        Args:
            area: 代理人网格区域
            downward: True 看后面的代理人（手指从下往上滑），False 回顶部
        """
        screen_areas.scroll_rect(
            self.ctx,
            area,
            direction='down' if downward else 'up',
            start_ratio=SCROLL_START_RATIO,
            end_ratio=SCROLL_END_RATIO,
            duration=SCROLL_DRAG_SECONDS,
        )

    def _close_dialog(self) -> None:
        """点弹窗右上角的红 X。

        这个区域没有文字也没模板，所以直接点矩形中心，不走按文字找区域那一套。
        """
        rect = self._dialog_area(AREA_CLOSE_BUTTON)
        if rect is None:
            self.warnings.append(
                f'{CHANGE_TARGET_SCREEN}.{AREA_CLOSE_BUTTON} 区域没配置，读完没法关弹窗'
            )
            return
        self.ctx.controller.click(rect.center)
        time.sleep(CLOSE_WAIT)


def __debug() -> None:
    from zzz_od.context.zzz_context import ZContext

    ctx = ZContext()
    ctx.init_by_config()
    ctx.init_ocr()
    ctx.run_context.start_running()
    op = ScanAgentList(ctx)
    op_result = op.execute()
    if op_result.success:
        name_list: AgentNameList = op_result.data
        for line in name_list.summary_lines():
            print(line)
    else:
        log.error('扫描代理人列表失败 %s', op_result.status)


if __name__ == '__main__':
    __debug()
