"""全角色核对：目录名清洗、汇总 markdown、落盘、失败记录。

核对模式只读不写配置，所以这些用例全在文件系统上验：造一张纯色图当截图，
把「清单 / 计划 / 截图」落进 tmp_path，再看文件内容和 `汇总.md` 是不是对的。

不碰游戏：纯函数直接喂数据；「一个角色失败不影响下一个」用假的 `_audit_one` 驱动循环节点。
"""

from __future__ import annotations

import time
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

import numpy as np
import pytest
import yaml
from agent_charge_plan.agent_charge_plan_app import AgentChargePlanApp
from agent_charge_plan.operation.agent_plan_page import AgentPlanItem, AgentPlanSnapshot
from agent_charge_plan.operation.audit_agents import (
    FALLBACK_DIR_NAME,
    ITEM_FILE_NAME,
    PLAN_FILE_NAME,
    ROOT_README_FILE_NAME,
    SCREEN_FILE_NAME,
    STATUS_AUDIT_DONE,
    SUMMARY_FILE_NAME,
    UID_MASK_X1,
    UID_MASK_Y1,
    AuditAgentResult,
    AuditAgents,
    AuditSummary,
    build_item_text,
    build_plan_yaml,
    build_summary_markdown,
    collect_skip_warnings,
    combined_warnings,
    mask_uid,
    resolve_output_root,
    sanitize_dir_name,
    unique_dir_name,
    unique_name_list,
    write_agent_files,
    write_root_readme,
)
from agent_charge_plan.planner.charge_budget import ChargeAvailability
from agent_charge_plan.planner.plan_mapper import PlanMapper
from agent_charge_plan.planner.plan_models import PlanSnapshot

from one_dragon.base.operation.operation_round_result import OperationRoundResultEnum

if TYPE_CHECKING:
    from conftest import StubContext


# ============================= 造数据的小工具 =============================


def _item(
    name: str,
    group: str = '代理人',
    charge: str = '20-100',
    progress: str | None = '129/290',
) -> AgentPlanItem:
    """造一条清单条目。"""
    return AgentPlanItem(group_name=group, name=name, charge_text=charge, progress_text=progress)


def _snapshot(*items: AgentPlanItem, agent_name: str = '安东·伊万诺夫') -> AgentPlanSnapshot:
    """造一份清单。"""
    return AgentPlanSnapshot(agent_name=agent_name, item_list=list(items))


def _screen(width: int = 1920, height: int = 1080) -> np.ndarray:
    """造一张纯色截图（BGR），省得为了落盘去开游戏。"""
    screen = np.zeros((height, width, 3), dtype=np.uint8)
    screen[:, :] = (40, 80, 120)
    return screen


def _fake_result(agent_name: str, **kwargs: Any) -> AuditAgentResult:
    """造一条核对结果。"""
    return AuditAgentResult(agent_name=agent_name, **kwargs)


def _now() -> datetime:
    """固定时间，汇总里的时间戳才可断言。"""
    return datetime(2026, 11, 21, 3, 4, 5)


class _FakeConfig:
    """核对链路只读配置里的这几个属性，不用真配置对象（那要读 YAML）。"""

    def __init__(
        self,
        batch_agent_names: list[str] | None = None,
        known_agent_list: list[str] | None = None,
        audit_output_dir: str = '',
    ) -> None:
        self.batch_agent_names: list[str] = batch_agent_names or []
        self.known_agent_list: list[str] = known_agent_list or []
        self.audit_output_dir: str = audit_output_dir
        self.default_plan_times: int = 1
        self.include_drive_disc: bool = False

    # 下面的属性是「开始」节点和别的链路会读的，核对链路本身不用
    run_mode: str = 'audit'
    goal_mode: str = 'agent_stage'
    dry_run: bool = True
    target_agent_name: str = ''
    scan_only: bool = False
    material_goals: list[dict[str, Any]] = []
    fallback_plan: dict[str, Any] = {}
    last_generated_keys: list[str] = []


class _FakeRunContext:
    """应用构造时只会调这两个，给个空壳就够。"""

    def __init__(self, config: _FakeConfig) -> None:
        self._config: _FakeConfig = config

    def get_config(self, app_id: str, instance_idx: int, group_id: str) -> _FakeConfig:
        return self._config

    def get_run_record(self, app_id: str, instance_idx: int) -> None:
        return None


