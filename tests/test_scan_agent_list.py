"""扫描代理人名单：名字解析、跨屏去重保序、滚动到底的判据、收尾关弹窗。

对应 `operation/scan_agent_list.py`。用例分三类：

- 真截图：用 `docs/screens/特训目标-更换特训目标/弹窗_代理人网格.webp` 跑真 OCR，
  守住「弹窗里那 9 个名字都能读出来、且不混进 Lv.60 / S / 按钮文字」。
- 纯函数：`merge_agent_names` 的顺序、去重、模糊判重（同一格在滚动边界上被两屏各读一次）。
- 节点行为：滚动节奏、连续两屏没新名字才收工、关不掉弹窗只记 warning 不算失败。
"""

from __future__ import annotations

import inspect
from typing import TYPE_CHECKING, Any

import pytest
from agent_charge_plan.operation import scan_agent_list as sas

from one_dragon.base.geometry.rectangle import Rect
from one_dragon.base.operation.operation_round_result import OperationRoundResultEnum

if TYPE_CHECKING:
    import numpy as np
    from conftest import StubContext


#: 弹窗截图里能认出来的 9 个代理人。
#:
#: 顺序 = 网格阅读顺序（先左右后上下）：网格 6 列 2 行，第一行 6 个、第二行 3 个。
#: 走的是 `agent_plan_page.parse_dialog_agents_by_grid`，按格子行列排序，
#: 不再按 OCR 文字块中心坐标排（同一行里 y 有 ±1px 抖动，顺序会乱）。
EXPECTED_DIALOG_NAMES: list[str] = [
    '叶瞬光',
    '蕾米埃尔',
    '佩洛伊斯',
    '琉音',
    '卢西娅',
    '仪玄',
    '照',
    '薇薇安',
    '艾莲',
]


# ============================= 真截图：一屏读全 =============================


@pytest.fixture(scope='module')
def grid_rect() -> Rect:
    """弹窗网格区域，坐标取自插件自带 screen_info。

    直接用 yml 的坐标（不走 `ScreenLoader`），保证测试和实机用的是同一份数据。
    """
    from conftest import read_screen_area

    return read_screen_area('agent_plan_change_target.yml', '区域-代理人网格')


def test_scan_reads_nine_names(
    bare_ctx: StubContext, dialog_screen: np.ndarray, grid_rect: Rect
) -> None:
    """截图里那 9 个代理人全部读出来，顺序按解析层的阅读顺序。"""
    name_list, _ = sas.read_dialog_screen(bare_ctx, dialog_screen, grid_rect)
    assert name_list == EXPECTED_DIALOG_NAMES


def test_scan_has_no_level_or_badge_text(
    bare_ctx: StubContext, dialog_screen: np.ndarray, grid_rect: Rect
) -> None:
    """`Lv.60`、稀有度角标 `S`、弹窗按钮文字都不能混进名单。"""
    name_list, _ = sas.read_dialog_screen(bare_ctx, dialog_screen, grid_rect)

    assert all(not name.lower().startswith('lv') for name in name_list)
    for noise in ('S', 'A', '特训方案', '清除目标', '更换特训目标', 'Lv.60'):
        assert noise not in name_list, noise


def test_scan_screen_fingerprint(
    bare_ctx: StubContext, dialog_screen: np.ndarray, grid_rect: Rect
) -> None:
    """文字指纹非空、且同一张图两次读出来一样（用来判断「滚动后画面动没动」）。"""
    _, first = sas.read_dialog_screen(bare_ctx, dialog_screen, grid_rect)
    _, second = sas.read_dialog_screen(bare_ctx, dialog_screen, grid_rect)

    assert first != ''
    assert first == second


# ============================= 跨屏合并：保序 + 去重 =============================


def test_merge_keeps_screen_order() -> None:
    """第一屏的名字排在前面，第二屏的新名字接在后面。"""
    merged, new_count = sas.merge_agent_names(['叶瞬光', '蕾米埃尔'], ['琉音', '卢西娅'])

    assert merged == ['叶瞬光', '蕾米埃尔', '琉音', '卢西娅']
    assert new_count == 2


def test_merge_drops_duplicates() -> None:
    """同一格在滚动边界上被两屏各读一次：只留第一次出现的位置。"""
    merged, new_count = sas.merge_agent_names(['叶瞬光', '蕾米埃尔', '琉音'], ['琉音', '卢西娅'])

    assert merged == ['叶瞬光', '蕾米埃尔', '琉音', '卢西娅']
    assert new_count == 1


