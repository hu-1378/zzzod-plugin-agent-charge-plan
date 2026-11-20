"""材料缺口的读取与解析：材料预览栏、明细四列表、需要获取的材料图标排。

对应 `operation/read_material_calc.py`。这里的用例分两种：

- 纯解析函数（`parse_preview_lines` / `parse_detail_table` / `group_rows` 等）用人工造的文字块，
  跑得快，用来钉住边界规则；
- `read_guide_preview` / `read_calc_detail` 用留档截图跑真 OCR，用来守住「真实画面上认得出来」。

两条容易踩的事实：
- 行尾绿色对勾时「仍需获取」那格**没有文字**，此时 `missing is None` 且 `satisfied is True`，
  不能当解析失败，也不能断言 `missing == 0`。
- 分区标题行右侧挂的是丁尼合计（带千分位逗号），不是某个材料的缺口，必须排除。
"""

from __future__ import annotations

import inspect
from typing import TYPE_CHECKING, Any

import pytest
from agent_charge_plan.operation import read_material_calc as rmc
from agent_charge_plan.operation.agent_plan_page import TextBlock
from agent_charge_plan.operation.read_material_calc import ReadMaterialCalc

from one_dragon.base.geometry.rectangle import Rect
from one_dragon.base.operation.operation_round_result import OperationRoundResultEnum

if TYPE_CHECKING:
    from conftest import StubContext


def _block(text: str, x1: int, y1: int, width: int = 120, height: int = 30) -> TextBlock:
    """造一块文字：坐标按整张截图算，和 `read_text_blocks` 的输出一致。"""
    return TextBlock(text=text, rect=Rect(x1, y1, x1 + width, y1 + height))


# ============================= 真截图：材料预览栏 =============================


def test_read_guide_preview(plugin_ctx: StubContext, guide_screen: object) -> None:
    """养成方案详情页的「材料预览」栏：4 行「已有/所需」，只有第一行还缺。"""
    item_list, warnings = rmc.read_guide_preview(plugin_ctx, guide_screen)

    assert warnings == []
    assert [item.progress_text for item in item_list] == [
        '60/159',
        '83/33',
        '8772526/320000',
        '3422/120',
    ]
    assert item_list[0].gap == 99
    assert item_list[0].satisfied is False
    assert [item.satisfied for item in item_list[1:]] == [True, True, True]


def test_preview_rows_have_no_material_name(
    plugin_ctx: StubContext, guide_screen: object
) -> None:
    """预览栏只有图标没有名字：宁可留空，也不猜成某个材料。"""
    item_list, _ = rmc.read_guide_preview(plugin_ctx, guide_screen)
    assert all(item.material_name == '' for item in item_list)
    assert all(item.has_name is False for item in item_list)


def test_preview_keeps_button_text(plugin_ctx: StubContext, guide_screen: object) -> None:
    """每行右侧按钮文字（获取 / 养成）要读出来，排查时有用。"""
    item_list, _ = rmc.read_guide_preview(plugin_ctx, guide_screen)
    assert all(item.button_text in rmc.PREVIEW_BUTTON_TEXT_LIST for item in item_list)
    assert item_list[0].button_text == '获取'


def test_read_guide_agent_name(plugin_ctx: StubContext, guide_screen: object) -> None:
    """养成方案详情页蓝色横幅上的代理人名字。"""
    name = rmc.read_guide_agent_name(plugin_ctx, guide_screen)
    assert '蕾米埃尔' in name


# ============================= 真截图：明细四列表 =============================


def test_read_calc_detail_four_columns(
    plugin_ctx: StubContext, calc_result_screen: object
) -> None:
    """四列明细表：6 种材料，材料名 / 所需 / 已有 / 仍需获取 都对上。"""
    item_list, _, warnings = rmc.read_calc_detail(plugin_ctx, calc_result_screen)

    assert warnings == []
    assert [item.material_name for item in item_list] == [
        '特化流明芯片',
        '「仓鼠笼」访问器',
        '异常组件',
        '增强型异常组件',
        '特化型异常组件',
        '音擎能源模块',
    ]
    first = item_list[0]
    assert (first.required, first.owned, first.missing) == (75, 0, 75)
    assert first.gap == 75
    assert first.satisfied is False


