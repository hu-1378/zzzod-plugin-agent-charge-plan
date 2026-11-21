"""更换特训目标。

游戏里同时只能设一个特训目标，所以批量配置要在这个弹窗里一个个换。
弹窗里每格是「头像 + 稀有度角标 + 名字 + Lv.60」，这里只认名字：
整块 OCR 后丢掉 Lv. 开头的和按钮文字，再用 difflib 跟想要的名字比。
游戏角色名可能不在项目的角色枚举里（例如「雷米埃尔」「薇蒂安」），所以一律按 OCR 文本处理。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from one_dragon.base.geometry.point import Point
from one_dragon.base.geometry.rectangle import Rect
from one_dragon.base.operation.operation_edge import node_from
from one_dragon.base.operation.operation_node import operation_node
from one_dragon.base.operation.operation_round_result import OperationRoundResult
from one_dragon.utils.log_utils import log
from zzz_od.operation.zzz_operation import ZOperation

from . import agent_plan_page, screen_areas
from .agent_plan_page import DialogAgent
from .goto_agent_plan import GotoAgentPlan

if TYPE_CHECKING:
    from zzz_od.context.zzz_context import ZContext


#: 更换弹窗的画面名（插件自带的 screen_info，已经被 screen_loader 注册）
CHANGE_TARGET_SCREEN: str = '特训目标-更换特训目标'
#: 弹窗左上角的标题，用来判断弹窗开没开
AREA_DIALOG_TITLE: str = '标题-更换特训目标'
#: 代理人头像格子整块区域
AREA_AGENT_GRID: str = '区域-代理人网格'
#: 弹窗底部的「清除目标」
AREA_CLEAR_BUTTON: str = '按钮-清除目标'
#: 选中代理人后，底部右侧「清除目标」会变成「设置为目标」
AREA_SET_TARGET_BUTTON: str = '按钮-设置为目标'
#: 点「设置为目标」之后弹出的确认框画面
CONFIRM_REPLACE_SCREEN: str = '特训目标-确认替换'
#: 确认框里的「确认」按钮
AREA_CONFIRM_OK_BUTTON: str = '按钮-确认'
#: 弹窗右上角的关闭
AREA_CLOSE_BUTTON: str = '按钮-关闭'

#: 弹窗里最多往下滚几屏找目标代理人
MAX_DIALOG_SCROLL_TIMES: int = 3
#: 弹窗里滚动时离网格上下边缘留多少像素
DIALOG_SCROLL_MARGIN: int = 40
#: 点完代理人或按钮后等界面反应的时间（秒）
CLICK_WAIT: float = 1.2
#: 弹窗里滚动后等界面停稳的时间（秒）
SCROLL_DIALOG_WAIT: float = 0.8

#: 节点返回的状态：要清除目标
STATUS_NEED_CLEAR: str = '需要清除目标'
#: 节点返回的状态：要切换目标
STATUS_NEED_CHANGE: str = '需要切换'


class ChangeAgentPlanTarget(ZOperation):
    """把特训目标换成指定的代理人。不传名字表示清除当前目标。"""

    def __init__(
        self,
        ctx: ZContext,
        target_agent_name: str = '',
        goto_first: bool = True,
    ) -> None:
        """
        构造函数。

        Args:
            ctx: 一条龙上下文
            target_agent_name: 想要的代理人名字，按 OCR 文本比。传空字符串表示清除特训目标
            goto_first: 调用前没在特训目标页时，是否自己先走过去
        """
        ZOperation.__init__(self, ctx, op_name='更换特训目标')

        self.target_agent_name: str = target_agent_name
        """想要的代理人名字，空字符串表示清除目标"""

        self.goto_first: bool = goto_first
        """没在特训目标页时是否自己导航过去"""

        self.current_agent_name: str = ''
        """进弹窗前页面上显示的当前特训目标"""

        self.dialog_scroll_times: int = 0
        """弹窗里已经往下滚了几次"""

        self.clicked_agent_name: str = ''
        """这次点过的代理人名字，用来在确认时核对"""

    @operation_node(name='读取当前目标', is_start_node=True, node_max_retry_times=3)
    def read_current_target(self) -> OperationRoundResult:
        """先看页面上现在的特训目标是谁，已经是目标就不再点一次。"""
        if not self._is_dialog_open() and self.goto_first \
                and not agent_plan_page.is_agent_plan_page(self.ctx, self.last_screenshot):
            op = GotoAgentPlan(self.ctx)
            op_result = op.execute()
            if op_result.success:
                # 回到本节点重新判断页面状态
                return self.round_wait(status='已进入特训目标页', wait=1)
            return self.round_retry(status=op_result.status, wait=1)

        branch_status = STATUS_NEED_CLEAR if not self.target_agent_name else STATUS_NEED_CHANGE
        if self._is_dialog_open():
            # 弹窗已经开着，这时候角色名那块被弹窗盖住了读不准，直接往下走
            return self.round_success(status=branch_status)

        self.current_agent_name = agent_plan_page.read_agent_name(self.ctx, self.last_screenshot)
        if not self.target_agent_name:
            return self.round_success(status=branch_status)

        if agent_plan_page.agent_name_similar(self.current_agent_name, self.target_agent_name):
            return self.round_success(status='已经是目标', data=self.current_agent_name)
        return self.round_success(status=branch_status)

    @node_from(from_name='读取当前目标', status=STATUS_NEED_CLEAR)
    @node_from(from_name='读取当前目标', status=STATUS_NEED_CHANGE)
    @operation_node(name='打开更换弹窗', node_max_retry_times=5)
    def open_dialog(self) -> OperationRoundResult:
        """点角色卡上的「更换特训目标」打开弹窗。弹窗已经开着就直接过。"""
        branch_status = STATUS_NEED_CLEAR if not self.target_agent_name else STATUS_NEED_CHANGE
        if self._is_dialog_open():
            return self.round_success(status=branch_status)

        rect: Rect = screen_areas.get_area_rect(
            self.ctx, screen_areas.COMPENDIUM_SCREEN, screen_areas.AREA_CHANGE_TARGET
        )
        self.ctx.controller.click(rect.center)
        return self.round_wait(status='点击更换特训目标', wait=CLICK_WAIT)

    @node_from(from_name='打开更换弹窗', status=STATUS_NEED_CLEAR)
    @operation_node(name='点清除目标', node_max_retry_times=5)
    def clear_target(self) -> OperationRoundResult:
        """点弹窗底部的「清除目标」。"""
        if not self._is_dialog_open():
            return self.round_success(status='弹窗已经关了')
        return self.round_by_find_and_click_area(
            self.last_screenshot, CHANGE_TARGET_SCREEN, AREA_CLEAR_BUTTON,
            success_wait=CLICK_WAIT, retry_wait=1,
        )

    @node_from(from_name='点清除目标')
    @operation_node(name='确认已清除', node_max_retry_times=5)
    def confirm_clear(self) -> OperationRoundResult:
        """核对页面上已经没有特训目标了。

        清掉之后角色名那块会变成空态的提示文字，所以判据是：
        读不到名字，或者读到的名字已经跟清除前不一样了。
        """
        if self._is_dialog_open():
            return self.round_retry(status='弹窗还没关', wait=1)
        name = agent_plan_page.read_agent_name(self.ctx, self.last_screenshot)
        if not name:
            return self.round_success(status='已清除特训目标')
        if self.current_agent_name \
                and not agent_plan_page.agent_name_similar(name, self.current_agent_name):
            return self.round_success(status=f'已清除特训目标（页面显示 {name}）')
        return self.round_retry(status=f'页面上还是 {name}', wait=1)

    @node_from(from_name='打开更换弹窗', status=STATUS_NEED_CHANGE)
    @operation_node(name='找并点击代理人', node_max_retry_times=MAX_DIALOG_SCROLL_TIMES + 5)
    def find_and_click_agent(self) -> OperationRoundResult:
        """在弹窗网格里找目标代理人。找不到就往下滚一屏再找。"""
        if not self._is_dialog_open():
            # 上一次点击直接把弹窗关了，说明已经选中生效了
            return self.round_success(status='弹窗已关闭')

        area = self._dialog_area(AREA_AGENT_GRID)
        if area is None:
            return self.round_fail(status='代理人网格区域没配置')

        block_list = agent_plan_page.read_text_blocks(self.ctx, self.last_screenshot, area)
        agent: DialogAgent | None = agent_plan_page.find_dialog_agent(
            block_list, self.target_agent_name
        )

        if agent is None:
            if self.dialog_scroll_times >= MAX_DIALOG_SCROLL_TIMES:
                return self.round_fail(status=f'弹窗里找不到 {self.target_agent_name}')
            self._drag_dialog(area)
            self.dialog_scroll_times += 1
            return self.round_retry(
                status=f'没看到 {self.target_agent_name}，滚动第 {self.dialog_scroll_times} 次',
                wait=SCROLL_DIALOG_WAIT,
            )

        if self._is_agent_selected(area, agent):
            # 已经是选中的那个了，不重复点
            return self.round_success(status=f'{agent.name} 已经是选中状态')

        self.clicked_agent_name = agent.name
        self.ctx.controller.click(agent.click_point())
        return self.round_wait(status=f'点击 {agent.name}', wait=CLICK_WAIT)

    @node_from(from_name='找并点击代理人')
    @operation_node(name='点设置为目标', node_max_retry_times=5)
    def click_set_target(self) -> OperationRoundResult:
        """选中代理人后，点底部右侧的「设置为目标」。

        这一步不能省：点完格子只是「选中」，底部按钮会从「清除目标」变成「设置为目标」，
        不点它的话特训目标根本没换。
        """
        if not self._is_dialog_open():
            # 有的情况点格子直接生效、弹窗自己关了
            return self.round_success(status='弹窗已关闭，按已生效处理')
        return self.round_by_find_and_click_area(
            self.last_screenshot, CHANGE_TARGET_SCREEN, AREA_SET_TARGET_BUTTON,
            success_wait=CLICK_WAIT, retry_wait=1,
        )

    @node_from(from_name='点设置为目标')
    @operation_node(name='点确认替换', node_max_retry_times=5)
    def click_confirm_replace(self) -> OperationRoundResult:
        """确认框「已将 xxx 设置为特训目标，是否替换？」里点「确认」。"""
        if not agent_plan_page.is_confirm_replace_dialog(self.ctx, self.last_screenshot):
            # 确认框没出来（已经生效或直接关了），交给下一步核对
            return self.round_success(status='没有确认框，按已生效处理')
        return self.round_by_find_and_click_area(
            self.last_screenshot, CONFIRM_REPLACE_SCREEN, AREA_CONFIRM_OK_BUTTON,
            success_wait=CLICK_WAIT, retry_wait=1,
        )

    @node_from(from_name='点确认替换')
    @operation_node(name='确认切换结果', node_max_retry_times=5)
    def confirm_change(self) -> OperationRoundResult:
        """弹窗关上后，核对页面上的特训目标确实换成了目标代理人。"""
        if self._is_dialog_open():
            if self.node_retry_times >= self.node_max_retry_times - 1:
                # 兜底：把弹窗关掉，免得卡在弹窗上，然后明确报失败
                self._close_dialog()
                return self.round_fail(status='点了代理人但弹窗没自己关，特训目标没换成')
            return self.round_retry(status='弹窗还没关', wait=1)

        name = agent_plan_page.read_agent_name(self.ctx, self.last_screenshot)
        if agent_plan_page.agent_name_similar(name, self.target_agent_name):
            return self.round_success(status=f'特训目标已换成 {name}', data=name)
        return self.round_retry(
            status=f'点了 {self.clicked_agent_name}，但页面上还是 {name}', wait=1
        )

    def _is_dialog_open(self) -> bool:
        """更换弹窗现在是不是开着。"""
        result = self.round_by_find_area(
            screen=self.last_screenshot,
            screen_name=CHANGE_TARGET_SCREEN,
            area_name=AREA_DIALOG_TITLE,
        )
        return result.is_success

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

    def _is_agent_selected(self, area: Rect, agent: DialogAgent) -> bool:
        """这一格代理人是不是当前选中的（有黄色描边）。

        Args:
            area: 代理人网格区域
            agent: 代理人

        Returns:
            选中就是 True。判断不出来也当没选中，会去点一下
        """
        cell_rect = agent_plan_page.find_selected_cell_rect(self.last_screenshot, area)
        if cell_rect is None:
            return False
        center: Point = agent.name_rect.center
        return (
            cell_rect.x1 <= center.x <= cell_rect.x2
            and cell_rect.y1 <= center.y <= cell_rect.y2
        )

    def _drag_dialog(self, area: Rect) -> None:
        """在弹窗网格里往下滚一屏。

        Args:
            area: 代理人网格区域
        """
        x: int = area.center.x
        start = Point(x, area.y2 - DIALOG_SCROLL_MARGIN)
        end = Point(x, area.y1 + DIALOG_SCROLL_MARGIN)
        self.ctx.controller.drag_to(start=start, end=end)

    def _close_dialog(self) -> None:
        """点弹窗右上角的关闭，避免停在弹窗上出不去。"""
        rect = self._dialog_area(AREA_CLOSE_BUTTON)
        if rect is not None:
            self.ctx.controller.click(rect.center)


def __debug() -> None:
    from zzz_od.context.zzz_context import ZContext

    ctx = ZContext()
    ctx.init_by_config()
    ctx.init_ocr()
    ctx.run_context.start_running()
    op = ChangeAgentPlanTarget(ctx, target_agent_name='叶曦光')
    op_result = op.execute()
    log.info('换目标结果 %s', op_result.status)


if __name__ == '__main__':
    __debug()
