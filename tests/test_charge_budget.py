"""电量换算与预算分配：电量文本解析、卡数、次数估算、总电量截断。

对应 `planner/charge_budget.py`。这里全是纯数字，不碰截图也不碰配置。
游戏侧的三个事实（写死在代码里、这里要守住）：实战模拟室按卡 20 点、一次最多 5 张；
专业挑战室固定 40；区域巡防和恶名狩猎固定 60。
"""

from __future__ import annotations

import pytest
from agent_charge_plan.planner.charge_budget import (
    CARD_CHARGE,
    CARD_NUM_DEFAULT,
    CARD_NUM_MAX,
    CARD_NUM_MIN,
    NATURAL_CHARGE_CAP,
    BudgetRequest,
    ChargeAvailability,
    ChargeBudget,
    ChargeRange,
    parse_charge_text,
    parse_progress_text,
)

# ============================= 电量文本 =============================


@pytest.mark.parametrize(
    ('raw', 'expected'),
    [
        ('20-100', (20, 100, True)),
        ('20~100', (20, 100, True)),
        ('20至100', (20, 100, True)),
        ('40', (40, 40, False)),
        ('60', (60, 60, False)),
        ('4 0', (40, 40, False)),
    ],
)
def test_parse_charge_text_forms(
    raw: str, expected: tuple[int, int, bool]
) -> None:
    """三种电量形态都要认得：区间（按卡数的实战模拟室）、固定 40、固定 60。"""
    charge_range = parse_charge_text(raw)
    assert charge_range is not None
    assert (charge_range.min_charge, charge_range.max_charge, charge_range.per_card) == expected


@pytest.mark.parametrize('raw', ['', '   ', None, '电量', '前往'])
def test_parse_charge_text_unknown(raw: str | None) -> None:
    """读不出来时返回 None，让调用方降级，绝不抛异常。"""
    assert parse_charge_text(raw) is None


def test_charge_range_text_roundtrip() -> None:
    """`text` 属性还原成给日志看的写法。"""
    assert parse_charge_text('20-100').text == '20-100'
    assert parse_charge_text('40').text == '40'


def test_card_bounds() -> None:
    """区间电量换算成卡数上下限，卡在 1~5 张之间。"""
    low, high = parse_charge_text('20-100').card_bounds
    assert (low, high) == (CARD_NUM_MIN, CARD_NUM_MAX)


def test_card_bounds_clamped() -> None:
    """OCR 读出离谱的区间时，卡数仍然被夹在 1~5 张。"""
    weird = ChargeRange(min_charge=0, max_charge=1000, per_card=True)
    assert weird.card_bounds == (CARD_NUM_MIN, CARD_NUM_MAX)


# ============================= 进度文本 =============================


@pytest.mark.parametrize(
    ('raw', 'expected'),
    [
        ('129/290', (129, 290)),
        ('4/60', (4, 60)),
        ('153／203', (153, 203)),
        ('1537203', None),
        ('1/2/3', None),
        ('/290', None),
        ('129/', None),
        ('', None),
        (None, None),
    ],
)
def test_parse_progress_text(raw: str | None, expected: tuple[int, int] | None) -> None:
    """进度只认「数字/数字」；被裁掉斜杠的 1537203 当「没有进度」，不许拆成两半。"""
    assert parse_progress_text(raw) == expected


# ============================= 卡数与次数 =============================


def test_pick_card_num() -> None:
    """卡数按手上电量定：每张 20 点，夹在 1~5 之间。"""
    budget = ChargeBudget(ChargeAvailability())
    charge_range = parse_charge_text('20-100')
    assert budget.pick_card_num(240, charge_range) == '5'
    assert budget.pick_card_num(60, charge_range) == '3'
    assert budget.pick_card_num(10, charge_range) == '1'


def test_estimate_plan_times() -> None:
    """次数 = 缺口 ÷ 单次产出，向上取整；不知道就退化成默认次数。"""
    budget = ChargeBudget(ChargeAvailability(), default_plan_times=2)
    assert budget.estimate_plan_times(0, 4) == 0
    assert budget.estimate_plan_times(-5, 4) == 0
    assert budget.estimate_plan_times(161, 4) == 41
    assert budget.estimate_plan_times(10, 3) == 4
    assert budget.estimate_plan_times(10, None) == 2
    assert budget.estimate_plan_times(None, 4) == 2


def test_run_cost_fixed_category() -> None:
    """固定电量分类：一次就是该分类的电量，卡数保持「默认数量」。"""
    budget = ChargeBudget(ChargeAvailability())
    request = BudgetRequest(
        key='k', label='L', category_name='专业挑战室',
        charge_range=parse_charge_text('40'), missing=10,
    )
    assert budget.run_cost(request, 240) == (CARD_NUM_DEFAULT, 40)


def test_run_cost_without_charge_text() -> None:
    """电量文本没读到：退回该分类的固定电量；实战模拟室退回 0（不占预算）。"""
    budget = ChargeBudget(ChargeAvailability())
    expert = BudgetRequest(
        key='k1', label='L1', category_name='专业挑战室', charge_range=None, missing=10
    )
    combat = BudgetRequest(
        key='k2', label='L2', category_name='实战模拟室', charge_range=None, missing=10
    )
    assert budget.run_cost(expert, 240) == (CARD_NUM_DEFAULT, 40)
    assert budget.run_cost(combat, 240) == (CARD_NUM_DEFAULT, 0)


