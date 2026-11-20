"""全角色核对：把名单里每个角色的特训目标和算出来的计划落到文件夹里，给人核对。

这个模式只读不写：

- **不写** `charge_plan.yml`，只是把「如果按这个角色排计划，会排成什么样」算出来给你看；
- 每个角色一个子目录，里面放 `清单.txt` / `计划.yml` / `特训目标页.png`；
- 输出根目录下再放一份 `汇总.md`，一眼看完哪个角色有几条、要多少电量、哪条排不上。

一个角色出岔子（切不过去、清单读不到）只记进汇总的备注列，接着核对下一个，
不能让一个坏角色把整轮停掉。

截图存盘前会把右下角的 UID 涂黑（游戏截图和 `Operation.save_screenshot` 一样处理）。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

import numpy as np
import yaml
from PIL import Image, ImageDraw

from agent_charge_plan import agent_charge_plan_const
from agent_charge_plan.planner.charge_budget import ChargeAvailability
from agent_charge_plan.planner.plan_mapper import PlanMapper
from agent_charge_plan.planner.plan_models import PlanBuildResult, PlanSnapshot
from one_dragon.base.operation.operation_edge import node_from
from one_dragon.base.operation.operation_node import operation_node
from one_dragon.base.operation.operation_round_result import OperationRoundResult
from one_dragon.utils.log_utils import log
from zzz_od.operation.zzz_operation import ZOperation

from . import agent_plan_page
from .agent_plan_page import AgentPlanItem, AgentPlanSnapshot
from .change_agent_plan_target import ChangeAgentPlanTarget
from .goto_agent_plan import GotoAgentPlan
from .read_agent_plan_list import ReadAgentPlanList
from .restore_agent_target import restore_agent_target

if TYPE_CHECKING:
    from cv2.typing import MatLike

    from agent_charge_plan.agent_charge_plan_config import AgentChargePlanConfig
    from zzz_od.context.zzz_context import ZContext


# ============================= 输出文件 =============================

#: 每个角色目录里的清单文本
ITEM_FILE_NAME: str = '清单.txt'
#: 每个角色目录里算出来的计划（结构和内置 charge_plan.yml 一样）
PLAN_FILE_NAME: str = '计划.yml'
#: 每个角色目录里的整屏截图
SCREEN_FILE_NAME: str = '特训目标页.png'
#: 输出根目录里的汇总
SUMMARY_FILE_NAME: str = '汇总.md'
#: 默认输出根目录旁的说明文件（写一次，免得用户以为这是要备份的配置）
ROOT_README_FILE_NAME: str = 'README.txt'
#: 说明文件的内容
ROOT_README_TEXT: str = (
    '这个目录是「特训目标体力计划」插件的全角色核对产物。\n'
    '\n'
    '每次核对会按时间戳建一个子目录，里面是每个角色一份：\n'
    '  清单.txt        该角色特训目标页读到的全部条目\n'
    '  计划.yml        按这些条目算出来的体力计划（只算不写，不会进内置体力计划）\n'
    '  特训目标页.png   读完清单时的整屏截图（右下角 UID 已涂黑）\n'
    '  汇总.md（在时间戳目录里）一个表看完所有角色\n'
    '\n'
    '这个目录可以随时整个删掉，插件的配置不在这里（配置是同级的 agent_charge_plan.yml）。\n'
)

#: 默认输出根目录名（放在 config/<实例>/<组>/ 下面，方便用户找）
AUDIT_DIR_NAME: str = 'agent_audit'

#: 截图里要涂黑的 UID 区域左上角：x >= 1740 且 y >= 1030 全部涂黑
UID_MASK_X1: int = 1740
UID_MASK_Y1: int = 1030

#: Windows 文件名里不能出现的字符，遇到就换成下划线
WINDOWS_ILLEGAL_CHARS: str = '\\/:*?"<>|'
#: 名字清洗后为空时的兜底目录名
FALLBACK_DIR_NAME: str = '未命名角色'
#: 目录名长度上限（Windows 路径总长有限，角色名不会这么长，纯防守）
MAX_DIR_NAME_LENGTH: int = 40

#: 汇总里「备注」列最多写几条 warning
SUMMARY_NOTE_LIMIT: int = 2

#: 节点状态：整轮遍历结束，去写汇总
STATUS_AUDIT_DONE: str = '遍历结束'


# ============================= 数据结构 =============================


@dataclass
class AuditAgentResult:
    """一个角色的核对结果。"""

    agent_name: str
    """游戏里读到的角色名"""

    dir_name: str = ''
    """落盘用的子目录名（清洗过、去重过）"""

    item_count: int = 0
    """特训目标页读到几条条目"""

    planned_count: int = 0
    """算出几条计划项"""

    estimated_charge: int = 0
    """预计要花多少电量"""

    warnings: list[str] = field(default_factory=list)
    """`PlanMapper` 给的提示（认不出的条目、电量不够之类）"""

    file_list: list[str] = field(default_factory=list)
    """这个角色落下来的文件（相对输出根目录）"""

    error: str = ''
    """整个角色失败的原因。有值说明这次没读到清单，其它计数都是 0"""

    @property
    def ok(self) -> bool:
        """这个角色核对成功了没有。"""
        return self.error == ''

    @property
    def summary_note(self) -> str:
        """备注列：失败原因，或者最多两条 warning。"""
        if self.error:
            return self.error
        if not self.warnings:
            return ''
        shown = self.warnings[:SUMMARY_NOTE_LIMIT]
        note = '；'.join(shown)
        if len(self.warnings) > SUMMARY_NOTE_LIMIT:
            note += f'（还有 {len(self.warnings) - SUMMARY_NOTE_LIMIT} 条看计划.yml）'
        return note


@dataclass
class AuditSummary:
    """整轮核对的结果，执行结果的 data 就是这个。"""

    output_root: str = ''
    """输出根目录"""

    summary_path: str = ''
    """汇总文件的完整路径"""

    result_list: list[AuditAgentResult] = field(default_factory=list)
    """每个角色一条"""

    @property
    def ok_count(self) -> int:
        """核对成功的角色数。"""
        return sum(1 for item in self.result_list if item.ok)

    @property
    def failed_count(self) -> int:
        """失败的角色数。"""
        return len(self.result_list) - self.ok_count

    def summary_lines(self) -> list[str]:
        """几行给人看的总结（打到日志里）。"""
        lines = [
            f'全角色核对结束：{len(self.result_list)} 个角色，'
            f'成功 {self.ok_count} 个，失败 {self.failed_count} 个',
            f'结果目录：{self.output_root}',
        ]
        for item in self.result_list:
            if item.ok:
                lines.append(
                    f'  {item.agent_name}：条目 {item.item_count} 条，'
                    f'可刷 {item.planned_count} 条，预计 {item.estimated_charge} 电量'
                )
            else:
                lines.append(f'  {item.agent_name}：{item.error}')
        return lines


# ============================= 纯函数（好单测） =============================


def sanitize_dir_name(agent_name: str) -> str:
    """把角色名洗成能当 Windows 目录名的样子。

    规则：Windows 非法字符换成下划线、控制字符换掉、去掉首尾的点和空格（Windows 不接受），
    洗完是空的就用兜底名。

    Args:
        agent_name: 游戏里读到的角色名

    Returns:
        能直接当目录名的字符串
    """
    text = agent_name or ''
    for char in WINDOWS_ILLEGAL_CHARS:
        text = text.replace(char, '_')
    text = ''.join('_' if ord(char) < 32 else char for char in text)
    text = text.strip().strip('.')
    text = text.strip()
    if text == '':
        return FALLBACK_DIR_NAME
    return text[:MAX_DIR_NAME_LENGTH]


def unique_dir_name(agent_name: str, used_name_set: set[str]) -> str:
    """算出这个角色该用哪个子目录名，撞名就加序号。

    两个角色名清洗后可能一样（比如 `A/B` 和 `A\\B` 都会变成 `A_B`），撞了不能互相覆盖。

    Args:
        agent_name: 角色名
        used_name_set: 已经用掉的目录名

    Returns:
        没用过的目录名
    """
    base = sanitize_dir_name(agent_name)
    if base not in used_name_set:
        return base
    index = 2
    while f'{base}_{index}' in used_name_set:
        index += 1
    return f'{base}_{index}'


def resolve_output_root(
    data_dir: Path,
    custom_dir: str,
    now: datetime,
) -> Path:
    """算出这次核对的输出根目录。

    默认放在插件数据目录下 `agent_audit/<时间戳>/`（guide 要求插件数据不写进 config/）；
    配了 `audit_output_dir` 就用那个目录，但仍然按时间戳再分一层，免得这次把上次的结果盖掉。

    Args:
        data_dir: 插件的持久化数据目录（`ctx.get_plugin_data_dir(app_id)`）
        custom_dir: 配置里的输出目录，空字符串表示用默认位置
        now: 这次核对的时间

    Returns:
        输出根目录（还没创建）
    """
    stamp = now.strftime('%Y%m%d_%H%M%S')
    custom = (custom_dir or '').strip()
    if custom:
        return Path(custom) / stamp
    return Path(data_dir) / AUDIT_DIR_NAME / stamp


def build_item_text(agent_name: str, item_list: list[AgentPlanItem]) -> str:
    """把清单写成文本。

    Args:
        agent_name: 角色名
        item_list: 读到的条目

    Returns:
        清单文本，一行一条
    """
    lines = [f'角色：{agent_name or "（读不到）"}', f'条目：{len(item_list)} 条', '']
    lines.append('分组 / 条目名 / 电量 / 进度')
    for item in item_list:
        lines.append(
            f'{item.group_name or "-"} / {item.name} / '
            f'{item.charge_text or "-"} / {item.progress_text or "-"}'
        )
    return '\n'.join(lines) + '\n'


def build_plan_yaml(
    agent_name: str,
    plan_result: PlanBuildResult,
) -> str:
    """把算出来的计划写成 YAML。

    结构和内置 `charge_plan.yml` 的 `plan_list` 一致，另加 `warnings` 段说明哪些条目被跳过、为什么。

    故意**不写** `plan_id` 和生成时间：这两样每次都不一样，会把 `diff` 弄得到处是噪声，
    而这个文件就是拿来两次核对相互比对的。时间看目录名和 `汇总.md` 就够了。

    Args:
        agent_name: 角色名
        plan_result: `PlanMapper` 的产出

    Returns:
        YAML 文本
    """
    plan_list: list[dict[str, Any]] = []
    for item in plan_result.plan_list:
        row = item.to_dict()
        row.pop('plan_id', None)
        plan_list.append(row)

    data: dict[str, Any] = {
        'agent_name': agent_name,
        'estimated_total_charge': plan_result.estimated_total_charge,
        'plan_list': plan_list,
        'warnings': combined_warnings(plan_result),
    }
    return yaml.safe_dump(data, allow_unicode=True, sort_keys=False)


def combined_warnings(plan_result: PlanBuildResult) -> list[str]:
    """这个角色要写进文件的全部提示：mapper 自己的 + 被跳过条目的原因。

    Args:
        plan_result: `PlanMapper` 的产出

    Returns:
        提示列表，直接写进 `计划.yml` 的 `warnings` 段
    """
    return list(plan_result.warnings) + collect_skip_warnings(plan_result)


def build_summary_markdown(
    result_list: list[AuditAgentResult],
    output_root: Path,
    generated_at: datetime,
    agent_source: str,
) -> str:
    """生成汇总用的 markdown（一张表）。

    Args:
        result_list: 每个角色的核对结果
        output_root: 输出根目录
        generated_at: 生成时间
        agent_source: 名单是从哪来的（batch_agent_names / known_agent_list）

    Returns:
        markdown 文本
    """
    lines = [
        '# 全角色特训目标核对',
        '',
        f'- 生成时间：{generated_at.strftime("%Y-%m-%d %H:%M:%S")}',
        f'- 名单来源：{agent_source}',
        f'- 输出目录：{output_root}',
        f'- 角色数：{len(result_list)}（成功 '
        f'{sum(1 for item in result_list if item.ok)} / 失败 '
        f'{sum(1 for item in result_list if not item.ok)}）',
        '',
        '| 角色 | 条目数 | 可刷条数 | 预计总电量 | 备注 |',
        '|---|---|---|---|---|',
    ]
    for item in result_list:
        lines.append(
            f'| {_md_cell(item.agent_name)} | {item.item_count} | {item.planned_count} '
            f'| {item.estimated_charge} | {_md_cell(item.summary_note)} |'
        )
    lines.append('')
    lines.append(
        '「可刷条数」是算出来的计划项条数（已经过电量预算）；备注里的提示来自『计划.yml』的 warnings。'
    )
    return '\n'.join(lines) + '\n'


def collect_skip_warnings(plan_result: PlanBuildResult) -> list[str]:
    """把 `PlanMapper` 里「被跳过的条目」补成 warnings。

    `PlanMapper` 只把跳过记在 `changes` 里（比如驱动盘分组按配置不排），warnings 里没有，
    于是「条目 4 条、可刷 3 条」会让人以为漏了一条。核对是把结果给人看的，差的那条必须说清楚原因。

    已经出现在 `plan_result.warnings` 里的不再重复加。

    Args:
        plan_result: `PlanMapper` 的产出

    Returns:
        多出来的说明，一行一条
    """
    extra: list[str] = []
    for change in plan_result.changes:
        if change.kind != 'skip':
            continue
        line = f'{change.label}：{change.detail}' if change.detail else change.label
        if line in plan_result.warnings or line in extra:
            continue
        extra.append(line)
    return extra


def write_root_readme(parent_dir: Path) -> Path:
    """在默认输出根目录旁边写一份说明（只写一次，不覆盖用户改过的）。

    Args:
        parent_dir: 放时间戳子目录的那个 `agent_audit` 目录

    Returns:
        说明文件的路径
    """
    parent_dir.mkdir(parents=True, exist_ok=True)
    readme_path = parent_dir / ROOT_README_FILE_NAME
    if not readme_path.exists():
        readme_path.write_text(ROOT_README_TEXT, encoding='utf-8')
    return readme_path


def mask_uid(screen: MatLike) -> Image.Image:
    """把截图右下角的 UID 涂黑，返回可以直接存的 PIL 图片。

    Args:
        screen: 游戏截图（BGR）

    Returns:
        涂黑过的 PIL 图片（RGB）
    """
    height, width = screen.shape[0], screen.shape[1]
    rgb = np.ascontiguousarray(screen[:, :, ::-1])
    image = Image.fromarray(rgb)
    draw = ImageDraw.Draw(image)
    draw.rectangle([UID_MASK_X1, UID_MASK_Y1, width - 1, height - 1], fill=(0, 0, 0))
    return image


def write_agent_files(
    target_dir: Path,
    agent_name: str,
    snapshot: AgentPlanSnapshot,
    plan_result: PlanBuildResult,
    screen: MatLike,
) -> list[str]:
    """把一个角色的清单、计划、截图写进它的目录。

    Args:
        target_dir: 这个角色的目录
        agent_name: 角色名
        snapshot: 读到的清单
        plan_result: 算出来的计划
        screen: 读完清单时的整屏截图（BGR）

    Returns:
        写出来的文件路径（字符串）
    """
    target_dir.mkdir(parents=True, exist_ok=True)

    item_path = target_dir / ITEM_FILE_NAME
    item_path.write_text(build_item_text(agent_name, snapshot.item_list), encoding='utf-8')

    plan_path = target_dir / PLAN_FILE_NAME
    plan_path.write_text(build_plan_yaml(agent_name, plan_result), encoding='utf-8')

    screen_path = target_dir / SCREEN_FILE_NAME
    mask_uid(screen).save(screen_path, format='PNG')

    return [str(item_path), str(plan_path), str(screen_path)]


def _md_cell(text: str) -> str:
    """markdown 表格单元格转义：竖线会把表格撑坏，换行会断行。"""
    cleaned = re.sub(r'\s+', ' ', text or '').strip()
    return cleaned.replace('|', '\\|') or '-'


def unique_name_list(name_list: list[str]) -> list[str]:
    """去空、去重、保留顺序。

    Args:
        name_list: 配置里的角色名单

    Returns:
        洗干净的名单
    """
    names: list[str] = []
    for raw in name_list:
        name = raw.strip() if isinstance(raw, str) else ''
        if name and name not in names:
            names.append(name)
    return names


# ============================= 执行用的 Operation =============================


class AuditAgents(ZOperation):
    """全角色核对：逐个切角色、读清单、算计划、落盘。

    执行结果的 data 是 `AuditSummary`。
    """

    def __init__(
        self,
        ctx: ZContext,
        config: AgentChargePlanConfig,
        availability: ChargeAvailability,
        instance_idx: int,
        group_id: str,
    ) -> None:
        """
        构造函数。

        Args:
            ctx: 一条龙上下文
            config: 插件配置（名单、输出目录从这里取）
            availability: 当前可用电量，算计划时按它做预算
            instance_idx: 账号下标，拼默认输出目录用
            group_id: 应用组，拼默认输出目录用
        """
        ZOperation.__init__(self, ctx, op_name='全角色核对')

        self.config: AgentChargePlanConfig = config
        """插件配置"""

        self.availability: ChargeAvailability = availability
        """可用电量"""

        self.instance_idx: int = instance_idx
        """账号下标"""

        self.group_id: str = group_id
        """应用组"""

        batch_name_list = unique_name_list(config.batch_agent_names)
        if batch_name_list:
            self.agent_name_list: list[str] = batch_name_list
            self.agent_source: str = 'batch_agent_names（Web 页勾选的要养的角色）'
        else:
            self.agent_name_list = unique_name_list(config.known_agent_list)
            self.agent_source = 'known_agent_list（游戏里扫到的全部代理人）'
        """要核对的角色名单"""

        self.pending_name_list: list[str] = list(self.agent_name_list)
        """还没核对的角色"""

        self.mapper: PlanMapper = PlanMapper(
            ctx,
            default_plan_times=config.default_plan_times,
            include_drive_disc=config.include_drive_disc,
        )
        """条目 → 计划项的映射表"""

        self.output_root: Path | None = None
        """输出根目录"""

        self.original_agent_name: str = ''
        """进来时的特训目标。跑完要恢复成它，读不到就跳过恢复"""

        self.summary: AuditSummary | None = None
        """写好的汇总，恢复原目标之后当作执行结果的 data 交出去"""

        self.used_dir_name_set: set[str] = set()
        """已经用掉的子目录名（防撞名覆盖）"""

        self.result_list: list[AuditAgentResult] = []
        """每个角色的结果"""

    # ── 节点 ────────────────────────────────────────────────────────────
    @operation_node(name='确认特训目标页', is_start_node=True, node_max_retry_times=3)
    def ensure_page(self) -> OperationRoundResult:
        """确认停在特训目标页。不在就自己走过去。"""
        if agent_plan_page.is_agent_plan_page(self.ctx, self.last_screenshot):
            return self.round_success(status='已在特训目标页')
        return self.round_by_op_result(GotoAgentPlan(self.ctx).execute(), retry_on_fail=True)

    @node_from(from_name='确认特训目标页')
    @operation_node(name='记住原目标')
    def remember_original_target(self) -> OperationRoundResult:
        """记下进来时的特训目标，跑完好放回去。读不到就留空，恢复那一步会跳过。"""
        self.original_agent_name = agent_plan_page.read_agent_name(
            self.ctx, self.last_screenshot
        )
        if not self.original_agent_name:
            return self.round_success(status='进来时没有特训目标')
        return self.round_success(status=f'原特训目标 {self.original_agent_name}')

    @node_from(from_name='记住原目标')
    @operation_node(name='准备输出目录')
    def prepare_output_dir(self) -> OperationRoundResult:
        """建输出根目录，顺便确认名单不是空的。"""
        if not self.agent_name_list:
            return self.round_fail(
                status='名单是空的：先在游戏里跑一次「扫描代理人列表」，或者去 Web 页勾选要养的角色'
            )

        # 配了自定义目录就不去问插件数据目录，免得白算一次路径
        custom_dir = (self.config.audit_output_dir or '').strip()
        data_dir = (
            Path('.') if custom_dir
            else self.ctx.get_plugin_data_dir(agent_charge_plan_const.APP_ID)
        )
        root = resolve_output_root(data_dir, custom_dir, datetime.now())
        root.mkdir(parents=True, exist_ok=True)
        self.output_root = root
        if not custom_dir:
            # 默认位置旁边说一句「这是产物，能删」，免得用户以为要备份
            readme_path = write_root_readme(root.parent)
            log.info('核对产物目录说明：%s', readme_path)
        log.info('要核对 %s 个角色（%s），结果放 %s', len(self.agent_name_list), self.agent_source, root)
        return self.round_success(status=f'输出目录 {root}')

    @node_from(from_name='准备输出目录')
    @operation_node(name='核对一个角色')
    def audit_one_agent(self) -> OperationRoundResult:
        """核对名单里的下一个角色。一轮一个，全部核对完就收尾。"""
        name = self._take_next_agent()
        if name is None:
            return self.round_success(status=STATUS_AUDIT_DONE)

        result = AuditAgentResult(agent_name=name)
        result.dir_name = unique_dir_name(name, self.used_dir_name_set)
        self.used_dir_name_set.add(result.dir_name)
        done_count = len(self.result_list) + 1
        log.info('核对 %s/%s：%s', done_count, len(self.agent_name_list), name)

        try:
            self._audit_one(name, result)
        except Exception as exc:  # noqa: BLE001 - 单个角色出岔子不能中断整轮
            log.error('核对「%s」时出错：%s', name, exc, exc_info=True)
            result.error = f'核对出错：{exc}'

        self.result_list.append(result)
        if result.ok:
            log.info(
                '「%s」条目 %s 条，可刷 %s 条，预计 %s 电量',
                name, result.item_count, result.planned_count, result.estimated_charge,
            )
        else:
            log.warning('「%s」没核对成功：%s', name, result.error)
        return self.round_wait(status=f'已核对 {name}', wait=0.3)

    @node_from(from_name='核对一个角色', status=STATUS_AUDIT_DONE)
    @operation_node(name='写汇总')
    def write_summary(self) -> OperationRoundResult:
        """写 `汇总.md`，整轮结束。"""
        if self.output_root is None:
            return self.round_fail(status='输出目录没建起来')

        generated_at = datetime.now()
        markdown = build_summary_markdown(
            self.result_list, self.output_root, generated_at, self.agent_source
        )
        summary_path = self.output_root / SUMMARY_FILE_NAME
        summary_path.write_text(markdown, encoding='utf-8')

        summary = AuditSummary(
            output_root=str(self.output_root),
            summary_path=str(summary_path),
            result_list=list(self.result_list),
        )
        self.summary = summary
        for line in summary.summary_lines():
            log.info(line)
        return self.round_success(status=f'核对 {len(self.result_list)} 个角色，汇总 {summary_path}',
                                  data=summary)

    @node_from(from_name='写汇总')
    @operation_node(name='恢复原目标')
    def restore_target(self) -> OperationRoundResult:
        """把特训目标放回进来时那个。核对只读，不该留下痕迹。"""
        ok, detail = restore_agent_target(self.ctx, self.original_agent_name)
        return self.round_success(status=detail, data=self.summary if ok else None)

    # ── 小工具 ──────────────────────────────────────────────────────────
    def _take_next_agent(self) -> str | None:
        """从待核对名单里取下一个角色；取完了返回 None。"""
        while self.pending_name_list:
            name = self.pending_name_list.pop(0).strip()
            if name:
                return name
        return None

    def _audit_one(self, agent_name: str, result: AuditAgentResult) -> None:
        """核对一个角色：切过去 → 读清单 → 算计划 → 落盘。

        Args:
            agent_name: 角色名
            result: 往里填结果
        """
        change_result = ChangeAgentPlanTarget(
            self.ctx, target_agent_name=agent_name, goto_first=True
        ).execute()
        if not change_result.success:
            result.error = f'切到这个角色失败：{change_result.status}'
            return

        read_result = ReadAgentPlanList(self.ctx, goto_first=False).execute()
        snapshot = read_result.data
        if not read_result.success or not isinstance(snapshot, AgentPlanSnapshot):
            result.error = f'读特训清单失败：{read_result.status}'
            return

        # 读完清单马上截一张，别等切下一个角色时再截（那时已经是别人的页面了）
        screen = self.screenshot()

        plan_result = self.mapper.build(
            PlanSnapshot.from_reader_snapshot(snapshot), self.availability
        )
        result.item_count = len(snapshot.item_list)
        result.planned_count = len(plan_result.plan_list)
        result.estimated_charge = plan_result.estimated_total_charge
        # PlanMapper 把「跳过」只记在 changes 里，这里补成 warnings，
        # 不然「条目 4 / 可刷 3」差的那条看起来像漏了
        result.warnings = combined_warnings(plan_result)

        if self.output_root is None:
            result.error = '输出目录没建起来'
            return

        target_dir = self.output_root / (result.dir_name or sanitize_dir_name(agent_name))
        result.file_list = write_agent_files(
            target_dir, agent_name, snapshot, plan_result, screen
        )
        log.info('「%s」的结果已写进 %s', agent_name, target_dir)


def __debug() -> None:
    from agent_charge_plan.agent_charge_plan_config import AgentChargePlanConfig
    from zzz_od.context.zzz_context import ZContext

    ctx = ZContext()
    ctx.init_by_config()
    ctx.init_ocr()
    ctx.run_context.start_running()
    config = AgentChargePlanConfig(instance_idx=ctx.current_instance_idx, group_id='one_dragon')
    op = AuditAgents(
        ctx,
        config=config,
        availability=ChargeAvailability(),
        instance_idx=ctx.current_instance_idx,
        group_id='one_dragon',
    )
    result = op.execute()
    log.info('核对结果 %s', result.status)


if __name__ == '__main__':
    __debug()
