"""条目 → 内置体力计划项：副本名直配、两级副本按属性选层、未知条目降级。

数据源是真副本表 `assets/game_data/compendium_data.yml`（走 `CompendiumService`），
所以这些用例同时也在守「代码里的映射规则和当前版本的副本表还对得上」。

降级是硬要求：游戏每个版本都可能加新副本、新条目，认不出来只能记 warning，
不能让整条流程抛异常中断。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from agent_charge_plan.planner.charge_budget import ChargeAvailability
from agent_charge_plan.planner.plan_mapper import (
    CATEGORY_AREA_PATROL,
    CATEGORY_COMBAT_SIMULATION,
    CATEGORY_EXPERT_CHALLENGE,
    PlanMapper,
    lookup_agent_element,
    normalize_name,
)
from agent_charge_plan.planner.plan_models import PlanEntry, PlanSnapshot

from zzz_od.application.charge_plan.charge_plan_config import ChargePlanConfig

if TYPE_CHECKING:
    from conftest import StubContext


def _snapshot(*rows: dict[str, str | None], agent_name: str = '安东·伊万诺夫') -> PlanSnapshot:
    """按「分组 / 条目名 / 电量 / 进度」快速造一份清单。"""
    return PlanSnapshot.from_rows(list(rows), agent_name=agent_name)


def _row(
    group: str, name: str, charge: str = '', progress: str | None = None
) -> dict[str, str | None]:
    """造一行条目字典。"""
    return {
        'group_name': group,
        'entry_name': name,
        'charge_text': charge,
        'progress_text': progress,
    }


# ============================= 名字归一化与属性查表 =============================


def test_normalize_name_strips_brackets_and_spaces() -> None:
    """OCR 常常丢括号或读成全角，比对前统一抹掉；中点也统一成一个。"""
    assert normalize_name('「提丰·重击者型') == '提丰·重击者型'
    assert normalize_name('「提丰·重击者型」') == '提丰·重击者型'
    assert normalize_name('提丰・重击者型') == '提丰·重击者型'
    assert normalize_name(' 调查员 记录 ') == '调查员记录'
    assert normalize_name(None) == ''


def test_lookup_agent_element() -> None:
    """全名查属性：游戏里写「安东·伊万诺夫」，项目里只有代号「安东」。"""
    assert lookup_agent_element('安东·伊万诺夫') == '电属性'
    assert lookup_agent_element('安东') == '电属性'
    assert lookup_agent_element('叶瞬光') == '物理属性'
    assert lookup_agent_element('项目里没有的新角色') is None
    assert lookup_agent_element('') is None


# ============================= 单条映射 =============================


def test_resolve_material_entry_rules(full_ctx: StubContext) -> None:
    """条目名本身就是材料名时走直配表：调查员记录 / 音擎能源模块。"""
    mapper = PlanMapper(full_ctx)
    snapshot = _snapshot()

    target, reason = mapper.resolve(PlanEntry(entry_name='调查员记录'), snapshot)
    assert reason == ''
    assert target is not None
    assert (target.category_name, target.mission_type_name, target.mission_name) == (
        CATEGORY_COMBAT_SIMULATION, '基础材料', '调查专项',
    )

    target, reason = mapper.resolve(PlanEntry(entry_name='音擎能源模块'), snapshot)
    assert reason == ''
    assert target is not None
    assert (target.category_name, target.mission_type_name, target.mission_name) == (
        CATEGORY_COMBAT_SIMULATION, '基础材料', '音擎专项',
    )


def test_resolve_area_patrol_name(full_ctx: StubContext) -> None:
    """区域巡防的副本名直接就是 mission_type（没有下一层）。"""
    mapper = PlanMapper(full_ctx)
    target, reason = mapper.resolve(
        PlanEntry(group_name='驱动盘', entry_name='猎人与忠犬', charge_text='60'),
        _snapshot(),
    )
    assert reason == ''
    assert target is not None
    assert target.category_name == CATEGORY_AREA_PATROL
    assert target.mission_type_name == '猎人与忠犬'
    assert target.mission_name is None
    assert target.has_missions is False


def test_resolve_expert_challenge_with_ocr_bracket(full_ctx: StubContext) -> None:
    """OCR 少读一个右括号，也要能对上专业挑战室里的「提丰·重击者型」。"""
    mapper = PlanMapper(full_ctx)
    target, reason = mapper.resolve(
        PlanEntry(group_name='技能', entry_name='「提丰·重击者型', charge_text='40'),
        _snapshot(),
    )
    assert reason == ''
    assert target is not None
    assert target.category_name == CATEGORY_EXPERT_CHALLENGE
    assert target.mission_type_name == '「提丰·重击者型」'
    assert target.key == '专业挑战室|「提丰·重击者型」|'


def test_resolve_agent_skill_by_element(full_ctx: StubContext) -> None:
    """「代理人技能」是两级副本，按角色属性选具体测试本。

    安东是电属性 → 电流测试；叶瞬光是物理属性 → 刚性测试。
    """
    mapper = PlanMapper(full_ctx)
    entry = PlanEntry(group_name='技能', entry_name='代理人技能', charge_text='20-100',
                      progress_text='153/203')

    target, reason = mapper.resolve(entry, _snapshot(agent_name='安东·伊万诺夫'))
    assert reason == ''
    assert target is not None
    assert (target.category_name, target.mission_type_name, target.mission_name) == (
        CATEGORY_COMBAT_SIMULATION, '代理人技能', '电流测试',
    )

    target, reason = mapper.resolve(entry, _snapshot(agent_name='叶瞬光'))
    assert reason == ''
    assert target is not None
    assert target.mission_name == '刚性测试'


def test_resolve_agent_skill_element_from_entry(full_ctx: StubContext) -> None:
    """读取层直接在条目上给了属性时，优先用它，不必再按角色名查。"""
    mapper = PlanMapper(full_ctx)
    entry = PlanEntry(group_name='技能', entry_name='代理人技能', element='冰属性')
    target, reason = mapper.resolve(entry, _snapshot(agent_name=''))
    assert reason == ''
    assert target is not None
    assert target.mission_name == '冰结测试'


def test_resolve_agent_skill_without_element_degrades(full_ctx: StubContext) -> None:
    """拿不到属性时只给说明，不猜副本，也不抛异常。"""
    mapper = PlanMapper(full_ctx)
    entry = PlanEntry(group_name='技能', entry_name='代理人技能', charge_text='20-100')

    target, reason = mapper.resolve(entry, _snapshot(agent_name=''))

    assert target is None
    assert '属性' in reason


def test_resolve_unknown_name_degrades(full_ctx: StubContext) -> None:
    """版本更新加了新条目时，只能记 warning，流程照走。"""
    mapper = PlanMapper(full_ctx)
    entry = PlanEntry(group_name='代理人', entry_name='新版本奇怪条目', charge_text='20-100')

    target, reason = mapper.resolve(entry, _snapshot())

    assert target is None
    assert '新版本奇怪条目' in reason
    assert '找不到' in reason


def test_resolve_empty_entry_name(full_ctx: StubContext) -> None:
    """空条目名直接跳过。"""
    mapper = PlanMapper(full_ctx)
    target, reason = mapper.resolve(PlanEntry(group_name='代理人', entry_name=''), _snapshot())
    assert target is None
    assert '空' in reason


def test_resolve_two_level_without_agent_skill_degrades(full_ctx: StubContext) -> None:
    """两级副本里只有「代理人技能」知道怎么选层；别的（例如「基础材料」）要明确说排不了。"""
    mapper = PlanMapper(full_ctx)
    target, reason = mapper.resolve(
        PlanEntry(group_name='代理人', entry_name='基础材料', charge_text='20-100'), _snapshot()
    )
    assert target is None
    assert '两级副本' in reason


# ============================= 整份清单 =============================


def _full_rows() -> list[dict[str, str | None]]:
    """一份和「安东」实机截图对得上的清单（外加一条认不出的新条目）。"""
    return [
        _row('代理人', '调查员记录', '20-100', '129/290'),
        _row('技能', '代理人技能', '20-100', '153/203'),
        _row('技能', '「提丰·重击者型', '40', '4/60'),
        _row('驱动盘', '猎人与忠犬', '60'),
        _row('代理人', '新版本奇怪条目', '20-100', '1/10'),
    ]


def test_build_default_excludes_drive_disc(full_ctx: StubContext) -> None:
    """默认不排驱动盘：词条随机，没有「够不够」的判断标准。"""
    mapper = PlanMapper(full_ctx)
    snapshot = PlanSnapshot.from_rows(_full_rows(), agent_name='安东·伊万诺夫')

    result = mapper.build(snapshot, ChargeAvailability())

    keys = {item.mission_name for item in result.plan_list}
    assert keys == {'调查专项', '电流测试', None}
    assert all(item.category_name != CATEGORY_AREA_PATROL for item in result.plan_list)
    assert any('驱动盘' in line for line in result.summary_lines())
    assert any('新版本奇怪条目' in warning for warning in result.warnings)


def test_build_include_drive_disc(full_ctx: StubContext) -> None:
    """配成 include_drive_disc=True 时才把驱动盘分组一起排。"""
    mapper = PlanMapper(full_ctx, include_drive_disc=True)
    snapshot = PlanSnapshot.from_rows(_full_rows(), agent_name='安东·伊万诺夫')

    result = mapper.build(snapshot, ChargeAvailability())

    assert any(item.category_name == CATEGORY_AREA_PATROL for item in result.plan_list)


def test_build_unknown_entry_does_not_raise(full_ctx: StubContext) -> None:
    """整份清单里混进认不出的条目：不抛异常，其余条目照常排上。"""
    mapper = PlanMapper(full_ctx)
    snapshot = PlanSnapshot.from_rows(
        [_row('代理人', '完全不认识的条目', '20-100', '1/10'), _row('代理人', '调查员记录', '20-100', '9/10')],
        agent_name='安东·伊万诺夫',
    )

    result = mapper.build(snapshot, ChargeAvailability())

    assert len(result.plan_list) == 1
    assert result.plan_list[0].mission_name == '调查专项'
    assert any('完全不认识的条目' in warning for warning in result.warnings)


def test_build_deduplicates_same_target(full_ctx: StubContext) -> None:
    """两条条目指向同一个副本时只排一条，并说明原因（否则同一个本会被刷两轮）。

    这里用同一个副本名在 OCR 上的两种写法（一个有左括号、一个有右括号）。
    """
    mapper = PlanMapper(full_ctx)
    snapshot = PlanSnapshot.from_rows(
        [
            _row('技能', '「提丰·重击者型', '40', '4/60'),
            _row('技能', '提丰·重击者型」', '40', '4/60'),
        ],
        agent_name='安东·伊万诺夫',
    )

    result = mapper.build(snapshot, ChargeAvailability())
    assert len(result.plan_list) == 1
    assert any('只排一条' in warning for warning in result.warnings)


def test_build_deduplicates_within_included_drive_disc(full_ctx: StubContext) -> None:
    """打开驱动盘后重复条目只留一条。"""
    mapper = PlanMapper(full_ctx, include_drive_disc=True)
    snapshot = PlanSnapshot.from_rows(
        [_row('驱动盘', '猎人与忠犬', '60'), _row('驱动盘', '猎人与忠犬', '60')],
        agent_name='安东·伊万诺夫',
    )

    result = mapper.build(snapshot, ChargeAvailability())

    assert len(result.plan_list) == 1
    assert any('只排一条' in warning for warning in result.warnings)


def test_build_warns_charge_mismatch(full_ctx: StubContext) -> None:
    """电量文本和分类对不上时给提醒，但不改映射结果。

    区域巡防固定 60 点，截图里读成 40 说明可能读错了（或者游戏改了规则），提醒人去看。
    """
    mapper = PlanMapper(full_ctx, include_drive_disc=True)
    snapshot = PlanSnapshot.from_rows(
        [_row('驱动盘', '猎人与忠犬', '40')], agent_name='安东·伊万诺夫'
    )

    result = mapper.build(snapshot, ChargeAvailability())

    assert any('固定 60 点电量' in warning for warning in result.warnings)


def test_build_items_pass_builtin_validation(full_ctx: StubContext) -> None:
    """生成出来的计划项必须能过内置体力计划自己的校验函数。"""
    mapper = PlanMapper(full_ctx, include_drive_disc=True)
    snapshot = PlanSnapshot.from_rows(_full_rows(), agent_name='安东·伊万诺夫')

    result = mapper.build(snapshot, ChargeAvailability())

    assert result.plan_list
    for item in result.plan_list:
        assert ChargePlanConfig.validate_item(full_ctx, item) is None, item


def test_build_budget_totals(full_ctx: StubContext) -> None:
    """电量预算：估算总量 + 剩余电量要自洽（都按 240 点起算）。"""
    mapper = PlanMapper(full_ctx)
    snapshot = PlanSnapshot.from_rows(_full_rows(), agent_name='安东·伊万诺夫')

    result = mapper.build(snapshot, ChargeAvailability())

    assert result.estimated_total_charge > 0
    assert result.estimated_total_charge + result.leftover_charge == ChargeAvailability().total


def test_build_skipped_when_progress_full(full_ctx: StubContext) -> None:
    """进度已经满了的条目不用生成计划项。"""
    mapper = PlanMapper(full_ctx)
    snapshot = PlanSnapshot.from_rows(
        [_row('代理人', '调查员记录', '20-100', '290/290')], agent_name='安东·伊万诺夫'
    )

    result = mapper.build(snapshot, ChargeAvailability())

    assert result.plan_list == []
    assert any('进度已经够了' in warning for warning in result.warnings)


def test_build_from_reader_snapshot(full_ctx: StubContext, plan_pages: dict) -> None:
    """读取层（真 OCR）的输出直接喂给映射层：这条跨层接线必须成立。"""
    from agent_charge_plan.operation.agent_plan_page import AgentPlanSnapshot

    page = plan_pages['特训目标_安东_顶部']
    reader_snapshot = AgentPlanSnapshot(agent_name=page.agent_name, item_list=page.items)

    snapshot = PlanSnapshot.from_reader_snapshot(reader_snapshot)
    result = PlanMapper(full_ctx).build(snapshot, ChargeAvailability())

    missions = {(item.mission_type_name, item.mission_name) for item in result.plan_list}
    assert ('基础材料', '调查专项') in missions
    assert ('代理人技能', '电流测试') in missions
    assert result.warnings == []
