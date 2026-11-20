"""特训目标体力计划的配置。

配置存在 config/<账号下标>/<应用组>/agent_charge_plan.yml，按账号和应用组分开。
现在只有一个占位项，等接了读画面的逻辑再往上加。
"""

from __future__ import annotations

from one_dragon.base.operation.application.application_config import ApplicationConfig

from . import agent_charge_plan_const


class AgentChargePlanConfig(ApplicationConfig):
    """插件自己的配置对象。"""

    def __init__(self, instance_idx: int, group_id: str) -> None:
        """按账号下标和应用组加载对应 YAML。"""
        ApplicationConfig.__init__(
            self,
            app_id=agent_charge_plan_const.APP_ID,
            instance_idx=instance_idx,
            group_id=group_id,
        )

    @property
    def target_agent_name(self) -> str:
        """要排体力计划的代理人名字。现在只是占位，业务逻辑还没接。"""
        return self.get('target_agent_name', '')

    @target_agent_name.setter
    def target_agent_name(self, new_value: str) -> None:
        """改代理人名字，写回 YAML。"""
        self.update('target_agent_name', new_value)