def test_green_check_mark_is_satisfied_not_failure(
    plugin_ctx: StubContext, calc_result_screen: object
) -> None:
    """行尾绿色对勾：仍需获取那格没字。要判成「已满足」，不是「解析失败」。"""
    item_list, _, _ = rmc.read_calc_detail(plugin_ctx, calc_result_screen)

    satisfied_without_missing = [
        item for item in item_list if item.satisfied and item.missing is None
    ]
    assert len(satisfied_without_missing) == 3
    for item in satisfied_without_missing:
        assert item.gap == 0
        assert item.progress_text != ''


def test_detail_section_name(plugin_ctx: StubContext, calc_result_screen: object) -> None:
    """分区标题行（音擎强化&进阶）认成 section_name，不算数据行。"""
    item_list, _, _ = rmc.read_calc_detail(plugin_ctx, calc_result_screen)

    section_list = [item.section_name for item in item_list]
    assert section_list[:2] == ['', '']
    assert all(name == '音擎强化&进阶' for name in section_list[2:])
    # 分区标题本身不能变成一条材料
    assert '音擎强化&进阶' not in [item.material_name for item in item_list]


def test_need_icon_summary(plugin_ctx: StubContext, calc_need_icon_screen: object) -> None:
    """「需要获取的材料」图标排：2 个还缺、3 个已满足，缺口数字按从左到右。

    这条能成立有个前提：`区域-需要获取的材料` 的矩形要**包含标题那一行**
    （现在 `(460, 245, 1270, 480)`，标题实测在 `y≈254`）。`read_calc_detail` 靠
    「区域里有没有『需要获取的材料』这行字」区分「图标排」和「表格数字」，
    矩形上边界一旦收到标题下面，这一屏就会被判成「不是图标排」、概况变空。
    """
    _, summary, _ = rmc.read_calc_detail(plugin_ctx, calc_need_icon_screen)

    assert summary.gap_count == 2
    assert summary.satisfied_count == 3
    assert summary.gap_number_list == (75, 140)


