"""边刷边算：三阶段优先级、本地记账、节点图。

这三块是最容易写错的地方，用假数据钉死：

- 优先级：①有活就不能去干 ②，①②都没活才轮到 ③
- 记账：进度按「单次产出」累加、电量按副本消耗扣，全程不重读页面
- 节点图：唯一起始节点，没有指向不存在节点的边，两条链路都走得通

不碰游戏：挑任务的 `ChargeTaskPicker` 是纯逻辑，只需要真副本表和真材料表；
执行链路的节点图靠 `_init_network()` 扫注解，不需要真的跑起来。
"""

from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

from agent_charge_plan.agent_charge_plan_app import AgentChargePlanApp
from agent_charge_plan.operation.execute_charge_plan import (
    FALLBACK_KEY,
    STAGE_AGENT_PLAN,
    STAGE_FALLBACK,
    STAGE_MATERIAL,
    STATUS_NO_TASK,
    ChargeTask,
    ChargeTaskPicker,
    ExecuteChargePlan,
    RunBook,
    entry_key,
    material_key,
    parse_fallback_plan,
)
from agent_charge_plan.planner.charge_budget import ChargeAvailability
from agent_charge_plan.planner.material_goal import (
    MaterialGoal,
    MaterialGoalPlanner,
    MaterialTable,
)
from agent_charge_plan.planner.plan_mapper import PlanMapper
from agent_charge_plan.planner.plan_models import PlanSnapshot

from zzz_od.application.charge_plan.charge_plan_config import ChargePlanItem

if TYPE_CHECKING:
    from conftest import StubContext


# ============================= 造数据的小工具 =============================


def _row(
    group: str, name: str, charge: str = '', progress: str | None = None
) -> dict[str, Any]:
    """造一行特训目标条目。"""
    return {
        'group_name': group,
        'entry_name': name,
        'charge_text': charge,
        'progress_text': progress,
    }


def _book(
    rows: list[dict[str, Any]],
    charge: int = 240,
    agent_name: str = '安东·伊万诺夫',
) -> RunBook:
    """造一本已经读过清单的记账本。"""
    book = RunBook(remaining_charge=charge)
    book.agent_snapshot = PlanSnapshot.from_rows(rows, agent_name=agent_name)
    book.agent_base_progress = {
        entry_key(entry): entry.progress
        for entry in book.agent_snapshot.entries
        if entry.progress is not None
    }
    return book


def _picker(
    full_ctx: StubContext,
    book: RunBook,
    material_goals: list[MaterialGoal] | None = None,
    fallback: dict[str, Any] | None = None,
) -> ChargeTaskPicker:
    """造一个挑任务器。"""
    goal_list = material_goals or []
    return ChargeTaskPicker(
        mapper=PlanMapper(full_ctx),
        book=book,
        material_planner=MaterialGoalPlanner() if goal_list else None,
        material_goal_list=goal_list,
        fallback_item=parse_fallback_plan(fallback),
    )


def _fallback_row() -> dict[str, Any]:
    """一个能跑的保底任务：区域巡防（一次 60 电量）。"""
    return {
        'category_name': '区域巡防',
        'mission_type_name': '猎人与忠犬',
        'mission_name': None,
        'card_num': '默认数量',
    }


def _drive_disc_only_row() -> dict[str, Any]:
    """驱动盘条目：只有电量、没有进度条。"""
    return _row('驱动盘', '猎人与忠犬', charge='60', progress=None)


# ============================= 三阶段优先级 =============================


def test_priority_agent_plan_wins_over_material_and_fallback(
    full_ctx: StubContext,
) -> None:
    """① 有活时，即使 ②③ 也有活，也只挑 ①。"""
    book = _book([_row('代理人', '调查员记录', '20-100', '129/290')])
    book.material_owned = {'特化流明芯片': 0}
    picker = _picker(
        full_ctx,
        book,
        material_goals=[MaterialGoal('特化流明芯片', 100)],
        fallback=_fallback_row(),
    )

    task = picker.pick()

    assert task is not None
    assert task.stage == STAGE_AGENT_PLAN
    assert task.label == '代理人-调查员记录'
    assert task.plan_item.category_name == '实战模拟室'