def _app_ctx(full_ctx: StubContext, config: _FakeConfig) -> SimpleNamespace:
    """核对链路用的上下文：真副本表 + 假运行上下文。"""
    return SimpleNamespace(
        compendium_service=full_ctx.compendium_service,
        ocr=full_ctx.ocr,
        screen_loader=full_ctx.screen_loader,
        run_context=_FakeRunContext(config),
        current_instance_idx=0,
    )


def _new_audit_op(
    full_ctx: StubContext,
    config: _FakeConfig,
    output_root: Path | None = None,
) -> AuditAgents:
    """建一个已经初始化过节点网络的核对操作，方便直接调节点方法。"""
    op = AuditAgents(
        _app_ctx(full_ctx, config),
        config=config,
        availability=ChargeAvailability(natural=240),
        instance_idx=1,
        group_id='one_dragon',
    )
    op._init_network()
    op.round_start_time = time.time()
    op.output_root = output_root
    return op


# ============================= 目录名清洗 =============================


def test_sanitize_normal_name() -> None:
    """正常名字原样保留，中文和间隔号都不用动。"""
    assert sanitize_dir_name('蕾米埃尔·丹') == '蕾米埃尔·丹'
    assert sanitize_dir_name('安东·伊万诺夫') == '安东·伊万诺夫'


def test_sanitize_windows_illegal_chars() -> None:
    """Windows 那 9 个非法字符全部换成下划线。"""
    assert sanitize_dir_name('A/B') == 'A_B'
    assert sanitize_dir_name('A\\B') == 'A_B'
    assert sanitize_dir_name('a:b*c?d"e<f>g|h') == 'a_b_c_d_e_f_g_h'


def test_sanitize_strips_trailing_dot_and_space() -> None:
    """Windows 不接受以点或空格结尾的目录名。"""
    assert sanitize_dir_name('叶瞬光...') == '叶瞬光'
    assert sanitize_dir_name('  叶瞬光  ') == '叶瞬光'


def test_sanitize_empty_and_control_chars() -> None:
    """洗完是空的就给兜底名；控制字符也换掉。"""
    assert sanitize_dir_name('') == FALLBACK_DIR_NAME
    assert sanitize_dir_name('   ') == FALLBACK_DIR_NAME
    assert sanitize_dir_name('a\tb') == 'a_b'
    assert sanitize_dir_name('a\x00b') == 'a_b'


def test_sanitize_long_name_is_truncated() -> None:
    """超长名字截断，避免路径过长。"""
    assert len(sanitize_dir_name('角' * 80)) == 40


def test_unique_dir_name_avoids_collision() -> None:
    """两个角色清洗后撞名时加序号，不互相覆盖。"""
    used: set[str] = set()

    first = unique_dir_name('A/B', used)
    used.add(first)
    second = unique_dir_name('A\\B', used)
    used.add(second)
    third = unique_dir_name('A:B', used)

    assert first == 'A_B'
    assert second == 'A_B_2'
    assert third == 'A_B_3'


def test_unique_name_list_dedupes() -> None:
    """名单去空、去重、保留顺序。"""
    assert unique_name_list(['安东', ' ', '安东', '叶瞬光', '']) == ['安东', '叶瞬光']


# ============================= 输出目录 =============================


def test_resolve_output_root_default() -> None:
    """默认放在插件数据目录下 agent_audit/<时间戳>/（不写 config/）。"""
    root = resolve_output_root(Path('Z:/data'), '', _now())

    assert root == Path('Z:/data/agent_audit/20261121_030405')


def test_resolve_output_root_custom_dir() -> None:
    """配了目录就用它，但仍然按时间戳分一层，免得盖掉上次的结果。"""
    root = resolve_output_root(Path('Z:/data'), 'D:/核对结果', _now())

    assert root == Path('D:/核对结果/20261121_030405')


def test_resolve_output_root_custom_dir_blank_means_default() -> None:
    """自定义目录只填空格也当没配。"""
    root = resolve_output_root(Path('Z:/data'), '   ', _now())

    assert root == Path('Z:/data/agent_audit/20261121_030405')


# ============================= 清单文本 =============================


