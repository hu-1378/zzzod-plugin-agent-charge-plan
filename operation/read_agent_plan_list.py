"""读「特训目标」清单，一路滚到底，输出结构化结果。

流程：先滚回顶部，然后一屏一屏往下读，读到没有新条目就认为到底了。
驱动盘分组的条目没有进度条，只有电量，这是游戏本身的样子，不是识别失败。
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

from one_dragon.base.geometry.point import Point
from one_dragon.base.geometry.rectangle import Rect
from one_dragon.base.operation.operation_edge import node_from
from one_dragon.base.operation.operation_node import operation_node
from one_dragon.base.operation.operation_round_result import OperationRoundResult
from one_dragon.utils.log_utils import log
from zzz_od.operation.zzz_operation import ZOperation

from . import agent_plan_page, screen_areas
from .agent_plan_page import AgentPlanItem, AgentPlanSnapshot
from .goto_agent_plan import GotoAgentPlan

if TYPE_CHECKING:
    from zzz_od.context.zzz_context import ZContext


#: 回顶部时最多往上拖几次。一次大约一屏，清单再长也够用了
BACK_TO_TOP_TIMES: int = 6
#: 往下最多滚几次。防止界面异常时死循环
MAX_SCROLL_TIMES: int = 12
#: 拖拽时离清单区上下边缘留多少像素
SCROLL_MARGIN: int = 60
#: 滚动后等界面停稳的时间（秒）
SCROLL_WAIT: float = 0.8
#: 回顶部时每次拖完等一下界面（秒）
SCROLL_SETTLE: float = 0.3


class ReadAgentPlanList(ZOperation):
    """读一整个特训目标清单。结果的 data 是 `AgentPlanSnapshot`。"""

    def __init__(self, ctx: ZContext, goto_first: bool = True) -> None:
        """
        构造函数。

        Args:
            ctx: 一条龙上下文
            goto_first: 调用前没在特训目标页时，是否自己先走过去。外层已经走过了可以传 False
        """
        ZOperation.__init__(self, ctx, op_name='读取特训目标清单')

        self.goto_first: bool = goto_first
        """没在特训目标页时是否自己导航过去"""

        self.agent_name: str = ''
        """顶部角色卡上的代理人名字"""

        self.item_list: list[AgentPlanItem] = []
        """累计读到的条目"""

        self.scroll_times: int = 0
        """已经往下滚了几次"""

        self.last_fingerprint: str = ''
        """上一屏的文字指纹。同一屏再读一次指纹不变，用来识别「拖拽没生效」"""

        self.stuck_times: int = 0
        """连续几次拖拽后画面没变化的计数"""

    @operation_node(name='确认页面', is_start_node=True, node_max_retry_times=3)
    def ensure_page(self) -> OperationRoundResult:
        """确认停在特训目标页。允许自己导航过去。"""
        if not self.goto_first:
            return self.round_success(status='跳过导航')
        if agent_plan_page.is_agent_plan_page(self.ctx, self.last_screenshot):
            return self.round_success(status='已在特训目标页')

        op = GotoAgentPlan(self.ctx)
        return self.round_by_op_result(op.execute(), retry_on_fail=True)

    @node_from(from_name='确认页面')
    @operation_node(name='读角色名')
    def read_agent_name(self) -> OperationRoundResult:
        """读顶部角色卡上的代理人名字。没设特训目标时读不到，不算失败。"""
        self.agent_name = agent_plan_page.read_agent_name(self.ctx, self.last_screenshot)
        if not self.agent_name:
            return self.round_success(status='没有设置特训目标')
        return self.round_success(status=f'当前特训目标 {self.agent_name}')

    @node_from(from_name='读角色名')
    @operation_node(name='回到顶部')
    def back_to_top(self) -> OperationRoundResult:
        """一直往上拖，把清单滚回最顶上，保证每次读的都一样。"""
        for _ in range(BACK_TO_TOP_TIMES):
            self._drag_plan_list(downward=False)
            time.sleep(SCROLL_SETTLE)
        return self.round_success()

    @node_from(from_name='回到顶部')
    @operation_node(name='读取清单', node_max_retry_times=MAX_SCROLL_TIMES + 5)
    def read_one_screen(self) -> OperationRoundResult:
        """读当前这一屏，合并进总表。没有新条目或者滚太多次就结束。"""
        area: Rect = self._plan_list_rect()
        block_list = agent_plan_page.read_text_blocks(self.ctx, self.last_screenshot, area)
        screen_item_list = agent_plan_page.parse_plan_items(block_list, area)
        self.item_list, new_count = agent_plan_page.merge_item_list(
            self.item_list, screen_item_list
        )
        log.info(
            '特训目标清单 第 %d 屏读到 %d 条，新增 %d 条，累计 %d 条',
            self.scroll_times + 1,
            len(screen_item_list),
            new_count,
            len(self.item_list),
        )

        fingerprint = '|'.join(sorted(block.text for block in block_list))
        same_screen = fingerprint == self.last_fingerprint and fingerprint != ''
        self.last_fingerprint = fingerprint

        if self.scroll_times > 0 and new_count == 0:
            if same_screen:
                # 画面一个字都没变，说明这一下拖拽没生效（惯性/边界），再拖一次。
                # 连着两次没变化才当成真的到底，否则会静默漏掉条目。
                self.stuck_times += 1
                if self.stuck_times >= 2:
                    log.warning('连续两次滚动画面没变化，当成到底；如果实际还有条目，调大这里或改拖拽距离')
                    return self.round_success(status='已经滚到底', data=self._make_snapshot())
                self._drag_plan_list(downward=True)
                self.scroll_times += 1
                return self.round_wait(status='画面没变化，再滚一次', wait=SCROLL_WAIT)
            return self.round_success(status='已经滚到底', data=self._make_snapshot())

        self.stuck_times = 0
        if self.scroll_times >= MAX_SCROLL_TIMES:
            return self.round_success(status='滚动次数达到上限', data=self._make_snapshot())

        self._drag_plan_list(downward=True)
        self.scroll_times += 1
        return self.round_wait(status=f'向下滚动第 {self.scroll_times} 次', wait=SCROLL_WAIT)

    def _make_snapshot(self) -> AgentPlanSnapshot:
        """把读到的结果打包成外部要用的结构。"""
        return AgentPlanSnapshot(agent_name=self.agent_name, item_list=list(self.item_list))

    def _plan_list_rect(self) -> Rect:
        """清单区矩形。主仓有就用主仓的，没有就用插件里写死的兜底坐标。"""
        return screen_areas.get_area_rect(
            self.ctx, screen_areas.COMPENDIUM_SCREEN, screen_areas.AREA_PLAN_LIST
        )

    def _drag_plan_list(self, downward: bool) -> None:
        """在清单区里拖一下，让列表往下（或往上）滚。

        Args:
            downward: True 表示看后面的条目（手指从下往上滑），False 表示回顶部
        """
        area: Rect = self._plan_list_rect()
        x: int = area.center.x
        top: Point = Point(x, area.y1 + SCROLL_MARGIN)
        bottom: Point = Point(x, area.y2 - SCROLL_MARGIN)
        if downward:
            self.ctx.controller.drag_to(start=bottom, end=top)
        else:
            self.ctx.controller.drag_to(start=top, end=bottom)


def __debug() -> None:
    from zzz_od.context.zzz_context import ZContext

    ctx = ZContext()
    ctx.init_by_config()
    ctx.init_ocr()
    ctx.run_context.start_running()
    op = ReadAgentPlanList(ctx)
    op_result = op.execute()
    if op_result.success:
        snapshot: AgentPlanSnapshot = op_result.data
        print('代理人', snapshot.agent_name)
        for item in snapshot.item_list:
            print(item.group_name, item.name, item.charge_text, item.progress_text)


if __name__ == '__main__':
    __debug()
