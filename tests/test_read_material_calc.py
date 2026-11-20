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

from typing import TYPE_CHECKING

from agent_charge_plan.operation import read_material_calc as rmc
from agent_charge_plan.operation.agent_plan_page import TextBlock

from one_dragon.base.geometry.rectangle import Rect

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
    """「需要获取的材料」图标排：2 个还缺、3 个已满足，缺口数字按从左到右。"""
    _, summary, _ = rmc.read_calc_detail(plugin_ctx, calc_need_icon_screen)

    assert summary.gap_count == 2
    assert summary.satisfied_count == 3
    assert summary.gap_number_list == (75, 140)


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