def test_run_cost_per_card() -> None:
    """按卡数的分类：一次花 卡数 × 20。"""
    budget = ChargeBudget(ChargeAvailability())
    request = BudgetRequest(
        key='k', label='L', category_name='实战模拟室',
        charge_range=parse_charge_text('20-100'), missing=100,
    )
    card_num, per_run = budget.run_cost(request, 120)
    assert card_num == '5'
    assert per_run == 5 * CARD_CHARGE


# ============================= 预算分配 =============================


def _requests() -> list[BudgetRequest]:
    """一份典型的排队清单：按卡数的实战模拟室 + 专业挑战室 + 已完成 + 没电量信息。"""
    return [
        BudgetRequest(
            key='a', label='实战模拟室', category_name='实战模拟室',
            charge_range=parse_charge_text('20-100'), missing=161,
            amount_per_run=4, priority=(0, -0.5),
        ),
        BudgetRequest(
            key='b', label='专业挑战室', category_name='专业挑战室',
            charge_range=parse_charge_text('40'), missing=56,
            amount_per_run=2, priority=(1, -0.9),
        ),
        BudgetRequest(
            key='c', label='已完成的', category_name='区域巡防',
            charge_range=parse_charge_text('60'), missing=0, priority=(2, 0.0),
        ),
        BudgetRequest(
            key='d', label='没电量的', category_name='恶名狩猎',
            charge_range=None, missing=None, priority=(3, 0.0),
        ),
    ]


def test_allocate_truncates_by_total_charge() -> None:
    """总电量 340：先给优先级高的截断次数，后面的按剩下的分。"""
    budget = ChargeBudget(ChargeAvailability(natural=240, backup=40, ether_battery=1))
    assert budget.available.total == 340

    alloc_dict = {alloc.key: alloc for alloc in budget.allocate(_requests())}

    assert alloc_dict['a'].plan_times == 3
    assert alloc_dict['a'].charge_total == 300
    assert '电量只够 3 次' in alloc_dict['a'].note

    assert alloc_dict['b'].plan_times == 1
    assert alloc_dict['b'].charge_total == 40

    assert alloc_dict['c'].skipped is True
    assert alloc_dict['d'].plan_times == 1
    # 排了 1 次就要把这 1 次的电量算进消耗，否则 estimated_total_charge 会偏少、
    # leftover_charge 会偏多（条目越多偏得越厉害）。以前这里断言的是 0，属于同一个 bug。
    assert alloc_dict['d'].charge_total == 60


def test_allocate_keeps_priority_order() -> None:
    """分配结果按优先级排（分组顺序 → 缺口占比），不是输入顺序。"""
    budget = ChargeBudget(ChargeAvailability())
    alloc_list = budget.allocate(list(reversed(_requests())))
    assert [alloc.key for alloc in alloc_list] == ['a', 'b', 'c', 'd']


def test_allocate_without_enough_charge_for_one_run() -> None:
    """电量连一次都不够：默认仍然排 1 次（等电量回来），并给出说明。"""
    budget = ChargeBudget(ChargeAvailability(natural=20))
    alloc_dict = {alloc.key: alloc for alloc in budget.allocate(_requests())}
    # 20 点正好够实战模拟室 1 张卡跑一次
    assert alloc_dict['a'].plan_times == 1
    assert alloc_dict['a'].charge_per_run == CARD_CHARGE
    assert alloc_dict['b'].skipped is False
    assert alloc_dict['b'].plan_times == 1
    assert '电量不够跑一次' in alloc_dict['b'].note


def test_allocate_can_drop_unaffordable() -> None:
    """配成 keep_unaffordable=False 时，电量不够的条目直接跳过。"""
    budget = ChargeBudget(ChargeAvailability(natural=0), keep_unaffordable=False)
    alloc_dict = {alloc.key: alloc for alloc in budget.allocate(_requests())}
    assert alloc_dict['a'].skipped is True
    assert alloc_dict['a'].note == '电量不够跑一次（一次 20 点），已跳过'


def test_allocate_does_not_spend_budget_on_missing_charge() -> None:
    """没读到电量的条目照样排（默认次数），但不占预算，后面的还能分到电量。"""
    budget = ChargeBudget(ChargeAvailability(natural=60))
    requests = [
        BudgetRequest(
            key='unknown', label='没电量', category_name='实战模拟室',
            charge_range=None, missing=None, priority=(0, 0.0),
        ),
        BudgetRequest(
            key='known', label='有电量', category_name='区域巡防',
            charge_range=parse_charge_text('60'), missing=6,
            amount_per_run=1, priority=(1, 0.0),
        ),
    ]
    alloc_dict = {alloc.key: alloc for alloc in budget.allocate(requests)}
    assert alloc_dict['unknown'].plan_times == 1
    assert alloc_dict['unknown'].charge_total == 0
    assert alloc_dict['known'].plan_times == 1
    assert alloc_dict['known'].charge_total == 60


def test_availability_total() -> None:
    """可用电量 = 自然电量 + 储蓄电量 + 以太电池 × 60。"""
    assert ChargeAvailability().total == NATURAL_CHARGE_CAP
    assert ChargeAvailability(natural=0, backup=30, ether_battery=2).total == 150
