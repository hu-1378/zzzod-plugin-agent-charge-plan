"""特训目标体力计划的应用本体。

骨架阶段只有一个开始节点，什么都不做就成功返回。
后面再把「读特训目标页面」「生成体力计划」拆成独立节点接在这里。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from one_dragon.base.operation.operation_node import operation_node
from zzz_od.application.zzz_application import ZApplication

from . import agent_charge_plan_const

if TYPE_CHECKING:
    from one_dragon.base.operation.operation_round_result import OperationRoundResult
    from zzz_od.context.zzz_context import ZContext


class AgentChargePlanApp(ZApplication):
    """特训目标体力计划的应用类。"""

    def __init__(self, ctx: ZContext) -> None:
        """只传上下文；配置和运行记录由框架按账号自己取。"""
        ZApplication.__init__(
            self,
            ctx=ctx,
            app_id=agent_charge_plan_const.APP_ID,
            op_name=agent_charge_plan_const.APP_NAME,
        )

    @operation_node(name='开始', is_start_node=True)
    def start(self) -> OperationRoundResult:
        """骨架阶段直接成功，不做游戏操作。"""
        return self.round_success()
