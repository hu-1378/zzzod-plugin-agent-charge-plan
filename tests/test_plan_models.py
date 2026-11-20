"""数据层的中转模型：条目/清单/结果的取值、降级和日志文本。

重点是两件容易被改坏的事：
- 读取层（`operation/`）和数据层（`planner/`）用属性名对接，不互相 import。
  所以 `PlanSnapshot.from_reader_snapshot` 必须认得读取层真实给出的对象。
- 进度文本读坏时（`1537203`）只能当「没有进度」，不能算出天文数字的缺口。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from agent_charge_plan.planner.plan_models import (
    PlanBuildResult,
    PlanChange,
    PlanEntry,
    PlanSnapshot,
    PlanWriteResult,
)

if TYPE_CHECKING:
    from conftest import PlanPageData


# ============================= PlanEntry =============================


def test_entry_from_row() -> None:
    """字典建条目：字段名对齐数据层自己的命名。"""
    entry = PlanEntry.from_row(
        {
            'group_name': '代理人',
            'entry_name': '调查员记录',
            'charge_text': '20-100',
            'progress_text': '129/290',
            'amount_per_run': '4',
            'element': '电属性',
        }
    )
    assert entry.group_name == '代理人'
    assert entry.entry_name == '调查员记录'
    assert entry.charge_text == '20-100'
    assert entry.progress_text == '129/290'
    assert entry.amount_per_run == 4
    assert entry.element == '电属性'


def test_entry_from_row_defaults() -> None:
    """缺字段时留空，不抛异常。"""
    entry = PlanEntry.from_row({})
    assert entry.group_name == ''
    assert entry.entry_name == ''
    assert entry.progress_text is None
    assert entry.element is None


def test_entry_from_row_ignores_odd_types() -> None:
    """看不懂的类型（布尔、结构体）当成空，不硬转。"""
    entry = PlanEntry.from_row(
        {'charge_text': True, 'progress_text': [], 'amount_per_run': True, 'element': {'a': 1}}
    )
    assert entry.charge_text == ''
    assert entry.progress_text is None
    assert entry.amount_per_run is None
    assert entry.element is None


def test_entry_label() -> None:
    """有分组时写成「分组-条目」，没有就只是条目名。"""
    assert PlanEntry(group_name='代理人', entry_name='调查员记录').label == '代理人-调查员记录'
    assert PlanEntry(entry_name='调查员记录').label == '调查员记录'


def test_entry_progress_and_missing() -> None:
    """进度 → 缺口：用于排优先级和估算次数。"""
    entry = PlanEntry(entry_name='调查员记录', progress_text='129/290')
    assert entry.progress == (129, 290)
    assert entry.missing == 161
    assert 0.55 < entry.missing_ratio < 0.56


def test_entry_missing_when_finished() -> None:
    """进度满了缺口是 0。"""
    entry = PlanEntry(entry_name='调查员记录', progress_text='290/290')
    assert entry.missing == 0
    assert entry.missing_ratio == 0.0


def test_entry_missing_when_broken_progress() -> None:
    """`1537203` 这种被裁掉斜杠的进度：当「没有进度」，缺口是 None 而不是巨大数字。"""
    entry = PlanEntry(entry_name='代理人技能', progress_text='1537203')
    assert entry.progress is None
    assert entry.missing is None
    assert entry.missing_ratio == 0.0


def test_entry_missing_without_progress() -> None:
    """驱动盘分组本来就没有进度条：缺口 None（不代表已经达成）。"""
    entry = PlanEntry(group_name='驱动盘', entry_name='猎人与忠犬', charge_text='60')
    assert entry.progress is None
    assert entry.missing is None


# ============================= PlanSnapshot =============================


def test_snapshot_from_rows() -> None:
    """一串字典建清单。"""
    snapshot = PlanSnapshot.from_rows(
        [{'entry_name': '调查员记录', 'charge_text': '20-100'}], agent_name='安东·伊万诺夫'
    )
    assert snapshot.agent_name == '安东·伊万诺夫'
    assert len(snapshot.entries) == 1
    assert snapshot.entries[0].entry_name == '调查员记录'


def test_snapshot_from_reader_snapshot_dict() -> None:
    """读取层如果交的是字典（模拟数据常用），也要能转。"""
    snapshot = PlanSnapshot.from_reader_snapshot(
        {
            'agent_name': '叶瞬光',
            'item_list': [
                {
                    'group_name': '驱动盘',
                    'name': '诡步与重壁',
                    'charge_text': '60',
                    'progress_text': None,
                }
            ],
        }
    )
    assert snapshot.agent_name == '叶瞬光'
    assert snapshot.entries[0].entry_name == '诡步与重壁'
    assert snapshot.entries[0].group_name == '驱动盘'


def test_snapshot_from_reader_snapshot_real_ocr(plan_pages: dict[str, PlanPageData]) -> None:
    """读取层（真 OCR）的 `AgentPlanSnapshot` 直接转换：字段名和进度都要带过来。"""
    from agent_charge_plan.operation.agent_plan_page import AgentPlanSnapshot

    page = plan_pages['特训目标_安东_顶部']
    snapshot = PlanSnapshot.from_reader_snapshot(
        AgentPlanSnapshot(agent_name=page.agent_name, item_list=page.items)
    )

    assert snapshot.agent_name == '安东·伊万诺夫'
    assert [entry.entry_name for entry in snapshot.entries] == ['调查员记录', '代理人技能']
    assert snapshot.entries[0].progress_text == '129/290'
    assert snapshot.entries[0].missing == 161
    assert snapshot.entries[0].charge_text == '20-100'


def test_snapshot_from_reader_snapshot_empty() -> None:
    """读取层什么都没给（没设特训目标）：转成空清单，不抛异常。"""
    snapshot = PlanSnapshot.from_reader_snapshot(None)
    assert snapshot.agent_name == ''
    assert snapshot.entries == []


# ============================= 结果结构 =============================


def test_build_result_summary_lines() -> None:
    """日志文本要能一眼看出「哪条排上了、哪条为什么没排」。"""
    result = PlanBuildResult(
        warnings=['某条目认不出'],
        estimated_total_charge=240,
        leftover_charge=0,
        changes=[
            PlanChange(
                kind='planned', key='实战模拟室|基础材料|调查专项', label='代理人-调查员记录',
                plan_times=2, card_num='5', charge=200, detail='电量只够 2 次',
            ),
            PlanChange(kind='skip', key='', label='驱动盘-猎人与忠犬', detail='驱动盘分组按配置不排'),
        ],
    )
    lines = result.summary_lines()
    assert any('[排上]' in line and '调查专项' in line for line in lines)
    assert any('[跳过]' in line and '驱动盘' in line for line in lines)
    assert any('[注意] 某条目认不出' in line for line in lines)
    assert lines[-1] == '合计电量 240，剩余 0。'


def test_write_result_defaults() -> None:
    """dry-run 的默认状态是「没写」，避免调用方误以为已经落盘。"""
    result = PlanWriteResult()
    assert result.written is False
    assert result.plan_list == []
    assert result.changes == []
