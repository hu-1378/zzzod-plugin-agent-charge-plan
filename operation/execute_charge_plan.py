"""边跑边算边刷：每一轮只刷一个副本，刷完记账，下一轮重新判断。

三阶段优先级（每一轮都是先看 ① 再看 ② 再看 ③）：

1. 特训目标缺口：当前角色的清单里还有没满足的条目
2. 材料库存保持：`material_goals` 里还有缺口的材料
3. 保底任务：①② 都没活干、电量还有，就刷配置里的 `fallback_plan`

三者都没活干时：批量名单里还有下一个角色就切过去重读清单，没有了就结束。

两条记账规矩：

- 刷完不重读页面。进度按「单次产出」自己累加，电量按副本消耗直接扣。
- 每切一个角色才重读一次清单校准，防止估值和游戏里的真实进度漂太远。

为什么不每个子 Operation 挂 `@node_notify`：一个角色要刷几十次，每个子流程都推一条
会把通知刷爆。通知只挂在「换角色」这一个节点上，加上应用层的「开始」「返回大世界」。
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Any

# planner 是 operation 的兄弟包，相对导入要写 `..planner`，但 ruff 的 TID252 禁掉了
# 从父包相对导入，所以这里用绝对名。插件运行时 `plugins/` 在 sys.path 里，能直接导入。
from agent_charge_plan.planner.charge_budget import (
    BudgetRequest,
    ChargeAvailability,
    ChargeBudget,
    parse_charge_text,
)
from agent_charge_plan.planner.material_goal import (
    MaterialGoal,
    MaterialGoalPlanner,
    normalize_material_name,
)
from agent_charge_plan.planner.plan_mapper import (
    CATEGORY_AREA_PATROL,
    CATEGORY_COMBAT_SIMULATION,
    CATEGORY_EXPERT_CHALLENGE,
    CATEGORY_NOTORIOUS_HUNT,
    GROUP_RANK,
    TAB_NAME,
    PlanMapper,
    normalize_name,
)
from agent_charge_plan.planner.plan_models import PlanEntry, PlanSnapshot
from one_dragon.base.operation.operation_edge import node_from
from one_dragon.base.operation.operation_node import operation_node
from one_dragon.base.operation.operation_notify import NotifyTiming, node_notify
from one_dragon.base.operation.operation_round_result import OperationRoundResult
from one_dragon.utils.log_utils import log
from zzz_od.application.charge_plan.charge_plan_config import (
    CardNumEnum,
    ChargePlanItem,
)
from zzz_od.operation.compendium.area_patrol import AreaPatrol
from zzz_od.operation.compendium.combat_simulation import CombatSimulation
from zzz_od.operation.compendium.expert_challenge import ExpertChallenge
from zzz_od.operation.compendium.notorious_hunt import NotoriousHunt
from zzz_od.operation.compendium.tp_by_compendium import TransportByCompendium
from zzz_od.operation.zzz_operation import ZOperation

from . import agent_plan_page
from .agent_plan_page import AgentPlanSnapshot
from .change_agent_plan_target import ChangeAgentPlanTarget
from .goto_agent_plan import GotoAgentPlan
from .read_agent_plan_list import ReadAgentPlanList
from .read_material_calc import MaterialGapSnapshot, ReadMaterialCalc
from .restore_agent_target import restore_agent_target

if TYPE_CHECKING:
    from agent_charge_plan.agent_charge_plan_config import AgentChargePlanConfig
    from zzz_od.context.zzz_context import ZContext


# ============================= 阶段与状态 =============================

#: 阶段 ①：特训目标缺口
STAGE_AGENT_PLAN: str = '特训目标缺口'
#: 阶段 ②：材料库存保持
STAGE_MATERIAL: str = '材料库存保持'
#: 阶段 ③：保底任务
STAGE_FALLBACK: str = '保底任务'

#: 这一轮没活干（交给「换下一个角色」决定要不要切角色）
STATUS_NO_TASK: str = '这一轮没活干'
#: 电量用完了，整轮结束
STATUS_CHARGE_EMPTY: str = '电量用完，结束'
#: 名单跑完了，整轮结束
STATUS_LIST_DONE: str = '名单跑完，结束'
#: 已经切到下一个角色，需要重读清单
STATUS_AGENT_SWITCHED: str = '已切换到下一个角色'
#: 收尾：恢复原特训目标
STATUS_RESTORED: str = '已恢复原特训目标'

#: 四个副本 Operation 报「电量不足」时用的都是同一个常量，这里直接引用，别手抄字符串
STATUS_CHARGE_NOT_ENOUGH: str = CombatSimulation.STATUS_CHARGE_NOT_ENOUGH
#: 恶名狩猎这周剩下的次数用完了，本轮刷不了，换一条
STATUS_HUNT_BLOCKED: str = NotoriousHunt.STATUS_BLOCKED_BY_LEFT_TIMES

#: ① 里单次刷本能推进多少进度。游戏没给「一次给多少」，只能按 1 估，属保守方向
#: （宁可多刷几次，也不会凭空认为条目已经满足）。切换角色重读清单会把账校准回来。
ASSUMED_AGENT_PLAN_GAIN: int = 1

#: 保底任务在记账里的固定键
FALLBACK_KEY: str = '保底任务'

#: 总结里最多列几次刷本明细。刷几十次时全列出来通知里看不动
SUMMARY_TASK_LOG_LIMIT: int = 8

#: 能自己跑的分类（和内置体力计划的分流一致：合成电池不在其中）
RUNNABLE_CATEGORY_LIST: tuple[str, ...] = (
    CATEGORY_COMBAT_SIMULATION,
    CATEGORY_AREA_PATROL,
    CATEGORY_EXPERT_CHALLENGE,
    CATEGORY_NOTORIOUS_HUNT,
)


# ============================= 数据结构 =============================


@dataclass
class ChargeTask:
    """这一轮要刷的一条任务。"""

    stage: str
    """来自哪个阶段，就是 STAGE_* 之一"""

    label: str
    """给人看的名字"""

    plan_item: ChargePlanItem
    """交给副本 Operation 的计划项，plan_times 固定为 1（一轮只刷一次）"""

    charge_per_run: int
    """这一轮大概要花多少电量"""

    key: str
    """记账用的键：条目键 / 材料键 / 保底任务固定键"""

    material_name: str = ''
    """材料阶段用：这一条对应哪种材料。记「刷了几次」按材料名存"""

    @property
    def description(self) -> str:
        """日志里一行写完的说明。"""
        return f'[{self.stage}] {self.label} → {self.plan_item.category_name} 约 {self.charge_per_run} 电量'


@dataclass
class RunBook:
    """本地记账本：只靠「刷了几次」推进度，不重读游戏页面。"""

    remaining_charge: int
    """还能花多少电量。开局是「自然 + 储蓄 + 以太电池×60」，每刷一次扣一次"""

    agent_gain: int = ASSUMED_AGENT_PLAN_GAIN
    """① 里单次刷本推进多少进度"""

    agent_snapshot: PlanSnapshot | None = None
    """当前角色的清单。进度会被就地改写，别拿它跟页面上的原文比"""

    agent_ran_times: dict[str, int] = field(default_factory=dict)
    """条目键 → 本轮已经刷了几次"""

    agent_base_progress: dict[str, tuple[int, int]] = field(default_factory=dict)
    """条目键 → 刚读到清单时的 (已有, 需要)。累加进度要基于这个原始值"""

    material_owned: dict[str, int | None] = field(default_factory=dict)
    """材料名（归一化后）→ 读到的已有数量"""

    material_ran_times: dict[str, int] = field(default_factory=dict)
    """材料名（归一化后）→ 本轮已经刷了几次"""

    fallback_ran_times: int = 0
    """保底任务刷了几次"""

    skipped_keys: dict[str, str] = field(default_factory=dict)
    """本轮放弃的任务键 → 原因（认不出副本 / 刷失败 / 本期刷不了）"""

    def load_agent(self, snapshot: AgentPlanSnapshot) -> PlanSnapshot:
        """换一个角色：把读取层的清单接下来，记账清零。

        Args:
            snapshot: 读取层读到的清单

        Returns:
            转成计划层的清单，后面挑任务都用这一份
        """
        plan_snapshot = PlanSnapshot.from_reader_snapshot(snapshot)
        self.agent_snapshot = plan_snapshot
        self.agent_ran_times = {}
        self.agent_base_progress = {}
        for entry in plan_snapshot.entries:
            progress = entry.progress
            if progress is not None:
                self.agent_base_progress[entry_key(entry)] = progress
        return plan_snapshot

    def load_material_gap(self, gap: MaterialGapSnapshot) -> None:
        """记下每种材料「现在大概有多少」。

        Args:
            gap: 材料缺口读数
        """
        self.material_owned = {}
        self.material_ran_times = {}
        for item in gap.named_item_list:
            if item.owned is None:
                continue
            self.material_owned[normalize_material_name(item.material_name)] = item.owned

    def estimate_entry_missing(self, entry: PlanEntry) -> int | None:
        """按记账估算这条条目还差多少。没有进度信息时返回 None。"""
        base = self.agent_base_progress.get(entry_key(entry))
        if base is None:
            return None
        owned, total = base
        owned = min(total, owned + self.agent_ran_times.get(entry_key(entry), 0) * self.agent_gain)
        return max(0, total - owned)

    def record_run(self, task: ChargeTask) -> None:
        """记一次「刷成功」。

        Args:
            task: 刚刷完的任务
        """
        if task.key == FALLBACK_KEY:
            self.fallback_ran_times += 1
        elif task.stage == STAGE_MATERIAL:
            # 材料按「归一化后的材料名」记账，和 _pick_material 里读的是同一把键
            name = normalize_material_name(task.material_name or task.label)
            self.material_ran_times[name] = self.material_ran_times.get(name, 0) + 1
        else:
            self.agent_ran_times[task.key] = self.agent_ran_times.get(task.key, 0) + 1
            self._rewrite_entry_progress(task.key)

    def spend(self, charge: int) -> None:
        """扣电量。不会扣成负数。

        Args:
            charge: 这次花掉的电量
        """
        self.remaining_charge = max(0, self.remaining_charge - max(0, charge))

    def record_skip(self, task: ChargeTask, reason: str) -> None:
        """记一条「这条以后别再挑了」。

        Args:
            task: 被放弃的任务
            reason: 放弃的原因
        """
        self.skipped_keys[task.key] = reason

    def _rewrite_entry_progress(self, key: str) -> None:
        """按「原始进度 + 已刷次数 × 单次推进」改写条目上的进度文本。"""
        snapshot = self.agent_snapshot
        if snapshot is None:
            return
        base = self.agent_base_progress.get(key)
        if base is None:
            return
        owned, total = base
        owned = min(total, owned + self.agent_ran_times.get(key, 0) * self.agent_gain)
        for entry in snapshot.entries:
            if entry_key(entry) == key:
                entry.progress_text = f'{owned}/{total}'
                return


@dataclass
class ExecuteReport:
    """一整轮执行的结果，给上层打日志用。"""

    run_count: int = 0
    """成功刷完的次数"""

    fail_count: int = 0
    """失败次数"""

    remaining_charge: int = 0
    """结束时还剩多少电量（估的）"""

    agent_name_list: list[str] = field(default_factory=list)
    """处理过的角色"""

    task_log: list[str] = field(default_factory=list)
    """每次刷本的一行记录"""

    skipped_list: list[str] = field(default_factory=list)
    """被放弃的任务和原因"""

    def summary_lines(self) -> list[str]:
        """拼成几行给人看的总结。

        每次刷本都打一行的话，刷几十次就是几十行，通知里看不动，所以只留最后几条。
        """
        lines = [
            f'执行结束：刷了 {self.run_count} 次，失败 {self.fail_count} 次，'
            f'剩余电量约 {self.remaining_charge}',
        ]
        if self.agent_name_list:
            lines.append(f'处理过的角色：{"、".join(self.agent_name_list)}')
        if len(self.task_log) > SUMMARY_TASK_LOG_LIMIT:
            lines.append(f'最近 {SUMMARY_TASK_LOG_LIMIT} 次（共 {len(self.task_log)} 次）：')
            lines.extend(self.task_log[-SUMMARY_TASK_LOG_LIMIT:])
        else:
            lines.extend(self.task_log)
        if self.skipped_list:
            lines.append(f'放弃的任务 {len(self.skipped_list)} 条：')
            lines.extend(f'  {item}' for item in self.skipped_list)
        return lines


# ============================= 保底任务解析 =============================


def parse_fallback_plan(raw: dict[str, Any] | None) -> ChargePlanItem | None:
    """把配置里的保底任务字典变成计划项。

    只认 `ChargePlanItem` 里跟「打哪个副本」有关的几个字段，别的字段用默认值。
    `plan_times` 固定成 1：这是「一轮刷一次」的语义，次数由循环自己控。

    Args:
        raw: 配置里的 `fallback_plan`，空字典表示不刷保底

    Returns:
        计划项；空字典、缺分类、分类跑不了时返回 None
    """
    if not isinstance(raw, dict) or not raw:
        return None

    category_name = _as_text(raw.get('category_name'))
    if category_name == '':
        log.warning('保底任务没写 category_name，忽略')
        return None
    if category_name not in RUNNABLE_CATEGORY_LIST:
        log.warning(
            '保底任务的分类「%s」不支持，只能填 %s，忽略',
            category_name,
            '、'.join(RUNNABLE_CATEGORY_LIST),
        )
        return None

    mission_type_name = _as_text(raw.get('mission_type_name'))
    if mission_type_name == '':
        log.warning('保底任务没写 mission_type_name，忽略')
        return None

    card_num = _as_text(raw.get('card_num')) or CardNumEnum.DEFAULT.value.value
    level = _as_text(raw.get('level')) or '默认等级'
    auto_battle_config = _as_text(raw.get('auto_battle_config')) or '全配队通用'
    mission_name: str | None = _as_text(raw.get('mission_name')) or None

    return ChargePlanItem(
        tab_name=TAB_NAME,
        category_name=category_name,
        mission_type_name=mission_type_name,
        mission_name=mission_name,
        level=level,
        auto_battle_config=auto_battle_config,
        plan_times=1,
        card_num=card_num,
        notorious_hunt_buff_num=_as_positive_int(raw.get('notorious_hunt_buff_num'), 1),
    )


def _as_text(value: Any) -> str:
    """把配置里的值读成去掉首尾空格的字符串。"""
    if isinstance(value, str):
        return value.strip()
    return ''


def _as_positive_int(value: Any, default: int) -> int:
    """把配置里的值读成正整数；读不出来就用默认值。"""
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    return number if number > 0 else default


def entry_key(entry: PlanEntry) -> str:
    """条目的记账键：分组 + 条目名。同一个副本在不同分组下算两条。"""
    return f'特训目标:{entry.group_name}|{entry.entry_name}'


def material_key(material_name: str) -> str:
    """材料目标的记账键（材料名归一化后再拼）。"""
    return f'材料:{normalize_material_name(material_name)}'


# ============================= 挑任务（纯逻辑） =============================


class ChargeTaskPicker:
    """按 ①②③ 优先级挑出这一轮要刷的那一条。

    全是纯逻辑：不截图、不点屏幕，方便单测直接喂假数据。
    """

    def __init__(
        self,
        mapper: PlanMapper,
        book: RunBook,
        material_planner: MaterialGoalPlanner | None = None,
        material_goal_list: list[MaterialGoal] | None = None,
        fallback_item: ChargePlanItem | None = None,
    ) -> None:
        """
        构造函数。

        Args:
            mapper: 条目 → 副本三元组的映射表
            book: 本地记账本
            material_planner: 材料缺口 → 次数换算器；没配材料目标就传 None
            material_goal_list: 配置里的材料目标
            fallback_item: 保底任务；空表示不刷保底
        """
        self.mapper: PlanMapper = mapper
        self.book: RunBook = book
        self.material_planner: MaterialGoalPlanner | None = material_planner
        self.material_goal_list: list[MaterialGoal] = list(material_goal_list or [])
        self.fallback_item: ChargePlanItem | None = fallback_item

    def pick(self) -> ChargeTask | None:
        """挑这一轮要刷的那一条。

        Returns:
            任务；三个阶段都没得刷（或者都刷不起）时返回 None
        """
        for stage, picker in (
            (STAGE_AGENT_PLAN, self._pick_agent_plan),
            (STAGE_MATERIAL, self._pick_material),
            (STAGE_FALLBACK, self._pick_fallback),
        ):
            task = picker()
            if task is not None:
                log.info('这一轮刷 %s', task.description)
                return task
            log.debug('阶段 %s 没活干，看下一阶段', stage)
        return None

    def _pick_agent_plan(self) -> ChargeTask | None:
        """阶段 ①：当前角色清单里还差进度的条目。"""
        snapshot = self.book.agent_snapshot
        if snapshot is None:
            return None

        for entry in self._ordered_entries(snapshot):
            key = entry_key(entry)
            if key in self.book.skipped_keys:
                continue
            missing = self.book.estimate_entry_missing(entry)
            if missing is None:
                # 没有进度条（驱动盘）或者进度文本读坏了，判断不了就差多少，不硬刷
                log.debug('%s 没有可用的进度，先不管', entry.label)
                continue
            if missing <= 0:
                continue
            task = self._make_task(entry, snapshot, STAGE_AGENT_PLAN, key)
            if task is not None:
                return task
        return None

    def _pick_material(self) -> ChargeTask | None:
        """阶段 ②：材料库存还没到下限的。"""
        planner = self.material_planner
        if planner is None or not self.material_goal_list:
            return None

        for goal in self.material_goal_list:
            key = material_key(goal.material_name)
            if key in self.book.skipped_keys:
                continue
            owned = self.book.material_owned.get(normalize_material_name(goal.material_name))
            ran_times = self.book.material_ran_times.get(
                normalize_material_name(goal.material_name), 0
            )
            plan = planner.build(goal, owned, ran_times)
            for warning in plan.warnings:
                log.debug(warning)
            if plan.entry is None:
                # 已经够了 / 不可体力刷 / 材料表里没有，这一条不参与 ②
                continue
            task = self._make_task(
                plan.entry,
                self.book.agent_snapshot or PlanSnapshot(),
                STAGE_MATERIAL,
                key,
                label=f'{goal.material_name} → {plan.entry.entry_name}',
            )
            if task is not None:
                return replace(task, material_name=goal.material_name)
        return None

    def _pick_fallback(self) -> ChargeTask | None:
        """阶段 ③：保底任务，只要电量还够就一直刷。"""
        item = self.fallback_item
        if item is None:
            return None
        cost = item.estimated_charge_power
        if cost <= 0:
            log.warning('保底任务的分类「%s」看不出来一次要多少电量，不刷', item.category_name)
            return None
        if cost > self.book.remaining_charge:
            log.info(
                '保底任务一次要 %s 电量，只剩 %s，不刷', cost, self.book.remaining_charge
            )
            return None
        return ChargeTask(
            stage=STAGE_FALLBACK,
            label=f'{item.category_name}/{item.mission_type_name}/{item.mission_name or ""}',
            plan_item=replace(item),
            charge_per_run=cost,
            key=FALLBACK_KEY,
        )

    def _make_task(
        self,
        entry: PlanEntry,
        snapshot: PlanSnapshot,
        stage: str,
        key: str,
        label: str = '',
    ) -> ChargeTask | None:
        """把一条条目翻成任务；认不出副本或者电量不够就返回 None。

        Args:
            entry: 条目
            snapshot: 整份清单（查属性时要用代理人名字）
            stage: 这个条目属于哪个阶段
            key: 记账键
            label: 日志里显示的名字，不传就用条目自己的名字

        Returns:
            任务；翻不出来或者刷不起时 None
        """
        target, reason = self.mapper.resolve(entry, snapshot)
        if target is None:
            log.warning('%s 认不出对应副本：%s', entry.label, reason)
            self.book.skipped_keys[key] = reason or '认不出对应副本'
            return None

        request = BudgetRequest(
            key=target.key,
            label=entry.label,
            category_name=target.category_name,
            charge_range=parse_charge_text(entry.charge_text),
            missing=entry.missing,
            amount_per_run=entry.amount_per_run,
        )
        # keep_unaffordable=False：电量不够就不排，交给上层去结束或换阶段。
        # 内置体力计划那边相反（排 1 次等明天），因为它是每天循环跑的。
        availability = ChargeAvailability(natural=self.book.remaining_charge)
        allocation = ChargeBudget(
            availability, default_plan_times=1, keep_unaffordable=False
        ).allocate([request])[0]
        if allocation.skipped:
            log.info('%s 这次不刷：%s', entry.label, allocation.note or '电量不够')
            return None

        item = ChargePlanItem(
            tab_name=TAB_NAME,
            category_name=target.category_name,
            mission_type_name=target.mission_type_name,
            mission_name=target.mission_name,
            card_num=allocation.card_num,
            plan_times=1,
        )
        cost = allocation.charge_per_run or item.estimated_charge_power
        return ChargeTask(
            stage=stage,
            label=label or entry.label,
            plan_item=item,
            charge_per_run=cost,
            key=key,
        )

    @staticmethod
    def _ordered_entries(snapshot: PlanSnapshot) -> list[PlanEntry]:
        """排一下条目：先按分组（代理人 → 技能 → 驱动盘），同组里缺口占比大的先。

        排序规则和 `PlanMapper.build()` 里一致，这样「边刷边算」和「只排计划」挑到的
        第一条是同一个。

        Args:
            snapshot: 清单

        Returns:
            排好序的条目
        """
        default_rank = len(GROUP_RANK)

        def sort_key(entry: PlanEntry) -> tuple[int, float, str]:
            rank = GROUP_RANK.get(normalize_name(entry.group_name), default_rank)
            return (rank, -entry.missing_ratio, entry.entry_name)

        return sorted(snapshot.entries, key=sort_key)


# ============================= 执行用的 Operation =============================


class ExecuteChargePlan(ZOperation):
    """边刷边算：每一轮挑一条任务、传过去、刷一次、记账，然后重新挑。

    执行结果的 data 是 `ExecuteReport`。
    """

    def __init__(
        self,
        ctx: ZContext,
        config: AgentChargePlanConfig,
        availability: ChargeAvailability,
    ) -> None:
        """
        构造函数。

        Args:
            ctx: 一条龙上下文
            config: 插件配置（批量名单、材料目标、保底任务都从这里取）
            availability: 开跑时能用的电量（自然 + 储蓄 + 以太电池×60）
        """
        ZOperation.__init__(self, ctx, op_name='边刷边算')

        self.config: AgentChargePlanConfig = config
        """插件配置"""

        self.availability: ChargeAvailability = availability
        """开跑时能用的电量"""

        self.book: RunBook = RunBook(remaining_charge=availability.total)
        """本地记账本"""

        self.mapper: PlanMapper = PlanMapper(
            ctx,
            default_plan_times=config.default_plan_times,
            include_drive_disc=config.include_drive_disc,
        )
        """条目 → 副本的映射表"""

        self.material_goal_list: list[MaterialGoal] = [
            MaterialGoal.from_row(row) for row in config.material_goals
        ]
        """配置里的材料目标"""

        self.material_planner: MaterialGoalPlanner | None = (
            MaterialGoalPlanner(default_plan_times=config.default_plan_times)
            if self.material_goal_list
            else None
        )
        """材料缺口换算器；没配材料目标就不建，省得读材料表"""

        self.fallback_item: ChargePlanItem | None = parse_fallback_plan(config.fallback_plan)
        """保底任务；没配就是 None"""

        self.picker: ChargeTaskPicker = ChargeTaskPicker(
            mapper=self.mapper,
            book=self.book,
            material_planner=self.material_planner,
            material_goal_list=self.material_goal_list,
            fallback_item=self.fallback_item,
        )
        """挑任务的纯逻辑"""

        self.agent_name: str = ''
        """当前特训目标是谁"""

        self.original_agent_name: str = ''
        """进来时的特训目标。跑完（电量用完或名单跑完）要恢复成它，读不到就跳过恢复"""

        self.pending_agent_list: list[str] = _unique_names(config.batch_agent_names)
        """还没轮到的角色（批量名单去掉当前这个）"""

        self.current_task: ChargeTask | None = None
        """这一轮正在刷的任务"""

        self.material_gap_loaded: bool = False
        """材料缺口读过没有（一次运行只读一次，仓库有 10 分钟延迟，重读没意义）"""

        self.report: ExecuteReport = ExecuteReport()
        """执行结果，最后交给上层打日志"""

    # ── 节点 ────────────────────────────────────────────────────────────
    @operation_node(name='确认特训目标页', is_start_node=True, node_max_retry_times=3)
    def ensure_page(self) -> OperationRoundResult:
        """确认停在特训目标页。不在就自己走过去。"""
        if agent_plan_page.is_agent_plan_page(self.ctx, self.last_screenshot):
            return self.round_success(status='已在特训目标页')

        op = GotoAgentPlan(self.ctx)
        return self.round_by_op_result(op.execute(), retry_on_fail=True)

    @node_from(from_name='确认特训目标页')
    @operation_node(name='记住原目标')
    def remember_original_target(self) -> OperationRoundResult:
        """记下进来时的特训目标，跑完（电量用完/名单跑完）要放回去。读不到就留空。"""
        self.original_agent_name = agent_plan_page.read_agent_name(
            self.ctx, self.last_screenshot
        )
        if not self.original_agent_name:
            return self.round_success(status='进来时没有特训目标')
        return self.round_success(status=f'原特训目标 {self.original_agent_name}')

    @node_from(from_name='记住原目标')
    @node_from(from_name='换下一个角色', status=STATUS_AGENT_SWITCHED)
    @operation_node(name='读清单')
    def read_plan_list(self) -> OperationRoundResult:
        """读当前角色的特训清单，顺便把记账本清空、记下原始进度。"""
        op = ReadAgentPlanList(self.ctx, goto_first=False)
        result = op.execute()
        if not result.success:
            return self.round_by_op_result(result)

        snapshot = result.data
        if not isinstance(snapshot, AgentPlanSnapshot):
            return self.round_fail(status='清单读取没有返回数据')

        self.agent_name = snapshot.agent_name
        plan_snapshot = self.book.load_agent(snapshot)
        if self.agent_name and self.agent_name not in self.report.agent_name_list:
            self.report.agent_name_list.append(self.agent_name)

        log.info(
            '特训目标「%s」清单 %s 条，其中 %s 条带进度',
            self.agent_name or '（读不到）',
            len(plan_snapshot.entries),
            sum(1 for entry in plan_snapshot.entries if entry.missing is not None),
        )
        return self.round_success(status=f'{len(plan_snapshot.entries)} 条')

    @node_from(from_name='读清单')
    @operation_node(name='读材料缺口', node_max_retry_times=1)
    def read_material_gap(self) -> OperationRoundResult:
        """配了材料目标时读一次材料缺口。读过就不再读（仓库数据有 10 分钟延迟）。"""
        if self.material_planner is None:
            return self.round_success(status='没配材料目标')
        if self.material_gap_loaded:
            return self.round_success(status='材料缺口已经读过')

        op = ReadMaterialCalc(
            self.ctx, goto_first=False, allow_dialog_fallback=True, prefer_dialog=True
        )
        result = op.execute()
        if not result.success:
            # 材料读不到不算致命，只是少了 ② 这一档
            log.warning('材料缺口没读出来：%s', result.status)
            self.material_gap_loaded = True
            return self.round_success(status='材料缺口没读到')

        gap = result.data
        if not isinstance(gap, MaterialGapSnapshot):
            self.material_gap_loaded = True
            return self.round_success(status='材料缺口没读到')

        self.book.load_material_gap(gap)
        self.material_gap_loaded = True
        for line in gap.summary_lines():
            log.info(line)
        log.info('记下 %s 种材料的已有数量', len(self.book.material_owned))
        return self.round_success(status=f'读到 {len(self.book.material_owned)} 种材料')

    @node_from(from_name='读材料缺口')
    @node_from(from_name='挑战副本')
    @node_from(from_name='挑战副本', success=False)
    @node_from(from_name='记下传送失败')
    @operation_node(name='挑任务', screenshot_before_round=False)
    def pick_task(self) -> OperationRoundResult:
        """挑这一轮要刷的任务。没活干就换角色，电量用完就整轮结束。"""
        if self.book.remaining_charge <= 0:
            return self.round_success(status=STATUS_CHARGE_EMPTY, data=self._make_report())

        task = self.picker.pick()
        if task is None:
            self.current_task = None
            return self.round_success(status=STATUS_NO_TASK, data=self._make_report())

        self.current_task = task
        return self.round_success(status=task.stage, data=task.label)

    @node_from(from_name='挑任务', status=STAGE_AGENT_PLAN)
    @node_from(from_name='挑任务', status=STAGE_MATERIAL)
    @node_from(from_name='挑任务', status=STAGE_FALLBACK)
    @operation_node(name='传送到副本', node_max_retry_times=2)
    def transport(self) -> OperationRoundResult:
        """用快捷手册传送到要刷的副本。"""
        item = self._current_item()
        if item is None:
            return self.round_fail(status='没有当前任务')

        op = TransportByCompendium(
            self.ctx, item.tab_name, item.category_name, item.mission_type_name
        )
        return self.round_by_op_result(op.execute(), retry_on_fail=True)

    @node_from(from_name='传送到副本', success=False)
    @operation_node(name='记下传送失败', screenshot_before_round=False)
    def record_transport_failed(self) -> OperationRoundResult:
        """传送失败就把这条任务划掉，不然会一直挑同一条、原地打转。"""
        if self.current_task is not None:
            self.book.record_skip(self.current_task, '传送失败')
            log.warning('%s 传送失败，本轮不再挑它', self.current_task.label)
        return self.round_success(status='已记下传送失败')

    @node_from(from_name='传送到副本')
    @operation_node(name='挑战副本')
    def challenge(self) -> OperationRoundResult:
        """按分类跑对应的副本 Operation，成功就记账。

        电量在这些 Operation 内部就扣掉了，所以这里只扣「本地账」上的电量。
        """
        task = self.current_task
        if task is None:
            return self.round_fail(status='没有当前任务')

        item = task.plan_item
        op: ZOperation
        if item.category_name == CATEGORY_COMBAT_SIMULATION:
            op = CombatSimulation(self.ctx, item)
        elif item.category_name == CATEGORY_AREA_PATROL:
            op = AreaPatrol(self.ctx, item)
        elif item.category_name == CATEGORY_EXPERT_CHALLENGE:
            op = ExpertChallenge(self.ctx, item)
        elif item.category_name == CATEGORY_NOTORIOUS_HUNT:
            op = NotoriousHunt(self.ctx, item, use_charge_power=True)
        else:
            self.book.record_skip(task, f'分类 {item.category_name} 跑不了')
            return self.round_fail(status=f'不支持的分类 {item.category_name}')

        result = op.execute()
        if result.success:
            self.book.spend(task.charge_per_run)
            self.book.record_run(task)
            self.report.run_count += 1
            self.report.task_log.append(
                f'  第 {self.report.run_count} 次：{task.label} → '
                f'{item.category_name}/{item.mission_type_name}/'
                f'{item.mission_name or ""}，扣 {task.charge_per_run} 电量'
            )
            log.info(
                '刷完第 %s 次：%s，剩余电量约 %s',
                self.report.run_count, task.label, self.book.remaining_charge,
            )
            return self.round_success(status='刷完一次', data=task.label)

        self.report.fail_count += 1
        if result.status == STATUS_CHARGE_NOT_ENOUGH:
            # 游戏说电量不足，把本地账清空，下一轮挑任务会直接结束
            log.warning('游戏报电量不足，结束本轮：%s', task.label)
            self.book.remaining_charge = 0
            return self.round_fail(status=result.status)

        if result.status == STATUS_HUNT_BLOCKED:
            # 这周剩下的次数用完了，换一条，不扣电量
            log.info('%s 这周刷不了（%s），换下一条', task.label, result.status)
            self.book.record_skip(task, result.status)
            return self.round_fail(status=result.status)

        # 其它失败：保守地把电量扣掉（可能已经进本了），并且这条别再挑
        self.book.spend(task.charge_per_run)
        self.book.record_skip(task, result.status or '刷本失败')
        log.warning('%s 没刷成功：%s', task.label, result.status)
        return self.round_fail(status=result.status or '刷本失败')

    @node_from(from_name='挑任务', status=STATUS_NO_TASK)
    @node_notify(when=NotifyTiming.CURRENT_DONE, detail=True)
    @operation_node(name='换下一个角色')
    def switch_next_agent(self) -> OperationRoundResult:
        """这一轮没活干：换批量名单里的下一个角色，名单完了就结束。"""
        while True:
            next_name = self._take_next_agent()
            if next_name is None:
                self.report.skipped_list = [
                    f'{key}：{reason}' for key, reason in self.book.skipped_keys.items()
                ]
                return self.round_success(status=STATUS_LIST_DONE)

            if agent_plan_page.agent_name_similar(next_name, self.agent_name):
                # 名单里就有当前这个角色，不用切，直接重读清单校准
                log.info('「%s」就是当前特训目标，直接重读清单', next_name)
                return self.round_success(status=STATUS_AGENT_SWITCHED, data=next_name)

            log.info('切换到下一个角色：%s', next_name)
            op = ChangeAgentPlanTarget(self.ctx, target_agent_name=next_name, goto_first=True)
            result = op.execute()
            if result.success:
                return self.round_success(status=STATUS_AGENT_SWITCHED, data=next_name)
            log.warning('切到「%s」失败：%s，换下一个', next_name, result.status)

    @node_from(from_name='挑任务', status=STATUS_CHARGE_EMPTY)
    @node_from(from_name='换下一个角色', status=STATUS_LIST_DONE)
    @operation_node(name='恢复原目标')
    def restore_target(self) -> OperationRoundResult:
        """收尾：把特训目标放回进来时那个，别把用户的游戏设置留在最后一个角色上。"""
        restore_agent_target(self.ctx, self.original_agent_name)
        return self.round_success(status=STATUS_RESTORED, data=self._make_report())

    # ── 小工具 ──────────────────────────────────────────────────────────
    def _current_item(self) -> ChargePlanItem | None:
        """当前任务的计划项。"""
        if self.current_task is None:
            return None
        return self.current_task.plan_item

    def _take_next_agent(self) -> str | None:
        """从待处理名单里取下一个角色；取完了返回 None。"""
        while self.pending_agent_list:
            name = self.pending_agent_list.pop(0)
            if name.strip() == '':
                continue
            return name.strip()
        return None

    def _make_report(self) -> ExecuteReport:
        """把当前计数打包成结果对象。"""
        self.report.remaining_charge = self.book.remaining_charge
        return self.report


def _unique_names(name_list: list[str]) -> list[str]:
    """去空、去重、保留顺序。

    Args:
        name_list: 配置里的角色名列表

    Returns:
        洗干净的名单
    """
    names: list[str] = []
    for raw in name_list:
        name = raw.strip() if isinstance(raw, str) else ''
        if name and name not in names:
            names.append(name)
    return names


def __debug() -> None:
    from zzz_od.context.zzz_context import ZContext

    from .agent_charge_plan_config import AgentChargePlanConfig

    ctx = ZContext()
    ctx.init_by_config()
    ctx.init_ocr()
    ctx.run_context.start_running()
    config = AgentChargePlanConfig(instance_idx=ctx.current_instance_idx, group_id='one_dragon')
    op = ExecuteChargePlan(ctx, config=config, availability=ChargeAvailability())
    result = op.execute()
    log.info('执行结果 %s', result.status)
    if isinstance(result.data, ExecuteReport):
        for line in result.data.summary_lines():
            log.info(line)


if __name__ == '__main__':
    __debug()