def test_merge_no_new_when_screen_repeats() -> None:
    """整屏名字都见过：新增 0 个，名单不变。"""
    merged, new_count = sas.merge_agent_names(['叶瞬光', '琉音'], ['琉音', '叶瞬光'])

    assert merged == ['叶瞬光', '琉音']
    assert new_count == 0


def test_merge_tolerates_one_char_ocr_diff() -> None:
    """OCR 把「蕾」读成「雷」时仍算同一个人，不能变成名单里的两条。"""
    merged, new_count = sas.merge_agent_names(['蕾米埃尔'], ['雷米埃尔'])

    assert merged == ['蕾米埃尔']
    assert new_count == 0


def test_merge_keeps_similar_but_different_names() -> None:
    """「安比」和「零号安比」是不同的代理人，不能因为像就合并掉一个。"""
    merged, new_count = sas.merge_agent_names(['安比'], ['零号安比'])

    assert merged == ['安比', '零号安比']
    assert new_count == 1


def test_merge_empty_screen() -> None:
    """这一屏什么都没读到（滚动中/网格空白）时名单不动。"""
    merged, new_count = sas.merge_agent_names(['叶瞬光'], [])

    assert merged == ['叶瞬光']
    assert new_count == 0


# ============================= 结果结构 =============================


def test_agent_name_list_summary() -> None:
    """名单的摘要行：带序号、warning 单独标出来。"""
    name_list = sas.AgentNameList(
        name_list=['叶瞬光', '琉音'], warnings=['弹窗没关掉']
    )

    assert name_list.count == 2
    assert name_list.summary_lines() == ['1. 叶瞬光', '2. 琉音', '[注意] 弹窗没关掉']


def test_scan_agent_list_constructor_signature() -> None:
    """构造参数：默认自己导航到特训目标页。"""
    params = inspect.signature(sas.ScanAgentList.__init__).parameters
    assert params['goto_first'].default is True


def test_no_new_screen_limit_is_two() -> None:
    """「没有新名字」必须连续两次才收工：一次就收会因为拖拽没生效而静默漏人。"""
    assert sas.NO_NEW_SCREEN_LIMIT == 2


def test_area_names_match_screen_info() -> None:
    """区域名和 screen_info yml 里的一致，改 yml 名字时这里会先红。"""
    assert sas.CHANGE_TARGET_SCREEN == '特训目标-更换特训目标'
    assert sas.AREA_DIALOG_TITLE == '标题-更换特训目标'
    assert sas.AREA_AGENT_GRID == '区域-代理人网格'
    assert sas.AREA_CLOSE_BUTTON == '按钮-关闭'


# ============================= 节点行为（不起 ZContext） =============================


@pytest.fixture()
def no_wait(monkeypatch: pytest.MonkeyPatch) -> None:
    """把各种等待时间清零，节点用例不用真睡。"""
    monkeypatch.setattr(sas, 'CLICK_WAIT', 0)
    monkeypatch.setattr(sas, 'SCROLL_WAIT', 0)
    monkeypatch.setattr(sas, 'CLOSE_WAIT', 0)
    monkeypatch.setattr(sas, 'SCROLL_SETTLE', 0)


def _bare_operation(ctx: Any = None) -> Any:
    """造一个没跑 `__init__` 的操作对象，只塞被测方法要用到的属性。

    真 `ScanAgentList(ctx)` 会连带建 `OpenAndEnterGame` 那一整套（要配置、要游戏窗口），
    测一个纯判断不值当。这里 `object.__new__` 跳过构造，缺的东西由用例自己补。
    """
    op: Any = object.__new__(sas.ScanAgentList)
    op.goto_first = True
    op.name_list = []
    op.warnings = []
    op.scroll_times = 0
    op.no_new_times = 0
    op.same_screen_times = 0
    op.last_fingerprint = ''
    op.fail_status = ''
    op.ctx = ctx if ctx is not None else object()
    op.last_screenshot = None
    op.node_retry_times = 0
    op.node_max_retry_times = 5
    return op


def test_handle_init_resets_state() -> None:
    """同实例复用前要把名单和滚动状态清干净。"""
    op = _bare_operation()
    op.name_list = ['叶瞬光']
    op.warnings = ['上次的提示']
    op.scroll_times = 3
    op.fail_status = '上次的失败原因'

    op.handle_init()

    assert op.name_list == []
    assert op.warnings == []
    assert op.scroll_times == 0
    assert op.fail_status == ''


def test_ensure_page_skips_navigation_when_dialog_open() -> None:
    """弹窗已经开着时不要再去导航（页面被弹窗盖住，乱点可能点到别的地方）。"""
    op = _bare_operation()
    op._is_dialog_open = lambda: True

    result = op.ensure_page()

    assert result.is_success
    assert result.status == '弹窗已经开着，跳过导航'


