"""滚动收集：把一屏一屏读到的条目合并成总表，以及「没有新条目 = 滚到底」的判定。

`read_agent_plan_list.py` 的循环只做两件事：读一屏 → `merge_item_list` → 新增 0 条就收工。
这里用留档的三张连续截图（顶部 / 滚动中 / 滚到底）真实复现那一段循环，
另外补几条纯数据结构的边界用例（不跑 OCR，跑得快）。

注意：`merge_item_list` 会**就地**改条目对象（补电量、进度、中心点），
所以每个用例先 deepcopy 一份再合并，否则同一份 session 夹具会被前一个用例改脏。
"""

from __future__ import annotations

import copy
from typing import TYPE_CHECKING

from agent_charge_plan.operation import agent_plan_page

if TYPE_CHECKING:
    from agent_charge_plan.operation.agent_plan_page import (
        AgentPlanItem,
        AgentPlanSnapshot,
    )
    from conftest import PlanPageData

    from one_dragon.base.geometry.rectangle import Rect


TOP = '特训目标_安东_顶部'
SCROLLING = '特训目标_安东_滚动中'
BOTTOM = '特训目标_安东_滚到底'


def _names(items: list[AgentPlanItem]) -> list[str]:
    """取条目名列表。"""
    return [item.name for item in items]


def _items(page: PlanPageData) -> list[AgentPlanItem]:
    """拷一份该屏读到的条目。"""
    return copy.deepcopy(page.items)


# ============================= 真实截图的滚动循环 =============================


def test_scroll_until_no_new_item(plan_pages: dict[str, PlanPageData]) -> None:
    """照读取层的写法滚三屏：顶部 → 滚动中 → 滚到底，最后一屏还能读出 1 条新条目。

    这一屏读到的第 4 条是驱动盘分组的「猎人与忠犬」，所以此时不能判定到底。
    """
    merged: list[AgentPlanItem] = []
    new_count_list: list[int] = []
    for name in (TOP, SCROLLING, BOTTOM):
        merged, new_count = agent_plan_page.merge_item_list(merged, _items(plan_pages[name]))
        new_count_list.append(new_count)

    assert new_count_list == [2, 1, 1]
    assert _names(merged) == ['调查员记录', '代理人技能', '「提丰·重击者型', '猎人与忠犬']


def test_scroll_stops_when_screen_repeats(plan_pages: dict[str, PlanPageData]) -> None:
    """列表滚到底后再读一屏（内容与上一屏一样）：新增 0 条，读取层就该结束循环。"""
    merged: list[AgentPlanItem] = []
    for name in (TOP, SCROLLING, BOTTOM):
        merged, _ = agent_plan_page.merge_item_list(merged, _items(plan_pages[name]))
    count_before = len(merged)

    merged, new_count = agent_plan_page.merge_item_list(merged, _items(plan_pages[BOTTOM]))

    assert new_count == 0
    assert len(merged) == count_before


def test_merge_keeps_group_from_earlier_screen(plan_pages: dict[str, PlanPageData]) -> None:
    """滚到底那一屏的分组标题「驱动盘」在上一屏，提丰这条读下来分组是空的；
    合并后必须保住第一屏读到的「技能」，不能变成空分组。"""
    merged: list[AgentPlanItem] = []
    for name in (TOP, SCROLLING, BOTTOM):
        merged, _ = agent_plan_page.merge_item_list(merged, _items(plan_pages[name]))

    changel = next(item for item in merged if '提丰' in item.name)
    assert changel.group_name == '技能'


def test_merge_fills_fields_read_later(plan_pages: dict[str, PlanPageData]) -> None:
    """上一屏只读到名字（被裁掉一半，没有电量和进度），这一屏读全了要补进来。"""
    full_item = _items(plan_pages[TOP])[0]
    partial = agent_plan_page.AgentPlanItem(
        group_name=full_item.group_name, name=full_item.name
    )
    assert partial.charge_text == ''
    assert partial.progress_current is None

    merged, new_count = agent_plan_page.merge_item_list([partial], [full_item])

    assert new_count == 0
    assert merged[0].charge_text == '20-100'
    assert (merged[0].progress_current, merged[0].progress_total) == (129, 290)


def test_merge_updates_click_point(plan_pages: dict[str, PlanPageData]) -> None:
    """同一个条目出现在两屏时，中心点取最新那屏的（滚动后位置变了，旧坐标点不到）。"""
    merged, _ = agent_plan_page.merge_item_list([], _items(plan_pages[SCROLLING]))
    before = next(item for item in merged if '提丰' in item.name).center

    bottom_screen = _items(plan_pages[BOTTOM])
    merged, new_count = agent_plan_page.merge_item_list(merged, bottom_screen)

    assert new_count == 1
    after = next(item for item in merged if '提丰' in item.name).center
    assert after != before
    assert after == next(item for item in bottom_screen if '提丰' in item.name).center


