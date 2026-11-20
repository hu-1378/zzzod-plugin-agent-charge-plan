"""材料缺口 → 刷本次数。

## 这个文件管什么

输入是「某材料要保持到多少」（`MaterialGoal`）加「现在有多少」（已有数量），
输出是「还要刷几次」以及一条能直接喂给 `plan_mapper.PlanMapper` 的 `PlanEntry`。

材料 → 副本入口的映射不写在代码里，读插件自带的
`assets/material_data.yml`（材料名 / 别名 / 副本三元组 / 是否可体力刷）。

## 换算规则（游戏事实，别随意改）

    缺口 = max(0, target_floor - 已有)
    次数 = ceil(缺口 / amount_per_run)

`amount_per_run`（单次刷本产出）在 `material_data.yml` 里目前**全是 null**：
公开资料没有稳定的「每次必掉 N 个」，所以不猜数字。这时退化成
「先刷 1 次，跑完重算」，并把这条降级原因写进 `warnings`。

## 游戏内 10 分钟延迟（重要约束，不许绕过）

游戏「养成材料计算」结果页自己写着「仓库数据计算存在10分钟左右延迟，请悉知」。
刚刷完的材料不会立刻反映到这个页面和仓库库存上。所以：

- **不实现**「跑一次 → 立刻重读材料页 → 精确判定够了没」这种闭环。
  那样读到的是旧值，会把「已经够了」判成「还差」，反复多刷。
- 替代做法一：按「已刷次数 × 单次产出」自己累加估算已有数量，见 `estimate_owned()`。
- 替代做法二：单次产出也不知道时，就按「刷 1 次」推进；等 10 分钟以上再重读一次
  `operation/read_material_calc.py` 的结果做校准（这才是允许的重读时机）。
- 本文件只做纯数字换算：不读游戏、不点画面、不重读、不抛异常。

## 一层两层的区别

`Planner` 拿到的 `PlanEntry` 里，条目名填的是**副本名**（`mission_name`，没有就是
`mission_type_name`），不是材料名——因为 `PlanMapper` 是按副本名索引来认副本的。
材料名留在 `MaterialGoalPlan.label` 里给人看。
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from .charge_budget import CARD_NUM_MAX, CATEGORY_COMBAT_SIMULATION
from .plan_models import PlanEntry, PlanSnapshot

#: 插件根目录（本文件在 <插件根>/planner/ 下）
PLUGIN_ROOT: Path = Path(__file__).resolve().parent.parent

#: 材料表现在放在插件的 assets 下，相对插件根
MATERIAL_DATA_RELATIVE_PATH: Path = Path('assets') / 'material_data.yml'

#: 材料表里 yaml 的顶层键
MATERIAL_LIST_KEY: str = 'material_list'

#: 匹配材料名前要去掉的括号（OCR 会带括号，yaml 里有的写有的不写）
_BRACKET_CHARS: tuple[str, ...] = (
    '「', '」', '『', '』', '【', '】', '[', ']',
    '（', '）', '(', ')', '《', '》',
)

#: 单次产出未知时的降级说明
DEGRADE_AMOUNT_UNKNOWN: str = '单次产出未知，先排 1 次，跑完按「已刷次数 × 单次产出」估算'


def normalize_material_name(text: str | None) -> str:
    """把材料名搓成能比对的形状：去空白、统一中点、去掉括号。

    Args:
        text: 原始文本，可能是 OCR 出来的

    Returns:
        归一化后的文本；空文本返回空字符串
    """
    if not text:
        return ''
    cleaned = ''.join(str(text).split())
    for bullet in ('・', '•', '‧'):
        cleaned = cleaned.replace(bullet, '·')
    for char in _BRACKET_CHARS:
        cleaned = cleaned.replace(char, '')
    return cleaned


def _as_int(value: Any) -> int | None:
    """尽力转成整数，转不出来返回 None（不猜）。"""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        text = value.strip()
        return int(text) if text.isdigit() else None
    return None


# ============================= 材料表 =============================


@dataclass(frozen=True)
class ChargeSource:
    """刷这个材料要进哪个副本。对应 `material_data.yml` 里 `charge_source` 的一项。"""

    category_name: str
    """副本分类：实战模拟室 / 专业挑战室 / 区域巡防 / 恶名狩猎。"""

    mission_type_name: str
    """副本大类，例如 代理人技能、恶名·杜拉罕。"""

    mission_name: str | None = None
    """两级副本里的具体副本名，例如 琢光测试。只有一级时是 None。"""

    charge_per_run: int | None = None
    """单次消耗电量。实战模拟室是每张卡 20 点。"""

    @property
    def entry_name(self) -> str:
        """填进 `PlanEntry.entry_name` 的名字：有具体副本就用副本名。"""
        return self.mission_name or self.mission_type_name

    @property
    def charge_text(self) -> str:
        """还原成页面上那种电量文本。

        实战模拟室按卡数扣电量（一张 20 点、一次最多 5 张），写成 `20-100`；
        其它分类是固定值，写成 `40` / `60`。都是 `charge_budget` 里那份口径。
        """
        if self.charge_per_run is None:
            return ''
        if self.category_name == CATEGORY_COMBAT_SIMULATION:
            return f'{self.charge_per_run}-{self.charge_per_run * CARD_NUM_MAX}'
        return str(self.charge_per_run)

    @classmethod
    def from_row(cls, row: Mapping[str, Any]) -> ChargeSource:
        """从 yaml 的一项建出来。缺字段用空值兜着，不抛异常。"""
        return cls(
            category_name=str(row.get('category_name') or ''),
            mission_type_name=str(row.get('mission_type_name') or ''),
            mission_name=_as_text_or_none(row.get('mission_name')),
            charge_per_run=_as_int(row.get('charge_per_run')),
        )


@dataclass(frozen=True)
class MaterialInfo:
    """材料表里的一条材料。对应 `material_data.yml` 的 `material_list` 的一项。"""

    material_name: str
    """正式材料名，例如 特化流明芯片。"""

    aliases: tuple[str, ...] = ()
    """别名。查表时按同样规则归一化后一起匹配（例如 高维数据：凶刑讣告 的短名）。"""

    category: str = ''
    """材料归类：agent_promotion / agent_skill / wengine / basic / expert_challenge / weekly_boss / not_farmable。"""

    tier: int | None = None
    """同族三阶时 1/2/3，不适用时 None。"""

    charge_source: tuple[ChargeSource, ...] = ()
    """能刷的副本入口。不可体力刷时是空元组。"""

    amount_per_run: int | None = None
    """单次刷本产出。目前全是 None（公开资料没有稳定数值）。"""

    farmable: bool = True
    """能不能用体力刷。False 表示菲林 / 母带这类只能靠活动任务攒的材料。"""

    note: str = ''
    """备注，例如「通用货币；副本单次产出波动大」。"""

    source: str = ''
    """这条映射的资料来源，方便以后核实。"""

    @classmethod
    def from_row(cls, row: Mapping[str, Any]) -> MaterialInfo:
        """从 yaml 的一项建出来。"""
        raw_aliases = row.get('aliases') or []
        alias_list: list[str] = []
        if isinstance(raw_aliases, str):
            alias_list.append(raw_aliases)
        elif isinstance(raw_aliases, Iterable):
            alias_list.extend(str(item) for item in raw_aliases if item)

        raw_source = row.get('charge_source') or []
        source_list: list[ChargeSource] = []
        if isinstance(raw_source, Mapping):
            source_list.append(ChargeSource.from_row(raw_source))
        elif isinstance(raw_source, Iterable):
            for item in raw_source:
                if isinstance(item, Mapping):
                    source_list.append(ChargeSource.from_row(item))

        return cls(
            material_name=str(row.get('material_name') or ''),
            aliases=tuple(alias_list),
            category=str(row.get('category') or ''),
            tier=_as_int(row.get('tier')),
            charge_source=tuple(source_list),
            amount_per_run=_as_int(row.get('amount_per_run')),
            farmable=bool(row.get('farmable', True)),
            note=str(row.get('note') or ''),
            source=str(row.get('source') or ''),
        )

    @property
    def first_source(self) -> ChargeSource | None:
        """第一个副本入口。现在每种材料只挂一个，取不到就是 None。"""
        return self.charge_source[0] if self.charge_source else None


def _as_text_or_none(value: Any) -> str | None:
    """空值统一成 None，其它转字符串。"""
    if value is None:
        return None
    text = str(value).strip()
    return text or None


class MaterialTable:
    """材料表：材料名（含别名）→ `MaterialInfo`。"""

    def __init__(self, info_list: Iterable[MaterialInfo]) -> None:
        """
        构造函数。

        Args:
            info_list: 材料列表。第一个名字（正式名或别名）相同的后项会被忽略，
                避免 yaml 写重复时行为不确定
        """
        self.info_list: list[MaterialInfo] = list(info_list)
        #: 归一化后的名字（正式名 + 别名）→ 材料
        self._index: dict[str, MaterialInfo] = {}
        for info in self.info_list:
            for name in (info.material_name, *info.aliases):
                key = normalize_material_name(name)
                if key != '' and key not in self._index:
                    self._index[key] = info

    @classmethod
    def load(cls, path: Path | None = None) -> MaterialTable:
        """从 yaml 读材料表。

        Args:
            path: 材料表路径。不传就用插件自带的 `assets/material_data.yml`

        Returns:
            材料表；文件不存在时是空表（不抛异常，由上层记 warning）
        """
        yaml_path: Path = path if path is not None else PLUGIN_ROOT / MATERIAL_DATA_RELATIVE_PATH
        if not yaml_path.exists():
            return cls([])
        with yaml_path.open('r', encoding='utf-8') as file:
            data = yaml.safe_load(file) or {}
        row_list = data.get(MATERIAL_LIST_KEY) or []
        return cls(MaterialInfo.from_row(row) for row in row_list if isinstance(row, Mapping))

    def __len__(self) -> int:
        """材料条数。"""
        return len(self.info_list)

    def get(self, material_name: str | None) -> MaterialInfo | None:
        """按材料名查，别名也算命中。

        只做归一化后的完全匹配，**不做模糊匹配**：同一族材料名字只差一个字
        （初阶 / 高阶 / 特化），模糊匹配会把缺口算到隔壁那一阶上去。

        Args:
            material_name: 材料名，可能是 OCR 出来的带括号写法

        Returns:
            材料；查不到就是 None
        """
        key = normalize_material_name(material_name)
        if key == '':
            return None
        return self._index.get(key)

    def name_list(self) -> list[str]:
        """所有正式的（别名包含）名字，用来在日志里提示「是不是想找这些」。"""
        return [info.material_name for info in self.info_list]


def load_material_table(path: Path | None = None) -> MaterialTable:
    """读材料表。`MaterialTable.load` 的简写。"""
    return MaterialTable.load(path)


# ============================= 目标与结果 =============================


@dataclass(frozen=True)
class MaterialGoal:
    """一条「这个材料要保持在多少个以上」。"""

    material_name: str
    """材料名，可以是别名。"""

    target_floor: int
    """目标下限。已有数量达到这个数就够了。"""

    @classmethod
    def from_row(cls, row: Mapping[str, Any]) -> MaterialGoal:
        """从字典建一条。字典键用本类的字段名。"""
        name = row.get('material_name')
        floor = _as_int(row.get('target_floor'))
        return cls(
            material_name='' if name is None else str(name).strip(),
            target_floor=floor if floor is not None else 0,
        )

    @property
    def label(self) -> str:
        """给人看的写法。"""
        if self.material_name:
            return self.material_name
        return '（空材料名）'


@dataclass(frozen=True)
class MaterialOwned:
    """一条完整输入：目标 + 当前已有 + 本轮已经刷了几次。

    `ran_times` 是给 10 分钟延迟用的：上次读到的「已有」是旧的，本轮刷过几次先用
    「次数 × 单次产出」自己补上，别指望页面立刻更新。
    """

    goal: MaterialGoal
    owned: int | None = None
    """上次读到的已有数量。读不到就是 None。"""

    ran_times: int = 0
    """本轮已经刷了几次（刷完之后、页面还没更新时用）。"""

    @classmethod
    def from_row(cls, row: Mapping[str, Any]) -> MaterialOwned:
        """从字典建一条。`material_name` / `target_floor` / `owned` / `ran_times` 四个键。"""
        ran = _as_int(row.get('ran_times'))
        return cls(
            goal=MaterialGoal.from_row(row),
            owned=_as_int(row.get('owned')),
            ran_times=ran if ran is not None and ran > 0 else 0,
        )


@dataclass
class MaterialGoalPlan:
    """一条材料目标算下来的结果。"""

    goal: MaterialGoal
    """原始目标。"""

    info: MaterialInfo | None = None
    """材料表里查到的材料；查不到是 None。"""

    owned: int | None = None
    """传进来的已有数量。"""

    estimated_owned: int | None = None
    """估算后的已有数量（读过已有 + 本轮已刷的估算产出）。"""

    gap: int | None = None
    """缺口 = max(0, 目标 - 估算已有)。不知道已有数量时是 None。"""

    plan_times: int = 0
    """按缺口算出来的建议刷本次数。0 表示不用刷。"""

    entry: PlanEntry | None = None
    """能直接喂给 `PlanMapper` 的条目。None 表示不生成计划项。"""

    warnings: list[str] = field(default_factory=list)
    """降级提示：材料查不到、不可体力刷、单次产出未知、已经够了都在这里。"""

    @property
    def label(self) -> str:
        """给人看的名字。"""
        return self.goal.label

    @property
    def planned(self) -> bool:
        """有没有生成计划项。"""
        return self.entry is not None

    def summary_line(self) -> str:
        """拼成一行日志。"""
        if self.entry is None:
            return f'[不排] {self.label}：{"；".join(self.warnings) or "没有可用计划项"}'
        return (
            f'[排上] {self.label} → {self.entry.entry_name} '
            f'缺口 {self.gap if self.gap is not None else "未知"} 次数 {self.plan_times}'
        )


# ============================= 换算 =============================


def estimate_owned(owned: int | None, ran_times: int, amount_per_run: int | None) -> int | None:
    """估算「现在大概有多少」：读到的已有 + 本轮已刷次数 × 单次产出。

    这是绕开游戏那 10 分钟仓库延迟的办法：不去重读页面，自己累加。

    Args:
        owned: 上次读到的已有数量；None 表示不知道
        ran_times: 本轮已经刷了几次
        amount_per_run: 单次产出；None 表示不知道

    Returns:
        估算的已有数量；已有和单次产出都没法用时返回 None（调用方按「刷 1 次」降级）
    """
    if owned is None:
        return None
    if ran_times <= 0 or amount_per_run is None or amount_per_run <= 0:
        return owned
    return owned + ran_times * amount_per_run


def plan_times_for(
    gap: int | None, amount_per_run: int | None, default_plan_times: int = 1
) -> tuple[int, str]:
    """按缺口算要刷几次。

    Args:
        gap: 还差多少个；None 表示不知道
        amount_per_run: 单次产出；None 表示不知道
        default_plan_times: 算不出来时先排几次

    Returns:
        (次数, 降级说明)。没有降级时说明是空字符串；次数 0 表示不用刷
    """
    if gap is not None and gap <= 0:
        return 0, ''
    if gap is None:
        return max(default_plan_times, 0), '没有读到已有数量，先按默认次数排'
    if amount_per_run is None or amount_per_run <= 0:
        return max(default_plan_times, 0), DEGRADE_AMOUNT_UNKNOWN
    return math.ceil(gap / amount_per_run), ''


# ============================= 主流程 =============================


class MaterialGoalPlanner:
    """把一组「材料保持到 N」换成计划项。

    和 `PlanMapper` 的分工：这里只按材料产出算次数，不碰电量预算；
    `PlanMapper` 拿到 `PlanSnapshot` 后会按电量再分一次次数。
    """

    def __init__(
        self,
        material_table: MaterialTable | None = None,
        material_data_path: Path | None = None,
        default_plan_times: int = 1,
    ) -> None:
        """
        构造函数。

        Args:
            material_table: 已经读好的材料表。和 material_data_path 同时传时以本参数为准
            material_data_path: 材料表路径，不传就用插件自带的 assets/material_data.yml
            default_plan_times: 算不出次数时先排几次（默认 1 次，靠体力计划的循环把缺口补完）
        """
        if material_table is not None:
            self.material_table: MaterialTable = material_table
        else:
            self.material_table = MaterialTable.load(material_data_path)
        self.default_plan_times: int = max(default_plan_times, 0)

    def build(
        self,
        goal: MaterialGoal | Mapping[str, Any],
        owned: int | None,
        ran_times: int = 0,
    ) -> MaterialGoalPlan:
        """算一条材料目标。

        Args:
            goal: 目标，`MaterialGoal` 或者带 `material_name` / `target_floor` 的字典
            owned: 上次读到的已有数量；None 表示没读到
            ran_times: 本轮已经刷了几次（用来在仓库延迟内估算已有）

        Returns:
            这条目标的结果。任何异常情况都写进 warnings，不抛异常
        """
        goal_item: MaterialGoal = goal if isinstance(goal, MaterialGoal) else MaterialGoal.from_row(goal)
        result = MaterialGoalPlan(goal=goal_item, owned=owned)

        info = self.material_table.get(goal_item.material_name)
        if info is None:
            reason = f'材料表里没有「{goal_item.material_name}」，没生成计划项'
            if goal_item.material_name == '':
                reason = '材料名是空的，没生成计划项'
            result.warnings.append(reason)
            return result
        result.info = info

        if goal_item.target_floor < 0:
            result.warnings.append(
                f'「{info.material_name}」的目标下限是负数，按 0 处理'
            )

        result.estimated_owned = estimate_owned(owned, ran_times, info.amount_per_run)
        if result.estimated_owned is None:
            result.warnings.append(f'没读到「{info.material_name}」的已有数量，缺口算不出来')
        else:
            result.gap = max(0, goal_item.target_floor - result.estimated_owned)

        if not info.farmable:
            result.warnings.append(
                f'「{info.material_name}」不消耗电量、没法用体力刷，没有生成计划项'
                f'（{info.note or "来源见材料表"}）'
            )
            return result

        times, degrade = plan_times_for(
            result.gap, info.amount_per_run, self.default_plan_times
        )
        result.plan_times = times
        if degrade:
            result.warnings.append(f'「{info.material_name}」{degrade}')

        if times <= 0:
            owned_text = result.estimated_owned if result.estimated_owned is not None else '未知'
            result.warnings.append(
                f'「{info.material_name}」已经够了（已有 {owned_text} / 目标 {goal_item.target_floor}），不排计划'
            )
            return result

        entry = self._make_entry(info, goal_item, result.estimated_owned)
        if entry is None:
            result.warnings.append(
                f'「{info.material_name}」在材料表里没有副本入口，没生成计划项'
            )
            return result
        result.entry = entry
        return result

    def build_snapshot(
        self, item_list: Iterable[MaterialOwned]
    ) -> tuple[PlanSnapshot, list[str]]:
        """一次算多条，拼成 `PlanMapper` 能吃的 `PlanSnapshot`。

        Args:
            item_list: 每条是 `MaterialOwned`（目标 + 已有 + 本轮已刷次数）

        Returns:
            (清单, 全部降级提示)。清单里只放生成成功的计划项，其它的原因都在提示里
        """
        entries: list[PlanEntry] = []
        warnings: list[str] = []

        for item in item_list:
            plan = self.build(item.goal, item.owned, item.ran_times)
            warnings.extend(plan.warnings)
            if plan.entry is not None:
                entries.append(plan.entry)

        return PlanSnapshot(entries=entries), warnings

    def _make_entry(
        self, info: MaterialInfo, goal: MaterialGoal, estimated_owned: int | None
    ) -> PlanEntry | None:
        """把材料翻成一条 `PlanEntry`。

        条目名填副本名（不是材料名）：`PlanMapper` 是按副本名索引认副本的。
        进度填成「估算已有 / 目标下限」，这样 `PlanMapper` 自己算出来的缺口和这里一致。
        """
        source: ChargeSource | None = info.first_source
        if source is None:
            return None

        progress_text: str | None = None
        if estimated_owned is not None:
            progress_text = f'{estimated_owned}/{max(goal.target_floor, 0)}'

        return PlanEntry(
            entry_name=source.entry_name,
            charge_text=source.charge_text,
            progress_text=progress_text,
            amount_per_run=info.amount_per_run,
        )


def __debug() -> None:
    """本地跑一下看看：不连游戏，只读材料表。"""
    planner = MaterialGoalPlanner()
    print('材料表条数', len(planner.material_table))
    for goal, owned in (
        (MaterialGoal('特化流明芯片', 100), 0),
        (MaterialGoal('高维数据：凶刑讣告', 20), 10),
        (MaterialGoal('菲林', 1000), 0),
        (MaterialGoal('不存在的材料', 10), 0),
    ):
        plan = planner.build(goal, owned)
        print(plan.summary_line())
        for warning in plan.warnings:
            print('   注意', warning)


if __name__ == '__main__':
    __debug()