def test_priority_material_when_agent_plan_finished(full_ctx: StubContext) -> None:
    """① 的条目都满足了，就轮到 ②（材料库存）。"""
    # 进度写满 = 缺口 0，① 没活干
    book = _book([_row('代理人', '调查员记录', '20-100', '290/290')])
    book.material_owned = {'特化流明芯片': 0}
    picker = _picker(
        full_ctx,
        book,
        material_goals=[MaterialGoal('特化流明芯片', 100)],
        fallback=_fallback_row(),
    )

    task = picker.pick()

    assert task is not None
    assert task.stage == STAGE_MATERIAL
    assert '特化流明芯片' in task.label


def test_priority_fallback_when_nothing_else(full_ctx: StubContext) -> None:
    """①② 都没活干，且配了保底任务 → 挑 ③。"""
    book = _book([_drive_disc_only_row()])
    picker = _picker(full_ctx, book, fallback=_fallback_row())

    task = picker.pick()

    assert task is not None
    assert task.stage == STAGE_FALLBACK
    assert task.key == FALLBACK_KEY
    assert task.plan_item.category_name == '区域巡防'
    assert task.plan_item.mission_type_name == '猎人与忠犬'
    assert task.charge_per_run == 60


def test_priority_none_when_no_work_anywhere(full_ctx: StubContext) -> None:
    """①② 没活干、也没配保底 → 这一轮没任务。"""
    book = _book([_drive_disc_only_row()])
    picker = _picker(full_ctx, book)

    assert picker.pick() is None


def test_drive_disc_entry_never_picked(full_ctx: StubContext) -> None:
    """驱动盘条目没有进度条，缺口判断不了，不进 ①（也就不会去刷盘）。"""
    book = _book([_drive_disc_only_row()])

    assert _picker(full_ctx, book).pick() is None


def test_entry_with_broken_progress_not_picked(full_ctx: StubContext) -> None:
    """进度读坏了（1537203 这种斜杠被吃）也当「判断不了」，不硬刷。"""
    book = _book([_row('技能', '代理人技能', '20-100', '1537203')])

    assert _picker(full_ctx, book).pick() is None


def test_unknown_entry_is_skipped_not_raised(full_ctx: StubContext) -> None:
    """认不出的条目只记跳过，不影响后面的条目。"""
    book = _book(
        [
            _row('代理人', '新版本才有的条目', '20-100', '1/10'),
            _row('代理人', '调查员记录', '20-100', '129/290'),
        ]
    )
    picker = _picker(full_ctx, book)

    task = picker.pick()

    assert task is not None
    assert task.label == '代理人-调查员记录'
    assert '特训目标:代理人|新版本才有的条目' in picker.book.skipped_keys


def test_finished_entry_moves_to_next_entry(full_ctx: StubContext) -> None:
    """排在前面的条目刷够了，就自动挑下一条。"""
    book = _book(
        [
            _row('代理人', '调查员记录', '20-100', '290/290'),
            _row('技能', '代理人技能', '20-100', '153/203'),
        ]
    )

    task = _picker(full_ctx, book).pick()

    assert task is not None
    assert task.label == '技能-代理人技能'


def test_priority_group_order_agent_before_skill(full_ctx: StubContext) -> None:
    """两个分组都缺进度时，先刷「代理人」分组（和 PlanMapper 的排序一致）。"""
    book = _book(
        [
            _row('技能', '代理人技能', '20-100', '1/203'),  # 缺口占比大
            _row('代理人', '调查员记录', '20-100', '280/290'),  # 缺口占比小
        ]
    )

    task = _picker(full_ctx, book).pick()

    assert task is not None
    assert task.label == '代理人-调查员记录'


# ============================= 电量不够 =============================


def test_not_enough_charge_returns_none(full_ctx: StubContext) -> None:
    """电量连一次都不够时，宁可不刷（内置体力计划那边是排 1 次等明天，这里不是）。"""
    book = _book([_row('驱动盘', '猎人与忠犬', '60', None)], charge=40)
    picker = _picker(full_ctx, book, fallback=_fallback_row())

    # 驱动盘没进度不进 ①；保底要 60，只剩 40 → 也不刷
    assert picker.pick() is None


def test_charge_shrinks_card_num(full_ctx: StubContext) -> None:
    """实战模拟室按卡数消耗电量：电量少时自动选少一点的卡数。"""
    book = _book([_row('代理人', '调查员记录', '20-100', '129/290')], charge=60)

    task = _picker(full_ctx, book).pick()

    assert task is not None
    assert task.plan_item.card_num == '3'
    assert task.charge_per_run == 60