def test_open_dialog_skips_click_when_already_open() -> None:
    """弹窗已经开着就不要再点一次（重复点可能把弹窗关掉）。"""
    op = _bare_operation()
    op._is_dialog_open = lambda: True

    result = op.open_dialog()

    assert result.is_success
    assert result.status == '弹窗已开'


def test_confirm_dialog_fails_with_title_reason() -> None:
    """弹窗一直没弹出来：status 要写清是哪一步卡住（标题没找到）。"""
    op = _bare_operation()
    op.node_retry_times = 4  # 最后一次重试
    op._is_dialog_open = lambda: False

    result = op.confirm_dialog()

    assert result.is_fail
    assert '更换特训目标' in result.status


def test_read_names_scrolls_until_two_empty_screens(
    monkeypatch: pytest.MonkeyPatch, no_wait: None, plugin_ctx: StubContext
) -> None:
    """逐屏读：新名字接着攒，连续两屏没有新名字才收工。"""
    op = _bare_operation(plugin_ctx)
    drag_list: list[bool] = []
    op._drag_grid = lambda area, downward: drag_list.append(downward)

    screens: list[tuple[list[str], str]] = [
        (['叶瞬光', '蕾米埃尔'], 'A'),
        (['蕾米埃尔', '琉音'], 'B'),
        (['琉音'], 'C'),
        (['琉音'], 'D'),
    ]
    monkeypatch.setattr(sas, 'read_dialog_screen', lambda ctx, screen, area: screens.pop(0))

    result_list: list[Any] = []
    for _ in range(10):
        result = op.read_names()
        result_list.append(result)
        if result.is_success:
            break

    assert op.name_list == ['叶瞬光', '蕾米埃尔', '琉音']
    assert op.scroll_times == 3
    assert drag_list == [True, True, True]
    # 前两次是「接着读」，最后一次才收工
    assert [r.result for r in result_list[:-1]] == [
        OperationRoundResultEnum.WAIT,
        OperationRoundResultEnum.WAIT,
        OperationRoundResultEnum.WAIT,
    ]
    assert result_list[-1].status == sas.STATUS_NAMES_READ
    assert op.warnings == []


def test_read_names_warns_when_screen_never_moves(
    monkeypatch: pytest.MonkeyPatch, no_wait: None, plugin_ctx: StubContext
) -> None:
    """画面一直没变（拖拽没生效）：收工，但必须记一条 warning，别让人以为真读全了。"""
    op = _bare_operation(plugin_ctx)
    op._drag_grid = lambda area, downward: None
    monkeypatch.setattr(
        sas, 'read_dialog_screen', lambda ctx, screen, area: (['叶瞬光'], 'A')
    )

    result_list: list[Any] = []
    for _ in range(5):
        result = op.read_names()
        result_list.append(result)
        if result.is_success:
            break

    assert result_list[-1].status == sas.STATUS_NAMES_READ
    assert op.name_list == ['叶瞬光']
    assert len(op.warnings) == 1
    assert '画面一个字都没变' in op.warnings[0]


def test_read_names_stops_at_max_scroll(
    monkeypatch: pytest.MonkeyPatch, no_wait: None, plugin_ctx: StubContext
) -> None:
    """每屏都有新名字时不会死循环：滚够 MAX_SCROLL_TIMES 就收工并记 warning。"""
    op = _bare_operation(plugin_ctx)
    op.scroll_times = sas.MAX_SCROLL_TIMES
    op._drag_grid = lambda area, downward: None
    monkeypatch.setattr(
        sas, 'read_dialog_screen', lambda ctx, screen, area: (['叶瞬光'], 'A')
    )

    result = op.read_names()

    assert result.status == sas.STATUS_NAMES_READ
    assert any('名单可能不全' in warning for warning in op.warnings)


def test_read_names_without_grid_area(no_wait: None) -> None:
    """网格区域没配置：不点不滚，把原因记下来交给收尾节点失败。"""
    op = _bare_operation()
    op._grid_area = lambda: None

    result = op.read_names()

    assert result.status == sas.STATUS_NAMES_EMPTY
    assert '区域没配置' in op.fail_status


def test_close_dialog_success() -> None:
    """读到名字：点关闭后带名单成功，界面回到特训目标页。"""
    op = _bare_operation()
    op.name_list = ['叶瞬光', '琉音']
    op._is_dialog_open = lambda: False
    op._close_dialog = lambda: None

    result = op.close_dialog()

    assert result.is_success
    assert result.data.name_list == ['叶瞬光', '琉音']
    assert '2 个代理人' in result.status
    assert op.warnings == []


