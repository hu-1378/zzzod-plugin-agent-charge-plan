"""特训目标体力计划的应用本体。

做的事情：进游戏 → 到「快捷手册-训练-特训目标」页 → 读游戏算好的养成缺口 →
翻译成内置体力计划的计划项写进去。执行仍然交给内置「体力计划」应用。

不改主仓任何代码：写配置走 `run_context.get_config('charge_plan', ...)` 拿同一个实例。

三种目标模式（配置 `goal_mode`）：

- `agent_stage`：按角色养成缺口排计划（完整可用）
- `material_floor`：按材料库存下限排计划（**目前只读不排**，原因见 `read_material_gap`）
- `both`：两者都做
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from one_dragon.base.operation.operation_edge import node_from
from one_dragon.base.operation.operation_node import operation_node
from one_dragon.base.operation.operation_notify import NotifyTiming, node_notify
from one_dragon.utils.log_utils import log
from zzz_od.application.charge_plan.charge_plan_app import ChargePlanApp
from zzz_od.application.zzz_application import ZApplication
from zzz_od.operation.back_to_normal_world import BackToNormalWorld

from . import agent_charge_plan_const
from .agent_charge_plan_config import (
    GOAL_MODE_AGENT_STAGE,
    GOAL_MODE_BOTH,
    GOAL_MODE_MATERIAL_FLOOR,
    AgentChargePlanConfig,
)
from .operation.agent_plan_page import AgentPlanSnapshot, read_agent_name
from .operation.change_agent_plan_target import ChangeAgentPlanTarget
from .operation.goto_agent_plan import GotoAgentPlan
from .operation.read_agent_plan_list import ReadAgentPlanList
from .operation.read_material_calc import MaterialGapSnapshot, ReadMaterialCalc
from .planner.charge_budget import ChargeAvailability
from .planner.plan_mapper import PlanMapper
from .planner.plan_models import PlanSnapshot
from .planner.plan_writer import PlanWriter, content_key

if TYPE_CHECKING:
    from one_dragon.base.operation.operation_round_result import OperationRoundResult
    from zzz_od.context.zzz_context import ZContext


class AgentChargePlanApp(ZApplication):
    """特训目标体力计划的应用类。"""

    #: 电量没读出来时按自然电量上限算，宁可少排也不要排多
    DEFAULT_NATURAL_CHARGE: int = 240

    def __init__(self, ctx: ZContext) -> None:
        """只传上下文；配置和运行记录由框架按账号自己取。"""
        ZApplication.__init__(
            self,
            ctx=ctx,
            app_id=agent_charge_plan_const.APP_ID,
            op_name=agent_charge_plan_const.APP_NAME,
        )
        self.config: AgentChargePlanConfig = self.ctx.run_context.get_config(
            app_id=agent_charge_plan_const.APP_ID,
            instance_idx=self.ctx.current_instance_idx,
            group_id=self.ctx.current_group_id,
        )
        #: 读到的特训目标清单
        self.snapshot: AgentPlanSnapshot | None = None
        #: 读到的材料缺口
        self.material_gap: MaterialGapSnapshot | None = None
        #: 当前剩余电量
        self.charge_power: int = 0
        #: 储蓄电量
        self.backup_charge_power: int = 0
        #: 以太电池颗数
        self.ether_battery: int = 0

    @operation_node(name='开始', is_start_node=True)
    def start(self) -> OperationRoundResult:
        """先看配置，决定后面走哪条路。"""
        log.info(
            '特训目标体力计划开始：目标模式 %s，预览模式 %s，指定代理人「%s」',
            self.config.goal_mode,
            self.config.dry_run,
            self.config.target_agent_name or '按游戏当前设置',
        )
        if self.config.goal_mode == GOAL_MODE_MATERIAL_FLOOR:
            return self.round_success('只处理材料库存')
        return self.round_success('读取特训目标')

    @node_from(from_name='开始', status='读取特训目标')
    @operation_node(name='前往特训目标页', node_max_retry_times=3)
    def goto_agent_plan(self) -> OperationRoundResult:
        """走到快捷手册的训练 TAB，再点开左侧「特训目标」。"""
        op = GotoAgentPlan(self.ctx)
        return self.round_by_op_result(op.execute())

    @node_from(from_name='前往特训目标页')
    @operation_node(name='对齐特训目标', node_max_retry_times=2)
    def align_target(self) -> OperationRoundResult:
        """配置里指定了代理人、且和游戏当前目标不一致时，先换过来。"""
        target = self.config.target_agent_name.strip()
        if target == '':
            return self.round_success('沿用游戏当前目标')

        current = read_agent_name(self.ctx, self.last_screenshot)
        if current == '':
            return self.round_retry('没读到当前的代理人名字', wait=1)
        if current == target:
            return self.round_success(f'已经是 {target}')

        op = ChangeAgentPlanTarget(self.ctx, target_agent_name=target, goto_first=False)
        result = op.execute()
        return self.round_by_op_result(result, success_wait=1)

    @node_from(from_name='对齐特训目标')
    @node_from(from_name='对齐特训目标', status='沿用游戏当前目标')
    @operation_node(name='读取清单', node_max_retry_times=2)
    def read_plan_list(self) -> OperationRoundResult:
        """读特训目标页的条目：分组、条目名、电量、进度。"""
        op = ReadAgentPlanList(self.ctx, goto_first=False)
        result = op.execute()
        if not result.is_success:
            return self.round_by_op_result(result)

        snapshot = op.data
        if not isinstance(snapshot, AgentPlanSnapshot):
            return self.round_fail('清单读取没有返回数据')
        self.snapshot = snapshot
        log.info('特训目标：%s，条目 %s 个', snapshot.agent_name, len(snapshot.item_list))
        for item in snapshot.item_list:
            log.info(
                '  %s / %s / 电量 %s / 进度 %s',
                item.group_name, item.name, item.charge_text, item.progress_text,
            )
        return self.round_success()

    @node_from(from_name='读取清单')
    @node_from(from_name='开始', status='只处理材料库存')
    @operation_node(name='读取电量')
    def read_charge(self) -> OperationRoundResult:
        """读快捷手册资源栏的电量、储蓄电量、以太电池。

        特训目标页也在快捷手册里，资源栏位置一样，所以复用内置的「电量检测」流水线。
        """
        try:
            pipeline_ctx = self.ctx.cv_service.run_pipeline('电量检测', self.last_screenshot)
            battery, backup, ether = ChargePlanApp._parse_battery_charge_ocr(pipeline_ctx.ocr_result)
        except Exception:
            log.warning('电量识别失败，按自然电量上限 %s 处理', self.DEFAULT_NATURAL_CHARGE, exc_info=True)
            self.charge_power = self.DEFAULT_NATURAL_CHARGE
            self.backup_charge_power = 0
            self.ether_battery = 0
            return self.round_success('电量识别失败，按上限估')

        self.charge_power = battery
        self.backup_charge_power = backup
        self.ether_battery = ether
        log.info('剩余电量 %s 储蓄电量 %s 以太电池 %s', battery, backup, ether)
        return self.round_success()

    @node_from(from_name='读取电量')
    @operation_node(name='读取材料缺口', node_max_retry_times=1)
    def read_material_gap(self) -> OperationRoundResult:
        """读养成方案详情页右侧的材料预览栏。

        **目前只读不排计划**：这一栏只有图标和「已有/所需」数字，没有材料名字，
        对不到材料表就换不算出次数。要真正按材料排计划，得先补其中一样：
        材料图标模板，或者「养成指南 → 养成材料计算」那页的建档（明细表有名字）。
        """
        op = ReadMaterialCalc(self.ctx, goto_first=False, allow_dialog_fallback=False)
        result = op.execute()
        if not result.is_success:
            # 读不到不算致命，照常往下走，只是少了材料这一档
            log.warning('材料缺口没读出来：%s', result.status)
            return self.round_success('材料缺口没读到')

        gap = op.data
        if not isinstance(gap, MaterialGapSnapshot):
            return self.round_success('材料缺口没读到')
        self.material_gap = gap
        for line in gap.summary_lines():
            log.info(line)
        return self.round_success()

    @node_from(from_name='读取材料缺口')
    @node_from(from_name='读取电量')
    @operation_node(name='生成并写入计划')
    def build_and_write(self) -> OperationRoundResult:
        """清单 → 计划项 → 写进内置体力计划配置。"""
        is_agent_stage = self.config.goal_mode in (GOAL_MODE_AGENT_STAGE, GOAL_MODE_BOTH)
        if is_agent_stage and self.snapshot is None:
            return self.round_fail('还没读到特训目标清单')
        if not is_agent_stage and self.material_gap is None:
            return self.round_fail('材料缺口也没读到，没有可排的东西')

        available = ChargeAvailability(
            natural=self.charge_power,
            backup=self.backup_charge_power,
            ether_battery=self.ether_battery,
        )

        mapper = PlanMapper(
            self.ctx,
            default_plan_times=self.config.default_plan_times,
            include_drive_disc=self.config.include_drive_disc,
        )
        snapshot = PlanSnapshot.from_reader_snapshot(self.snapshot) if self.snapshot is not None else PlanSnapshot()
        build_result = mapper.build(snapshot, available)

        if self.material_gap is not None:
            rows = self.material_gap.plan_row_list()
            if not rows:
                build_result.warnings.append(
                    f'材料预览栏只有图标没有材料名（{self.material_gap.unnamed_count} 条），'
                    '排不了材料计划。要按材料排，先补材料图标模板，或给「养成指南 → 养成材料计算」页建档'
                )

        writer = PlanWriter(self.ctx)
        write_result = writer.write(
            build_result,
            instance_idx=self.ctx.current_instance_idx,
            group_id=self.ctx.current_group_id,
            dry_run=self.config.dry_run,
            remove_keys=self.config.last_generated_keys,
        )

        for line in build_result.summary_lines():
            log.info(line)
        for warning in build_result.warnings:
            log.warning(warning)

        add_count = sum(1 for change in write_result.changes if change.kind == 'add')
        update_count = sum(1 for change in write_result.changes if change.kind == 'update')
        keep_count = sum(1 for change in write_result.changes if change.kind == 'keep')
        for change in write_result.changes:
            if change.kind != 'keep':
                log.info(
                    '[%s] %s 次数 %s 卡数 %s %s',
                    change.kind, change.key, change.plan_times, change.card_num, change.detail,
                )
        for warning in write_result.warnings:
            log.warning(warning)
        log.info(
            '写入结果：%s，计划项 %s 条（新增 %s / 更新 %s / 保持 %s）',
            '预览（没写入）' if self.config.dry_run else '已写入',
            len(write_result.plan_list), add_count, update_count, keep_count,
        )

        if not self.config.dry_run and write_result.written:
            self.config.last_generated_keys = [content_key(item) for item in build_result.plan_list]

        return self.round_success(f'计划项 {len(write_result.plan_list)} 条')

    @node_from(from_name='生成并写入计划')
    @node_from(from_name='生成并写入计划', success=False)
    @node_notify(when=NotifyTiming.CURRENT_DONE, detail=True)
    @operation_node(name='返回大世界')
    def back_to_world(self) -> OperationRoundResult:
        """回大世界，结束。"""
        op = BackToNormalWorld(self.ctx)
        return self.round_by_op_result(op.execute())
