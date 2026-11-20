"""特训目标体力计划的配置。

配置存在 `config/<账号下标>/<应用组>/agent_charge_plan.yml`，按账号和应用组分开。
"""

from __future__ import annotations

from one_dragon.base.operation.application.application_config import ApplicationConfig

from . import agent_charge_plan_const

#: 目标模式：只按角色养成缺口排计划
GOAL_MODE_AGENT_STAGE: str = 'agent_stage'
#: 目标模式：只按材料库存下限排计划
GOAL_MODE_MATERIAL_FLOOR: str = 'material_floor'
#: 目标模式：两者都要
GOAL_MODE_BOTH: str = 'both'

#: 运行模式：只算计划写进内置体力计划，执行交给内置应用（默认）
RUN_MODE_PLAN_ONLY: str = 'plan_only'
#: 运行模式：自己边跑边算边刷，不再需要内置体力计划
RUN_MODE_EXECUTE: str = 'execute'
#: 运行模式：全角色核对。遍历名单逐个读清单、算计划、落盘，不写内置体力计划配置
RUN_MODE_AUDIT: str = 'audit'
#: 运行模式可选值
RUN_MODE_LIST: tuple[str, ...] = (RUN_MODE_PLAN_ONLY, RUN_MODE_EXECUTE, RUN_MODE_AUDIT)


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
        """要读的特训目标代理人名字。空字符串表示「用游戏里当前设的那个」。"""
        return self.get('target_agent_name', '')

    @target_agent_name.setter
    def target_agent_name(self, new_value: str) -> None:
        """改代理人名字，写回 YAML。"""
        self.update('target_agent_name', new_value)

    @property
    def goal_mode(self) -> str:
        """目标模式：`agent_stage` / `material_floor` / `both`。"""
        return self.get('goal_mode', GOAL_MODE_AGENT_STAGE)

    @goal_mode.setter
    def goal_mode(self, new_value: str) -> None:
        """改目标模式。"""
        self.update('goal_mode', new_value)

    @property
    def dry_run(self) -> bool:
        """只预览不写入。默认开，避免第一次跑就把用户的计划改乱。"""
        return self.get('dry_run', True)

    @dry_run.setter
    def dry_run(self, new_value: bool) -> None:
        """改预览开关。"""
        self.update('dry_run', new_value)

    @property
    def default_plan_times(self) -> int:
        """算不出「缺口 ÷ 单次产出」时，每条计划默认排几次。"""
        return self.get('default_plan_times', 1)

    @default_plan_times.setter
    def default_plan_times(self, new_value: int) -> None:
        """改默认次数。"""
        self.update('default_plan_times', new_value)

    @property
    def include_drive_disc(self) -> bool:
        """驱动盘分组要不要一起排。

        驱动盘没有库存概念（词条随机），「维持数量」对它不成立，
        所以默认不排；想刷盘子的自己打开。
        """
        return self.get('include_drive_disc', False)

    @include_drive_disc.setter
    def include_drive_disc(self, new_value: bool) -> None:
        """改驱动盘开关。"""
        self.update('include_drive_disc', new_value)

    @property
    def material_goals(self) -> list[dict]:
        """材料库存目标列表，每项形如 `{'material_name': '特化流明芯片', 'target_floor': 100}`。"""
        raw = self.get('material_goals', [])
        if not isinstance(raw, list):
            return []
        return [i for i in raw if isinstance(i, dict)]

    @material_goals.setter
    def material_goals(self, new_value: list[dict]) -> None:
        """改材料目标列表。"""
        self.update('material_goals', new_value)

    @property
    def last_generated_keys(self) -> list[str]:
        """上一次本插件写进去的计划内容键。

        下一轮用它把「缺口已经填平」的旧条目清掉，不清用户自己手工加的条目。
        """
        raw = self.get('last_generated_keys', [])
        if not isinstance(raw, list):
            return []
        return [i for i in raw if isinstance(i, str)]

    @last_generated_keys.setter
    def last_generated_keys(self, new_value: list[str]) -> None:
        """记下这一轮写进去的内容键。"""
        self.update('last_generated_keys', new_value)

    @property
    def scan_only(self) -> bool:
        """只扫描代理人名单、不生成计划。打开后跑一次就能把 known_agent_list 刷出来。"""
        return self.get('scan_only', False)

    @scan_only.setter
    def scan_only(self, new_value: bool) -> None:
        """改扫描模式开关。"""
        self.update('scan_only', new_value)

    @property
    def known_agent_list(self) -> list[str]:
        """游戏里扫到的全部代理人名字，按弹窗顺序。扫描模式跑一次就会写进这里。"""
        raw = self.get('known_agent_list', [])
        if not isinstance(raw, list):
            return []
        return [i for i in raw if isinstance(i, str)]

    @known_agent_list.setter
    def known_agent_list(self, new_value: list[str]) -> None:
        """记下扫到的代理人名单。"""
        self.update('known_agent_list', new_value)

    @property
    def batch_agent_names(self) -> list[str]:
        """要批量配置的角色名单。空 = 只处理 `target_agent_name`（或游戏当前目标）。

        游戏只能设一个特训目标，所以这里多选之后要逐个切过去读一遍。
        """
        raw = self.get('batch_agent_names', [])
        if not isinstance(raw, list):
            return []
        return [i for i in raw if isinstance(i, str) and i.strip()]

    @batch_agent_names.setter
    def batch_agent_names(self, new_value: list[str]) -> None:
        """改批量名单。"""
        self.update('batch_agent_names', new_value)

    @property
    def run_mode(self) -> str:
        """运行模式：`plan_only`（只排计划，默认）/ `execute`（自己边跑边算边刷）/ `audit`（全角色核对）。"""
        raw = self.get('run_mode', RUN_MODE_PLAN_ONLY)
        if raw not in RUN_MODE_LIST:
            return RUN_MODE_PLAN_ONLY
        return raw

    @run_mode.setter
    def run_mode(self, new_value: str) -> None:
        """改运行模式。写进来的值不认识就按默认的 `plan_only` 处理。"""
        self.update('run_mode', new_value)

    @property
    def audit_output_dir(self) -> str:
        """全角色核对的结果放哪。空字符串 = 用默认位置。

        默认位置是 `config/<账号>/<组>/agent_audit/<时间戳>/`，和配置放一起方便找。
        填了就用这个目录，但仍然按时间戳再分一层，免得这次把上次的结果盖掉。
        """
        raw = self.get('audit_output_dir', '')
        if not isinstance(raw, str):
            return ''
        return raw.strip()

    @audit_output_dir.setter
    def audit_output_dir(self, new_value: str) -> None:
        """改核对输出目录。"""
        self.update('audit_output_dir', new_value)

    @property
    def fallback_plan(self) -> dict:
        """保底任务的副本，字段同 `ChargePlanItem`：分类 / 副本类型 / 副本名 / 卡数。

        空字典表示「①② 都没活干时就结束」，不刷保底。
        """
        raw = self.get('fallback_plan', {})
        if not isinstance(raw, dict):
            return {}
        return dict(raw)

    @fallback_plan.setter
    def fallback_plan(self, new_value: dict) -> None:
        """改保底任务。"""
        self.update('fallback_plan', new_value)