def test_build_item_text() -> None:
    """一行一条，没进度/没电量写成短横。"""
    text = build_item_text(
        '安东·伊万诺夫',
        [
            _item('调查员记录', '代理人', '20-100', '129/290'),
            _item('猎人与忠犬', '驱动盘', '60', None),
        ],
    )

    lines = text.strip().split('\n')
    assert lines[0] == '角色：安东·伊万诺夫'
    assert lines[1] == '条目：2 条'
    assert '代理人 / 调查员记录 / 20-100 / 129/290' in lines
    assert '驱动盘 / 猎人与忠犬 / 60 / -' in lines


def test_build_item_text_empty() -> None:
    """一条条目都没有时也写得出来。"""
    text = build_item_text('', [])

    assert '角色：（读不到）' in text
    assert '条目：0 条' in text


# ============================= 计划 yml =============================


def test_build_plan_yaml_structure(full_ctx: StubContext) -> None:
    """计划 yml 和内置 charge_plan.yml 同结构，另加 warnings 段。"""
    mapper = PlanMapper(full_ctx)
    snapshot = PlanSnapshot.from_rows(
        [
            {
                'group_name': '代理人',
                'entry_name': '调查员记录',
                'charge_text': '20-100',
                'progress_text': '129/290',
            }
        ],
        agent_name='安东·伊万诺夫',
    )
    plan_result = mapper.build(snapshot, ChargeAvailability(natural=240))

    text = build_plan_yaml('安东·伊万诺夫', plan_result)
    data = yaml.safe_load(text)

    assert data['agent_name'] == '安东·伊万诺夫'
    assert len(data['plan_list']) == len(plan_result.plan_list) == 1
    item = data['plan_list'][0]
    assert item['category_name'] == '实战模拟室'
    assert item['mission_type_name'] == '基础材料'
    assert item['mission_name'] == '调查专项'
    assert item['plan_times'] >= 1
    # 内置配置里其它字段都在
    assert item['tab_name'] == '训练'
    assert item['card_num']
    # 只跑一次的临时标记不落盘
    assert 'skipped' not in item
    assert isinstance(data['warnings'], list)


def test_build_plan_yaml_is_diffable(full_ctx: StubContext) -> None:
    """两次核对同一个角色，`计划.yml` 必须一模一样，才能直接 diff。

    所以不能写 `plan_id`（每次随机）和生成时间（每次都变）。
    """
    mapper = PlanMapper(full_ctx)
    snapshot = PlanSnapshot.from_rows(
        [
            {
                'group_name': '代理人',
                'entry_name': '调查员记录',
                'charge_text': '20-100',
                'progress_text': '129/290',
            }
        ],
        agent_name='安东',
    )
    first_result = mapper.build(snapshot, ChargeAvailability(natural=240))
    second_result = mapper.build(snapshot, ChargeAvailability(natural=240))

    first_text = build_plan_yaml('安东', first_result)
    second_text = build_plan_yaml('安东', second_result)

    assert first_text == second_text
    assert 'plan_id' not in first_text
    assert 'generated_at' not in first_text


def test_build_plan_yaml_keeps_warnings(full_ctx: StubContext) -> None:
    """认不出的条目进 warnings，别静默丢掉。"""
    mapper = PlanMapper(full_ctx)
    snapshot = PlanSnapshot.from_rows(
        [
            {
                'group_name': '代理人',
                'entry_name': '新版本才有的条目',
                'charge_text': '20-100',
                'progress_text': '1/10',
            }
        ],
        agent_name='安东',
    )
    plan_result = mapper.build(snapshot, ChargeAvailability(natural=240))

    data = yaml.safe_load(build_plan_yaml('安东', plan_result))

    assert data['plan_list'] == []
    assert data['warnings'], '认不出的条目必须留一条 warning'


