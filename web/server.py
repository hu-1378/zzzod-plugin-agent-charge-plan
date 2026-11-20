"""体力计划本地配置页 —— 后端服务（只用 Python 标准库）。

为什么单独做一个 Web 页：
- 一条龙 GUI / backend server 慢，是因为要初始化 ``ZContext``（OCR / YOLO 模型）。
- 体力计划配置本身就是 ``config/<实例号>/<组名>/charge_plan.yml`` 纯文本，直接读写 YAML 即可。
  本服务不初始化 ``ZContext``、不 import ``PySide6``，只 import 配置类和
  ``CompendiumService``（只读 ``assets/game_data/compendium_data.yml``），启动约 1 秒。

接口：
- ``GET  /``            返回 index.html
- ``GET  /api/meta``    实例 / 组 / 配置文件列表 + 体力计划下拉选项
- ``GET  /api/config``  读取某个 YAML 的完整内容
- ``POST /api/config``  校验后写回（先备份、再原子替换）

启动：``python plugins/agent_charge_plan/web/server.py``（或双击同目录「启动配置页.bat」）
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import threading
import traceback
import uuid
import webbrowser
from collections.abc import Callable
from dataclasses import replace
from datetime import datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any
from urllib.parse import parse_qs, urlparse

import yaml

if TYPE_CHECKING:
    from zzz_od.application.charge_plan.charge_plan_config import (
        CardNumEnum,
        ChargePlanConfig,
        ChargePlanItem,
        RestoreChargeEnum,
    )
    from zzz_od.game_data.compendium import CompendiumService

#: 本文件所在目录（plugins/agent_charge_plan/web）
WEB_DIR: Path = Path(__file__).resolve().parent
#: index.html 与本文件放一起
INDEX_FILE: Path = WEB_DIR / 'index.html'
#: 备份目录名（放在配置所在组目录下）
BACKUP_DIR_NAME: str = '.backup'
#: 默认端口，被占用就 +1 重试
DEFAULT_PORT: int = 23099
#: 最多连续尝试的端口数量
MAX_PORT_TRIES: int = 30
#: POST 请求体上限（8 MiB），防止误传超大内容把内存吃满
MAX_BODY_BYTES: int = 8 * 1024 * 1024
#: 插件自己的配置文件（扫描结果 known_agent_list / 勾选结果 batch_agent_names 在这里）
AGENT_APP_ID: str = 'agent_charge_plan'
#: 一条龙默认组名
ONE_DRAGON_GROUP: str = 'one_dragon'
#: agent_charge_plan.yml 里「要养的角色」那个键
AGENT_NAMES_FIELD: str = 'batch_agent_names'

_INSTANCE_RE = re.compile(r'^\d{1,3}$')
_APP_RE = re.compile(r'^[A-Za-z0-9_-]+$')
# 组名允许中文，但禁止路径分隔符与 Windows 非法字符
_GROUP_RE = re.compile(r'^[^\\/:*?"<>|\x00-\x1f]+$')


class ConfigValidationError(Exception):
    """配置校验不通过：``errors`` 里逐条说明是第几条计划（或第几个角色）的哪个字段有问题。"""

    def __init__(self, errors: list[dict[str, Any]]) -> None:
        self.errors: list[dict[str, Any]] = errors
        summary = '配置校验不通过'
        if errors:
            first = errors[0]
            message = str(first.get('message', ''))
            index = first.get('index')
            if first.get('field') == AGENT_NAMES_FIELD:
                place = f'角色名单第 {int(index) + 1} 个' if index is not None else '角色名单'
                summary = f'{place}：{message}'
            elif index is not None:
                summary = f'第 {int(index) + 1} 条计划：{message}'
            else:
                summary = message
            if len(errors) > 1:
                summary += f'（共 {len(errors)} 处问题）'
        super().__init__(summary)


def looks_like_repo_root(path: Path) -> bool:
    """判断目录是不是一条龙仓库根目录（同时有 config/ 和 src/zzz_od/）。"""
    return (path / 'config').is_dir() and (path / 'src' / 'zzz_od').is_dir()


def detect_repo_root(start: Path | None = None) -> Path | None:
    """从 web 目录 / 当前工作目录逐级向上找仓库根目录。"""
    starts = [start] if start is not None else [WEB_DIR, Path.cwd()]
    for base in starts:
        if base is None:
            continue
        base = Path(base)
        for candidate in [base, *base.parents]:
            if looks_like_repo_root(candidate):
                return candidate
    return None


class ChargePlanStore:
    """扫描 ``config/`` 并按「实例号 / 组名 / 应用」读写 YAML。"""

    #: ChargePlanItem 的 dataclass 字段名，用来从用户数据里挑出已知字段做校验
    _ITEM_FIELDS: tuple[str, ...] = (
        'tab_name',
        'category_name',
        'mission_type_name',
        'mission_name',
        'level',
        'auto_battle_config',
        'run_times',
        'plan_times',
        'card_num',
        'predefined_team_idx',
        'notorious_hunt_buff_num',
        'plan_id',
        'skipped',
    )

    def __init__(self, root: Path) -> None:
        self.root: Path = root
        self.config_dir: Path = root / 'config'
        # 延迟 import：只有真正用到下拉选项时才加载 zzz_od 模块
        self.compendium: CompendiumService | None = None
        self.config_cls: type[ChargePlanConfig] | None = None
        self.item_cls: type[ChargePlanItem] | None = None
        self.card_num_enum: type[CardNumEnum] | None = None
        self.restore_charge_enum: type[RestoreChargeEnum] | None = None

    # ── 数据源 ──────────────────────────────────────────────────────────
    def ensure_modules(self) -> None:
        """import 体力计划配置类与 CompendiumService（轻量，不碰 ZContext）。"""
        if self.compendium is not None:
            return

        src_dir = self.root / 'src'
        if src_dir.is_dir() and str(src_dir) not in sys.path:
            sys.path.insert(0, str(src_dir))

        from zzz_od.application.charge_plan.charge_plan_config import (
            CardNumEnum,
            ChargePlanConfig,
            ChargePlanItem,
            RestoreChargeEnum,
        )
        from zzz_od.game_data.compendium import CompendiumService

        compendium = CompendiumService()
        compendium.reload()

        self.compendium = compendium
        self.config_cls = ChargePlanConfig
        self.item_cls = ChargePlanItem
        self.card_num_enum = CardNumEnum
        self.restore_charge_enum = RestoreChargeEnum

    # ── 目录扫描 ────────────────────────────────────────────────────────
    def list_instances(self) -> list[str]:
        """扫 ``config/`` 下的数字目录，如 01 / 02。"""
        if not self.config_dir.is_dir():
            return []
        names = [p.name for p in self.config_dir.iterdir() if p.is_dir() and _INSTANCE_RE.match(p.name)]
        return sorted(names)

    def list_groups(self, instance: str) -> list[str]:
        """扫 ``config/<实例>/`` 下的组目录，如 one_dragon。"""
        instance_dir = self.config_dir / instance
        if not instance_dir.is_dir():
            return []
        return sorted(
            p.name for p in instance_dir.iterdir() if p.is_dir() and not p.name.startswith('.')
        )

    def list_apps(self, instance: str, group: str) -> list[str]:
        """扫 ``config/<实例>/<组>/`` 下的 ``*.yml``。"""
        group_dir = self.config_dir / instance / group
        if not group_dir.is_dir():
            return []
        return sorted(p.name for p in group_dir.iterdir() if p.is_file() and p.suffix == '.yml')

    def build_meta(self) -> dict[str, Any]:
        """``GET /api/meta`` 的返回内容。"""
        instances = self.list_instances()
        groups: dict[str, list[str]] = {}
        apps: dict[str, dict[str, list[str]]] = {}
        for instance in instances:
            group_names = self.list_groups(instance)
            groups[instance] = group_names
            apps[instance] = {g: self.list_apps(instance, g) for g in group_names}

        default_instance = instances[0] if instances else ''
        default_group = ''
        default_app = ''
        for candidate in ('one_dragon', *groups.get(default_instance, [])):
            if candidate in groups.get(default_instance, []):
                default_group = candidate
                break
        if default_instance and default_group:
            app_names = apps[default_instance][default_group]
            default_app = 'charge_plan' if 'charge_plan.yml' in app_names else (app_names[0] if app_names else '')

        return {
            'root': str(self.root),
            'config_dir': str(self.config_dir),
            'instances': instances,
            'groups': groups,
            'apps': apps,
            'default': {
                'instance': default_instance,
                'group': default_group,
                'app': default_app,
            },
            'charge_plan': self.charge_plan_options(),
            'agent_charge_plan': self.agent_meta(instances, groups),
        }

    def agent_meta(self, instances: list[str], groups: dict[str, list[str]]) -> dict[str, Any]:
        """`agent_charge_plan.yml` 里的角色名单（扫描结果 + 已勾选），按实例分开返回。

        角色名单是插件在游戏里扫出来的，本页只负责展示和写回 `batch_agent_names`。
        """
        per_instance: dict[str, dict[str, Any]] = {}
        for instance in instances:
            group, path = self.find_app_path(instance, AGENT_APP_ID, groups.get(instance, []))
            if path is None:
                per_instance[instance] = {
                    'group': group,
                    'exists': False,
                    'rel_path': f'config/{instance}/{group}/{AGENT_APP_ID}.yml',
                    'known_agent_list': [],
                    'batch_agent_names': [],
                }
                continue
            known: list[str] = []
            batch: list[str] = []
            error = ''
            try:
                data = self.load_yaml(path)
                known = self.clean_agent_names(data.get('known_agent_list'))
                batch = self.clean_agent_names(data.get('batch_agent_names'))
            except (OSError, ValueError, yaml.YAMLError) as exc:
                error = f'读取 {self.relative_path(path)} 失败: {exc}'
            entry: dict[str, Any] = {
                'group': group,
                'exists': True,
                'rel_path': self.relative_path(path),
                'known_agent_list': known,
                'batch_agent_names': batch,
            }
            if error:
                entry['error'] = error
            per_instance[instance] = entry

        return {'app': AGENT_APP_ID, 'instances': per_instance}

    def find_app_path(
        self, instance: str, app: str, group_names: list[str]
    ) -> tuple[str, Path | None]:
        """在某个实例下找 `app.yml`，优先 `one_dragon` 组，找不到则给出兜底组名。"""
        ordered = ([ONE_DRAGON_GROUP] if ONE_DRAGON_GROUP in group_names else []) + [
            name for name in group_names if name != ONE_DRAGON_GROUP
        ]
        for group in ordered:
            candidate = self.config_dir / instance / group / f'{app}.yml'
            if candidate.is_file():
                return group, candidate
        fallback = ordered[0] if ordered else ONE_DRAGON_GROUP
        return fallback, None

    def charge_plan_options(self) -> dict[str, Any]:
        """体力计划的可选值：分类 / 副本类型 / 副本名 / 卡片数量 / 恢复电量。"""
        self.ensure_modules()
        assert self.compendium is not None
        assert self.card_num_enum is not None
        assert self.restore_charge_enum is not None

        category_list: list[dict[str, str]] = []
        mission_type_map: dict[str, list[dict[str, str]]] = {}
        mission_map: dict[str, dict[str, list[dict[str, str]]]] = {}

        for category in self.compendium.get_charge_plan_category_list():
            category_list.append({'label': category.label, 'value': category.value})
            mission_types: list[dict[str, str]] = []
            sub_map: dict[str, list[dict[str, str]]] = {}
            for mission_type in self.compendium.get_charge_plan_mission_type_list(category.value):
                mission_types.append({'label': mission_type.label, 'value': mission_type.value})
                sub_map[mission_type.value] = [
                    {'label': mission.label, 'value': mission.value}
                    for mission in self.compendium.get_charge_plan_mission_list(
                        category.value, mission_type.value
                    )
                ]
            mission_type_map[category.value] = mission_types
            mission_map[category.value] = sub_map

        return {
            'category_list': category_list,
            'mission_type_map': mission_type_map,
            'mission_map': mission_map,
            'card_num_list': [
                {'label': label, 'value': value} for label, value in self.card_num_pairs()
            ],
            'restore_charge_list': [
                {'label': item.value.label, 'value': item.value.value}
                for item in self.restore_charge_enum
            ],
        }

    # ── 路径解析 ────────────────────────────────────────────────────────
    def config_path(self, instance: str, group: str, app: str) -> Path:
        """把三个参数拼成 YAML 路径，并做防穿越校验。"""
        if not _INSTANCE_RE.match(instance or ''):
            raise ValueError(f'实例号不合法: {instance!r}（应为数字目录名，如 01）')
        if not _GROUP_RE.match(group or '') or group.startswith('.'):
            raise ValueError(f'组名不合法: {group!r}')
        if not _APP_RE.match(app or ''):
            raise ValueError(f'配置文件名不合法: {app!r}')

        path = self.config_dir / instance / group / f'{app}.yml'
        resolved = path.resolve()
        if not resolved.is_relative_to(self.config_dir.resolve()):
            raise ValueError(f'配置文件路径越界: {resolved}')
        return path

    def relative_path(self, path: Path) -> str:
        """相对仓库根目录的路径写法，用于页面提示。"""
        try:
            return path.resolve().relative_to(self.root.resolve()).as_posix()
        except ValueError:
            return path.as_posix()

    # ── 读写 ────────────────────────────────────────────────────────────
    def load_yaml(self, path: Path) -> dict[str, Any]:
        """读 YAML 文本并解析成字典。"""
        if not path.is_file():
            raise FileNotFoundError(f'配置文件不存在: {self.relative_path(path)}')
        text = path.read_text(encoding='utf-8')
        data = yaml.safe_load(text)
        if data is None:
            return {}
        if not isinstance(data, dict):
            raise ValueError(f'{self.relative_path(path)} 的根节点不是字典，本页不处理这种文件')
        return data

    def read_config(self, instance: str, group: str, app: str) -> dict[str, Any]:
        """``GET /api/config``：返回 YAML 完整内容（不丢字段）。"""
        path = self.config_path(instance, group, app)
        data = self.load_yaml(path)
        plan_list = data.get('plan_list')
        return {
            'instance': instance,
            'group': group,
            'app': app,
            'path': str(path.resolve()),
            'rel_path': self.relative_path(path),
            'plan_count': len(plan_list) if isinstance(plan_list, list) else 0,
            'data': data,
        }

    def write_config(
        self, instance: str, group: str, app: str, data: Any
    ) -> dict[str, Any]:
        """``POST /api/config``：只覆盖 data 里给的字段，其余原样保留。"""
        if not isinstance(data, dict):
            raise ConfigValidationError(
                [{'index': None, 'field': 'data', 'message': 'data 必须是对象'}]
            )

        path = self.config_path(instance, group, app)
        current = self.load_yaml(path)
        warnings: list[str] = []

        merged: dict[str, Any] = dict(current)
        if 'plan_list' in data:
            plan_list, plan_warnings = self.validate_plan_list(data['plan_list'])
            merged['plan_list'] = plan_list
            warnings.extend(plan_warnings)
        if 'batch_agent_names' in data:
            batch_names = self.validate_agent_names(data['batch_agent_names'])
            known_agent_list = self.clean_agent_names(current.get('known_agent_list'))
            unknown = [name for name in batch_names if known_agent_list and name not in known_agent_list]
            if unknown:
                warnings.append(
                    f'这些角色不在当前 known_agent_list 里，仍按勾选结果写入：{"、".join(unknown)}'
                )
            merged['batch_agent_names'] = batch_names
        for key, value in data.items():
            if key in ('plan_list', 'batch_agent_names'):
                continue
            merged[key] = value

        dump_text = yaml.safe_dump(merged, allow_unicode=True, sort_keys=False)
        backup_path = self.write_backup(path, app)
        self.atomic_write(path, dump_text)

        result: dict[str, Any] = {
            'instance': instance,
            'group': group,
            'app': app,
            'path': str(path.resolve()),
            'rel_path': self.relative_path(path),
            'backup_rel_path': f'{self.relative_path(path.parent / BACKUP_DIR_NAME)}/{backup_path.name}',
            'backup_path': str(backup_path.resolve()),
            'plan_count': len(merged.get('plan_list') or []),
            'warnings': warnings,
            'message': (
                f'已写入 {self.relative_path(path)}'
                f'（备份 {BACKUP_DIR_NAME}/{backup_path.name}）'
            ),
        }
        if 'batch_agent_names' in merged:
            result['batch_agent_names'] = merged['batch_agent_names']
        return result

    # ── 角色名单（agent_charge_plan.yml）──────────────────────────────────
    @staticmethod
    def clean_agent_names(raw: Any) -> list[str]:
        """把 YAML 里读到的名字列表洗成干净的字符串列表（去空、去重、保留顺序）。"""
        if not isinstance(raw, list):
            return []
        names: list[str] = []
        for item in raw:
            if not isinstance(item, str):
                continue
            name = item.strip()
            if name and name not in names:
                names.append(name)
        return names

    def validate_agent_names(self, raw: Any) -> list[str]:
        """校验 ``batch_agent_names``；不合规直接抛 ConfigValidationError。"""
        if not isinstance(raw, list):
            raise ConfigValidationError(
                [
                    {
                        'index': None,
                        'field': 'batch_agent_names',
                        'message': 'batch_agent_names 必须是数组（角色名列表）',
                    }
                ]
            )

        names: list[str] = []
        errors: list[dict[str, Any]] = []
        for index, item in enumerate(raw):
            if not isinstance(item, str):
                errors.append(
                    {
                        'index': index,
                        'field': AGENT_NAMES_FIELD,
                        'message': f'角色名必须是字符串，实际是 {item!r}',
                    }
                )
                continue
            name = item.strip()
            if not name:
                errors.append(
                    {
                        'index': index,
                        'field': AGENT_NAMES_FIELD,
                        'message': '角色名不能是空的',
                    }
                )
                continue
            if name not in names:
                names.append(name)

        if errors:
            raise ConfigValidationError(errors)
        return names

    # ── 校验 ────────────────────────────────────────────────────────────
    def validate_plan_list(self, raw: Any) -> tuple[list[dict[str, Any]], list[str]]:
        """逐条校验计划；有问题抛 ConfigValidationError，绝不静默写坏。

        Returns:
            (规整后的 plan_list, 提示列表)
        """
        if not isinstance(raw, list):
            raise ConfigValidationError(
                [{'index': None, 'field': 'plan_list', 'message': 'plan_list 必须是列表'}]
            )

        self.ensure_modules()
        assert self.compendium is not None
        assert self.config_cls is not None
        assert self.item_cls is not None
        ctx = SimpleNamespace(compendium_service=self.compendium)

        plans: list[dict[str, Any]] = []
        errors: list[dict[str, Any]] = []
        warnings: list[str] = []

        for index, entry in enumerate(raw):
            if not isinstance(entry, dict):
                errors.append(
                    {'index': index, 'field': 'plan_list', 'message': '这一条不是对象（键值对）'}
                )
                continue

            normalized, entry_errors = self.normalize_plan(entry, index)
            if entry_errors:
                errors.extend(entry_errors)
                continue
            assert normalized is not None

            kwargs = {k: v for k, v in normalized.items() if k in self._ITEM_FIELDS}
            try:
                item = self.item_cls(**kwargs)
            except (TypeError, ValueError) as exc:
                errors.append(
                    {'index': index, 'field': '', 'message': f'字段不匹配: {exc}'}
                )
                continue

            error, warning = self.validate_plan_item(ctx, item, index)
            if error is not None:
                errors.append(dict(error, index=index))
                continue
            if warning is not None:
                warnings.append(warning)
            plans.append(normalized)

        if errors:
            raise ConfigValidationError(errors)
        return plans, warnings

    def validate_plan_item(
        self, ctx: Any, item: ChargePlanItem, index: int
    ) -> tuple[dict[str, Any] | None, str | None]:
        """调用 ``ChargePlanConfig.validate_item``，并把「历史残留 mission_name」放行。"""
        assert self.config_cls is not None
        message = self.config_cls.validate_item(ctx, item)
        if message is None:
            return None, None

        # 历史数据里 mission_name 常留着 ChargePlanItem 的默认值「调查专项」，
        # 而这些分类本身没有可选副本（compendium 的 mission_list 为空）。
        # validate_item 会把这种残留值判成不合法；这里只放行这一种情况，并给出提示。
        missions = self.mission_values(item.category_name, item.mission_type_name)
        if missions or item.mission_name is None:
            return {'field': self.guess_field(message), 'message': message}, None

        probe = replace(item, mission_name=None)
        if self.config_cls.validate_item(ctx, probe) is not None:
            return {'field': self.guess_field(message), 'message': message}, None

        warning = (
            f'第 {index + 1} 条：{item.category_name} 没有可选副本，'
            f'mission_name={item.mission_name!r} 是历史残留值，已按原样保留'
        )
        return None, warning

    def mission_values(self, category_name: str, mission_type_name: str | None) -> list[str]:
        """取某个分类 / 副本类型下的副本名列表；取不到就当作空。"""
        if self.compendium is None:
            return []
        try:
            missions = self.compendium.get_charge_plan_mission_list(
                category_name, mission_type_name or ''
            )
        except Exception:  # noqa: BLE001 - 数据源异常不影响其它校验
            return []
        return [mission.value for mission in missions]

    @staticmethod
    def guess_field(message: str) -> str:
        """从 validate_item 的报错文本里猜出字段名，方便前端标红。"""
        lowered = message.lower()
        if lowered.startswith('category'):
            return 'category_name'
        if lowered.startswith('mission_type'):
            return 'mission_type_name'
        if lowered.startswith('mission ') or lowered.startswith('mission_name'):
            return 'mission_name'
        if '没有 mission_type' in message:
            return 'mission_type_name'
        return ''

    def normalize_plan(
        self, entry: dict[str, Any], index: int
    ) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
        """把一条计划的数据规整成可写回的形式；返回 (规整结果, 错误列表)。"""
        assert self.card_num_enum is not None
        plan: dict[str, Any] = dict(entry)
        errors: list[dict[str, Any]] = []

        category_name = plan.get('category_name')
        if not isinstance(category_name, str) or not category_name:
            errors.append({'index': index, 'field': 'category_name', 'message': '分类不能为空'})
        elif category_name not in self.category_values():
            allowed = '、'.join(self.category_values())
            errors.append(
                {
                    'index': index,
                    'field': 'category_name',
                    'message': f'分类 {category_name!r} 不合法（可选：{allowed}）',
                }
            )

        mission_type_name = plan.get('mission_type_name')
        if mission_type_name is None:
            plan['mission_type_name'] = ''
        elif not isinstance(mission_type_name, str):
            errors.append(
                {'index': index, 'field': 'mission_type_name', 'message': '副本类型必须是字符串'}
            )

        mission_name = plan.get('mission_name')
        if mission_name is not None and not isinstance(mission_name, str):
            errors.append(
                {'index': index, 'field': 'mission_name', 'message': '副本名必须是字符串或 null'}
            )

        # 卡片数量：允许传显示名（3张卡片）或存储值（3），统一成存储值
        card_num = plan.get('card_num')
        card_num_map = self.card_num_value_map()
        if card_num is None:
            plan['card_num'] = self.card_num_pairs()[0][1]
        else:
            text = str(card_num)
            if text in card_num_map:
                plan['card_num'] = card_num_map[text]
            else:
                allowed = '、'.join(label for label, _ in self.card_num_pairs())
                errors.append(
                    {
                        'index': index,
                        'field': 'card_num',
                        'message': f'卡片数量 {card_num!r} 不合法（可选：{allowed}）',
                    }
                )

        for field_name in ('run_times', 'plan_times', 'predefined_team_idx', 'notorious_hunt_buff_num'):
            if field_name not in plan or plan[field_name] is None:
                continue
            try:
                value = int(plan[field_name])
            except (TypeError, ValueError):
                errors.append(
                    {'index': index, 'field': field_name, 'message': f'{field_name} 必须是整数'}
                )
                continue
            plan[field_name] = value
            if field_name in ('run_times', 'plan_times') and value < 0:
                errors.append(
                    {
                        'index': index,
                        'field': field_name,
                        'message': f'{field_name} 不能是负数（当前 {value}）',
                    }
                )

        plan.setdefault('tab_name', '训练')
        plan.setdefault('level', '默认等级')
        plan.setdefault('auto_battle_config', '全配队通用')
        plan.setdefault('run_times', 0)
        plan.setdefault('plan_times', 1)
        plan.setdefault('predefined_team_idx', -1)
        plan.setdefault('notorious_hunt_buff_num', 1)
        if not plan.get('plan_id'):
            plan['plan_id'] = str(uuid.uuid4())

        return plan, errors

    def category_values(self) -> list[str]:
        """分类的可选值（与 validate_item 用同一份 compendium 数据）。"""
        if self.compendium is None:
            return []
        return [item.value for item in self.compendium.get_charge_plan_category_list()]

    def card_num_pairs(self) -> list[tuple[str, str]]:
        """卡片数量的 (显示名, 存储值) 列表，如 ('3张卡片', '3')。"""
        if self.card_num_enum is None:
            return []
        return [(item.value.label, str(item.value.value)) for item in self.card_num_enum]

    def card_num_value_map(self) -> dict[str, str]:
        """卡片数量：显示名或存储值 → 存储值。"""
        mapping: dict[str, str] = {}
        for label, value in self.card_num_pairs():
            mapping[label] = value
            mapping[value] = value
        return mapping

    # ── 落盘 ────────────────────────────────────────────────────────────
    def write_backup(self, path: Path, app: str) -> Path:
        """把当前文件原样复制到 ``.backup/<app>.<时间戳>.yml``。"""
        backup_dir = path.parent / BACKUP_DIR_NAME
        backup_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        candidate = backup_dir / f'{app}.{stamp}.yml'
        index = 1
        while candidate.exists():
            candidate = backup_dir / f'{app}.{stamp}_{index}.yml'
            index += 1
        candidate.write_bytes(path.read_bytes())
        return candidate

    def atomic_write(self, path: Path, text: str) -> None:
        """先写同目录临时文件，再 ``os.replace`` 覆盖，避免写一半崩掉。"""
        tmp_path = path.with_name(f'.{path.name}.{os.getpid()}.tmp')
        try:
            with open(tmp_path, 'w', encoding='utf-8', newline='\n') as file:
                file.write(text)
                file.flush()
                os.fsync(file.fileno())
            os.replace(tmp_path, path)
        finally:
            if tmp_path.exists():
                tmp_path.unlink()


class ConfigPageHandler(BaseHTTPRequestHandler):
    """HTTP 请求处理；``store`` 由 :func:`create_server` 注入。"""

    store: ChargePlanStore
    server_version: str = 'ChargePlanWeb/1.0'
    protocol_version: str = 'HTTP/1.1'

    # ── 路由 ────────────────────────────────────────────────────────────
    def do_GET(self) -> None:
        route = urlparse(self.path)
        if route.path in ('/', '/index.html'):
            self.send_index()
            return
        if route.path == '/favicon.ico':
            self.send_bytes(HTTPStatus.NO_CONTENT, b'', 'image/x-icon')
            return
        if route.path == '/api/meta':
            self.run_safely(self.store.build_meta)
            return
        if route.path == '/api/config':
            query = parse_qs(route.query)
            instance = self.query_value(query, 'instance')
            group = self.query_value(query, 'group')
            app = self.query_value(query, 'app')
            self.run_safely(lambda: self.store.read_config(instance, group, app))
            return
        self.send_json(HTTPStatus.NOT_FOUND, {'ok': False, 'error': f'未知路径 {route.path}'})

    def do_POST(self) -> None:
        route = urlparse(self.path)
        if route.path != '/api/config':
            self.send_json(HTTPStatus.NOT_FOUND, {'ok': False, 'error': f'未知路径 {route.path}'})
            return

        body = self.read_json_body()
        if body is None:
            return
        instance = str(body.get('instance') or '')
        group = str(body.get('group') or '')
        app = str(body.get('app') or '')
        data = body.get('data')
        self.run_safely(lambda: self.store.write_config(instance, group, app, data))

    # ── 请求工具 ────────────────────────────────────────────────────────
    @staticmethod
    def query_value(query: dict[str, list[str]], key: str) -> str:
        values = query.get(key) or ['']
        return values[0]

    def read_json_body(self) -> dict[str, Any] | None:
        """读 POST 的 JSON 请求体；解析失败时已回写错误，返回 None。"""
        try:
            length = int(self.headers.get('Content-Length') or 0)
        except ValueError:
            length = 0
        if length <= 0:
            self.send_json(HTTPStatus.BAD_REQUEST, {'ok': False, 'error': '请求体为空'})
            return None
        if length > MAX_BODY_BYTES:
            self.send_json(
                HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
                {'ok': False, 'error': f'请求体超过 {MAX_BODY_BYTES} 字节'},
            )
            return None
        raw = self.rfile.read(length)
        try:
            body = json.loads(raw.decode('utf-8'))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            self.send_json(
                HTTPStatus.BAD_REQUEST, {'ok': False, 'error': f'请求体不是合法 JSON: {exc}'}
            )
            return None
        if not isinstance(body, dict):
            self.send_json(
                HTTPStatus.BAD_REQUEST, {'ok': False, 'error': '请求体必须是 JSON 对象'}
            )
            return None
        return body

    def run_safely(self, func: Callable[[], dict[str, Any]]) -> None:
        """跑一个业务函数，把异常翻译成 JSON 错误，不让服务挂掉。"""
        try:
            payload = func()
        except ConfigValidationError as exc:
            self.send_json(
                HTTPStatus.BAD_REQUEST,
                {'ok': False, 'error': str(exc), 'errors': exc.errors},
            )
            return
        except FileNotFoundError as exc:
            self.send_json(HTTPStatus.NOT_FOUND, {'ok': False, 'error': str(exc)})
            return
        except ValueError as exc:
            self.send_json(HTTPStatus.BAD_REQUEST, {'ok': False, 'error': str(exc)})
            return
        except Exception as exc:  # noqa: BLE001 - 兜底，避免单个请求打断服务
            traceback.print_exc()
            self.send_json(
                HTTPStatus.INTERNAL_SERVER_ERROR,
                {'ok': False, 'error': f'服务内部错误: {exc}'},
            )
            return
        self.send_json(HTTPStatus.OK, {'ok': True, **payload})

    # ── 响应工具 ────────────────────────────────────────────────────────
    def send_json(self, status: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=False, indent=2).encode('utf-8')
        self.send_bytes(status, body, 'application/json; charset=utf-8')

    def send_index(self) -> None:
        if not INDEX_FILE.is_file():
            self.send_bytes(
                HTTPStatus.INTERNAL_SERVER_ERROR,
                f'找不到 {INDEX_FILE.name}'.encode(),
                'text/plain; charset=utf-8',
            )
            return
        self.send_bytes(HTTPStatus.OK, INDEX_FILE.read_bytes(), 'text/html; charset=utf-8')

    def send_bytes(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(int(status))
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        if body:
            self.wfile.write(body)

    def log_message(self, format: str, *args: Any) -> None:
        """只打印一行请求日志，中文控制台里也不会糊成一片。"""
        print(f'[web] {self.address_string()} {format % args}', flush=True)

    def handle_one_request(self) -> None:
        """浏览器关掉页面 / 掐断 keep-alive 连接时不要喷一屏堆栈。"""
        try:
            super().handle_one_request()
        except ConnectionResetError:
            self.close_connection = True


class LocalThreadingHTTPServer(ThreadingHTTPServer):
    """只监听本机的线程化 HTTP 服务。

    Windows 上 ``allow_reuse_address``（即 SO_REUSEADDR）会允许绑定一个**已被占用**的端口，
    于是「被占用就 +1 重试」永远触发不到，两个进程的请求还会串在一起。
    所以 Windows 下必须关掉它；POSIX 下保留，避免重启时撞 TIME_WAIT。
    """

    daemon_threads: bool = True
    allow_reuse_address: bool = os.name != 'nt'


def create_server(
    store: ChargePlanStore, start_port: int = DEFAULT_PORT
) -> tuple[LocalThreadingHTTPServer, int]:
    """在 127.0.0.1 上绑定端口，被占用就 +1 重试。"""
    ConfigPageHandler.store = store
    last_error: OSError | None = None
    for offset in range(MAX_PORT_TRIES):
        port = start_port + offset
        try:
            server = LocalThreadingHTTPServer(('127.0.0.1', port), ConfigPageHandler)
        except OSError as exc:
            last_error = exc
            print(f'端口 {port} 被占用（{exc.strerror or exc}），换 {port + 1} 再试', flush=True)
            continue
        return server, port
    raise SystemExit(
        f'从 {start_port} 开始连续 {MAX_PORT_TRIES} 个端口都不可用，'
        f'请用 --port 指定其它端口（最后一次错误: {last_error}）'
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description='体力计划本地配置页（只监听 127.0.0.1）')
    parser.add_argument(
        '--port', type=int, default=DEFAULT_PORT, help='起始端口，默认 23099，被占用就 +1 重试'
    )
    parser.add_argument('--root', help='仓库根目录（含 config/ 与 src/），默认自动向上查找')
    parser.add_argument('--no-browser', action='store_true', help='启动后不自动打开浏览器')
    args = parser.parse_args(argv)

    root = Path(args.root).resolve() if args.root else detect_repo_root()
    if root is None or not looks_like_repo_root(root):
        print(
            '找不到仓库根目录（需要同时存在 config/ 和 src/zzz_od/）。'
            '请用 --root "Z:\\code\\zzz" 指定。',
            file=sys.stderr,
        )
        return 2

    store = ChargePlanStore(root)
    try:
        store.ensure_modules()
    except Exception as exc:  # noqa: BLE001 - 预加载失败也要先把服务起起来
        print(f'[警告] 预加载体力计划数据失败（下拉选项可能为空）: {exc}', file=sys.stderr)

    server, port = create_server(store, args.port)
    url = f'http://127.0.0.1:{port}/'
    print(f'仓库根目录: {root}', flush=True)
    print(f'配置目录:   {store.config_dir}', flush=True)
    print(f'配置页地址: {url}', flush=True)
    print('按 Ctrl+C 结束服务。', flush=True)

    if not args.no_browser:
        threading.Timer(0.4, webbrowser.open, args=(url,)).start()

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print('\n已停止。', flush=True)
    finally:
        server.server_close()
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