def test_need_icon_summary_parses_when_title_present(
    plugin_ctx: StubContext, calc_need_icon_screen: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    """把标题喂进图标区（模拟区域修好之后）：缺口数字和已满足个数都要对。"""
    from agent_charge_plan.operation import agent_plan_page

    real_read = agent_plan_page.read_text_blocks

    def fake_read(ctx: object, screen: object, area: Rect) -> list[TextBlock]:
        """图标区里补一行标题，其它区域照旧走真 OCR。"""
        blocks = real_read(ctx, screen, area)
        if area == rmc.calc_area(plugin_ctx, rmc.AREA_CALC_NEED_ICONS):
            return [*blocks, _block('需要获取的材料', 460, 250, width=200)]
        return blocks

    monkeypatch.setattr(agent_plan_page, 'read_text_blocks', fake_read)

    _, summary, _ = rmc.read_calc_detail(plugin_ctx, calc_need_icon_screen)

    assert summary.gap_count == 2
    assert summary.satisfied_count == 3
    assert summary.gap_number_list == (75, 140)


def test_need_icon_summary_skipped_without_title(
    plugin_ctx: StubContext, calc_result_screen: object
) -> None:
    """停在明细表那一屏：图标区里读到的是表格数字，没有标题 → 概况留空，不当缺口。"""
    _, summary, _ = rmc.read_calc_detail(plugin_ctx, calc_result_screen)

    assert summary.gap_count == 0
    assert summary.gap_number_list == ()


def test_need_icon_screen_reads_only_visible_rows(
    plugin_ctx: StubContext, calc_need_icon_screen: object
) -> None:
    """页面停在顶部时，表格只露出前两行；图标上的数字不能变成假行。"""
    item_list, _, warnings = rmc.read_calc_detail(plugin_ctx, calc_need_icon_screen)

    assert warnings == []
    assert [item.material_name for item in item_list] == ['特化流明芯片', '「仓鼠笼」访问器']
    assert all(item.section_name == '代理人技能升级' for item in item_list)


def test_calc_detail_not_ready(plugin_ctx: StubContext, calc_setting_screen: object) -> None:
    """页面还停在设置页（结果没出来）：一行都读不到，要给出明确提示。"""
    item_list, summary, warnings = rmc.read_calc_detail(plugin_ctx, calc_setting_screen)

    assert item_list == []
    assert summary.gap_count == 0
    assert any('结果页' in warning for warning in warnings)


# ============================= 区域没配置时的降级 =============================


def test_read_guide_preview_without_area(
    bare_ctx: StubContext, guide_screen: object
) -> None:
    """主仓/插件都没配区域时返回空列表 + 提示，不抛异常。"""
    item_list, warnings = rmc.read_guide_preview(bare_ctx, guide_screen)
    assert item_list == []
    assert any('区域没配置' in warning for warning in warnings)


def test_read_calc_detail_without_area(
    bare_ctx: StubContext, calc_result_screen: object
) -> None:
    """明细表相关区域全缺时，一次把缺的区域都列出来。"""
    item_list, summary, warnings = rmc.read_calc_detail(bare_ctx, calc_result_screen)
    assert item_list == []
    assert summary == rmc.NeedIconSummary()
    assert any('缺少区域配置' in warning for warning in warnings)


# ============================= 纯解析函数 =============================


def test_group_rows() -> None:
    """按 y 聚行：同一行内按 x 从左到右，行与行从上到下。"""
    blocks = [
        _block('乙', 200, 102),
        _block('甲', 100, 100),
        _block('丙', 100, 300),
    ]
    rows = rmc.group_rows(blocks)
    assert len(rows) == 2
    assert [block.text for block in rows[0]] == ['甲', '乙']
    assert [block.text for block in rows[1]] == ['丙']


def test_group_rows_respects_tolerance() -> None:
    """同一行的 y 允许有小偏差（OCR 框高度不一样），超出容差就算新行。"""
    near = [_block('甲', 100, 100), _block('乙', 200, 100 + rmc.ROW_Y_TOLERANCE - 2)]
    far = [_block('甲', 100, 100), _block('乙', 200, 100 + rmc.ROW_Y_TOLERANCE + 10)]
    assert len(rmc.group_rows(near)) == 1
    assert len(rmc.group_rows(far)) == 2


def test_parse_preview_lines_ignores_noise() -> None:
    """预览栏的栏目名、按钮、驱动盘图标上的件数都不能变成材料条目。"""
    blocks = [
        _block('材料预览', 1200, 120, width=200),
        _block('60/159', 1200, 200),
        _block('获取', 1700, 200, width=60),
        _block('83/33', 1200, 260),
        _block('养成', 1700, 260, width=60),
        _block('2', 1200, 320, width=20),
        _block('驱动盘', 1200, 380, width=120),
    ]
    item_list, warnings = rmc.parse_preview_lines(blocks)

    assert warnings == []
    assert [item.progress_text for item in item_list] == ['60/159', '83/33']
    assert [item.satisfied for item in item_list] == [False, True]


def test_parse_preview_lines_empty() -> None:
    """一条都读不到时给明确提示（可能是空栏，也可能页面没停稳）。"""
    item_list, warnings = rmc.parse_preview_lines([])
    assert item_list == []
    assert any('材料预览' in warning for warning in warnings)


def test_pick_button_text() -> None:
    """一行里挑右侧按钮文字。"""
    assert rmc.pick_button_text([_block('60/159', 100, 100), _block('获取', 400, 100)]) == '获取'
    assert rmc.pick_button_text([_block('60/159', 100, 100)]) == ''


def test_column_of() -> None:
    """文字块按 x 归到四列（相邻列中线当边界）。"""
    name_rect = Rect(500, 245, 950, 1010)
    required_rect = Rect(990, 245, 1120, 1010)
    owned_rect = Rect(1150, 245, 1290, 1010)
    missing_rect = Rect(1300, 245, 1470, 1010)

    def column(x: int) -> int:
        return rmc.column_of(Rect(x, 300, x + 60, 330), name_rect, required_rect,
                             owned_rect, missing_rect)

    assert column(600) == rmc.COLUMN_NAME
    assert column(1000) == rmc.COLUMN_REQUIRED
    assert column(1200) == rmc.COLUMN_OWNED
    assert column(1380) == rmc.COLUMN_MISSING


def test_is_table_header() -> None:
    """表头行（所需 / 已有）要能认出来，否则会被当成数据行。"""
    header = [_block('养成材料', 500, 300), _block('所需', 1000, 300),
              _block('已有', 1200, 300), _block('仍需获取', 1350, 300)]
    assert rmc.is_table_header(header) is True
    assert rmc.is_table_header([_block('特化流明芯片', 500, 400)]) is False


def test_looks_like_money() -> None:
    """带千分位逗号的是丁尼合计，不是材料个数。"""
    assert rmc.looks_like_money('675,000') is True
    assert rmc.looks_like_money('675，000') is True
    assert rmc.looks_like_money('75') is False


def test_first_count() -> None:
    """取一列里第一个纯数字；丁尼合计和「已满足」都不算。"""
    assert rmc.first_count([_block('675,000', 1300, 300)]) is None
    assert rmc.first_count([_block('已满足', 1300, 300)]) is None
    assert rmc.first_count([_block('75', 1300, 300)]) == 75


def test_join_text() -> None:
    """一格里被 OCR 拆成两段的材料名要接起来。"""
    assert rmc.join_text([_block('特化', 500, 300), _block('流明芯片', 560, 300)]) == '特化流明芯片'


def test_normalize_text() -> None:
    """去空白、全角斜杠转半角。"""
    assert rmc.normalize_text(' 60 ／ 159 ') == '60/159'


def test_parse_need_icons() -> None:
    """图标排：带数字的是缺口，「已满足」单独计数。"""
    summary = rmc.parse_need_icons(
        [
            _block('75', 500, 300),
            _block('已满足', 700, 300),
            _block('140', 900, 300),
        ]
    )
    assert summary.gap_count == 2
    assert summary.satisfied_count == 1
    assert summary.gap_number_list == (75, 140)


# ============================= 结果结构与交付 =============================


def test_material_gap_item_gap() -> None:
    """缺口优先用「仍需获取」那一格；没那格就用 所需-已有。"""
    assert rmc.MaterialGapItem(missing=7).gap == 7
    assert rmc.MaterialGapItem(missing=-3).gap == 0
    assert rmc.MaterialGapItem(owned=10, required=30).gap == 20
    assert rmc.MaterialGapItem(owned=40, required=30).gap == 0
    assert rmc.MaterialGapItem().gap is None


def test_gap_snapshot_properties(
    plugin_ctx: StubContext, calc_result_screen: object
) -> None:
    """快照的几个派生属性：缺口条目、有名字的条目、没名字的个数。"""
    item_list, summary, warnings = rmc.read_calc_detail(plugin_ctx, calc_result_screen)
    snapshot = rmc.MaterialGapSnapshot(
        source=rmc.SOURCE_CALC_DETAIL,
        item_list=item_list,
        need_icon_summary=summary,
        warnings=warnings,
    )

    assert len(snapshot.gap_item_list) == 3
    assert len(snapshot.named_item_list) == 6
    assert snapshot.unnamed_count == 0
    assert snapshot.source == rmc.SOURCE_CALC_DETAIL


def test_gap_snapshot_plan_rows(
    plugin_ctx: StubContext, calc_result_screen: object
) -> None:
    """`plan_row_list` 只给有名字的条目，能直接喂 `PlanSnapshot.from_rows`。"""
    from agent_charge_plan.planner.plan_models import PlanSnapshot

    item_list, _, _ = rmc.read_calc_detail(plugin_ctx, calc_result_screen)
    snapshot = rmc.MaterialGapSnapshot(item_list=item_list)

    rows = snapshot.plan_row_list()
    assert len(rows) == 6
    assert rows[0] == {'entry_name': '特化流明芯片', 'progress_text': '0/75'}

    plan_snapshot = PlanSnapshot.from_rows(rows)
    assert [entry.entry_name for entry in plan_snapshot.entries][0] == '特化流明芯片'
    assert plan_snapshot.entries[0].missing == 75


def test_preview_snapshot_has_no_plan_rows(
    plugin_ctx: StubContext, guide_screen: object
) -> None:
    """预览栏读出来的条目全都没名字，`plan_row_list` 为空、`unnamed_count` 是 4。"""
    item_list, warnings = rmc.read_guide_preview(plugin_ctx, guide_screen)
    snapshot = rmc.MaterialGapSnapshot(
        source=rmc.SOURCE_GUIDE_PREVIEW, item_list=item_list, warnings=warnings
    )

    assert snapshot.plan_row_list() == []
    assert snapshot.unnamed_count == 4


def test_gap_snapshot_summary_lines(
    plugin_ctx: StubContext, calc_result_screen: object
) -> None:
    """日志行：每种材料一行，最后的图标概况一行。"""
    item_list, summary, warnings = rmc.read_calc_detail(plugin_ctx, calc_result_screen)
    snapshot = rmc.MaterialGapSnapshot(
        agent_name='蕾米埃尔·丹',
        item_list=item_list,
        need_icon_summary=summary,
        warnings=warnings,
    )

    lines = snapshot.summary_lines()
    assert lines[0].startswith('代理人 蕾米埃尔·丹')
    assert any('特化流明芯片' in line and '还差 75' in line for line in lines)
    assert any('需要获取的材料' in line for line in lines)


# ============================= 养成指南页（材料计算弹窗那条路） =============================

#: 养成指南页左侧竖排导航能读到的文字（选中态是黄底，不靠文字判断）
EXPECTED_NAV_TEXT = ['方案概览', '属性&等级', '驱动盘', '音擎', '技能', '推荐队伍']


def test_agent_guide_areas_exist(plugin_ctx: StubContext) -> None:
    """养成指南页的区域来自插件自带 screen_info，取名取得到、坐标和 yml 一致。"""
    title = rmc.agent_guide_area(plugin_ctx, rmc.AREA_AGENT_GUIDE_TITLE)
    calc_button = rmc.agent_guide_area(plugin_ctx, rmc.AREA_AGENT_GUIDE_CALC_BUTTON)
    name_area = rmc.agent_guide_area(plugin_ctx, rmc.AREA_AGENT_GUIDE_AGENT_NAME)

    assert title is not None
    assert calc_button is not None
    assert name_area is not None
    assert (title.x1, title.y1, title.x2, title.y2) == (160, 20, 400, 85)
    # 「养成材料计算」入口在角色名横幅下面
    assert calc_button.y1 >= name_area.y1


def test_agent_guide_area_unknown(plugin_ctx: StubContext) -> None:
    """没配置的区域返回 None，不抛异常。"""
    assert rmc.agent_guide_area(plugin_ctx, '区域-不存在') is None


def test_agent_guide_area_without_screen_info(bare_ctx: StubContext) -> None:
    """主仓/插件都没这个画面的区域时，取区域和读名字都安全返回空。"""
    assert rmc.agent_guide_area(bare_ctx, rmc.AREA_AGENT_GUIDE_TITLE) is None
    assert rmc.read_agent_guide_name(bare_ctx, None) == ''


def test_read_agent_guide_name(plugin_ctx: StubContext, agent_guide_screen: object) -> None:
    """养成指南页粉色横幅上的代理人名字（项目代理人表里可能只有简称，所以按 OCR 文本读）。"""
    assert rmc.read_agent_guide_name(plugin_ctx, agent_guide_screen) == '蕾米埃尔'


def test_agent_guide_title_text(plugin_ctx: StubContext, agent_guide_screen: object) -> None:
    """标题区能读到「养成指南」，这是判断「已经在养成指南页」的文本锚点。"""
    from agent_charge_plan.operation import agent_plan_page

    area = rmc.agent_guide_area(plugin_ctx, rmc.AREA_AGENT_GUIDE_TITLE)
    assert area is not None
    assert agent_plan_page.has_text(plugin_ctx, agent_guide_screen, area, '养成指南') is True


def test_agent_guide_calc_button_text(
    plugin_ctx: StubContext, agent_guide_screen: object
) -> None:
    """「养成材料计算」入口靠文本匹配去点，区域里必须读到这四个字。"""
    from agent_charge_plan.operation import agent_plan_page

    area = rmc.agent_guide_area(plugin_ctx, rmc.AREA_AGENT_GUIDE_CALC_BUTTON)
    assert area is not None
    assert agent_plan_page.has_text(
        plugin_ctx, agent_guide_screen, area, '养成材料计算'
    ) is True


def test_agent_guide_back_button_is_icon_only(
    plugin_ctx: StubContext, agent_guide_screen: object
) -> None:
    """左上角「返回」是纯图标按钮，读不到文字——所以不能用它当文本锚点。"""
    from agent_charge_plan.operation import agent_plan_page

    area = rmc.agent_guide_area(plugin_ctx, rmc.AREA_AGENT_GUIDE_BACK)
    assert area is not None
    assert agent_plan_page.read_text_blocks(plugin_ctx, agent_guide_screen, area) == []


def test_agent_guide_left_nav_text(
    plugin_ctx: StubContext, agent_guide_screen: object
) -> None:
    """左侧竖排导航的文字能读全（顺序固定：方案概览在最上）。"""
    from agent_charge_plan.operation import agent_plan_page

    area = rmc.agent_guide_area(plugin_ctx, '区域-左侧导航')
    assert area is not None
    blocks = agent_plan_page.read_text_blocks(plugin_ctx, agent_guide_screen, area)
    texts = [block.text.strip() for block in blocks]
    assert texts[: len(EXPECTED_NAV_TEXT)] == EXPECTED_NAV_TEXT


# ============================= 表头判断：结果页到底出没出来 =============================


def test_has_detail_table_header_on_result(
    plugin_ctx: StubContext, calc_result_screen: object
) -> None:
    """结果页出来了：表头（所需 / 已有 / 仍需获取）在，判 True。"""
    assert rmc.has_detail_table_header(plugin_ctx, calc_result_screen) is True


def test_has_detail_table_header_on_setting(
    plugin_ctx: StubContext, calc_setting_screen: object
) -> None:
    """还停在设置页：没有表头，判 False —— 说明「确定」没生效、结果页没出来。"""
    assert rmc.has_detail_table_header(plugin_ctx, calc_setting_screen) is False


def test_has_detail_table_header_without_area(bare_ctx: StubContext) -> None:
    """区域没配置时判 False，不抛异常。"""
    assert rmc.has_detail_table_header(bare_ctx, None) is False


# ============================= 弹窗兜底开关的默认值 =============================


def test_dialog_fallback_defaults_to_true() -> None:
    """`ReadMaterialCalc` 默认允许走「养成指南 → 养成材料计算」这条兜底路。

    这是构造签名层面的约定：默认值被改回去，材料缺口就又只剩「只读不排」那一条路了。
    """
    import inspect

    from agent_charge_plan.operation.read_material_calc import ReadMaterialCalc

    params = inspect.signature(ReadMaterialCalc.__init__).parameters
    assert params['goto_first'].default is True
    assert params['allow_dialog_fallback'].default is True


def test_guide_status_constants() -> None:
    """养成指南页相关常量：节点出边是按这些 status 精确匹配的，改字就会断链。"""
    assert rmc.AGENT_GUIDE_SCREEN == '养成指南'
    assert rmc.STATUS_GUIDE_READY == '已在养成指南页'
    assert rmc.STATUS_DIALOG_OPEN == '弹窗已开'
    assert rmc.STATUS_PREVIEW_EMPTY == '材料预览没读到'


def test_read_material_calc_node_chain() -> None:
    """节点链的关键一环：`进养成指南` / `确认养成指南` / `确认弹窗已开` 都在。

    这两条出边是精确匹配 status 的（没有兜底边），节点改名或被删会让材料缺口
    永远读不出来，所以在签名层面钉一下。
    """
    from agent_charge_plan.operation.read_material_calc import ReadMaterialCalc

    for node_method in ('goto_agent_guide', 'confirm_agent_guide', 'confirm_dialog',
                        'read_detail_table', 'close_dialog', 'handle_init'):
        assert hasattr(ReadMaterialCalc, node_method), node_method
    # 旧的直连方法名已经改掉了，避免有人调旧名字
    assert not hasattr(ReadMaterialCalc, 'goto_guide_page')


# ============================= 弹窗路径的开关与收尾（节点级，不起 ZContext） =============================


def _bare_operation() -> Any:
    """造一个没跑 `__init__` 的操作对象，只塞被测方法要用到的属性。

    真 `ReadMaterialCalc(ctx)` 会连带建 `OpenAndEnterGame` 那一整套（要配置、要游戏窗口），
    跑一个纯判断方法不值当。这里 `object.__new__` 跳过构造，缺失的东西由用例自己补。
    """
    op: Any = object.__new__(ReadMaterialCalc)
    op.agent_name = ''
    op.snapshot = rmc.MaterialGapSnapshot()
    op.fail_status = ''
    op.prefer_dialog = False
    op.goto_first = False
    op.allow_dialog_fallback = True
    op.ctx = object()
    op.last_screenshot = None
    op.node_retry_times = 0
    op.node_max_retry_times = 2
    return op


def test_read_material_calc_signature_defaults() -> None:
    """构造参数的默认值：默认允许走弹窗兜底，但**不**跳过材料预览。"""
    params = inspect.signature(ReadMaterialCalc.__init__).parameters
    assert params['goto_first'].default is True
    assert params['allow_dialog_fallback'].default is True
    assert params['prefer_dialog'].default is False


def test_prefer_dialog_skips_preview() -> None:
    """`prefer_dialog=True` 时不读材料预览，直接复用「材料预览没读到」状态走弹窗路径。"""
    op = _bare_operation()
    op.prefer_dialog = True

    result = op.read_preview()

    assert result.is_success
    assert result.status == rmc.STATUS_PREVIEW_EMPTY


def test_handle_init_resets_state() -> None:
    """同一个实例复用前要清干净：名字、结果、失败原因都不能留给下一次。"""
    op = _bare_operation()
    op.agent_name = '蕾米埃尔'
    op.snapshot = rmc.MaterialGapSnapshot(item_list=[rmc.MaterialGapItem(required=75, owned=0)])
    op.fail_status = '上一次的失败原因'

    op.handle_init()

    assert op.agent_name == ''
    assert op.snapshot is not None
    assert op.snapshot.item_list == []
    assert op.fail_status == ''


def test_close_dialog_success() -> None:
    """读到数据：点关闭后带数据成功，status 里写明读了几行。"""
    op = _bare_operation()
    op.snapshot = rmc.MaterialGapSnapshot(
        item_list=[rmc.MaterialGapItem(material_name='特化流明芯片', required=75, owned=0)]
    )
    closed: list[bool] = []
    op._is_dialog_open = lambda: True
    op._close_calc_dialog = lambda: closed.append(True)

    result = op.close_dialog()

    assert result.is_success
    assert closed == [True]
    assert '明细表读到 1 行' in result.status
    assert result.data is op.snapshot


def test_close_dialog_uses_fail_status() -> None:
    """弹窗路径中途失败：收尾节点带着原因失败，不是成功。"""
    op = _bare_operation()
    op.fail_status = '点了「确定」但结果页没出来：明细表表头「所需 / 已有」没找到'
    op._is_dialog_open = lambda: False
    op._close_calc_dialog = lambda: None

    result = op.close_dialog()

    assert result.is_fail
    assert result.status == op.fail_status


def test_close_dialog_empty_without_reason() -> None:
    """没数据也没记原因（例如两天都没读到）：给一条兜底说明再失败。"""
    op = _bare_operation()
    op._is_dialog_open = lambda: False
    op._close_calc_dialog = lambda: None

    result = op.close_dialog()

    assert result.is_fail
    assert result.status == '明细表一行都没读到'


def test_close_dialog_skips_click_when_not_open() -> None:
    """弹窗本来就没开：不要瞎点关闭。"""
    op = _bare_operation()
    op.snapshot = rmc.MaterialGapSnapshot(item_list=[rmc.MaterialGapItem(required=1, owned=0)])
    closed: list[bool] = []
    op._is_dialog_open = lambda: False
    op._close_calc_dialog = lambda: closed.append(True)

    op.close_dialog()

    assert closed == []


# ============================= 明细表读空 → 等一秒再读一次 =============================


def test_read_detail_table_retries_once_when_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    """第一次读空：返回重试（不是失败），让框架等一秒再读一次。"""
    op = _bare_operation()
    op.node_retry_times = 0
    monkeypatch.setattr(
        rmc, 'read_calc_detail',
        lambda ctx, screen: ([], rmc.NeedIconSummary(), ['明细表的四列表一行都没读到']),
    )

    result = op.read_detail_table()

    assert result.result == OperationRoundResultEnum.RETRY
    assert '再读一次' in result.status
    assert op.fail_status == ''


def test_read_detail_table_empty_after_retry(monkeypatch: pytest.MonkeyPatch) -> None:
    """第二次还是空：记下原因、返回「没读到数据」状态，交给收尾节点去关弹窗。"""
    op = _bare_operation()
    op.node_retry_times = 1
    monkeypatch.setattr(
        rmc, 'read_calc_detail',
        lambda ctx, screen: ([], rmc.NeedIconSummary(), ['明细表的四列表一行都没读到']),
    )
    monkeypatch.setattr(rmc, 'has_detail_table_header', lambda ctx, screen: False)

    result = op.read_detail_table()

    assert result.is_success
    assert result.status == rmc.STATUS_DETAIL_EMPTY
    assert '结果页没出来' in op.fail_status
    assert op.snapshot.item_list == []


def test_read_detail_table_empty_but_header_found(
    monkeypatch: pytest.MonkeyPatch
) -> None:
    """有表头没数据行：原因写成「结果页出来了但没数据」，方便区分是加载慢还是要往下滚。"""
    op = _bare_operation()
    op.node_retry_times = 1
    monkeypatch.setattr(
        rmc, 'read_calc_detail', lambda ctx, screen: ([], rmc.NeedIconSummary(), [])
    )
    monkeypatch.setattr(rmc, 'has_detail_table_header', lambda ctx, screen: True)

    result = op.read_detail_table()

    assert result.status == rmc.STATUS_DETAIL_EMPTY
    assert '有表头但没有数据行' in op.fail_status


def test_read_detail_table_returns_data(monkeypatch: pytest.MonkeyPatch) -> None:
    """读到数据：状态是「明细表读到数据」，结果里带上代理人名字和数据来源。"""
    op = _bare_operation()
    op.agent_name = '蕾米埃尔'
    item = rmc.MaterialGapItem(material_name='特化流明芯片', required=75, owned=0, missing=75)
    monkeypatch.setattr(
        rmc, 'read_calc_detail',
        lambda ctx, screen: ([item], rmc.NeedIconSummary(gap_count=1, gap_number_list=(75,)), []),
    )

    result = op.read_detail_table()

    assert result.is_success
    assert result.status == rmc.STATUS_DETAIL_READ
    assert op.snapshot.source == rmc.SOURCE_CALC_DETAIL
    assert op.snapshot.agent_name == '蕾米埃尔'
    assert op.snapshot.item_list[0].material_name == '特化流明芯片'


# ============================= 节点图：不许有悬空边 / 多个起点 =============================


def _node_map() -> dict[str, Any]:
    """把类上带 `@operation_node` 的方法收成 {节点名: OperationNode}。"""
    nodes: dict[str, Any] = {}
    for method_name in dir(ReadMaterialCalc):
        node = getattr(getattr(ReadMaterialCalc, method_name, None),
                       'operation_node_annotation', None)
        if node is not None:
            nodes[node.cn] = node
    return nodes


def _edge_list() -> list[tuple[str, str, bool, str | None]]:
    """把所有 `@node_from` 收成 (起点, 终点, success, status) 四元组。

    `node_to_name` 由图构建时自动填充，这里用被装饰方法自己的节点名当终点。
    `ignore_status` 不参与比较：`OperationEdgeDesc` 保留的是装饰器入参默认值，
    真正的「忽略状态」是建图时按 `status is None` 算的，拿它断言会误判。
    """
    edges: list[tuple[str, str, bool, str | None]] = []
    for method_name in dir(ReadMaterialCalc):
        method = getattr(ReadMaterialCalc, method_name, None)
        node = getattr(method, 'operation_node_annotation', None)
        for desc in getattr(method, 'operation_edge_annotation', None) or []:
            if node is None:
                continue
            edges.append((desc.node_from_name, node.cn, desc.success, desc.status))
    return edges


def test_node_graph_has_single_start_and_no_dangling_edge() -> None:
    """节点图的结构约束：一个起点、没有悬空边、除收尾节点外每个节点都能往下走。"""
    nodes = _node_map()
    edges = _edge_list()

    assert nodes, '一个节点都没找到，装饰器元数据可能变了'
    start_node_list = [name for name, node in nodes.items() if node.is_start_node]
    assert start_node_list == ['确认特训目标页']

    for from_name, to_name, _, _ in edges:
        assert from_name in nodes, f'边的起点 {from_name} 不是已声明的节点'
        assert to_name in nodes, f'边的终点 {to_name} 不是已声明的节点'

    # 收尾节点只进不出，其它节点都要有出边
    from_name_set = {edge[0] for edge in edges}
    terminal_node_list = [name for name in nodes if name not in from_name_set]
    assert terminal_node_list == ['关掉弹窗']


def test_node_graph_has_close_dialog_edges() -> None:
    """收尾的三条入边：读到数据（任意状态）、两个中途失败（success=False）。"""
    edges = _edge_list()

    assert ('读明细表', '关掉弹窗', True, None) in edges
    assert ('确认弹窗已开', '关掉弹窗', False, None) in edges
    assert ('点确定出结果', '关掉弹窗', False, None) in edges


def test_node_graph_preview_to_guide_is_status_matched() -> None:
    """`读材料预览 → 进养成指南` 是按状态精确匹配的（status 不是 None）。"""
    edges = _edge_list()

    assert ('读材料预览', '进养成指南', True, rmc.STATUS_PREVIEW_EMPTY) in edges


def test_read_detail_table_node_retries_twice() -> None:
    """「读空等一秒再读」是靠节点重试实现的：重试次数必须是 2。

    被改成 1 的话就只剩一次机会，结果页加载慢一点就永远读不到。
    """
    nodes = _node_map()
    assert nodes['读明细表'].node_max_retry_times == 2


def test_dialog_result_status_constants() -> None:
    """明细表读数的两个状态、关闭按钮区域名、两个等待时间，都被出边/收尾依赖。"""
    assert rmc.STATUS_DETAIL_READ == '明细表读到数据'
    assert rmc.STATUS_DETAIL_EMPTY == '明细表没读到数据'
    assert rmc.AREA_CALC_CLOSE == '按钮-关闭'
    assert rmc.RESULT_RETRY_WAIT == 1.0
    assert rmc.CLOSE_WAIT == 1.0
