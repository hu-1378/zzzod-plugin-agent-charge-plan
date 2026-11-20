"""特训清单 → 内置体力计划项。

特训目标页（快捷手册 → 训练 → 特训目标）上的一行长这样：

    分组：代理人
    条目：调查员记录      电量 20-100      进度 129/290

要翻成内置体力计划能吃的三元组：

    category_name（实战模拟室 / 区域巡防 / 专业挑战室 / 恶名狩猎）
    mission_type_name
    mission_name（只有两级副本才填，其它是 None）

数据源是主仓的 `assets/game_data/compendium_data.yml`，通过
`ctx.compendium_service` 读，不自己解析 yml。

映射顺序是「先查直配表 → 再查副本名索引 → 再查 mission 名索引 → 都不中就降级」。
认不出来的条目只记 warning，绝对不抛异常：游戏每个版本都可能加新副本、加新条目。
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import TYPE_CHECKING

from one_dragon.utils.log_utils import log
from zzz_od.application.charge_plan.charge_plan_config import (
    ChargePlanConfig,
    ChargePlanItem,
)
from zzz_od.game_data.agent import AgentEnum

from .charge_budget import (
    CATEGORY_COMBAT_SIMULATION,
    CATEGORY_FIXED_CHARGE,
    BudgetRequest,
    ChargeAvailability,
    ChargeBudget,
    ChargeRange,
    parse_charge_text,
)
from .plan_models import PlanBuildResult, PlanChange, PlanEntry, PlanSnapshot

if TYPE_CHECKING:
    from zzz_od.context.zzz_context import ZContext


#: 体力计划里的 tab 名称，内置数据都在「训练」这个 tab 下
TAB_NAME = '训练'

#: 三个固定电量分类，和 charge_budget.CATEGORY_FIXED_CHARGE 对上
CATEGORY_AREA_PATROL = '区域巡防'
CATEGORY_EXPERT_CHALLENGE = '专业挑战室'
CATEGORY_NOTORIOUS_HUNT = '恶名狩猎'

#: 两级副本里唯一需要按角色属性再选一层的 mission_type
MISSION_TYPE_AGENT_SKILL = '代理人技能'

#: 驱动盘分组。词条随机、没有「库存数量」这回事，所以默认不排（插件配置 include_drive_disc 控制）
DRIVE_DISC_GROUP = '驱动盘'

#: 分组顺序：页面上从上到下就是 代理人 → 技能 → 驱动盘，电量先给靠前的分组
GROUP_RANK: dict[str, int] = {'代理人': 0, '技能': 1, DRIVE_DISC_GROUP: 2}

#: 分组 → 这个分组可能指向哪些分类。副本名跨分类重名时用它消歧
GROUP_CATEGORY_HINT: dict[str, tuple[str, ...]] = {
    '代理人': (CATEGORY_COMBAT_SIMULATION,),
    '技能': (CATEGORY_COMBAT_SIMULATION, CATEGORY_EXPERT_CHALLENGE),
    DRIVE_DISC_GROUP: (CATEGORY_AREA_PATROL, CATEGORY_NOTORIOUS_HUNT),
}

#: 要抹掉的括号对（OCR 有时读不到括号，有时读成全角）
_BRACKET_CHARS: tuple[str, ...] = (
    '「', '」', '【', '】', '[', ']', '（', '）', '(', ')',
    '《', '》', '“', '”', '‘', '’', '"', "'",
)


@dataclass(frozen=True)
class MissionTarget:
    """一个「分类 / mission_type / mission」三元组，就是内置体力计划要的形状。"""

    category_name: str
    mission_type_name: str
    mission_name: str | None = None
    has_missions: bool = False
    """这个 mission_type 下面还有一层 mission（两级副本，例如实战模拟室的基础材料）。"""

    @property
    def key(self) -> str:
        """内容级去重键。实战模拟室带上 mission_name，才不会把 7 个属性芯片本混成一个。"""
        return f'{self.category_name}|{self.mission_type_name}|{self.mission_name or ""}'

    @property
    def label(self) -> str:
        """给人看的写法。"""
        if self.mission_name:
            return f'{self.category_name}/{self.mission_type_name}/{self.mission_name}'
        return f'{self.category_name}/{self.mission_type_name}'


def normalize_name(text: str | None) -> str:
    """把 OCR 文本搓成能比对的形状：去空白、统一中点、去掉各种括号。

    Args:
        text: 原始文本

    Returns:
        归一化后的文本；空文本返回空字符串
    """
    if not text:
        return ''

    cleaned = ''.join(text.split())
    for bullet in ('・', '•', '‧'):
        cleaned = cleaned.replace(bullet, '·')
    for char in _BRACKET_CHARS:
        cleaned = cleaned.replace(char, '')
    return cleaned


def _element_keys(text: str | None) -> set[str]:
    """属性文本的几种写法：`物理属性` 和 `物理` 算同一个属性。"""
    normalized = normalize_name(text)
    if normalized == '':
        return set()
    keys = {normalized}
    if normalized.endswith('属性'):
        keys.add(normalized[:-2])
    return keys


def lookup_agent_element(agent_name: str) -> str | None:
    """按游戏里的代理人名字查属性，返回「电属性」这种文本。

    游戏页面上是「安东·伊万诺夫」这种全名，项目的代理人表里只有代号「安东」，
    所以先拿全名比，再拿「·」前面的代号比。都不中就返回 None，由调用方降级成 warning。

    Args:
        agent_name: 画面上读到的代理人名字

    Returns:
        属性文本；查不到时 None
    """
    text = normalize_name(agent_name)
    if text == '':
        return None

    candidates = [text]
    if '·' in text:
        candidates.append(text.split('·')[0])

    for candidate in candidates:
        for member in AgentEnum:
            if normalize_name(member.value.agent_name) == candidate:
                return member.value.dmg_type.value

    return None


def _charge_matches_category(charge_text: str | None, category_name: str) -> bool:
    """电量文本和分类对得上吗（副本名跨分类重名时用来消歧）。"""
    charge_range = parse_charge_text(charge_text)
    if charge_range is None:
        return False
    if category_name == CATEGORY_COMBAT_SIMULATION:
        return charge_range.per_card
    expected = CATEGORY_FIXED_CHARGE.get(category_name)
    if expected is None:
        return False
    return not charge_range.per_card and charge_range.max_charge == expected


class PlanMapper:
    """把特训清单翻成计划项，并按电量预算分配次数。"""

    def __init__(
        self,
        ctx: ZContext,
        allow_agent_element_lookup: bool = True,
        default_plan_times: int = 1,
        keep_unaffordable: bool = True,
        include_drive_disc: bool = False,
    ) -> None:
        """建映射表。

        Args:
            ctx: 一条龙上下文，只用它的 compendium_service
            allow_agent_element_lookup: 读取层没给属性时，允不允许按角色名去代理人表查属性
            default_plan_times: 不知道单次产出时先排几次
            keep_unaffordable: 今天电量连一次都不够时，还排不排（True = 排 1 次等明天）
            include_drive_disc: 驱动盘分组要不要一起排（默认不排，和插件配置的默认值一致）
        """
        self.ctx: ZContext = ctx
        self.allow_agent_element_lookup: bool = allow_agent_element_lookup
        self.default_plan_times: int = default_plan_times
        self.keep_unaffordable: bool = keep_unaffordable
        self.include_drive_disc: bool = include_drive_disc
        #: mission_type 名字（含显示名、别名）→ 候选三元组
        self._by_mission_type: dict[str, list[MissionTarget]] = {}
        #: mission 名字（含显示名）→ 候选三元组
        self._by_mission: dict[str, list[MissionTarget]] = {}
        self._rebuild_index()

    def _rebuild_index(self) -> None:
        """从 compendium 重建索引。游戏更新后调一次就能跟上新副本。"""
        self._by_mission_type = {}
        self._by_mission = {}

        for category in self.ctx.compendium_service.get_category_list_data(TAB_NAME):
            for mission_type in category.mission_type_list:
                if mission_type.is_agent_plan:
                    # 「代理人方案培养」不生成：那会让内置应用去盲选列表第一个头像
                    continue

                target = MissionTarget(
                    category_name=category.category_name,
                    mission_type_name=mission_type.mission_type_name,
                    mission_name=None,
                    has_missions=len(mission_type.mission_list) > 0,
                )
                for name in (
                    mission_type.mission_type_name,
                    mission_type.mission_type_name_display,
                    *mission_type.alias_list,
                ):
                    self._add(self._by_mission_type, name, target)

                for mission in mission_type.mission_list:
                    mission_target = MissionTarget(
                        category_name=category.category_name,
                        mission_type_name=mission_type.mission_type_name,
                        mission_name=mission.mission_name,
                        has_missions=False,
                    )
                    for name in (mission.mission_name, mission.mission_name_display):
                        self._add(self._by_mission, name, mission_target)

    @staticmethod
    def _add(
        index: dict[str, list[MissionTarget]], name: str | None, target: MissionTarget
    ) -> None:
        """往索引里加一条，同名重复的不重复加。"""
        key = normalize_name(name)
        if key == '':
            return
        bucket = index.setdefault(key, [])
        if target not in bucket:
            bucket.append(target)

    def build(
        self, snapshot: PlanSnapshot, available: ChargeAvailability
    ) -> PlanBuildResult:
        """清单 → 计划项。

        流程：逐条映射 → 去重 → 算优先级排队 → 按电量预算分配次数 → 校验。

        Args:
            snapshot: 读取层给的清单
            available: 手上能用的电量

        Returns:
            生成结果，认不出的条目都在 warnings 里
        """
        warnings: list[str] = []
        changes: list[PlanChange] = []
        requests: list[BudgetRequest] = []
        prepared: dict[str, tuple[PlanEntry, MissionTarget]] = {}

        for entry in snapshot.entries:
            label = entry.label or '（空条目）'
            if not self._group_allowed(entry):
                changes.append(
                    PlanChange(
                        kind='skip',
                        key='',
                        label=label,
                        detail=f'{DRIVE_DISC_GROUP}分组按配置不排',
                    )
                )
                continue

            target, reason = self.resolve(entry, snapshot)
            if target is None:
                warnings.append(reason or f'{label} 认不出对应副本，已跳过')
                changes.append(PlanChange(kind='skip', key='', label=label, detail=reason))
                continue

            if target.key in prepared:
                warnings.append(f'{label} 和前面的条目都指向 {target.label}，只排一条')
                changes.append(
                    PlanChange(kind='skip', key=target.key, label=label, detail='和前面的条目重复')
                )
                continue

            charge_range = parse_charge_text(entry.charge_text)
            if charge_range is None and entry.charge_text:
                warnings.append(
                    f'{label} 的电量文本「{entry.charge_text}」看不出来，没算进电量预算'
                )
            mismatch = self._charge_mismatch(target, charge_range)
            if mismatch:
                warnings.append(f'{label}：{mismatch}')

            prepared[target.key] = (entry, target)
            requests.append(
                BudgetRequest(
                    key=target.key,
                    label=label,
                    category_name=target.category_name,
                    charge_range=charge_range,
                    missing=entry.missing,
                    amount_per_run=entry.amount_per_run,
                    priority=(self._group_rank(entry), -entry.missing_ratio),
                )
            )

        budget = ChargeBudget(
            available,
            default_plan_times=self.default_plan_times,
            keep_unaffordable=self.keep_unaffordable,
        )
        plan_list: list[ChargePlanItem] = []
        total_charge = 0

        for allocation in budget.allocate(requests):
            entry, target = prepared[allocation.key]
            if allocation.skipped:
                if allocation.note:
                    warnings.append(f'{entry.label}：{allocation.note}')
                changes.append(
                    PlanChange(
                        kind='skip',
                        key=allocation.key,
                        label=entry.label,
                        detail=allocation.note,
                    )
                )
                continue

            item = ChargePlanItem(
                tab_name=TAB_NAME,
                category_name=target.category_name,
                mission_type_name=target.mission_type_name,
                mission_name=target.mission_name,
                card_num=allocation.card_num,
                plan_times=allocation.plan_times,
            )
            invalid = self._validate_item(item)
            if invalid is not None:
                warnings.append(f'{entry.label} 生成的计划项不合法（{invalid}），已跳过')
                changes.append(
                    PlanChange(
                        kind='skip', key=allocation.key, label=entry.label, detail=invalid
                    )
                )
                continue

            plan_list.append(item)
            total_charge += allocation.charge_total
            if allocation.note:
                warnings.append(f'{entry.label}：{allocation.note}')
            changes.append(
                PlanChange(
                    kind='planned',
                    key=allocation.key,
                    label=entry.label,
                    plan_times=allocation.plan_times,
                    card_num=allocation.card_num,
                    charge=allocation.charge_total,
                    plan_id=item.plan_id,
                    detail=allocation.note,
                )
            )

        log.debug('映射完成：%s 条排上，%s 条跳过', len(plan_list), len(warnings))
        return PlanBuildResult(
            plan_list=plan_list,
            warnings=warnings,
            estimated_total_charge=total_charge,
            changes=changes,
            leftover_charge=available.total - total_charge,
        )

    def resolve(
        self, entry: PlanEntry, snapshot: PlanSnapshot
    ) -> tuple[MissionTarget | None, str]:
        """把一条条目翻成三元组。

        Args:
            entry: 一条条目
            snapshot: 整份清单（查属性时要用代理人名字）

        Returns:
            (三元组, 说明)。认不出来时三元组是 None，说明里写清原因。
        """
        name = normalize_name(entry.entry_name)
        if name == '':
            return None, '条目名是空的，跳过'

        # 1) 条目名本身就是材料名（调查员记录 / 音擎能源模块），直配表里有就直配
        rule = _MATERIAL_ENTRY_RULES.get(name)
        if rule is not None:
            return rule, ''

        # 2) 条目名就是副本名：区域巡防 / 专业挑战室 / 恶名狩猎 都是这一层
        candidates = self._by_mission_type.get(name)
        target = self._pick(candidates, entry)
        if target is not None:
            if not target.has_missions:
                return target, ''
            # 两级副本（实战模拟室）：还得再选一层 mission
            return self._resolve_two_level(target, entry, snapshot)
        if candidates:
            return None, self._ambiguous_reason(entry, candidates)

        # 3) 条目名是 mission 名（锋御演练 之类），反查一层
        candidates = self._by_mission.get(name)
        target = self._pick(candidates, entry)
        if target is not None:
            return target, ''
        if candidates:
            return None, self._ambiguous_reason(entry, candidates)

        return None, f'「{entry.entry_name}」在副本表里找不到（可能是新版本条目），已跳过'

    def _group_allowed(self, entry: PlanEntry) -> bool:
        """这个分组要不要排。驱动盘默认不排：词条随机，没有「够不够」的判断标准。"""
        if self.include_drive_disc:
            return True
        return normalize_name(entry.group_name) != DRIVE_DISC_GROUP

    @staticmethod
    def _ambiguous_reason(entry: PlanEntry, candidates: list[MissionTarget]) -> str:
        """同名条目跨了多个分类、分组又认不出来时的说明。"""
        options = '、'.join(sorted({c.label for c in candidates}))
        group = entry.group_name or '（空）'
        return (
            f'「{entry.entry_name}」在副本表里对应多个副本（{options}），'
            f'分组「{group}」判断不出是哪个，已跳过'
        )

    def _pick(
        self, candidates: list[MissionTarget] | None, entry: PlanEntry
    ) -> MissionTarget | None:
        """同名候选有多个时，用分组和电量文本挑一个；挑不出来就当没认出。"""
        if not candidates:
            return None
        if len(candidates) == 1:
            return candidates[0]

        hinted = GROUP_CATEGORY_HINT.get(normalize_name(entry.group_name), ())
        filtered = [c for c in candidates if c.category_name in hinted] if hinted else []
        if len(filtered) == 1:
            return filtered[0]

        pool = filtered or candidates
        by_charge = [c for c in pool if _charge_matches_category(entry.charge_text, c.category_name)]
        if len(by_charge) == 1:
            return by_charge[0]

        return None

    def _resolve_two_level(
        self, target: MissionTarget, entry: PlanEntry, snapshot: PlanSnapshot
    ) -> tuple[MissionTarget | None, str]:
        """两级副本再选一层 mission。目前只有「代理人技能」要按角色属性选。"""
        if target.mission_type_name != MISSION_TYPE_AGENT_SKILL:
            return None, (
                f'「{entry.entry_name}」是两级副本（{target.label}），'
                f'要到具体副本那一层才能排计划，已跳过'
            )

        element, reason = self._entry_element(entry, snapshot)
        if element is None:
            return None, f'「{entry.entry_name}」要按角色属性选副本，但{reason}，已跳过'

        mission_name = self._mission_by_element(target, element)
        if mission_name is None:
            return None, f'「{entry.entry_name}」的属性「{element}」在副本表里没有对应条目，已跳过'

        return replace(target, mission_name=mission_name, has_missions=False), ''

    def _entry_element(
        self, entry: PlanEntry, snapshot: PlanSnapshot
    ) -> tuple[str | None, str]:
        """拿属性。先后顺序：条目自带 → 清单自带 → 按角色名查代理人表。

        Returns:
            (属性文本, 拿不到时的原因)
        """
        if entry.element:
            return entry.element, ''
        if snapshot.element:
            return snapshot.element, ''
        if not self.allow_agent_element_lookup:
            return None, '读取层没给属性，也没开「按角色名查属性」'
        if not snapshot.agent_name:
            return None, '读取层没给属性，也没读到角色名'

        element = lookup_agent_element(snapshot.agent_name)
        if element is None:
            return None, f'角色「{snapshot.agent_name}」不在项目的代理人表里，查不到属性'
        return element, ''

    def _mission_by_element(self, target: MissionTarget, element: str) -> str | None:
        """属性 → mission 名。

        直接拿属性文本跟副本表里的显示名对，`物理属性` 和 `物理` 算同一个属性，
        所以副本表改了属性写法也不会写死在代码里。
        """
        wanted = _element_keys(element)
        if not wanted:
            return None

        for category in self.ctx.compendium_service.get_category_list_data(TAB_NAME):
            if category.category_name != target.category_name:
                continue
            for mission_type in category.mission_type_list:
                if mission_type.mission_type_name != target.mission_type_name:
                    continue
                for mission in mission_type.mission_list:
                    if _element_keys(mission.mission_name_display) & wanted:
                        return mission.mission_name
        return None

    def _charge_mismatch(
        self, target: MissionTarget, charge_range: ChargeRange | None
    ) -> str | None:
        """电量文本和分类对不上时给个提醒。只提醒，不改映射结果。"""
        if charge_range is None:
            return None

        if target.category_name == CATEGORY_COMBAT_SIMULATION:
            if not charge_range.per_card:
                return (
                    f'{target.label} 通常按卡数扣电量（20-100），'
                    f'读到的却是 {charge_range.text}，请留意'
                )
            return None

        expected = CATEGORY_FIXED_CHARGE.get(target.category_name)
        if expected is None:
            return None
        if charge_range.per_card or charge_range.max_charge != expected:
            return (
                f'{target.label} 固定 {expected} 点电量，'
                f'读到的却是 {charge_range.text}，请留意'
            )
        return None

    @staticmethod
    def _group_rank(entry: PlanEntry) -> int:
        """分组的先后顺序，认不出的分组排最后。"""
        return GROUP_RANK.get(normalize_name(entry.group_name), len(GROUP_RANK))

    def _validate_item(self, item: ChargePlanItem) -> str | None:
        """用内置体力计划的校验函数过一遍，保证写进去的项是合法的。"""
        try:
            return ChargePlanConfig.validate_item(self.ctx, item)
        except Exception as error:
            log.error('校验计划项时出错', exc_info=True)
            return f'校验时出错: {error}'


#: 条目名就是材料名时的直配表。加新映射就往这里加一条。
#: 数据来自 assets/game_data/compendium_data.yml 里「实战模拟室 / 基础材料」的 mission 列表。
_MATERIAL_ENTRY_RULES: dict[str, MissionTarget] = {
    # 代理人经验材料，基础材料-调查专项
    '调查员记录': MissionTarget(CATEGORY_COMBAT_SIMULATION, '基础材料', '调查专项'),
    # 音擎经验材料，基础材料-音擎专项
    '音擎能源模块': MissionTarget(CATEGORY_COMBAT_SIMULATION, '基础材料', '音擎专项'),
}