# ============================= 纯数据结构的边界 =============================


def test_merge_empty_new_list_keeps_old(plan_pages: dict[str, PlanPageData]) -> None:
    """这一屏一条都没读到（例如滚动惯性导致空白），已读到的不能丢。"""
    merged, _ = agent_plan_page.merge_item_list([], _items(plan_pages[TOP]))
    after, new_count = agent_plan_page.merge_item_list(merged, [])
    assert new_count == 0
    assert len(after) == len(merged)


def test_merge_fuzzy_same_name() -> None:
    """名字 OCR 结果略有差别（括号、错一个字）要算同一条，否则一屏会记两遍。"""
    old = [agent_plan_page.AgentPlanItem(group_name='技能', name='提丰·重击者型」')]
    new = [agent_plan_page.AgentPlanItem(group_name='技能', name='「提丰·重击者型', charge_text='40')]

    merged, new_count = agent_plan_page.merge_item_list(old, new)

    assert new_count == 0
    assert len(merged) == 1
    assert merged[0].charge_text == '40'


def test_merge_does_not_merge_different_names() -> None:
    """名字完全不同的两条不能合并（否则永远读不到新条目，会误判到底）。"""
    old = [agent_plan_page.AgentPlanItem(group_name='驱动盘', name='猎人与忠犬')]
    new = [agent_plan_page.AgentPlanItem(group_name='驱动盘', name='高塔与巨炮')]
    merged, new_count = agent_plan_page.merge_item_list(old, new)
    assert new_count == 1
    assert len(merged) == 2


def test_merge_keeps_existing_progress() -> None:
    """旧条目已经有进度时，新一屏读到更差的进度不能覆盖它。"""
    old = [
        agent_plan_page.AgentPlanItem(
            group_name='代理人',
            name='调查员记录',
            charge_text='20-100',
            progress_text='129/290',
            progress_current=129,
            progress_total=290,
        )
    ]
    new = [agent_plan_page.AgentPlanItem(group_name='代理人', name='调查员记录')]

    merged, _ = agent_plan_page.merge_item_list(old, new)

    assert merged[0].progress_current == 129
    assert merged[0].progress_text == '129/290'


def test_snapshot_from_merged_list(plan_pages: dict[str, PlanPageData]) -> None:
    """合并结果能被打包成读取层的输出结构（外层流程就是这么交出去的）。"""
    merged: list[AgentPlanItem] = []
    for name in (TOP, SCROLLING, BOTTOM):
        merged, _ = agent_plan_page.merge_item_list(merged, _items(plan_pages[name]))

    snapshot: AgentPlanSnapshot = agent_plan_page.AgentPlanSnapshot(
        agent_name='安东·伊万诺夫', item_list=merged
    )

    assert snapshot.agent_name == '安东·伊万诺夫'
    assert len(snapshot.item_list) == 4


def test_merge_synthetic_repeated_screen_is_bottom() -> None:
    """模拟「拖动没生效、每次都读到同一屏」：第二次就一定新增 0 条。

    这是读取层判定到底的方式，也说明为什么实机清单里要单独验证滚动量——
    拖不动会立刻被判成到底，条目会漏。
    """
    screen: list[AgentPlanItem] = [
        agent_plan_page.AgentPlanItem(group_name='驱动盘', name='诡步与重壁', charge_text='60'),
        agent_plan_page.AgentPlanItem(group_name='驱动盘', name='怪兽二重奏', charge_text='60'),
    ]
    merged, first = agent_plan_page.merge_item_list([], screen)
    merged, second = agent_plan_page.merge_item_list(merged, screen)

    assert (first, second) == (2, 0)
    assert len(merged) == 2


def test_classify_go_button_block(plan_list_rect: Rect) -> None:
    """「前往」按钮单独一类，用来带点击位置；不能被当成条目名。"""
    from one_dragon.base.geometry.rectangle import Rect

    block = agent_plan_page.TextBlock(
        text='前往',
        rect=Rect(plan_list_rect.x1 + 967, plan_list_rect.y1 + 221,
                  plan_list_rect.x1 + 1042, plan_list_rect.y1 + 265),
    )
    assert agent_plan_page.classify_text_block(block, plan_list_rect) == 'go'