def test_close_dialog_warns_when_still_open(no_wait: None) -> None:
    """点了关闭弹窗还在：只记 warning，不算失败（名单已经拿到了）。"""
    op = _bare_operation()
    op.name_list = ['叶瞬光']
    op._is_dialog_open = lambda: True
    op._close_dialog = lambda: None
    op.screenshot = lambda: None

    result = op.close_dialog()

    assert result.is_success
    assert result.data.name_list == ['叶瞬光']
    assert len(op.warnings) == 1
    assert '弹窗还在' in op.warnings[0]


def test_close_dialog_skips_click_when_not_open() -> None:
    """弹窗本来就没开：不要瞎点关闭。"""
    op = _bare_operation()
    op.name_list = ['叶瞬光']
    closed: list[bool] = []
    op._is_dialog_open = lambda: False
    op._close_dialog = lambda: closed.append(True)

    op.close_dialog()

    assert closed == []


def test_close_dialog_fails_without_names() -> None:
    """一个名字都没读到：收尾后报失败，status 说明是网格没对准还是没代理人。"""
    op = _bare_operation()
    op._is_dialog_open = lambda: False
    op._close_dialog = lambda: None

    result = op.close_dialog()

    assert result.is_fail
    assert '一个代理人名字都没读到' in result.status


def test_close_dialog_reports_fail_status() -> None:
    """读名字阶段记下的原因（比如网格区域没配置）优先作为这次操作失败的原因。"""
    op = _bare_operation()
    op.fail_status = '网格区域没配置'
    op._is_dialog_open = lambda: False
    op._close_dialog = lambda: None

    result = op.close_dialog()

    assert result.is_fail
    assert result.status == '网格区域没配置'


# ============================= 节点图：不许有悬空边 / 多个起点 =============================


def _node_map() -> dict[str, Any]:
    """把类上带 `@operation_node` 的方法收成 {节点名: OperationNode}。"""
    nodes: dict[str, Any] = {}
    for method_name in dir(sas.ScanAgentList):
        node = getattr(
            getattr(sas.ScanAgentList, method_name, None),
            'operation_node_annotation',
            None,
        )
        if node is not None:
            nodes[node.cn] = node
    return nodes


def _edge_list() -> list[tuple[str, str]]:
    """把所有 `@node_from` 收成 (起点, 终点) 二元组。"""
    edges: list[tuple[str, str]] = []
    for method_name in dir(sas.ScanAgentList):
        method = getattr(sas.ScanAgentList, method_name, None)
        node = getattr(method, 'operation_node_annotation', None)
        for desc in getattr(method, 'operation_edge_annotation', None) or []:
            if node is None:
                continue
            edges.append((desc.node_from_name, node.cn))
    return edges


def test_node_graph_has_single_start_and_no_dangling_edge() -> None:
    """一个起点、边都指向已声明的节点、除收尾节点外每个节点都能往下走。"""
    nodes = _node_map()
    edges = _edge_list()

    assert nodes, '一个节点都没找到，装饰器元数据可能变了'
    assert sorted(nodes) == [
        '关掉弹窗',
        '回到网格顶部',
        '打开更换弹窗',
        '确认弹窗已开',
        '确认特训目标页',
        '逐屏读名字',
    ], '节点名重复或漏声明，框架建图时会丢掉同名的那个'
    start_node_list = [name for name, node in nodes.items() if node.is_start_node]
    assert start_node_list == ['确认特训目标页']

    for from_name, to_name in edges:
        assert from_name in nodes, f'边的起点 {from_name} 不是已声明的节点'
        assert to_name in nodes, f'边的终点 {to_name} 不是已声明的节点'

    from_name_set = {edge[0] for edge in edges}
    terminal_node_list = [name for name in nodes if name not in from_name_set]
    assert terminal_node_list == ['关掉弹窗']


def _sorted_edge_list() -> list[tuple[str, str]]:
    """边按图的走向排序（装饰器在源码里是自下往上写的，读起来跟流程反着）。"""
    order = [
        '确认特训目标页',
        '打开更换弹窗',
        '确认弹窗已开',
        '回到网格顶部',
        '逐屏读名字',
        '关掉弹窗',
    ]
    return sorted(_edge_list(), key=lambda edge: order.index(edge[0]))


def test_node_graph_order() -> None:
    """节点链：确认页 → 开弹窗 → 确认弹窗 → 回顶部 → 逐屏读 → 关弹窗。"""
    assert _sorted_edge_list() == [
        ('确认特训目标页', '打开更换弹窗'),
        ('打开更换弹窗', '确认弹窗已开'),
        ('确认弹窗已开', '回到网格顶部'),
        ('回到网格顶部', '逐屏读名字'),
        ('逐屏读名字', '关掉弹窗'),
    ]
