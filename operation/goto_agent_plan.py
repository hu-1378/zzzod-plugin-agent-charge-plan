"""走到「快捷手册 → 训练 → 特训目标」页。

路径：大世界（或任意能到快捷手册的画面）→ 训练 TAB → 左侧分类「特训目标」。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from one_dragon.base.geometry.rectangle import Rect
from one_dragon.base.operation.operation_edge import node_from
from one_dragon.base.operation.operation_node import operation_node
from one_dragon.base.operation.operation_round_result import OperationRoundResult
from zzz_od.operation.zzz_operation import ZOperation

from . import agent_plan_page, screen_areas

if TYPE_CHECKING:
    from zzz_od.context.zzz_context import ZContext


class GotoAgentPlan(ZOperation):
    """进入特训目标页。执行完就停在特训目标页上。"""

    def __init__(self, ctx: ZContext) -> None:
        ZOperation.__init__(self, ctx, op_name='进入特训目标页')

    @operation_node(name='前往训练TAB', is_start_node=True, node_max_retry_times=10)
    def goto_training_tab(self) -> OperationRoundResult:
        """用主仓的画面路线走到快捷手册的训练 TAB。"""
        return self.round_by_goto_screen(
            screen=self.last_screenshot,
            screen_name=screen_areas.COMPENDIUM_TRAINING_SCREEN,
            success_wait=1,
            retry_wait=1,
        )

    @node_from(from_name='前往训练TAB')
    @operation_node(name='点开特训目标', node_max_retry_times=10)
    def open_agent_plan(self) -> OperationRoundResult:
        """点左侧分类里的「特训目标」。已经在特训目标页就不点。"""
        if agent_plan_page.is_agent_plan_page(self.ctx, self.last_screenshot):
            return self.round_success()

        area: Rect = screen_areas.get_area_rect(
            self.ctx, screen_areas.COMPENDIUM_SCREEN, screen_areas.AREA_LEFT_CAT_TARGET
        )
        self.ctx.controller.click(area.center)
        return self.round_wait(status='点击左侧特训目标', wait=1)

    @node_from(from_name='点开特训目标')
    @operation_node(name='确认特训目标页', node_max_retry_times=5)
    def confirm_agent_plan(self) -> OperationRoundResult:
        """再确认一次真的在特训目标页上。"""
        if agent_plan_page.is_agent_plan_page(self.ctx, self.last_screenshot):
            return self.round_success(status='已进入特训目标页')
        return self.round_retry(status='没有进到特训目标页', wait=1)


def __debug() -> None:
    from zzz_od.context.zzz_context import ZContext

    ctx = ZContext()
    ctx.init_by_config()
    ctx.init_ocr()
    ctx.run_context.start_running()
    GotoAgentPlan(ctx).execute()


if __name__ == '__main__':
    __debug()
