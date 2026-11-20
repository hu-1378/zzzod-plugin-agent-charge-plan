"""特训目标体力计划的工厂。

只要文件名叫 agent_charge_plan_factory.py、类继承 ApplicationFactory，
ApplicationFactoryManager 扫 plugins/ 目录时就会自动发现并注册，不用手工登记。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from one_dragon.base.operation.application.application_factory import ApplicationFactory

from . import agent_charge_plan_const
from .agent_charge_plan_app import AgentChargePlanApp
from .agent_charge_plan_config import AgentChargePlanConfig

if TYPE_CHECKING:
    from zzz_od.context.zzz_context import ZContext


class AgentChargePlanFactory(ApplicationFactory):
    """把常量、应用、配置三样串起来。"""

    def __init__(self, ctx: ZContext) -> None:
        """读常量拿到 app_id、app_name 和入口行为。"""
        ApplicationFactory.__init__(self, agent_charge_plan_const)
        self.ctx: ZContext = ctx

    def create_application(self, instance_idx: int, group_id: str) -> AgentChargePlanApp:
        """建应用；账号和应用组由框架的配置、运行记录按下标自己取。"""
        return AgentChargePlanApp(self.ctx)

    def create_config(self, instance_idx: int, group_id: str) -> AgentChargePlanConfig:
        """建配置，按账号下标和应用组落到各自的 YAML。"""
        return AgentChargePlanConfig(instance_idx=instance_idx, group_id=group_id)
