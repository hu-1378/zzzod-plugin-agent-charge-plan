"""特训目标体力计划的应用本体。

两种运行模式（配置 `run_mode`）：

- `plan_only`（默认）：进游戏 → 到「快捷手册-训练-特训目标」页 → 读游戏算好的养成缺口 →
  翻译成内置体力计划的计划项写进去。执行仍然交给内置「体力计划」应用。
- `execute`：自己边跑边算边刷。读完清单和电量后一轮刷一个副本，刷完记账，
  下一轮重新按 ①特训目标缺口 → ②材料库存保持 → ③保底任务 的优先级挑活。

不改主仓任何代码：写配置走 `run_context.get_config('charge_plan', ...)` 拿同一个实例，
刷副本走主仓那几个公开的副本 Operation。

三种目标模式（配置 `goal_mode`，只在 `plan_only` 下起作用）：

- `agent_stage`：按角色养成缺口排计划
- `material_floor`：按材料库存下限排计划（缺口从「养成材料计算」的明细表读，要配 `material_goals`）
- `both`：两者都做
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from one_dragon.base.operation.application.application_const import DEFAULT_GROUP_ID
from one_dragon.base.operation.operation_edge import node_from
from one_dragon.base.operation.operation_node import operation_node
from one_dragon.base.operation.operation_notify import NotifyTiming, node_notify
from one_dragon.utils.log_utils import log
from zzz_od.application.zzz_application import ZApplication
from zzz_od.operation.back_to_normal_world import BackToNormalWorld

from . import agent_charge_plan_const
from .agent_charge_plan_config import (
    GOAL_MODE_AGENT_STAGE,
    GOAL_MODE_BOTH,
    GOAL_MODE_MATERIAL_FLOOR,
    RUN_MODE_AUDIT,
    RUN_MODE_EXECUTE,
    AgentChargePlanConfig,
)
from .operation.agent_plan_page import (
    AgentPlanSnapshot,
    agent_name_similar,
    read_agent_name,
)
from .operation.audit_agents import AuditAgents, AuditSummary
from .operation.change_agent_plan_target import ChangeAgentPlanTarget
from .operation.execute_charge_plan import ExecuteChargePlan, ExecuteReport
from .operation.goto_agent_plan import GotoAgentPlan
from .operation.read_agent_plan_list import ReadAgentPlanList
from .operation.read_charge_power import read_charge
from .operation.read_material_calc import MaterialGapSnapshot, ReadMaterialCalc
from .operation.scan_agent_list import AgentNameList, ScanAgentList
from .planner.charge_budget import ChargeAvailability
from .planner.material_goal import (
    MaterialGoal,
    MaterialGoalPlanner,
    MaterialOwned,
    normalize_material_name,
)
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

    def __init__(
        self,
        ctx: ZContext,
        instance_idx: int | None = None,
        group_id: str | None = None,
    ) -> None:
        """建应用。

        Args:
            ctx: 一条龙上下文
            instance_idx: 账号下标。不传就用当前账号（工厂调用时框架会传）
            group_id: 应用组。不传就用默认组（工厂调用时框架会传）

        注意：`run_context.current_group_id` 是在 `create_application` **之后**才赋值的，
        在构造函数里读它拿不到，所以这里必须由工厂把两个值传进来，或者用默认值兜底。
        """
        ZApplication.__init__(
            self,
            ctx=ctx,
            app_id=agent_charge_plan_const.APP_ID,
            op_name=agent_charge_plan_const.APP_NAME,
        )
        self.instance_idx: int = (
            instance_idx if instance_idx is not None else self.ctx.current_instance_idx
        )
        """账号下标，读写配置时用"""
        self.group_id: str = group_id if group_id else DEFAULT_GROUP_ID
        """应用组，读写配置时用"""
        self.config: AgentChargePlanConfig = self.ctx.run_context.get_config(
            app_id=agent_charge_plan_const.APP_ID,
            instance_idx=self.instance_idx,
            group_id=self.group_id,
        )
        #: 读到的特训目标清单
        self.snapshot_list: list[AgentPlanSnapshot] = []
        """读到的特训目标清单。批量模式下每个角色一份"""
        #: 读到的材料缺口
        self.material_gap: MaterialGapSnapshot | None = None
        #: 当前剩余电量
        self.charge_power: int = 0
        #: 储蓄电量
        self.backup_charge_power: int = 0
        #: 以太电池颗数
        self.ether_battery: int = 0
        #: 边刷边算模式下的可用电量（进「循环刷本」前读一次）
        self.execute_availability: ChargeAvailability = ChargeAvailability()

    @operation_node(name='开始', is_start_node=True)
    def start(self) -> OperationRoundResult:
        """先看配置，决定后面走哪条路。"""
        log.info(
            '特训目标体力计划开始：运行模式 %s，目标模式 %s，预览模式 %s，指定代理人「%s」，批量 %d 个，扫描模式 %s',
            self.config.run_mode,
            self.config.goal_mode,
            self.config.dry_run,
            self.config.target_agent_name or '按游戏当前设置',
            len(self.config.batch_agent_names),
            self.config.scan_only,
        )
        if self.config.scan_only:
            return self.round_success('扫描代理人名单')
        if self.config.run_mode == RUN_MODE_EXECUTE:
            return self.round_success('边刷边算')
        if self.config.run_mode == RUN_MODE_AUDIT:
            return self.round_success('全角色核对')
        if self.config.goal_mode == GOAL_MODE_MATERIAL_FLOOR:
            return self.round_success('只处理材料库存')
        return self.round_success('读取特训目标')

    @node_from(from_name='开始', status='扫描代理人名单')
    @operation_node(name='扫描代理人名单', node_max_retry_times=2)
    def scan_agents(self) -> OperationRoundResult:
        """把游戏里「更换特训目标」弹窗的全部代理人名字扫出来，写进配置。

        只读不点，也不会动当前的特训目标；扫完把名单存进 `known_agent_list`，
        之后在 Web 配置页勾选要养的角色。
        """
        op = ScanAgentList(self.ctx, goto_first=True)
        result = op.execute()
        if not result.success:
            return self.round_by_op_result(result)

        data = result.data
        if not isinstance(data, AgentNameList):
            return self.round_fail('扫描没有返回名单')
        for warning in data.warnings:
            log.warning(warning)

        name_list = list(data.name_list)
        self.config.known_agent_list = name_list
        log.info('扫到 %d 个代理人：%s', len(name_list), '、'.join(name_list))
        return self.round_success(f'扫到 {len(name_list)} 个代理人')

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
        if agent_name_similar(current, target):
            return self.round_success(f'已经是 {target}')

        op = ChangeAgentPlanTarget(self.ctx, target_agent_name=target, goto_first=False)
        result = op.execute()
        return self.round_by_op_result(result, success_wait=1)

    @node_from(from_name='对齐特训目标')
    @node_from(from_name='对齐特训目标', status='沿用游戏当前目标')
    @operation_node(name='读取清单', node_max_retry_times=1)
    def read_plan_list(self) -> OperationRoundResult:
        """读特训目标页的条目：分组、条目名、电量、进度。

        批量名单（`batch_agent_names`）非空时逐个切过去读一遍，读完把特训目标恢复成
        进来时的那个。游戏只能设一个特训目标，所以这是唯一的批量办法。
        """
        batch = self.config.batch_agent_names
        if not batch:
            return self._read_one_agent()
        return self._read_batch_agents(batch)

    def _read_one_agent(self) -> OperationRoundResult:
        """读当前特训目标这一个角色的清单。"""
        op = ReadAgentPlanList(self.ctx, goto_first=False)
        result = op.execute()
        if not result.success:
            return self.round_by_op_result(result)

        snapshot = result.data
        if not isinstance(snapshot, AgentPlanSnapshot):
            return self.round_fail('清单读取没有返回数据')
        self.snapshot_list = [snapshot]
        self._log_snapshot(snapshot)
        return self.round_success()

    def _read_batch_agents(self, batch: list[str]) -> OperationRoundResult:
        """逐个切换特训目标并读清单，读完恢复原目标。"""
        original_name = read_agent_name(self.ctx, self.last_screenshot)
        log.info('批量模式：要读 %d 个角色，当前特训目标是「%s」', len(batch), original_name or '读不到')

        self.snapshot_list = []
        failed_name_list: list[str] = []
        for idx, name in enumerate(batch, start=1):
            log.info('批量读清单 %d/%d：%s', idx, len(batch), name)

            change_op = ChangeAgentPlanTarget(self.ctx, target_agent_name=name, goto_first=False)
            change_result = change_op.execute()
            if not change_result.success:
                log.warning('切到「%s」失败：%s', name, change_result.status)
                failed_name_list.append(name)
                continue

            read_op = ReadAgentPlanList(self.ctx, goto_first=False)
            read_result = read_op.execute()
            if not read_result.success or not isinstance(read_result.data, AgentPlanSnapshot):
                log.warning('读「%s」的清单失败：%s', name, read_result.status)
                failed_name_list.append(name)
                continue

            self.snapshot_list.append(read_result.data)
            self._log_snapshot(read_result.data)

        # 无论成功几个，都把特训目标恢复成进来时的那个，别把用户的设置改坏
        if original_name:
            restore_op = ChangeAgentPlanTarget(
                self.ctx, target_agent_name=original_name, goto_first=False
            )
            if restore_op.execute().success:
                log.info('特训目标已恢复为「%s」', original_name)
            else:
                log.warning('特训目标没能恢复成「%s」，你需要在游戏里手动改回来', original_name)

        if failed_name_list:
            log.warning('这些角色没读到：%s', '、'.join(failed_name_list))
        if not self.snapshot_list:
            return self.round_fail('批量读取一个角色都没成功')
        return self.round_success(
            f'读到 {len(self.snapshot_list)}/{len(batch)} 个角色'
        )

    def _log_snapshot(self, snapshot: AgentPlanSnapshot) -> None:
        """把一份清单打进日志。"""
        log.info('特训目标：%s，条目 %s 个', snapshot.agent_name, len(snapshot.item_list))
        for item in snapshot.item_list:
            log.info(
                '  %s / %s / 电量 %s / 进度 %s',
                item.group_name, item.name, item.charge_text, item.progress_text,
            )

    @node_from(from_name='读取清单')
    @node_from(from_name='开始', status='只处理材料库存')
    @operation_node(name='读取电量')
    def read_charge(self) -> OperationRoundResult:
        """读快捷手册资源栏的电量、储蓄电量、以太电池。

        特训目标页也在快捷手册里，资源栏位置一样，所以复用内置的「电量检测」流水线。
        """
        try:
            battery, backup, ether = read_charge(self.ctx, self.last_screenshot)
        except Exception:
            log.error('电量识别失败，按自然电量上限 %s 处理', self.DEFAULT_NATURAL_CHARGE, exc_info=True)
            self.charge_power = self.DEFAULT_NATURAL_CHARGE
            self.backup_charge_power = 0
            self.ether_battery = 0
            return self.round_success('电量识别失败，按上限估')

        if battery <= 0:
            # 内置的 _parse_battery_charge_ocr 在全字段识别失败时不抛异常，而是返回 (0, 0, 0)，
            # 直接拿 0 去算预算会把所有条目压成「今天不够跑一次」，所以这里也要降级。
            log.warning(
                '电量识别结果是 0（多半是识别失败），按自然电量上限 %s 处理',
                self.DEFAULT_NATURAL_CHARGE,
            )
            battery = self.DEFAULT_NATURAL_CHARGE

        self.charge_power = battery
        self.backup_charge_power = backup
        self.ether_battery = ether
        log.info('剩余电量 %s 储蓄电量 %s 以太电池 %s', battery, backup, ether)
        return self.round_success()

    @node_from(from_name='读取电量')
    @operation_node(name='读取材料缺口', node_max_retry_times=1)
    def read_material_gap(self) -> OperationRoundResult:
        """读材料缺口：先读养成方案详情右侧的材料预览栏，读空再走「养成指南 → 养成材料计算」弹窗。

        预览栏只有图标和数字、没有材料名，拿到也没法换算成刷本次数；弹窗里的四列明细表才有名字。
        所以配了材料目标（`material_goals`）时，真正有用的是弹窗那条路。
        """
        # 配了材料目标时直奔「养成材料计算」弹窗：材料预览栏只有图标和数字、没有材料名，
        # 读出来也换不算出次数。没配目标就省掉这几次导航，只读预览栏。
        prefer_dialog = bool(self.config.material_goals)
        op = ReadMaterialCalc(
            self.ctx,
            goto_first=False,
            allow_dialog_fallback=True,
            prefer_dialog=prefer_dialog,
        )
        result = op.execute()
        if not result.success:
            # 读不到不算致命，照常往下走，只是少了材料这一档
            log.warning('材料缺口没读出来：%s', result.status)
            return self.round_success('材料缺口没读到')

        gap = result.data
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
        if is_agent_stage and not self.snapshot_list:
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
        # 批量模式下会有多份清单，全部摊平交给映射层；同一个副本会被内容键去重
        snapshot = PlanSnapshot()
        for one in self.snapshot_list:
            snapshot.entries.extend(PlanSnapshot.from_reader_snapshot(one).entries)
        if len(self.snapshot_list) > 1:
            log.info('合并 %d 个角色的清单，共 %d 条原始条目', len(self.snapshot_list), len(snapshot.entries))

        # 材料库存目标：有配置就按缺口算出次数，和角色养成的条目合并后一起过电量预算
        material_snapshot = self._build_material_snapshot()
        if material_snapshot is not None:
            snapshot.entries.extend(material_snapshot.entries)

        build_result = mapper.build(snapshot, available)

        writer = PlanWriter(self.ctx)
        write_result = writer.write(
            build_result,
            instance_idx=self.instance_idx,
            group_id=self.group_id,
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

    @node_from(from_name='循环刷本')
    @node_from(from_name='循环刷本', success=False)
    @node_from(from_name='全角色核对')
    @node_from(from_name='全角色核对', success=False)
    @node_notify(when=NotifyTiming.CURRENT_DONE, detail=True)
    @operation_node(name='返回大世界')
    def back_to_world(self) -> OperationRoundResult:
        """回大世界，结束。"""
        op = BackToNormalWorld(self.ctx)
        return self.round_by_op_result(op.execute())

    # ── 边刷边算（run_mode = execute）─────────────────────────────────────
    @node_from(from_name='开始', status='边刷边算')
    @operation_node(name='执行前准备')
    def prepare_execute(self) -> OperationRoundResult:
        """先进快捷手册，再读一次资源栏的电量，作为这一轮的「总预算」。"""
        goto_result = GotoAgentPlan(self.ctx).execute()
        if not goto_result.success:
            return self.round_by_op_result(goto_result)

        # 节点开始前那张截图是导航之前的画面（可能还停在大世界），资源栏不一定在，
        # 所以走完导航必须重新截一张再去读电量。
        self.screenshot()
        self.execute_availability = self._read_charge_availability()
        log.info(
            '边刷边算开始：可用电量 %s（自然 %s + 储蓄 %s + 以太电池 %s 颗）',
            self.execute_availability.total,
            self.execute_availability.natural,
            self.execute_availability.backup,
            self.execute_availability.ether_battery,
        )
        return self.round_success(status=f'可用电量 {self.execute_availability.total}')

    @node_from(from_name='执行前准备')
    @operation_node(name='循环刷本', node_max_retry_times=1)
    def run_execute(self) -> OperationRoundResult:
        """边刷边算的主体：挑任务 → 传送 → 刷 → 记账，直到没活干或者电量用完。"""
        op = ExecuteChargePlan(
            self.ctx,
            config=self.config,
            availability=self.execute_availability,
        )
        result = op.execute()
        if isinstance(result.data, ExecuteReport):
            for line in result.data.summary_lines():
                log.info(line)
        return self.round_by_op_result(result)

    # ── 全角色核对（run_mode = audit）─────────────────────────────────────
    @node_from(from_name='开始', status='全角色核对')
    @operation_node(name='审计前准备')
    def prepare_audit(self) -> OperationRoundResult:
        """先进快捷手册，再读一次电量（算计划要用它做预算）。"""
        goto_result = GotoAgentPlan(self.ctx).execute()
        if not goto_result.success:
            return self.round_by_op_result(goto_result)

        # 导完航重新截一张：节点开头那张还停在大世界，资源栏不在屏幕上
        self.screenshot()
        self.execute_availability = self._read_charge_availability()
        log.info(
            '全角色核对开始：名单 %s 个，可用电量 %s',
            len(self.config.batch_agent_names) or len(self.config.known_agent_list),
            self.execute_availability.total,
        )
        return self.round_success(status=f'可用电量 {self.execute_availability.total}')

    @node_from(from_name='审计前准备')
    @operation_node(name='全角色核对', node_max_retry_times=1)
    def run_audit(self) -> OperationRoundResult:
        """全角色核对的主体：逐个切角色、读清单、算计划、落盘，最后写汇总。"""
        op = AuditAgents(
            self.ctx,
            config=self.config,
            availability=self.execute_availability,
            instance_idx=self.instance_idx,
            group_id=self.group_id,
        )
        result = op.execute()
        if isinstance(result.data, AuditSummary):
            for line in result.data.summary_lines():
                log.info(line)
        return self.round_by_op_result(result)

    def _read_charge_availability(self) -> ChargeAvailability:
        """读一次快捷手册资源栏的电量，打包成「这一轮能用多少电量」。

        为什么不直接调 `read_charge` 节点：那是 `plan_only` 链路上的节点，
        节点方法也不能在别处当普通函数调。这里只多写几行流水线调用，
        解析仍然复用主仓那个静态方法，不重复写一份算法。

        Returns:
            可用电量；识别失败时按自然电量上限保守估
        """
        battery, backup, ether = read_charge(self.ctx, self.last_screenshot)

        if battery <= 0:
            log.warning(
                '电量识别结果是 0（多半是识别失败），按自然电量上限 %s 处理',
                self.DEFAULT_NATURAL_CHARGE,
            )
            battery = self.DEFAULT_NATURAL_CHARGE

        return ChargeAvailability(natural=battery, backup=backup, ether_battery=ether)

    def _build_material_snapshot(self) -> PlanSnapshot | None:
        """把「材料保持到 N」换算成计划条目。

        需要两样东西：配置里的目标列表，和页面上读到的「已有数量」。
        两个缺一个就只给提示、不生成条目（不让半截数据把计划写歪）。

        Returns:
            材料目标换算出来的清单；没配置或读不到就返回 None
        """
        goal_row_list = self.config.material_goals
        if not goal_row_list:
            return None

        if self.material_gap is None:
            log.warning('配了 %d 条材料目标，但这次没读到材料缺口，跳过材料排计划', len(goal_row_list))
            return None

        # 两边都要归一化再对：页面给的是「「仓鼠笼」访问器」，配置里可能写「仓鼠笼访问器」，
        # 直接拿原名做键会查不到，然后静默按「没读到已有数量」只排 1 次。
        owned_map: dict[str, int] = {
            normalize_material_name(item.material_name): item.owned
            for item in self.material_gap.named_item_list
            if item.owned is not None
        }
        if not owned_map:
            log.warning(
                '材料缺口里没有带材料名的条目（材料预览栏只有图标，看不懂是哪一种），跳过材料排计划',
            )
            return None

        planner = MaterialGoalPlanner(default_plan_times=self.config.default_plan_times)
        owned_list: list[MaterialOwned] = []
        for goal_row in goal_row_list:
            goal = MaterialGoal.from_row(goal_row)
            owned_list.append(
                MaterialOwned(
                    goal=goal,
                    owned=owned_map.get(normalize_material_name(goal.material_name)),
                )
            )

        snapshot, warnings = planner.build_snapshot(owned_list)
        for warning in warnings:
            log.warning(warning)
        log.info('材料目标 %d 条，换算成 %d 条计划条目', len(goal_row_list), len(snapshot.entries))
        return snapshot
