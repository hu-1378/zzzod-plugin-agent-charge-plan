"""更换特训目标弹窗：名字识别、模糊匹配、黄框选中判定。

弹窗里每格是「头像 + 稀有度角标 + 名字 + Lv.60」。识别规则要先把这些杂字丢掉，
再按名字找目标；找到之后如果这一格已经有黄色选中框，就不要再点一次
（重复点击会白等 1.2 秒，甚至把弹窗弄关）。

真 OCR 用 `docs/screens/特训目标-更换特训目标/弹窗_代理人网格.webp`，
网格坐标取自插件自带 screen_info，保证测试和实机用的是同一份坐标。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from agent_charge_plan.operation import agent_plan_page

if TYPE_CHECKING:
    import numpy as np
    from agent_charge_plan.operation.agent_plan_page import DialogAgent, TextBlock

    from one_dragon.base.geometry.rectangle import Rect


#: 弹窗截图里能认出来的 9 个代理人。
#: 顺序 = 读取层的排序规则（先按名字框中心纵坐标，再按左边缘），不是人眼看的「先上后下」：
#: 叶瞬光那格名字框略低 1px，所以排在同一行其他人的后面。
EXPECTED_NAMES = [
    '佩洛伊斯',
    '琉音',
    '卢西娅',
    '仪玄',
    '叶瞬光',
    '蕾米埃尔',
    '薇薇安',
    '艾莲',
    '照',
]


@pytest.fixture(scope='module')
def dialog_blocks(
    bare_ctx: object, dialog_screen: np.ndarray, dialog_grid_rect: Rect
) -> list[TextBlock]:
    """弹窗网格区整块 OCR 出来的文字块。"""
    return agent_plan_page.read_text_blocks(bare_ctx, dialog_screen, dialog_grid_rect)


@pytest.fixture(scope='module')
def dialog_agents(dialog_blocks: list[TextBlock]) -> list[DialogAgent]:
    """过滤掉等级、角标、按钮文字之后的代理人列表。"""
    return agent_plan_page.parse_dialog_agents(dialog_blocks)


# ============================= 名字过滤 =============================


def test_dialog_agent_names(dialog_agents: list[DialogAgent]) -> None:
    """9 格代理人全部认出来，顺序按阅读顺序。"""
    assert [agent.name for agent in dialog_agents] == EXPECTED_NAMES


def test_level_text_is_dropped(dialog_agents: list[DialogAgent]) -> None:
    """Lv.60 这种等级文字不能当成代理人名字。"""
    assert all(not agent.name.lower().startswith('lv') for agent in dialog_agents)


def test_short_noise_text_is_dropped(dialog_blocks: list[TextBlock]) -> None:
    """稀有度角标 S、格子下方的碎字（高度只有 13px 的「佳哈」）都要丢掉。

    这两个是用例在真实截图上抓到的噪声，如果过滤条件放松它们会混进代理人列表。
    """
    kept = [agent.name for agent in agent_plan_page.parse_dialog_agents(dialog_blocks)]
    assert 'S' not in kept
    assert '佳哈' not in kept


@pytest.mark.parametrize(
    ('text', 'height', 'expected'),
    [
        ('叶瞬光', 30, True),
        ('Lv.60', 27, False),
        ('S', 35, False),
        ('佳哈', 13, False),
        ('特训方案', 30, False),
        ('清除目标', 30, False),
        ('6037', 30, False),
        ('', 30, False),
    ],
)
def test_is_dialog_agent_name(text: str, height: int, expected: bool) -> None:
    """逐个验证名字过滤规则（不依赖截图，纯规则）。"""
    from one_dragon.base.geometry.rectangle import Rect

    block = agent_plan_page.TextBlock(text=text, rect=Rect(0, 0, 100, height))
    assert agent_plan_page.is_dialog_agent_name(block) is expected


# ============================= 找人与点击位置 =============================


def test_find_dialog_agent_exact(dialog_blocks: list[TextBlock]) -> None:
    """精确名字能找到对应的那一格。"""
    agent = agent_plan_page.find_dialog_agent(dialog_blocks, '叶瞬光')
    assert agent is not None
    assert agent.name == '叶瞬光'


def test_find_dialog_agent_fuzzy(dialog_blocks: list[TextBlock]) -> None:
    """目标名字和 OCR 结果差一个字（蕾 / 雷）也要能命中，靠 difflib 容错。"""
    agent = agent_plan_page.find_dialog_agent(dialog_blocks, '雷米埃尔')
    assert agent is not None
    assert agent.name == '蕾米埃尔'


def test_find_dialog_agent_missing(dialog_blocks: list[TextBlock]) -> None:
    """弹窗里没有的代理人返回 None，由调用方去滚动下一屏。"""
    assert agent_plan_page.find_dialog_agent(dialog_blocks, '完全不存在的人') is None


def test_click_point_is_above_name(dialog_agents: list[DialogAgent]) -> None:
    """点击位置 = 名字框中心往上挪 80px（头像在名字上面），横坐标不变。"""
    for agent in dialog_agents:
        point = agent.click_point()
        assert point.x == agent.name_rect.center.x
        assert point.y == agent.name_rect.center.y - agent_plan_page.DIALOG_NAME_CLICK_OFFSET
        # 往上挪之后仍要落在头像上，不能挪出格子（实机清单里专门验证这一点）
        assert point.y >= agent.name_rect.y1 - 120


def test_click_point_hits_avatar_band(dialog_agents: list[DialogAgent]) -> None:
    """同一行代理人的点击纵坐标应当基本一致（格子是网格对齐的）。"""
    y_list = [agent.click_point().y for agent in dialog_agents[:6]]
    assert max(y_list) - min(y_list) <= 10


# ============================= 黄框选中判定 =============================


def test_selected_cell_detected(dialog_screen: np.ndarray, dialog_grid_rect: Rect) -> None:
    """截图里当前选中的是「叶瞬光」，黄框能找出来，且够大。"""
    cell = agent_plan_page.find_selected_cell_rect(dialog_screen, dialog_grid_rect)
    assert cell is not None
    assert cell.width >= agent_plan_page.SELECTED_FRAME_MIN_WIDTH
    assert cell.height >= agent_plan_page.SELECTED_FRAME_MIN_HEIGHT


def test_selected_cell_contains_current_agent(
    dialog_screen: np.ndarray,
    dialog_grid_rect: Rect,
    dialog_agents: list[DialogAgent],
) -> None:
    """黄框正好框住「叶瞬光」那一格——这就是「不要重复点」的依据。"""
    cell = agent_plan_page.find_selected_cell_rect(dialog_screen, dialog_grid_rect)
    assert cell is not None

    def inside(agent: DialogAgent) -> bool:
        """和 `change_agent_plan_target._is_agent_selected` 同一个判据。"""
        center = agent.name_rect.center
        return cell.x1 <= center.x <= cell.x2 and cell.y1 <= center.y <= cell.y2

    selected = [agent.name for agent in dialog_agents if inside(agent)]
    assert selected == ['叶瞬光']


def test_selected_cell_does_not_cover_other_rows(
    dialog_screen: np.ndarray,
    dialog_grid_rect: Rect,
    dialog_agents: list[DialogAgent],
) -> None:
    """第二行的代理人（照 / 薇薇安 / 艾莲）不能被判成选中，否则该点的时候不点。"""
    cell = agent_plan_page.find_selected_cell_rect(dialog_screen, dialog_grid_rect)
    assert cell is not None
    second_row = [agent for agent in dialog_agents if agent.name in ('照', '薇薇安', '艾莲')]
    assert len(second_row) == 3
    for agent in second_row:
        assert agent.name_rect.center.y > cell.y2


def test_no_selected_cell_on_plan_screen(
    plan_list_rect: Rect, dialog_screen: np.ndarray
) -> None:
    """没有黄框的地方（拿清单区当网格区）要返回 None，不能瞎报一个格子。"""
    assert agent_plan_page.find_selected_cell_rect(dialog_screen, plan_list_rect) is None