def test_plan_times_is_always_one(full_ctx: StubContext) -> None:
    """一轮只刷一次：计划项的 plan_times 固定 1，次数由循环自己控。"""
    book = _book([_row('代理人', '调查员记录', '20-100', '1/290')])

    task = _picker(full_ctx, book).pick()

    assert task is not None
    assert task.plan_item.plan_times == 1


# ============================= 本地记账 =============================


def test_bookkeeping_three_runs() -> None:
    """刷 3 次后进度按「单次推进 1」累加，电量按次扣除。"""
    book = _book([_row('技能', '代理人技能', '20-100', '153/203')], charge=240)
    entry = book.agent_snapshot.entries[0]
    key = entry_key(entry)

    assert book.estimate_entry_missing(entry) == 50

    task = ChargeTask(
        stage=STAGE_AGENT_PLAN,
        label=entry.label,
        plan_item=ChargePlanItem(),
        charge_per_run=20,
        key=key,
    )
    for _ in range(3):
        book.spend(task.charge_per_run)
        book.record_run(task)

    assert entry.progress_text == '156/203'
    assert book.estimate_entry_missing(entry) == 47
    assert book.remaining_charge == 240 - 60
    assert book.agent_ran_times[key] == 3


def test_bookkeeping_stops_at_total() -> None:
    """刷过头也不会把进度刷成 204/203。"""
    book = _book([_row('技能', '代理人技能', '20-100', '201/203')], charge=240)
    entry = book.agent_snapshot.entries[0]
    task = ChargeTask(
        stage=STAGE_AGENT_PLAN,
        label=entry.label,
        plan_item=ChargePlanItem(),
        charge_per_run=20,
        key=entry_key(entry),
    )

    for _ in range(5):
        book.record_run(task)

    assert entry.progress_text == '203/203'
    assert entry.missing == 0
    assert book.estimate_entry_missing(entry) == 0


def test_bookkeeping_charge_never_negative() -> None:
    """电量扣到 0 就停，不会变负数。"""
    book = RunBook(remaining_charge=30)

    book.spend(40)

    assert book.remaining_charge == 0


def _fake_material_planner(material_name: str, amount_per_run: int) -> MaterialGoalPlanner:
    """造一个「知道单次产出」的材料换算器。

    真实材料表里现在所有材料的 `amount_per_run` 都是 null（没实测数据），
    那样缺口永远估不出来；要验证「刷够就退出」得先给个确定值。
    """
    real_table = MaterialTable.load()
    real_info = real_table.get(material_name)
    assert real_info is not None, f'材料表里没有 {material_name}'
    fake_info = replace(real_info, amount_per_run=amount_per_run)
    return MaterialGoalPlanner(material_table=MaterialTable([fake_info]))


def test_bookkeeping_material_runs() -> None:
    """材料阶段按「刷了几次」累加已有数量，缺口跟着缩小。"""
    book = _book([])
    planner = _fake_material_planner('特化流明芯片', 10)
    goal = MaterialGoal('特化流明芯片', 100)
    book.material_owned = {'特化流明芯片': 0}

    first = planner.build(goal, book.material_owned['特化流明芯片'], 0)
    assert first.entry is not None
    assert first.gap == 100

    task = ChargeTask(
        stage=STAGE_MATERIAL,
        label='特化流明芯片',
        plan_item=ChargePlanItem(),
        charge_per_run=40,
        key=material_key('特化流明芯片'),
        material_name='特化流明芯片',
    )
    for _ in range(3):
        book.record_run(task)

    ran_times = book.material_ran_times['特化流明芯片']
    assert ran_times == 3
    second = planner.build(goal, book.material_owned['特化流明芯片'], ran_times)
    # 已有 0 + 刷 3 次 × 每次 10 = 30，缺口 100 - 30 = 70
    assert second.estimated_owned == 30
    assert second.gap == 70


def test_material_gap_unknown_amount_keeps_gap() -> None:
    """材料表里单次产出是 null 时，缺口估不出来：已有不变，次数按默认走，但次数照记。"""
    book = _book([])
    planner = MaterialGoalPlanner()
    real_table = MaterialTable.load()
    info = real_table.get('特化流明芯片')
    assert info is not None and info.amount_per_run is None

    goal = MaterialGoal('特化流明芯片', 100)
    book.material_owned = {'特化流明芯片': 0}
    task = ChargeTask(
        stage=STAGE_MATERIAL,
        label='特化流明芯片',
        plan_item=ChargePlanItem(),
        charge_per_run=40,
        key=material_key('特化流明芯片'),
        material_name='特化流明芯片',
    )
    for _ in range(3):
        book.record_run(task)

    plan = planner.build(goal, book.material_owned['特化流明芯片'], book.material_ran_times['特化流明芯片'])
    assert plan.estimated_owned == 0
    assert plan.gap == 100
    assert plan.plan_times == 1


