"""数据层的中转模型：读取层给进来什么、映射层交出去什么。

整条链路：

    读取层（operation/）→ PlanSnapshot → 映射层（plan_mapper）→ PlanBuildResult
    → 写入层（plan_writer）→ 内置体力计划的 ChargePlanConfig

为什么数据层自己再定义一份输入类型：画面操作部分（operation/）是另一拨人并行开发的，
数据层不该 import 它，否则两个任务互相等。两边字段是平行的，接线时用
`PlanSnapshot.from_rows(...)` 或 `PlanSnapshot.from_reader_snapshot(...)` 转一下就行。
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from .charge_budget import parse_progress_text

if TYPE_CHECKING:
    from zzz_od.application.charge_plan.charge_plan_config import ChargePlanItem


def _as_text(value: Any) -> str:
    """把各种东西搓成字符串。看不懂的类型（结构体、列表）当成空字符串。"""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, bool):
        return ''
    if isinstance(value, int | float):
        return str(value)
    return ''


def _as_text_or_none(value: Any) -> str | None:
    """同上，但空字符串返回 None。"""
    text = _as_text(value)
    return text or None


def _as_int(value: Any) -> int | None:
    """尽力转成整数。转不出来返回 None，不猜。"""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        text = ''.join(ch for ch in value if ch.isdigit())
        return int(text) if text != '' else None
    return None


def _get_first(obj: Any, *names: str) -> Any:
    """按候选字段名依次取值（字典和对象都支持）。取不到返回 None。"""
    if obj is None:
        return None
    if isinstance(obj, Mapping):
        for name in names:
            if name in obj:
                return obj[name]
        return None
    for name in names:
        if hasattr(obj, name):
            return getattr(obj, name)
    return None


@dataclass
class PlanEntry:
    """特训目标页上的一行，还没翻译成体力计划项。"""

    group_name: str = ''
    """分组标题：代理人 / 技能 / 驱动盘。读取层没认出来就留空。"""
    entry_name: str = ''
    """条目名。实测就是副本名或材料名，例如 调查员记录、猎人与忠犬、「提丰·重击者型」。"""
    charge_text: str = ''
    """右侧电量文本，三种形态：20-100、40、60。"""
    progress_text: str | None = None
    """进度文本，例如 129/290。驱动盘分组没有进度条，这里是 None。"""
    amount_per_run: int | None = None
    """单次刷本能拿多少个；材料层算出来才有，没有就按默认次数排。"""
    element: str | None = None
    """属性提示（物理属性 之类）。读取层能认出条目图标就给，认不出留 None。"""

    @classmethod
    def from_row(cls, row: Mapping[str, Any]) -> PlanEntry:
        """从字典建一条。字典的键用本类的字段名。"""
        return cls(
            group_name=_as_text(row.get('group_name')),
            entry_name=_as_text(row.get('entry_name')),
            charge_text=_as_text(row.get('charge_text')),
            progress_text=_as_text_or_none(row.get('progress_text')),
            amount_per_run=_as_int(row.get('amount_per_run')),
            element=_as_text_or_none(row.get('element')),
        )

    @property
    def label(self) -> str:
        """给人看的名字：有分组就写成「分组-条目」。"""
        if self.group_name:
            return f'{self.group_name}-{self.entry_name}'
        return self.entry_name

    @property
    def progress(self) -> tuple[int, int] | None:
        """(已有, 需要) 两个数；没有进度条或看不懂时 None。"""
        return parse_progress_text(self.progress_text)

    @property
    def missing(self) -> int | None:
        """还差多少个。没有进度信息时 None（不代表已经达成）。"""
        progress = self.progress
        if progress is None:
            return None
        owned, total = progress
        return max(0, total - owned)

    @property
    def missing_ratio(self) -> float:
        """缺口占比 0~1，用来排优先级。没有进度信息时算 0。"""
        progress = self.progress
        if progress is None or progress[1] <= 0:
            return 0.0
        owned, total = progress
        return max(0, total - owned) / total


@dataclass
class PlanSnapshot:
    """一个角色的特训目标清单。"""

    agent_name: str = ''
    """当前特训目标的代理人名字，用来查属性。"""
    entries: list[PlanEntry] = field(default_factory=list)
    """页面上读到的所有条目。"""
    element: str | None = None
    """代理人属性。读取层或插件配置能给出就填，能给就少一次查表。"""

    @classmethod
    def from_rows(
        cls,
        rows: Iterable[Mapping[str, Any]],
        agent_name: str = '',
        element: str | None = None,
    ) -> PlanSnapshot:
        """从一串字典建清单。"""
        return cls(
            agent_name=agent_name,
            entries=[PlanEntry.from_row(row) for row in rows],
            element=element,
        )

    @classmethod
    def from_reader_snapshot(cls, snapshot: Any) -> PlanSnapshot:
        """把读取层的 AgentPlanSnapshot 转成本类型。

        只按属性名取值，不 import 读取层的类（两拨人并行开发）。读取层字段名改了的话，
        改这里的候选名列表即可。注意：进度、电量必须是文本字段，结构体在这里认不出来。
        """
        raw_items = _get_first(snapshot, 'entries', 'items', 'item_list', 'plan_items') or []
        entries: list[PlanEntry] = []
        for raw in raw_items:
            entries.append(
                PlanEntry(
                    group_name=_as_text(_get_first(raw, 'group_name', 'group', 'group_title')),
                    entry_name=_as_text(
                        _get_first(raw, 'entry_name', 'item_name', 'name', 'mission_name')
                    ),
                    charge_text=_as_text(_get_first(raw, 'charge_text', 'charge')),
                    progress_text=_as_text_or_none(_get_first(raw, 'progress_text', 'progress')),
                    amount_per_run=_as_int(_get_first(raw, 'amount_per_run', 'per_run_amount')),
                    element=_as_text_or_none(_get_first(raw, 'element', 'dmg_type')),
                )
            )

        return cls(
            agent_name=_as_text(_get_first(snapshot, 'agent_name', 'agent')),
            entries=entries,
            element=_as_text_or_none(_get_first(snapshot, 'element', 'dmg_type')),
        )


@dataclass
class PlanChange:
    """映射阶段对一条条目的处理结果（还没写进配置）。"""

    kind: str
    """planned（要写进去）/ skip（跳过，原因看 detail）。"""
    key: str
    """内容级去重键：分类|mission_type|mission。"""
    label: str
    """给人看的名字。"""
    plan_times: int = 0
    card_num: str = ''
    charge: int = 0
    plan_id: str | None = None
    detail: str = ''


@dataclass
class PlanBuildResult:
    """一次「清单 → 计划项」的完整结果。"""

    plan_list: list[ChargePlanItem] = field(default_factory=list)
    """生成的计划项，可以直接交给写入层。"""
    warnings: list[str] = field(default_factory=list)
    """降级提示：认不出的条目、对不上的电量、被截断的次数都在这里。"""
    estimated_total_charge: int = 0
    """这些计划项预计一共花多少电量。"""
    changes: list[PlanChange] = field(default_factory=list)
    """逐条处理结果，方便日志里说清「为什么这条没排上」。"""
    leftover_charge: int = 0
    """分完还剩多少电量。"""

    def summary_lines(self) -> list[str]:
        """拼成几行日志文本。"""
        lines: list[str] = []
        for change in self.changes:
            if change.kind == 'planned':
                note = f'（{change.detail}）' if change.detail else ''
                lines.append(
                    f'[排上] {change.label} → {change.key} '
                    f'次数 {change.plan_times} 卡数 {change.card_num} 电量 {change.charge}{note}'
                )
            else:
                lines.append(f'[跳过] {change.label}：{change.detail}')
        lines.extend(f'[注意] {warning}' for warning in self.warnings)
        lines.append(f'合计电量 {self.estimated_total_charge}，剩余 {self.leftover_charge}。')
        return lines


@dataclass
class PlanWriteChange:
    """写入阶段对单条计划项做的事。"""

    kind: str
    """add（新增）/ update（改了次数或卡数）/ keep（配置里已经是这样）。"""
    key: str
    """内容级去重键。"""
    plan_id: str | None = None
    plan_times: int = 0
    card_num: str = ''
    detail: str = ''


@dataclass
class PlanWriteResult:
    """一次写入的结果。dry-run 时 written 是 False，plan_list 是「假设写下去会变成什么」。"""

    written: bool = False
    """是否真的写回了配置文件。"""
    plan_list: list[ChargePlanItem] = field(default_factory=list)
    """写入之后的计划列表（dry-run 时是预览）。"""
    changes: list[PlanWriteChange] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