def test_collect_skip_warnings_covers_drive_disc(full_ctx: StubContext) -> None:
    """驱动盘条目被跳过时，warnings 里必须有原因，不能让人以为漏了一条。"""
    mapper = PlanMapper(full_ctx)
    snapshot = PlanSnapshot.from_rows(
        [
            {
                'group_name': '代理人',
                'entry_name': '调查员记录',
                'charge_text': '20-100',
                'progress_text': '129/290',
            },
            {
                'group_name': '驱动盘',
                'entry_name': '猎人与忠犬',
                'charge_text': '60',
                'progress_text': None,
            },
        ],
        agent_name='安东',
    )
    plan_result = mapper.build(snapshot, ChargeAvailability(natural=240))

    # mapper 自己只说排上了 1 条，不说为什么少了一条
    assert len(plan_result.plan_list) == 1
    extra = collect_skip_warnings(plan_result)

    assert len(extra) == 1
    assert '猎人与忠犬' in extra[0]
    assert '驱动盘' in extra[0]
    # 补进 warnings 之后，条目数和可刷条数差的那条能对上
    assert len(list(plan_result.warnings) + extra) >= 1


def test_collect_skip_warnings_no_duplicate(full_ctx: StubContext) -> None:
    """已经在 warnings 里的说明不重复加。"""
    mapper = PlanMapper(full_ctx)
    snapshot = PlanSnapshot.from_rows(
        [
            {
                'group_name': '代理人',
                'entry_name': '新版本才有的条目',
                'charge_text': '20-100',
                'progress_text': '1/10',
            }
        ],
        agent_name='安东',
    )
    plan_result = mapper.build(snapshot, ChargeAvailability(natural=240))

    extra = collect_skip_warnings(plan_result)

    assert all(line not in plan_result.warnings for line in extra)


def test_plan_yaml_warnings_explain_reduced_count(full_ctx: StubContext) -> None:
    """`计划.yml` 的 warnings 里必须能看出「条目 3 条、只排 2 条」差在哪。"""
    mapper = PlanMapper(full_ctx)
    snapshot = PlanSnapshot.from_rows(
        [
            {
                'group_name': '代理人',
                'entry_name': '调查员记录',
                'charge_text': '20-100',
                'progress_text': '129/290',
            },
            {
                'group_name': '驱动盘',
                'entry_name': '猎人与忠犬',
                'charge_text': '60',
                'progress_text': None,
            },
        ],
        agent_name='安东',
    )
    plan_result = mapper.build(snapshot, ChargeAvailability(natural=240))

    data = yaml.safe_load(build_plan_yaml('安东', plan_result))

    assert len(data['plan_list']) == 1
    assert any('驱动盘' in line and '猎人与忠犬' in line for line in data['warnings'])
    assert combined_warnings(plan_result) == data['warnings']


# ============================= 汇总 markdown =============================


def test_build_summary_markdown_rows() -> None:
    """表头、表行、备注列都对。"""
    result_list = [
        _fake_result(
            '蕾米埃尔·丹',
            item_count=4,
            planned_count=3,
            estimated_charge=200,
            warnings=['电量不够跑一次（一次 60 点），先排 1 次'],
        ),
        _fake_result('叶瞬光', error='切到这个角色失败：弹窗里找不到 叶瞬光'),
    ]
    output_root = Path('Z:/repo/config/01/one_dragon/agent_audit/20261121_030405')

    markdown = build_summary_markdown(result_list, output_root, _now(), 'batch_agent_names')

    lines = markdown.split('\n')
    assert lines[0] == '# 全角色特训目标核对'
    assert any('2026-11-21 03:04:05' in line for line in lines)
    assert any('batch_agent_names' in line for line in lines)
    assert any('成功 1 / 失败 1' in line for line in lines)

    table = [line for line in lines if line.startswith('|')]
    assert table[0] == '| 角色 | 条目数 | 可刷条数 | 预计总电量 | 备注 |'
    assert table[1] == '|---|---|---|---|---|'
    assert table[2] == (
        '| 蕾米埃尔·丹 | 4 | 3 | 200 | 电量不够跑一次（一次 60 点），先排 1 次 |'
    )
    assert table[3] == '| 叶瞬光 | 0 | 0 | 0 | 切到这个角色失败：弹窗里找不到 叶瞬光 |'


def test_build_summary_markdown_note_limit() -> None:
    """备注最多写 2 条 warning，多出来的只提一句。"""
    result = _fake_result(
        '安东',
        item_count=5,
        planned_count=2,
        estimated_charge=120,
        warnings=['一', '二', '三', '四'],
    )

    markdown = build_summary_markdown([result], Path('D:/x'), _now(), 'known_agent_list')

    assert '一；二（还有 2 条看计划.yml）' in markdown
    assert '三' not in markdown