def test_pick_after_three_runs_moves_on(full_ctx: StubContext) -> None:
    """刷到条目满足后，同一份清单再挑就换下一条（不需要重读页面）。"""
    book = _book(
        [
            _row('代理人', '调查员记录', '20-100', '289/290'),
            _row('技能', '代理人技能', '20-100', '153/203'),
        ]
    )
    picker = _picker(full_ctx, book)

    first = picker.pick()
    assert first is not None
    assert first.label == '代理人-调查员记录'

    book.record_run(first)
    book.spend(first.charge_per_run)

    second = picker.pick()
    assert second is not None
    assert second.label == '技能-代理人技能'


def test_material_picked_after_local_bookkeeping(full_ctx: StubContext) -> None:
    """材料刷够之后，② 自己会退出，不会一直刷同一种材料。"""
    book = _book([])
    planner = _fake_material_planner('特化流明芯片', 10)
    book.material_owned = {'特化流明芯片': 0}
    picker = ChargeTaskPicker(
        mapper=PlanMapper(full_ctx),
        book=book,
        material_planner=planner,
        material_goal_list=[MaterialGoal('特化流明芯片', 20)],
    )

    task = picker.pick()
    assert task is not None and task.stage == STAGE_MATERIAL
    assert task.material_name == '特化流明芯片'

    # 刷 2 次 = 20，正好到目标下限，② 就该没活了
    book.record_run(task)
    book.record_run(task)
    assert picker.pick() is None


# ============================= 保底任务解析 =============================


def test_parse_fallback_plan_full() -> None:
    """完整配置能解析成计划项，次数固定 1。"""
    item = parse_fallback_plan(
        {
            'category_name': '实战模拟室',
            'mission_type_name': '基础材料',
            'mission_name': '调查专项',
            'card_num': '3',
        }
    )

    assert item is not None
    assert (item.category_name, item.mission_type_name, item.mission_name) == (
        '实战模拟室',
        '基础材料',
        '调查专项',
    )
    assert item.card_num == '3'
    assert item.plan_times == 1
    assert item.tab_name == '训练'


def test_parse_fallback_plan_empty() -> None:
    """空字典 = 不刷保底。"""
    assert parse_fallback_plan(None) is None
    assert parse_fallback_plan({}) is None


def test_parse_fallback_plan_needs_category() -> None:
    """缺分类、缺副本类型、分类跑不了 → 都当没配。"""
    assert parse_fallback_plan({'mission_type_name': '猎人与忠犬'}) is None
    assert parse_fallback_plan({'category_name': '区域巡防'}) is None
    assert parse_fallback_plan(
        {'category_name': '合成电池', 'mission_type_name': '合成电池'}
    ) is None


def test_parse_fallback_plan_ignores_unknown_fields() -> None:
    """配置里多写的字段直接忽略，不报错。"""
    item = parse_fallback_plan(
        {
            'category_name': '区域巡防',
            'mission_type_name': '猎人与忠犬',
            '不认识的字段': 1,
            'notorious_hunt_buff_num': 'abc',
        }
    )

    assert item is not None
    assert item.notorious_hunt_buff_num == 1
    assert item.card_num == '默认数量'


# ============================= 节点图 =============================


class _FakeConfig:
    """执行链路只读配置里的这几个属性，用不着真配置对象（那要读 YAML）。"""

    default_plan_times: int = 1
    include_drive_disc: bool = False
    material_goals: list[dict[str, Any]] = []
    fallback_plan: dict[str, Any] = {}
    batch_agent_names: list[str] = []
    run_mode: str = 'execute'
    goal_mode: str = 'agent_stage'
    dry_run: bool = True
    target_agent_name: str = ''
    scan_only: bool = False
    last_generated_keys: list[str] = []


class _FakeRunContext:
    """应用构造时只会调这两个，给个空壳就够。"""

    def get_config(self, app_id: str, instance_idx: int, group_id: str) -> _FakeConfig:
        return _FakeConfig()

    def get_run_record(self, app_id: str, instance_idx: int) -> None:
        return None


