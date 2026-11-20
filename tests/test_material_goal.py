"""材料缺口 → 刷本次数：材料表查表、缺口换算、降级行为。

对应 `planner/material_goal.py`。数据源是插件自带的 `assets/material_data.yml`
（122 条材料），不用连游戏。

两条要守住的规则：
- 缺口 = max(0, 目标下限 - 已有)，次数 = ceil(缺口 ÷ 单次产出)。
- 材料表里 `amount_per_run` 目前全是 null —— 这时不许猜数字，只能按默认次数排 1 次并给提示。
另外游戏写着「仓库数据计算存在 10 分钟延迟」，所以这里靠「已刷次数 × 单次产出」自己估算，
不靠跑完立刻重读页面。
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from agent_charge_plan.planner.charge_budget import ChargeAvailability
from agent_charge_plan.planner.material_goal import (
    DEGRADE_AMOUNT_UNKNOWN,
    ChargeSource,
    MaterialGoal,
    MaterialGoalPlanner,
    MaterialInfo,
    MaterialOwned,
    MaterialTable,
    estimate_owned,
    normalize_material_name,
    plan_times_for,
)
from agent_charge_plan.planner.plan_mapper import PlanMapper

if TYPE_CHECKING:
    from conftest import StubContext


# ============================= 名字归一化与查表 =============================


def test_normalize_material_name() -> None:
    """材料名比对前统一去空白、去括号、统一中点。"""
    assert normalize_material_name('「仓鼠笼」访问器') == '仓鼠笼访问器'
    assert normalize_material_name('高维数据：凶刑讣告') == '高维数据：凶刑讣告'
    assert normalize_material_name(' 特化 流明 芯片 ') == '特化流明芯片'
    assert normalize_material_name(None) == ''


def test_material_table_loads_plugin_assets() -> None:
    """插件自带的材料表能读出来，覆盖面够用。"""
    table = MaterialTable.load()
    assert len(table) >= 100
    assert table.get('特化流明芯片') is not None


def test_material_table_missing_file_is_empty(tmp_path: Path) -> None:
    """材料表文件不存在时是空表，不抛异常（由上层记 warning）。"""
    table = MaterialTable.load(tmp_path / 'nope.yml')
    assert len(table) == 0
    assert table.get('特化流明芯片') is None


def test_material_table_lookup_by_alias() -> None:
    """别名也能查到同一条材料（高维数据写了短名）。"""
    table = MaterialTable.load()
    short = table.get('凶刑讣告')
    full = table.get('高维数据：凶刑讣告')
    assert short is not None and full is not None
    assert short.material_name == full.material_name == '高维数据：凶刑讣告'


def test_material_table_lookup_with_brackets() -> None:
    """OCR 带不带括号都要命中同一个材料。"""
    table = MaterialTable.load()
    assert table.get('仓鼠笼访问器').material_name == '「仓鼠笼」访问器'
    assert table.get('「仓鼠笼」访问器').material_name == '「仓鼠笼」访问器'


def test_material_table_does_not_fuzzy_match() -> None:
    """刻意不做模糊匹配：初阶/高阶/特化只差一个字，模糊会把缺口算到隔壁那一阶。"""
    table = MaterialTable.load()
    assert table.get('特化流明芯') is None
    assert table.get('初阶流明芯片') is None


def test_charge_source_properties() -> None:
    """副本入口还原成「条目名 + 电量文本」：实战模拟室按卡数写成 20-100。"""
    combat = ChargeSource('实战模拟室', '代理人技能', '琢光测试', 20)
    assert combat.entry_name == '琢光测试'
    assert combat.charge_text == '20-100'

    expert = ChargeSource('专业挑战室', '恶名·杜拉罕', None, 40)
    assert expert.entry_name == '恶名·杜拉罕'
    assert expert.charge_text == '40'

    unknown = ChargeSource('区域巡防', '猎人与忠犬')
    assert unknown.charge_text == ''


def test_material_info_from_row() -> None:
    """yaml 一行 → 材料信息，缺字段兜着不抛异常。"""
    info = MaterialInfo.from_row(
        {
            'material_name': '某材料',
            'aliases': ['别名一', '别名二'],
            'category': 'basic',
            'tier': 2,
            'charge_source': [
                {
                    'category_name': '区域巡防',
                    'mission_type_name': '猎人与忠犬',
                    'charge_per_run': 60,
                }
            ],
            'amount_per_run': None,
            'farmable': True,
            'source': '游戏内仓库 DETAIL',
        }
    )
    assert info.material_name == '某材料'
    assert info.aliases == ('别名一', '别名二')
    assert info.tier == 2
    assert info.amount_per_run is None
    assert info.first_source is not None
    assert info.first_source.mission_type_name == '猎人与忠犬'
    assert info.first_source.charge_per_run == 60


# ============================= 换算函数 =============================


def test_estimate_owned() -> None:
    """绕开 10 分钟延迟：已有 + 已刷次数 × 单次产出。"""
    assert estimate_owned(100, 2, 5) == 110
    assert estimate_owned(100, 0, 5) == 100
    assert estimate_owned(100, 2, None) == 100
    assert estimate_owned(None, 2, 5) is None


def test_plan_times_for() -> None:
    """次数 = ceil(缺口 ÷ 单次产出)；缺口 0 不用刷；单次产出未知按默认次数并给说明。"""
    assert plan_times_for(10, 4) == (3, '')
    assert plan_times_for(0, 4) == (0, '')
    assert plan_times_for(-3, 4) == (0, '')
    times, degrade = plan_times_for(100, None)
    assert times == 1
    assert degrade == DEGRADE_AMOUNT_UNKNOWN
    times, degrade = plan_times_for(None, None, default_plan_times=3)
    assert times == 3
    assert '默认次数' in degrade


# ============================= 单条材料目标 =============================


def test_build_farmable_material() -> None:
    """可刷材料：缺口 100、单次产出未知 → 排 1 次，条目名是副本名而不是材料名。"""
    planner = MaterialGoalPlanner()

    plan = planner.build({'material_name': '特化流明芯片', 'target_floor': 100}, 0)

    assert plan.gap == 100
    assert plan.plan_times == 1
    assert plan.planned is True
    assert plan.entry is not None
    assert plan.entry.entry_name == '琢光测试'
    assert plan.entry.charge_text == '20-100'
    assert plan.entry.progress_text == '0/100'
    assert any(DEGRADE_AMOUNT_UNKNOWN in warning for warning in plan.warnings)


def test_build_not_farmable_material() -> None:
    """菲林这类材料不消耗电量、没法用体力刷：不生成计划项，但缺口照样算出来给人看。"""
    planner = MaterialGoalPlanner()

    plan = planner.build(MaterialGoal('菲林', 1000), 0)

    assert plan.info is not None
    assert plan.gap == 1000
    assert plan.plan_times == 0
    assert plan.planned is False
    assert any('没法用体力刷' in warning for warning in plan.warnings)


def test_build_alias_expert_challenge_material() -> None:
    """高维数据用短名查表，落到专业挑战室的对应 boss，电量 40。"""
    planner = MaterialGoalPlanner()

    plan = planner.build(MaterialGoal('凶刑讣告', 20), 10)

    assert plan.gap == 10
    assert plan.entry is not None
    assert plan.entry.entry_name == '恶名·杜拉罕'
    assert plan.entry.charge_text == '40'


def test_build_unknown_material() -> None:
    """材料表里没有的材料：不生成计划项，不抛异常。"""
    planner = MaterialGoalPlanner()

    plan = planner.build(MaterialGoal('不存在的材料', 10), 0)

    assert plan.info is None
    assert plan.gap is None
    assert plan.plan_times == 0
    assert plan.planned is False
    assert any('材料表里没有' in warning for warning in plan.warnings)


def test_build_empty_material_name() -> None:
    """空材料名给单独一句提示，不是「查不到」那一句。"""
    planner = MaterialGoalPlanner()
    plan = planner.build(MaterialGoal('', 10), 0)
    assert plan.planned is False
    assert any('材料名是空的' in warning for warning in plan.warnings)


def test_build_already_enough() -> None:
    """已有数量已经超过目标：不排计划。"""
    planner = MaterialGoalPlanner()

    plan = planner.build(MaterialGoal('特化流明芯片', 100), 200)

    assert plan.gap == 0
    assert plan.plan_times == 0
    assert plan.planned is False
    assert any('已经够了' in warning for warning in plan.warnings)


def test_build_owned_unknown() -> None:
    """已有数量读不到：缺口算不出来，按默认次数排，并说明原因。"""
    planner = MaterialGoalPlanner(default_plan_times=2)

    plan = planner.build(MaterialGoal('特化流明芯片', 100), None)

    assert plan.gap is None
    assert plan.plan_times == 2
    assert plan.planned is True
    assert len([w for w in plan.warnings if '已有数量' in w or '默认次数' in w]) >= 2
    assert plan.entry is not None
    assert plan.entry.progress_text is None


def test_build_negative_target() -> None:
    """目标下限写负数时按 0 处理，并留一条提示。"""
    planner = MaterialGoalPlanner()
    plan = planner.build(MaterialGoal('特化流明芯片', -5), 0)
    assert any('负数' in warning for warning in plan.warnings)


def test_build_uses_ran_times_to_estimate() -> None:
    """已知单次产出时，本轮已刷的次数要算进「已有」，避免延迟期内重复多刷。"""
    table = MaterialTable(
        [
            MaterialInfo(
                material_name='测试材料',
                charge_source=(ChargeSource('区域巡防', '猎人与忠犬', None, 60),),
                amount_per_run=5,
            )
        ]
    )
    planner = MaterialGoalPlanner(material_table=table)

    plan = planner.build(MaterialGoal('测试材料', 30), 10, ran_times=2)

    assert plan.estimated_owned == 20
    assert plan.gap == 10
    assert plan.plan_times == 2


def test_build_without_charge_source() -> None:
    """材料表里有这条、但没有副本入口：不生成计划项。"""
    table = MaterialTable([MaterialInfo(material_name='没入口材料', farmable=True)])
    planner = MaterialGoalPlanner(material_table=table)

    plan = planner.build(MaterialGoal('没入口材料', 10), 0)

    assert plan.planned is False
    assert any('没有副本入口' in warning for warning in plan.warnings)


def test_summary_line() -> None:
    """日志行：排上的写副本名和次数，没排的写原因。"""
    planner = MaterialGoalPlanner()
    planned = planner.build(MaterialGoal('特化流明芯片', 100), 0)
    skipped = planner.build(MaterialGoal('菲林', 1000), 0)

    assert planned.summary_line().startswith('[排上]')
    assert '琢光测试' in planned.summary_line()
    assert skipped.summary_line().startswith('[不排]')


# ============================= 批量与跨层接线 =============================


def test_build_snapshot() -> None:
    """多条目标一次算：生成成功的进清单，全部提示汇总返回。"""
    planner = MaterialGoalPlanner()
    item_list = [
        MaterialOwned(MaterialGoal('特化流明芯片', 100), owned=0),
        MaterialOwned(MaterialGoal('菲林', 1000), owned=0),
        MaterialOwned(MaterialGoal('凶刑讣告', 20), owned=10),
    ]

    snapshot, warnings = planner.build_snapshot(item_list)

    assert len(snapshot.entries) == 2
    assert {entry.entry_name for entry in snapshot.entries} == {'琢光测试', '恶名·杜拉罕'}
    assert any('菲林' in warning for warning in warnings)


def test_material_owned_from_row() -> None:
    """配置里的一条材料目标（字典）直接读。"""
    owned = MaterialOwned.from_row(
        {'material_name': '特化流明芯片', 'target_floor': 100, 'owned': 40, 'ran_times': 3}
    )
    assert owned.goal.material_name == '特化流明芯片'
    assert owned.goal.target_floor == 100
    assert owned.owned == 40
    assert owned.ran_times == 3


def test_material_entry_feeds_plan_mapper(full_ctx: StubContext) -> None:
    """材料层生成的条目能被映射层认出来——这条跨层接线必须成立。

    `PlanEntry.entry_name` 填的是副本名（琢光测试），不是材料名（特化流明芯片），
    所以 `PlanMapper` 按副本名索引能找到它。
    """
    from agent_charge_plan.planner.plan_models import PlanSnapshot

    planner = MaterialGoalPlanner()
    plan = planner.build(MaterialGoal('特化流明芯片', 100), 0)
    assert plan.entry is not None

    result = PlanMapper(full_ctx).build(PlanSnapshot(entries=[plan.entry]), ChargeAvailability())

    assert len(result.plan_list) == 1
    item = result.plan_list[0]
    assert (item.category_name, item.mission_type_name, item.mission_name) == (
        '实战模拟室', '代理人技能', '琢光测试',
    )
    assert result.warnings == []