def test_build_summary_markdown_escapes_pipe() -> None:
    """备注里有竖线也不能把表格撑坏。"""
    result = _fake_result('安东', item_count=1, planned_count=1, warnings=['A|B'])

    markdown = build_summary_markdown([result], Path('D:/x'), _now(), 'known_agent_list')

    assert 'A\\|B' in markdown


def test_build_summary_markdown_empty_note() -> None:
    """没有 warning 时备注列写短横，不留空。"""
    result = _fake_result('安东', item_count=2, planned_count=2, estimated_charge=80)

    markdown = build_summary_markdown([result], Path('D:/x'), _now(), 'known_agent_list')

    assert '| 安东 | 2 | 2 | 80 | - |' in markdown


def test_summary_counts() -> None:
    """成功/失败计数。"""
    summary = AuditSummary(
        result_list=[_fake_result('A'), _fake_result('B', error='读清单失败')]
    )

    assert summary.ok_count == 1
    assert summary.failed_count == 1
    assert len(summary.summary_lines()) == 4


# ============================= 落盘 =============================


def test_mask_uid_blacks_out_corner() -> None:
    """右下角 UID 那块涂黑，别的地方一个像素都不动。"""
    screen = _screen()
    image = mask_uid(screen)

    assert image.size == (1920, 1080)
    assert image.getpixel((UID_MASK_X1 + 5, UID_MASK_Y1 + 5)) == (0, 0, 0)
    assert image.getpixel((1919, 1079)) == (0, 0, 0)
    assert image.getpixel((UID_MASK_X1 - 5, UID_MASK_Y1 - 5)) == (120, 80, 40)
    assert image.getpixel((10, 10)) == (120, 80, 40)


def test_write_agent_files(tmp_path: Path, full_ctx: StubContext) -> None:
    """三个文件都落地，内容能读回来。"""
    mapper = PlanMapper(full_ctx)
    snapshot = _snapshot(_item('调查员记录'))
    plan_result = mapper.build(
        PlanSnapshot.from_reader_snapshot(snapshot), ChargeAvailability(natural=240)
    )
    target_dir = tmp_path / '安东·伊万诺夫'

    file_list = write_agent_files(
        target_dir, '安东·伊万诺夫', snapshot, plan_result, _screen()
    )

    assert {Path(path).name for path in file_list} == {
        ITEM_FILE_NAME,
        PLAN_FILE_NAME,
        SCREEN_FILE_NAME,
    }
    item_text = (target_dir / ITEM_FILE_NAME).read_text(encoding='utf-8')
    assert '代理人 / 调查员记录 / 20-100 / 129/290' in item_text

    plan_data = yaml.safe_load((target_dir / PLAN_FILE_NAME).read_text(encoding='utf-8'))
    assert plan_data['plan_list'], '正常条目应该排出一条计划'
    assert plan_data['agent_name'] == '安东·伊万诺夫'

    assert (target_dir / SCREEN_FILE_NAME).stat().st_size > 0


# ============================= 遍历与失败记录 =============================


def test_audit_keeps_going_after_one_failure(
    full_ctx: StubContext, tmp_path: Path
) -> None:
    """一个角色失败只记进列表，接着核对下一个；全部核对完返回「遍历结束」。"""
    config = _FakeConfig(batch_agent_names=['坏的', '好的'])
    op = _new_audit_op(full_ctx, config, output_root=tmp_path)

    def fake_audit(name: str, result: AuditAgentResult) -> None:
        """第一个角色写个 error，第二个正常返回。"""
        if name == '坏的':
            result.error = '切到这个角色失败：弹窗里找不到'
            return
        result.item_count = 2
        result.planned_count = 1

    op._audit_one = fake_audit  # type: ignore[method-assign]

    first = op.audit_one_agent()
    second = op.audit_one_agent()
    third = op.audit_one_agent()

    assert first.result == OperationRoundResultEnum.WAIT
    assert second.result == OperationRoundResultEnum.WAIT
    assert third.is_success and third.status == STATUS_AUDIT_DONE
    assert [item.agent_name for item in op.result_list] == ['坏的', '好的']
    assert op.result_list[0].ok is False
    assert op.result_list[1].ok is True