def _app_ctx(full_ctx: StubContext) -> SimpleNamespace:
    """给应用构造用的上下文：真副本表 + 假运行上下文。"""
    return SimpleNamespace(
        compendium_service=full_ctx.compendium_service,
        ocr=full_ctx.ocr,
        screen_loader=full_ctx.screen_loader,
        run_context=_FakeRunContext(),
        current_instance_idx=0,
    )


def _check_graph(op: Any, name: str) -> None:
    """检查一份节点图：唯一起始节点、边都指向存在的节点。"""
    op._init_network()
    node_name_list = list(op._node_map.keys())
    start_node_list = [node for node in op._node_map.values() if node.is_start_node]

    assert len(start_node_list) == 1, f'{name} 起始节点应该有 1 个，实际 {len(start_node_list)}'
    assert op._start_node is not None, f'{name} 没找到起始节点'
    for from_name, edge_list in op._node_edges_map.items():
        assert from_name in node_name_list, f'{name} 的边起点 {from_name} 不存在'
        for edge in edge_list:
            assert edge.node_to.cn in node_name_list, f'{name} 的边终点 {edge.node_to.cn} 不存在'


def test_execute_op_node_graph(full_ctx: StubContext) -> None:
    """执行链路的节点图合法，而且循环能走通。"""
    op = ExecuteChargePlan(
        _app_ctx(full_ctx),
        config=_FakeConfig(),
        availability=ChargeAvailability(),
    )
    _check_graph(op, 'ExecuteChargePlan')

    node_name_set = set(op._node_map.keys())
    assert {
        '确认特训目标页',
        '读清单',
        '读材料缺口',
        '挑任务',
        '传送到副本',
        '记下传送失败',
        '挑战副本',
        '换下一个角色',
    } <= node_name_set

    # 挑任务必须能回到自己（刷完一轮继续挑），否则循环断了
    pick_edges = {edge.node_to.cn for edge in op._node_edges_map.get('挑任务', [])}
    assert '传送到副本' in pick_edges
    assert '换下一个角色' in pick_edges
    back_edges = {
        source
        for source, edge_list in op._node_edges_map.items()
        if any(edge.node_to.cn == '挑任务' for edge in edge_list)
    }
    assert {'挑战副本', '记下传送失败', '读材料缺口'} <= back_edges


def test_app_node_graph_has_both_paths(full_ctx: StubContext) -> None:
    """应用层两条链路都在：plan_only 走到「返回大世界」，execute 也走到「返回大世界」。"""
    app = AgentChargePlanApp(_app_ctx(full_ctx))
    _check_graph(app, 'AgentChargePlanApp')

    node_name_set = set(app._node_map.keys())
    assert {'开始', '执行前准备', '循环刷本', '返回大世界'} <= node_name_set

    start_edges = {
        (edge.status, edge.node_to.cn) for edge in app._node_edges_map.get('开始', [])
    }
    assert ('边刷边算', '执行前准备') in start_edges
    assert ('读取特训目标', '前往特训目标页') in start_edges

    # 「循环刷本」成功失败都回大世界
    loop_edges = {
        (edge.success, edge.node_to.cn)
        for edge in app._node_edges_map.get('循环刷本', [])
    }
    assert (True, '返回大世界') in loop_edges
    assert (False, '返回大世界') in loop_edges

    # plan_only 那条链路一个节点都没少
    plan_only_edges = {
        (edge.status, edge.node_to.cn)
        for edge in app._node_edges_map.get('开始', [])
    }
    assert ('只处理材料库存', '读取电量') in plan_only_edges
    assert ('扫描代理人名单', '扫描代理人名单') in plan_only_edges


def test_pick_task_status_no_task_is_routable(full_ctx: StubContext) -> None:
    """「挑任务」返回「没活干」时有边可走（不然会静默结束整轮）。"""
    op = ExecuteChargePlan(
        _app_ctx(full_ctx),
        config=_FakeConfig(),
        availability=ChargeAvailability(),
    )
    op._init_network()
    status_map = {
        edge.status: edge.node_to.cn for edge in op._node_edges_map.get('挑任务', [])
    }

    assert status_map[STATUS_NO_TASK] == '换下一个角色'
    assert status_map[STAGE_AGENT_PLAN] == '传送到副本'
    assert status_map[STAGE_MATERIAL] == '传送到副本'
    assert status_map[STAGE_FALLBACK] == '传送到副本'
