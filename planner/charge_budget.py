"""电量相关的换算：解析电量文本、算卡数、按总电量预算分配次数。

先把游戏里的几个事实写在这（实测，别改）：

- 实战模拟室按卡数扣电量：一张卡 20 点，一次最多 5 张，所以页面上写的是 `20-100`。
- 专业挑战室固定 40 点一次。
- 区域巡防、恶名狩猎（深度追猎）固定 60 点一次。
- 自然电量一天上限 240 点；储蓄电量和以太电池是另外两块，一颗以太电池顶 60 点。

这里只做纯数字计算，不碰配置、不碰画面，方便单测。
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from zzz_od.application.charge_plan.charge_plan_config import CardNumEnum

#: 一张卡要多少电量
CARD_CHARGE = 20
#: 实战模拟室一次能选几张卡
CARD_NUM_MIN = 1
CARD_NUM_MAX = 5
#: 自然电量一天的上限
NATURAL_CHARGE_CAP = 240
#: 一颗以太电池顶多少电量
ETHER_BATTERY_CHARGE = 60
#: 内置体力计划里「不指定卡数」的写法，直接取枚举，别抄字面量
CARD_NUM_DEFAULT: str = CardNumEnum.DEFAULT.value.value

#: 固定电量的分类，跟 ChargePlanItem.estimated_charge_power 保持一致
CATEGORY_FIXED_CHARGE: dict[str, int] = {
    '专业挑战室': 40,
    '区域巡防': 60,
    '恶名狩猎': 60,
    '合成电池': 60,
}

#: 实战模拟室是唯一按卡数浮动电量的分类
CATEGORY_COMBAT_SIMULATION: str = '实战模拟室'

#: 电量文本里可能出现的分隔符（OCR 可能给短横线、波浪线或中文的「至」）
_CHARGE_SEPARATORS: tuple[str, ...] = ('-', '~', '－', '—', '–', '至', '到')


def _digits_only(text: str) -> str:
    """只留下数字字符。"""
    return ''.join(ch for ch in text if ch.isdigit())


@dataclass(frozen=True)
class ChargeRange:
    """一次刷本要花多少电量。"""

    min_charge: int
    max_charge: int
    per_card: bool = False
    """True 表示按卡数浮动（`20-100`），False 表示固定（`40` / `60`）。"""

    @property
    def card_bounds(self) -> tuple[int, int]:
        """按卡数算的上下限。只有 per_card 为 True 时才有意义。"""
        low = min(max(self.min_charge // CARD_CHARGE, CARD_NUM_MIN), CARD_NUM_MAX)
        high = min(max(self.max_charge // CARD_CHARGE, CARD_NUM_MIN), CARD_NUM_MAX)
        return low, high

    @property
    def text(self) -> str:
        """还原成给人看的写法。"""
        if self.per_card:
            return f'{self.min_charge}-{self.max_charge}'
        return str(self.max_charge)


def parse_charge_text(text: str | None) -> ChargeRange | None:
    """把电量文本翻成电量范围。

    认三种写法：`20-100`（按卡数浮动）、`40`、`60`（固定）。
    认不出来返回 None，调用方降级处理，不要抛异常。

    Args:
        text: 读取层 OCR 出来的电量文本，可能是 None

    Returns:
        电量范围；看不懂时 None
    """
    if text is None:
        return None

    cleaned = ''.join(text.split())  # 去掉所有空白，含全角空格
    if cleaned == '':
        return None

    numbers: list[int] = []
    buffer = ''
    for ch in cleaned:
        if ch.isdigit():
            buffer += ch
        elif ch in _CHARGE_SEPARATORS and buffer:
            numbers.append(int(buffer))
            buffer = ''
    if buffer != '':
        numbers.append(int(buffer))

    if not numbers:
        return None

    low, high = min(numbers), max(numbers)
    return ChargeRange(min_charge=low, max_charge=high, per_card=low < high)


def parse_progress_text(text: str | None) -> tuple[int, int] | None:
    """把进度文本 `129/290` 翻成 `(已有, 需要)`。

    只认「数字/数字」这一种形态，其它一律返回 None，不去拆数字猜。两种已知的坏形态：

    - 驱动盘分组没有进度条，读取层给 None。
    - 行被裁掉一半时 OCR 会把斜杠吃掉，`153/203` 读成 `1537203`（读取层多屏合并会用完整形态覆盖，
      但单屏直读时可能就这一份）。这种当「没有进度」，宁可按默认次数排一次，也不把 1537203 拆成两半。

    Args:
        text: 进度文本

    Returns:
        (已有, 需要)；解析不出来时 None
    """
    if text is None:
        return None

    cleaned = ''.join(text.split()).replace('／', '/')
    left, sep, right = cleaned.partition('/')
    if sep == '':
        return None
    if '/' in left or '/' in right:
        # 不是「数字/数字」的形态（例如 1/2/3），也不猜
        return None

    owned = _digits_only(left)
    total = _digits_only(right)
    if owned == '' or total == '':
        return None

    return int(owned), int(total)


@dataclass(frozen=True)
class ChargeAvailability:
    """手上能用多少电量。"""

    natural: int = NATURAL_CHARGE_CAP
    """自然电量，一天上限 240。"""
    backup: int = 0
    """储蓄电量。"""
    ether_battery: int = 0
    """以太电池颗数，一颗顶 60 点。"""

    @property
    def total(self) -> int:
        """总共能花多少电量。"""
        return self.natural + self.backup + self.ether_battery * ETHER_BATTERY_CHARGE


@dataclass(frozen=True)
class BudgetRequest:
    """一个条目来排队要电量。"""

    key: str
    """内容级去重键：`分类|mission_type|mission`。"""
    label: str
    """给人看的名字。"""
    category_name: str
    """副本分类，决定电量是固定还是按卡数浮动。"""
    charge_range: ChargeRange | None
    """页面上读到的电量范围，读不到就是 None。"""
    missing: int | None
    """还差多少个材料，没有进度信息时 None。"""
    amount_per_run: int | None = None
    """单次刷本能拿多少个，材料层算出来才有。"""
    priority: tuple[int, float] = (0, 0.0)
    """越小越先占电量：先看分组顺序，再看缺口占比大的先排。"""


@dataclass(frozen=True)
class BudgetAllocation:
    """分给一个条目的电量。"""

    key: str
    plan_times: int = 0
    """排几次。0 表示跳过。"""
    card_num: str = CARD_NUM_DEFAULT
    """实战模拟室一次选几张卡；其他分类保持「默认数量」。"""
    charge_per_run: int = 0
    """一次要多少电量。"""
    charge_total: int = 0
    """这个条目一共要多少电量。"""
    skipped: bool = False
    """是否被跳过（进度够了、电量不够）。"""
    note: str = ''
    """给人看的说明，比如「电量只够 2 次」。"""


class ChargeBudget:
    """按优先级把总电量分给各个条目，分不到的就截断。

    截断只砍「刷几次」，不删条目：内置体力计划是循环跑的，今天电量不够的条目排上一次，
    明天电量回来了接着跑。硬删掉会让缺口永远排不上计划。
    """

    def __init__(
        self,
        available: ChargeAvailability,
        default_plan_times: int = 1,
        keep_unaffordable: bool = True,
    ) -> None:
        """初始化。

        Args:
            available: 手上能用的电量
            default_plan_times: 不知道单次产出时先排几次（默认 1 次，靠内置体力计划的循环把缺口补完）
            keep_unaffordable: 今天电量连一次都不够时，还排不排（True = 排 1 次等明天）
        """
        self.available: ChargeAvailability = available
        self.default_plan_times: int = max(default_plan_times, 0)
        self.keep_unaffordable: bool = keep_unaffordable

    def allocate(self, requests: list[BudgetRequest]) -> list[BudgetAllocation]:
        """按优先级依次分配电量。

        Args:
            requests: 所有要排队的条目

        Returns:
            和 requests 一一对应的分配结果（顺序按优先级重排过）
        """
        remaining = self.available.total
        allocations: list[BudgetAllocation] = []

        for request in sorted(requests, key=lambda r: (r.priority, r.key)):
            allocation = self._allocate_one(request, remaining)
            remaining -= allocation.charge_total
            allocations.append(allocation)

        return allocations

    def estimate_plan_times(self, missing: int | None, amount_per_run: int | None) -> int:
        """按缺口和单次产出估算要刷几次。

        Args:
            missing: 还差多少个材料；None 表示不知道
            amount_per_run: 单次产出；None 表示不知道

        Returns:
            次数；0 表示进度已经够了，不用刷
        """
        if missing is not None and missing <= 0:
            return 0
        if missing is None or amount_per_run is None or amount_per_run <= 0:
            return self.default_plan_times
        return math.ceil(missing / amount_per_run)

    def pick_card_num(self, available_charge: int, charge_range: ChargeRange) -> str:
        """按手上电量定实战模拟室的卡数：每张 20 点，卡数卡在 1~5 之间。

        Args:
            available_charge: 还没花掉的电量
            charge_range: 页面上读到的电量范围

        Returns:
            卡数字符串，直接塞给 ChargePlanItem.card_num
        """
        low, high = charge_range.card_bounds
        card = available_charge // CARD_CHARGE
        return str(min(max(card, low), high))

    def run_cost(self, request: BudgetRequest, remaining: int) -> tuple[str, int]:
        """算这个条目刷一次要花多少电量、实战模拟室选几张卡。

        Args:
            request: 排队条目
            remaining: 还没花掉的电量，用来定卡数

        Returns:
            (卡数字符串, 一次要花的电量)
        """
        charge_range = request.charge_range
        if charge_range is None:
            # 没读到电量文本时退回该分类的固定电量；实战模拟室退回 0（不占预算）
            return CARD_NUM_DEFAULT, CATEGORY_FIXED_CHARGE.get(request.category_name, 0)
        if charge_range.per_card:
            card_num = self.pick_card_num(remaining, charge_range)
            return card_num, int(card_num) * CARD_CHARGE
        return CARD_NUM_DEFAULT, charge_range.max_charge

    def _allocate_one(self, request: BudgetRequest, remaining: int) -> BudgetAllocation:
        """分一个条目的电量。"""
        times = self.estimate_plan_times(request.missing, request.amount_per_run)
        if times <= 0:
            return BudgetAllocation(key=request.key, skipped=True, note='进度已经够了，不排计划')

        card_num, per_run = self.run_cost(request, remaining)

        if per_run <= 0:
            # 没有电量信息，不知道要花多少，就不占预算，次数照默认排
            return BudgetAllocation(
                key=request.key,
                plan_times=times,
                card_num=CARD_NUM_DEFAULT,
                note='没读到电量，没算进电量预算',
            )

        affordable = remaining // per_run
        if affordable <= 0:
            if not self.keep_unaffordable:
                return BudgetAllocation(
                    key=request.key,
                    skipped=True,
                    note=f'电量不够跑一次（一次 {per_run} 点），已跳过',
                )
            return BudgetAllocation(
                key=request.key,
                plan_times=1,
                card_num=card_num,
                charge_per_run=per_run,
                note=f'今天电量不够跑一次（一次 {per_run} 点），先排 1 次，等电量回来了再跑',
            )

        used_times = min(times, affordable)
        note = '' if used_times == times else f'电量只够 {used_times} 次（本来要 {times} 次）'
        return BudgetAllocation(
            key=request.key,
            plan_times=used_times,
            card_num=card_num,
            charge_per_run=per_run,
            charge_total=used_times * per_run,
            note=note,
        )