def test_audit_records_switch_failure(
    full_ctx: StubContext, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """切角色失败时记下原因，不写文件、不抛异常。"""
    from agent_charge_plan.operation import audit_agents

    from one_dragon.base.operation.operation_base import OperationResult

    config = _FakeConfig(batch_agent_names=['蕾米埃尔·丹'])
    op = _new_audit_op(full_ctx, config, output_root=tmp_path)
    result = AuditAgentResult(agent_name='蕾米埃尔·丹', dir_name='蕾米埃尔·丹')

    monkeypatch.setattr(
        audit_agents.ChangeAgentPlanTarget,
        'execute',
        lambda self: OperationResult(success=False, status='弹窗里找不到 蕾米埃尔·丹'),
    )

    op._audit_one('蕾米埃尔·丹', result)

    assert result.ok is False
    assert '弹窗里找不到' in result.error
    assert not (tmp_path / '蕾米埃尔·丹').exists(), '失败时不该建目录'


def test_audit_records_exception(
    full_ctx: StubContext, tmp_path: Path
) -> None:
    """_audit_one 抛异常时也只是一条失败记录，不影响别的角色。"""
    config = _FakeConfig(batch_agent_names=['会炸的'])
    op = _new_audit_op(full_ctx, config, output_root=tmp_path)

    def boom(name: str, result: AuditAgentResult) -> None:
        raise RuntimeError('读清单时炸了')

    op._audit_one = boom  # type: ignore[method-assign]

    op.audit_one_agent()

    assert len(op.result_list) == 1
    assert '读清单时炸了' in op.result_list[0].error


def test_write_summary_node_creates_file(
    full_ctx: StubContext, tmp_path: Path
) -> None:
    """`写汇总` 节点真的把汇总.md 写出来。"""
    config = _FakeConfig(batch_agent_names=['安东'])
    op = _new_audit_op(full_ctx, config, output_root=tmp_path)
    op.result_list = [
        _fake_result('安东', item_count=3, planned_count=2, estimated_charge=120),
        _fake_result('叶瞬光', error='切到这个角色失败：弹窗里找不到'),
    ]

    round_result = op.write_summary()

    assert round_result.is_success
    summary = round_result.data
    assert isinstance(summary, AuditSummary)
    summary_path = Path(summary.summary_path)
    assert summary_path.name == SUMMARY_FILE_NAME
    assert summary_path.exists()
    text = summary_path.read_text(encoding='utf-8')
    assert '| 安东 | 3 | 2 | 120 | - |' in text
    assert '叶瞬光 | 0 | 0 | 0 | 切到这个角色失败' in text


def test_prepare_output_dir_rejects_empty_list(full_ctx: StubContext) -> None:
    """名单空着就明确失败，别默默跑一轮什么都不产出。"""
    config = _FakeConfig()
    op = _new_audit_op(full_ctx, config)

    round_result = op.prepare_output_dir()

    assert round_result.is_fail
    assert '名单是空的' in round_result.status


def test_agent_source_prefers_batch(full_ctx: StubContext) -> None:
    """batch_agent_names 非空就用它，否则退回 known_agent_list。"""
    batch_op = _new_audit_op(
        full_ctx, _FakeConfig(batch_agent_names=['安东'], known_agent_list=['安东', '妮可'])
    )
    assert batch_op.agent_name_list == ['安东']
    assert 'batch_agent_names' in batch_op.agent_source

    known_op = _new_audit_op(
        full_ctx, _FakeConfig(known_agent_list=['安东', '妮可', '安东'])
    )
    assert known_op.agent_name_list == ['安东', '妮可']
    assert 'known_agent_list' in known_op.agent_source


# ============================= 节点图 =============================


def test_audit_op_node_graph(full_ctx: StubContext) -> None:
    """核对链路的节点图合法，循环能走通。"""
    op = _new_audit_op(full_ctx, _FakeConfig(batch_agent_names=['安东']))

    node_name_list = list(op._node_map.keys())
    start_node_list = [node for node in op._node_map.values() if node.is_start_node]

    assert len(start_node_list) == 1
    assert node_name_list.count('核对一个角色') == 1
    assert {
        '确认特训目标页',
        '准备输出目录',
        '核对一个角色',
        '写汇总',
    } <= set(node_name_list)
    for from_name, edge_list in op._node_edges_map.items():
        assert from_name in node_name_list
        for edge in edge_list:
            assert edge.node_to.cn in node_name_list
    # 「遍历结束」必须能进「写汇总」，否则永远写不出汇总
    status_map = {
        edge.status: edge.node_to.cn for edge in op._node_edges_map.get('核对一个角色', [])
    }
    assert status_map[STATUS_AUDIT_DONE] == '写汇总'


def test_app_node_graph_has_audit_path(full_ctx: StubContext) -> None:
    """应用层多了 audit 一条链路，而且它也能走回大世界。"""
    config = _FakeConfig()
    app = AgentChargePlanApp(_app_ctx(full_ctx, config))
    app._init_network()

    node_name_set = set(app._node_map.keys())
    assert {'审计前准备', '全角色核对', '返回大世界'} <= node_name_set

    start_edges = {
        (edge.status, edge.node_to.cn) for edge in app._node_edges_map.get('开始', [])
    }
    assert ('全角色核对', '审计前准备') in start_edges
    # 另外两条链路还在
    assert ('边刷边算', '执行前准备') in start_edges
    assert ('读取特训目标', '前往特训目标页') in start_edges

    back_edges = {
        (edge.success, edge.node_to.cn)
        for edge in app._node_edges_map.get('全角色核对', [])
    }
    assert (True, '返回大世界') in back_edges
    assert (False, '返回大世界') in back_edges


def test_audit_result_dir_name_unique_in_op(full_ctx: StubContext, tmp_path: Path) -> None:
    """同一轮里两个角色撞目录名时，各自的目录分开。"""
    config = _FakeConfig(batch_agent_names=['A/B', 'A\\B'])
    op = _new_audit_op(full_ctx, config, output_root=tmp_path)
    op._audit_one = lambda name, result: None  # type: ignore[method-assign]

    op.audit_one_agent()
    op.audit_one_agent()

    dir_name_list = [item.dir_name for item in op.result_list]
    assert dir_name_list == ['A_B', 'A_B_2']


def test_audit_summary_lists_files(tmp_path: Path, full_ctx: StubContext) -> None:
    """结果里带上写出来的文件，方便上层直接报给用户。"""
    mapper = PlanMapper(full_ctx)
    snapshot = _snapshot(_item('调查员记录'))
    plan_result = mapper.build(
        PlanSnapshot.from_reader_snapshot(snapshot), ChargeAvailability(natural=240)
    )
    target_dir = tmp_path / 'x'

    file_list = write_agent_files(target_dir, 'x', snapshot, plan_result, _screen())

    assert all(Path(path).parent == target_dir for path in file_list)
    assert len(file_list) == 3


# ============================= 默认目录旁的 README =============================


def test_write_root_readme(tmp_path: Path) -> None:
    """默认输出目录旁边写一份说明，告诉用户这是产物、可以删。"""
    readme_path = write_root_readme(tmp_path / 'agent_audit')

    assert readme_path.name == ROOT_README_FILE_NAME
    assert readme_path.exists()
    text = readme_path.read_text(encoding='utf-8')
    assert '核对产物' in text
    assert '随时整个删掉' in text
    assert 'agent_charge_plan.yml' in text


def test_write_root_readme_keeps_user_edits(tmp_path: Path) -> None:
    """用户改过说明文件就别覆盖。"""
    parent = tmp_path / 'agent_audit'
    parent.mkdir()
    readme_path = parent / ROOT_README_FILE_NAME
    readme_path.write_text('我自己写的', encoding='utf-8')

    write_root_readme(parent)

    assert readme_path.read_text(encoding='utf-8') == '我自己写的'


def test_prepare_output_dir_writes_readme_for_default_root(
    full_ctx: StubContext, tmp_path: Path
) -> None:
    """走默认输出位置（插件数据目录）时旁边会有 README.txt；配了自定义目录就不写。"""
    plugin_data = tmp_path / 'plugin_data'
    config = _FakeConfig(batch_agent_names=['安东'])
    op = _new_audit_op(full_ctx, config)
    # op 里的 ctx 是 _app_ctx 包的替身，得在它上面补这个方法
    op.ctx.get_plugin_data_dir = lambda app_id: plugin_data  # type: ignore[attr-defined]

    round_result = op.prepare_output_dir()

    assert round_result.is_success
    parent = plugin_data / 'agent_audit'
    assert (parent / ROOT_README_FILE_NAME).exists()
    assert op.output_root is not None
    assert op.output_root.parent == parent

    custom_dir = tmp_path / '我自己选的'
    custom_op = _new_audit_op(
        full_ctx, _FakeConfig(batch_agent_names=['安东'], audit_output_dir=str(custom_dir))
    )

    custom_op.prepare_output_dir()

    assert not (custom_dir / ROOT_README_FILE_NAME).exists()


# ============================= 恢复原特训目标 =============================


def test_restore_agent_target_skips_when_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """进来时没读到名字就什么都不做，别瞎设一个。"""
    from agent_charge_plan.operation import restore_agent_target as module

    def boom(*args: Any, **kwargs: Any) -> None:
        raise AssertionError('不该真的去切角色')

    monkeypatch.setattr(module.ChangeAgentPlanTarget, '__init__', boom)

    ok, detail = module.restore_agent_target(SimpleNamespace(), '')

    assert ok is True
    assert '跳过恢复' in detail


def test_restore_agent_target_success(
    full_ctx: StubContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    """名字在就切回去，成功时说明里带名字。"""
    from agent_charge_plan.operation import restore_agent_target as module

    from one_dragon.base.operation.operation_base import OperationResult

    monkeypatch.setattr(
        module.ChangeAgentPlanTarget,
        'execute',
        lambda self: OperationResult(success=True, status='特训目标已换成 叶瞬光'),
    )

    ok, detail = module.restore_agent_target(_app_ctx(full_ctx, _FakeConfig()), '叶瞬光')

    assert ok is True
    assert '叶瞬光' in detail


def test_restore_agent_target_reports_failure(
    full_ctx: StubContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    """切不回去也得说清楚，让用户手动改。"""
    from agent_charge_plan.operation import restore_agent_target as module

    from one_dragon.base.operation.operation_base import OperationResult

    monkeypatch.setattr(
        module.ChangeAgentPlanTarget,
        'execute',
        lambda self: OperationResult(success=False, status='弹窗里找不到 叶瞬光'),
    )

    ok, detail = module.restore_agent_target(_app_ctx(full_ctx, _FakeConfig()), '叶瞬光')

    assert ok is False
    assert '没能恢复' in detail
    assert '弹窗里找不到' in detail


def test_execute_restores_target_after_finish(
    full_ctx: StubContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    """边刷边算收尾节点会去恢复原目标，并且把报告当结果交出去。"""
    from agent_charge_plan.operation import execute_charge_plan as module

    config = _FakeConfig()
    op = module.ExecuteChargePlan(
        _app_ctx(full_ctx, config),
        config=config,
        availability=ChargeAvailability(natural=240),
    )
    op._init_network()
    op.round_start_time = time.time()
    op.original_agent_name = '叶瞬光'
    op.book.remaining_charge = 100
    calls: list[str] = []

    def fake_restore(ctx: Any, name: str) -> tuple[bool, str]:
        calls.append(name)
        return True, f'特训目标已恢复为「{name}」'

    monkeypatch.setattr(module, 'restore_agent_target', fake_restore)

    round_result = op.restore_target()

    assert calls == ['叶瞬光']
    assert round_result.is_success
    assert round_result.data.remaining_charge == 100


def test_audit_restores_target_after_summary(
    full_ctx: StubContext, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """核对收尾节点会去恢复原目标，并把汇总当结果交出去。"""
    from agent_charge_plan.operation import audit_agents as module

    config = _FakeConfig(batch_agent_names=['安东'])
    op = _new_audit_op(full_ctx, config, output_root=tmp_path)
    op.original_agent_name = '蕾米埃尔·丹'

    def fake_restore(ctx: Any, name: str) -> tuple[bool, str]:
        return True, f'特训目标已恢复为「{name}」'

    monkeypatch.setattr(module, 'restore_agent_target', fake_restore)
    op.write_summary()

    round_result = op.restore_target()

    assert round_result.is_success
    assert isinstance(round_result.data, AuditSummary)
