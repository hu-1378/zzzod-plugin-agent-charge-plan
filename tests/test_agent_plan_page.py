"""特训目标页的解析规则：分组、条目名、电量三形态、进度、页面判定。

这些用例跑真 OCR（`OnnxOcrMatcher`，CPU），fixture 是 `docs/screens/快捷手册/` 里的留档截图。
判据都来自 `operation/agent_plan_page.py`，所以这里断言的是「插件声称能认出来的东西」。

一个重要的背景：OCR 结果会随模型版本轻微变化，所以断言尽量写成
「这个名字/这个分组/这个数值范围」，而不是把整段 OCR 文本原样钉死。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from agent_charge_plan.operation import agent_plan_page, screen_areas

if TYPE_CHECKING:
    import numpy as np
    from agent_charge_plan.operation.agent_plan_page import AgentPlanItem, TextBlock
    from conftest import PlanPageData, StubContext

    from one_dragon.base.geometry.rectangle import Rect


#: 5 张留档截图的名字（去后缀），和 docs/screens/快捷手册/ 一一对应
TOP = '特训目标_安东_顶部'
SCROLLING = '特训目标_安东_滚动中'
BOTTOM = '特训目标_安东_滚到底'
ONLY_DISC = '特训目标_已达成只剩驱动盘'
DISC_GROUP = '特训目标_驱动盘分组'


def _names(items: list[AgentPlanItem]) -> list[str]:
    """取条目名列表，方便断言。"""
    return [item.name for item in items]


# ============================= 区域坐标 =============================


def test_plan_list_area_uses_fallback_constant(plan_list_rect: Rect) -> None:
    """主仓 screen_info 没有清单区时，用插件里的兜底坐标（1080p）。"""
    assert (plan_list_rect.x1, plan_list_rect.y1) == (470, 410)
    assert (plan_list_rect.x2, plan_list_rect.y2) == (1675, 975)


def test_unknown_area_raises_key_error(bare_ctx: StubContext) -> None:
    """既没主仓定义、也没兜底常量的区域要明确报错，而不是静默返回 0。"""
    with pytest.raises(KeyError):
        screen_areas.get_area_rect(bare_ctx, screen_areas.COMPENDIUM_SCREEN, '区域-不存在')


# ============================= 页面判定 =============================


def test_blue_highlight_on_all_plan_screens(
    plan_pages: dict[str, PlanPageData], left_cat_rect: Rect
) -> None:
    """左侧分类「特训目标」在 5 张截图上都是蓝底高亮。"""
    for page in plan_pages.values():
        assert agent_plan_page.is_blue_highlight(page.screen, left_cat_rect) is True, page.name


def test_blue_highlight_rejects_non_highlight_area(
    plan_pages: dict[str, PlanPageData],
) -> None:
    """拿一块明显不是蓝底的地方（角色卡右侧空白）判，必须为 False，否则判据等于没写。"""
    from one_dragon.base.geometry.rectangle import Rect

    page = plan_pages[TOP]
    assert agent_plan_page.is_blue_highlight(page.screen, Rect(1700, 60, 1800, 140)) is False


def test_is_agent_plan_page_on_plan_screens(
    bare_ctx: StubContext, plan_pages: dict[str, PlanPageData]
) -> None:
    """5 张特训目标页截图都判成「在特训目标页」。"""
    for page in plan_pages.values():
        assert agent_plan_page.is_agent_plan_page(bare_ctx, page.screen) is True, page.name


def test_is_agent_plan_page_false_on_other_screen(
    bare_ctx: StubContext, material_calc_screen: np.ndarray
) -> None:
    """换成别的画面（这里用材料计算弹窗的截图）就不能判成特训目标页。"""
    assert agent_plan_page.is_agent_plan_page(bare_ctx, material_calc_screen) is False


def test_has_text_finds_change_target_button(
    bare_ctx: StubContext, plan_pages: dict[str, PlanPageData]
) -> None:
    """「更换特训目标」按钮只在特训目标页有，兜底判据靠它。"""
    page = plan_pages[TOP]
    rect: Rect = screen_areas.get_area_rect(
        bare_ctx, screen_areas.COMPENDIUM_SCREEN, screen_areas.AREA_CHANGE_TARGET
    )
    assert agent_plan_page.has_text(bare_ctx, page.screen, rect, '更换特训目标') is True


# ============================= 角色名 =============================


def test_read_agent_name(plan_pages: dict[str, PlanPageData]) -> None:
    """角色卡上的名字按 OCR 文本读回来，两种不同角色都要对。"""
    assert plan_pages[TOP].agent_name == '安东·伊万诺夫'
    assert plan_pages[ONLY_DISC].agent_name == '叶瞬光'


def test_pick_agent_name_takes_longest_chinese() -> None:
    """角色名区域里有多块文字时取含中文且最长的那块。"""
    from one_dragon.base.geometry.rectangle import Rect

    blocks = [
        agent_plan_page.TextBlock(text=' ', rect=Rect(0, 0, 10, 10)),
        agent_plan_page.TextBlock(text='安东·伊万诺夫', rect=Rect(0, 0, 100, 30)),
        agent_plan_page.TextBlock(text='Lv.', rect=Rect(0, 0, 20, 10)),
    ]
    assert agent_plan_page.pick_agent_name(blocks) == '安东·伊万诺夫'


def test_pick_agent_name_empty() -> None:
    """区域里没有中文时返回空字符串，不抛异常。"""
    assert agent_plan_page.pick_agent_name([]) == ''


# ============================= 清单解析 =============================


def test_parse_top_screen(plan_pages: dict[str, PlanPageData]) -> None:
    """顶部截图：代理人分组一条 + 技能分组一条，电量都是按卡数浮动的 20-100。"""
    items = plan_pages[TOP].items
    assert _names(items) == ['调查员记录', '代理人技能']
    assert [item.group_name for item in items] == ['代理人', '技能']
    assert [item.charge_text for item in items] == ['20-100', '20-100']
    assert (items[0].progress_current, items[0].progress_total) == (129, 290)
    assert (items[1].progress_current, items[1].progress_total) == (153, 203)


def test_parse_scrolling_screen_fixes_group_name(plan_pages: dict[str, PlanPageData]) -> None:
    """滚动中截图：OCR 把分组标题「技能」读成「按能」，要靠候选表纠正回来。"""
    items = plan_pages[SCROLLING].items
    assert [item.group_name for item in items] == ['技能', '技能']
    assert '代理人技能' in _names(items)
    assert [item.charge_text for item in items] == ['20-100', '40']


def test_parse_bottom_screen_broken_progress(plan_pages: dict[str, PlanPageData]) -> None:
    """滚到底截图：有一块被裁掉一半的进度读成 1537203，不能让它变成一条脏数据。

    实测这块文字在第一条条目名字的上面，所以它不属于任何条目，会被丢掉。
    """
    items = plan_pages[BOTTOM].items
    assert len(items) == 2
    for item in items:
        if item.progress_current is None:
            continue
        assert item.progress_current <= item.progress_total, item
        assert item.progress_total < 10_000, item


def test_parse_bottom_screen_items(plan_pages: dict[str, PlanPageData]) -> None:
    """滚到底截图：专业挑战室的 40 点条目 + 驱动盘分组的 60 点条目（没有进度条）。"""
    items = plan_pages[BOTTOM].items
    assert '猎人与忠犬' in _names(items)
    disc = next(item for item in items if item.name == '猎人与忠犬')
    assert disc.group_name == '驱动盘'
    assert disc.charge_text == '60'
    assert disc.progress_text is None
    assert disc.is_finished is False

    changel = next(item for item in items if '提丰' in item.name)
    assert changel.charge_text == '40'
    assert (changel.progress_current, changel.progress_total) == (4, 60)
    assert changel.is_finished is False


def test_parse_disc_group_screen(plan_pages: dict[str, PlanPageData]) -> None:
    """驱动盘分组截图：两条都没有进度条，只有电量，这是游戏本来的样子。"""
    items = plan_pages[DISC_GROUP].items
    assert _names(items) == ['猎人与忠犬', '高塔与巨炮']
    assert all(item.group_name == '驱动盘' for item in items)
    assert all(item.charge_text == '60' for item in items)
    assert all(item.progress_text is None for item in items)


def test_parse_only_disc_screen(plan_pages: dict[str, PlanPageData]) -> None:
    """养成完成的角色只剩驱动盘分组（诡步与重壁 / 怪兽二重奏），一样要能读出来。"""
    items = plan_pages[ONLY_DISC].items
    assert _names(items) == ['诡步与重壁', '怪兽二重奏']
    assert all(item.group_name == '驱动盘' for item in items)


def test_every_item_has_center_and_charge(plan_pages: dict[str, PlanPageData]) -> None:
    """每条条目都要有中心点（点击用）和电量文本，缺了后面点不到。"""
    for page in plan_pages.values():
        for item in page.items:
            assert item.center.x > 0 and item.center.y > 0, (page.name, item)
            assert item.charge_text != '', (page.name, item)


def test_text_block_rect_is_screen_coordinate(
    plan_pages: dict[str, PlanPageData], plan_list_rect: Rect
) -> None:
    """文字块的坐标要换算成整张截图的坐标，不能是裁剪区内的相对坐标。"""
    page = plan_pages[TOP]
    assert page.blocks, '清单区一块文字都没读到，检查 OCR 模型'
    for block in page.blocks:
        assert block.rect.y1 >= plan_list_rect.y1
        assert block.rect.x1 >= plan_list_rect.x1


# ============================= 分类与纠正 =============================


def test_classify_text_block(plan_list_rect: Rect) -> None:
    """文字块分类：分组标题 / 条目名 / 电量 / 进度 / 前往按钮。"""
    from one_dragon.base.geometry.rectangle import Rect

    def block(text: str, x1: int, y1: int, height: int = 30) -> TextBlock:
        return agent_plan_page.TextBlock(
            text=text, rect=Rect(x1, y1, x1 + 120, y1 + height)
        )

    left = plan_list_rect.x1
    assert agent_plan_page.classify_text_block(block('代理人', left + 20, 460), plan_list_rect) == 'group'
    assert agent_plan_page.classify_text_block(block('调查员记录', left + 65, 540), plan_list_rect) == 'name'
    assert agent_plan_page.classify_text_block(block('20-100', left + 970, 558), plan_list_rect) == 'charge'
    assert agent_plan_page.classify_text_block(block('60', left + 999, 556), plan_list_rect) == 'charge'
    assert agent_plan_page.classify_text_block(block('129/290', left + 432, 636), plan_list_rect) == 'progress'
    assert agent_plan_page.classify_text_block(block('前往', left + 967, 631), plan_list_rect) == 'go'


def test_classify_ignores_charge_in_wrong_column(plan_list_rect: Rect) -> None:
    """清单区左边那些短数字（例如格子里的等级）不能被当成电量。"""
    from one_dragon.base.geometry.rectangle import Rect

    near_left = agent_plan_page.TextBlock(
        text='60', rect=Rect(plan_list_rect.x1 + 10, 500, plan_list_rect.x1 + 60, 530)
    )
    assert agent_plan_page.classify_text_block(near_left, plan_list_rect) != 'charge'


@pytest.mark.parametrize(
    ('raw', 'expected'),
    [
        ('技能', '技能'),
        ('按能', '技能'),
        ('代理人', '代理人'),
        ('驱动盘', '驱动盘'),
        ('驱功盘', '驱动盘'),
        ('新分组', '新分组'),
        ('', ''),
    ],
)
def test_normalize_group_name(raw: str, expected: str) -> None:
    """分组标题纠正：OCR 错字对到三个已分组名，对不上就原样返回交给上层降级。"""
    assert agent_plan_page.normalize_group_name(raw) == expected


@pytest.mark.parametrize(
    ('raw', 'expected'),
    [
        ('129/290', (129, 290)),
        ('4/60', (4, 60)),
        ('153／203', (153, 203)),
        ('1537203', (None, None)),
        ('1/2/3', (None, None)),
        ('', (None, None)),
        (None, (None, None)),
    ],
)
def test_parse_progress_text(raw: str | None, expected: tuple[int | None, int | None]) -> None:
    """进度拆分：只认「数字/数字」，被裁掉斜杠的坏数据一律当没有进度。"""
    assert agent_plan_page.parse_progress_text(raw) == expected


def test_is_finished() -> None:
    """进度满了算完成；没有进度的条目（驱动盘）一律算没完成。"""
    item = agent_plan_page.AgentPlanItem(group_name='代理人', name='调查员记录')
    assert item.is_finished is False

    item.progress_current, item.progress_total = 290, 290
    assert item.is_finished is True

    item.progress_current, item.progress_total = 291, 290
    assert item.is_finished is True

    item.progress_current, item.progress_total = 129, 290
    assert item.is_finished is False


def test_agent_name_similar() -> None:
    """名字比较：支持全名/简称/间隔号差异，不同的人要判 False。"""
    assert agent_plan_page.agent_name_similar('安东·伊万诺夫', '安东') is True
    assert agent_plan_page.agent_name_similar('安东', '安东·伊万诺夫') is True
    assert agent_plan_page.agent_name_similar('叶瞬光', '叶瞬光') is True
    assert agent_plan_page.agent_name_similar('叶瞬光', '安东') is False
    assert agent_plan_page.agent_name_similar('', '安东') is False


def test_parse_plan_items_ignores_orphan_numbers(plan_list_rect: Rect) -> None:
    """只有零散数字、没有名字的那一截（滚到一半被裁掉的行）不产生条目。"""
    from one_dragon.base.geometry.rectangle import Rect

    blocks: list[TextBlock] = [
        agent_plan_page.TextBlock(
            text='153/203',
            rect=Rect(plan_list_rect.x1 + 432, plan_list_rect.y1 + 5,
                      plan_list_rect.x1 + 551, plan_list_rect.y1 + 35),
        ),
        agent_plan_page.TextBlock(
            text='40',
            rect=Rect(plan_list_rect.x1 + 999, plan_list_rect.y1 + 8,
                      plan_list_rect.x1 + 1044, plan_list_rect.y1 + 45),
        ),
    ]
    assert agent_plan_page.parse_plan_items(blocks, plan_list_rect) == []


def test_parse_plan_items_attaches_fields_to_nearest_name(plan_list_rect: Rect) -> None:
    """名字之后出现的电量、进度、前往按钮都算这一条的。"""
    from one_dragon.base.geometry.rectangle import Rect

    left, top = plan_list_rect.x1, plan_list_rect.y1
    blocks: list[TextBlock] = [
        agent_plan_page.TextBlock(text='驱动盘', rect=Rect(left + 20, top + 30, left + 110, top + 66)),
        agent_plan_page.TextBlock(text='高塔与巨炮', rect=Rect(left + 65, top + 120, left + 227, top + 160)),
        agent_plan_page.TextBlock(text='60', rect=Rect(left + 999, top + 136, left + 1044, top + 174)),
        agent_plan_page.TextBlock(text='前往', rect=Rect(left + 967, top + 211, left + 1042, top + 254)),
    ]
    items = agent_plan_page.parse_plan_items(blocks, plan_list_rect)
    assert len(items) == 1
    assert items[0].group_name == '驱动盘'
    assert items[0].name == '高塔与巨炮'
    assert items[0].charge_text == '60'
    assert items[0].go_button_center is not None


def test_screen_array_is_bgr(plan_pages: dict[str, PlanPageData]) -> None:
    """夹具给的是 BGR 图（不是 RGB），判颜色之前必须先确认这一点。"""
    screen: np.ndarray = plan_pages[TOP].screen
    assert screen.ndim == 3
    assert screen.shape[2] == 3
    assert screen.shape[0] == 1080
    assert screen.shape[1] == 1920
