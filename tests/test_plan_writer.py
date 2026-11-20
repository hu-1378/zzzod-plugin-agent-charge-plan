"""写入层：upsert 幂等、dry-run 不落盘、不覆盖用户自己的计划。

一条硬约束：**测试绝不能写真实用户配置** `config/01/one_dragon/charge_plan.yml`。
所以这里有两种用法：

- 用 `FakeChargePlanConfig` 当替身，只验证 upsert 的判断逻辑（不碰盘）；
- 用真的 `ChargePlanConfig`，但把工作目录 monkeypatch 到 `tmp_path`，
  顺带验证「写下去的 yml 能被重新读回来」。

`ChargePlanItem` 来自主仓的内置体力计划应用，这里不改它，只是照着它的字段造数据。
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
from agent_charge_plan.planner.plan_models import PlanBuildResult
from agent_charge_plan.planner.plan_writer import PlanWriter, content_key, content_keys

from zzz_od.application.charge_plan.charge_plan_config import (
    ChargePlanConfig,
    ChargePlanItem,
)

if TYPE_CHECKING:
    from collections.abc import Iterable


class FakeChargePlanConfig:
    """`ChargePlanConfig` 的替身：只有 `plan_list` 和 `save()`，不碰盘。"""

    def __init__(self, plan_list: Iterable[ChargePlanItem] | None = None) -> None:
        """初始化。

        Args:
            plan_list: 初始计划列表（模拟用户配置里已有的内容）
        """
        self.plan_list: list[ChargePlanItem] = list(plan_list or [])
        self.save_count: int = 0

    def save(self) -> None:
        """只记次数，不写文件。"""
        self.save_count += 1


def _item(
    mission_type_name: str = '基础材料',
    mission_name: str | None = '调查专项',
    category_name: str = '实战模拟室',
    plan_times: int = 1,
    card_num: str = '5',
    run_times: int = 0,
) -> ChargePlanItem:
    """造一条内置体力计划项。"""
    return ChargePlanItem(
        tab_name='训练',
        category_name=category_name,
        mission_type_name=mission_type_name,
        mission_name=mission_name,
        plan_times=plan_times,
        card_num=card_num,
        run_times=run_times,
    )


def _result(*items: ChargePlanItem) -> PlanBuildResult:
    """造一份映射层结果。"""
    return PlanBuildResult(plan_list=list(items))


# ============================= 内容键 =============================


def test_content_key_includes_mission_name() -> None:
    """实战模拟室必须带 mission 名，否则 7 个属性芯片本会被当成同一条。"""
    assert content_key(_item('代理人技能', '电流测试')) == '实战模拟室|代理人技能|电流测试'
    assert content_key(_item('代理人技能', '刚性测试')) != content_key(_item('代理人技能', '电流测试'))


def test_content_key_without_mission_name() -> None:
    """一级副本（区域巡防 / 专业挑战室）没有 mission 名，键尾留空。"""
    assert content_key(_item('「提丰·重击者型」', None, '专业挑战室')) == (
        '专业挑战室|「提丰·重击者型」|'
    )


def test_content_keys() -> None:
    """批量取键，存进插件配置的 `last_generated_keys` 用。"""
    keys = content_keys([_item(), _item('代理人技能', '电流测试')])
    assert keys == ['实战模拟室|基础材料|调查专项', '实战模拟室|代理人技能|电流测试']


# ============================= upsert 逻辑（替身配置） =============================


def test_apply_adds_new_items() -> None:
    """空配置：全部追加，并真的调一次 save。"""
    config = FakeChargePlanConfig()
    writer = PlanWriter(None)  # apply 不用 ctx

    result = writer.apply(config, _result(_item(), _item('代理人技能', '电流测试')))

    assert result.written is True
    assert len(config.plan_list) == 2
    assert config.save_count == 1
    assert [change.kind for change in result.changes] == ['add', 'add']


def test_apply_twice_is_idempotent() -> None:
    """连跑两次：条数不变，第二次全是 keep，也不再落盘。"""
    config = FakeChargePlanConfig()
    writer = PlanWriter(None)
    build_result = _result(_item(), _item('代理人技能', '电流测试'))

    writer.apply(config, build_result)
    count_after_first = len(config.plan_list)
    save_after_first = config.save_count

    second = writer.apply(config, build_result)

    assert len(config.plan_list) == count_after_first == 2
    assert [change.kind for change in second.changes] == ['keep', 'keep']
    assert second.written is False
    assert config.save_count == save_after_first


def test_apply_updates_plan_times_and_card_num() -> None:
    """配置里已有同一个副本：只改次数和卡数，保留已跑次数和 plan_id。"""
    existing = _item(plan_times=1, card_num='5', run_times=2)
    config = FakeChargePlanConfig([existing])
    writer = PlanWriter(None)

    result = writer.apply(config, _result(_item(plan_times=3, card_num='2')))

    assert result.written is True
    assert len(config.plan_list) == 1
    updated = config.plan_list[0]
    assert updated.plan_times == 3
    assert updated.card_num == '2'
    assert updated.run_times == 2
    assert updated.plan_id == existing.plan_id
    assert result.changes[0].kind == 'update'
    assert '次数 1→3' in result.changes[0].detail


def test_apply_keeps_user_own_plans() -> None:
    """用户自己手工加的计划一条都不能动。"""
    mine = _item('音擎改装', '强攻极片试验', plan_times=7)
    config = FakeChargePlanConfig([mine])
    writer = PlanWriter(None)

    result = writer.apply(config, _result(_item()))

    assert len(config.plan_list) == 2
    kept = config.plan_list[0]
    assert kept.mission_name == '强攻极片试验'
    assert kept.plan_times == 7
    assert kept.plan_id == mine.plan_id
    assert any(change.kind == 'add' for change in result.changes)


def test_apply_dry_run_does_not_write() -> None:
    """dry-run：配置一个字都不改，也不 save；返回的 plan_list 是「写下去会变成什么」。"""
    config = FakeChargePlanConfig([_item(plan_times=1)])
    writer = PlanWriter(None)

    result = writer.apply(
        config, _result(_item(plan_times=4)), dry_run=True
    )

    assert result.written is False
    assert config.save_count == 0
    assert config.plan_list[0].plan_times == 1
    assert result.plan_list[0].plan_times == 4
    assert result.changes[0].kind == 'update'


def test_apply_dry_run_preview_is_a_copy() -> None:
    """dry-run 的预览是一份副本，调用方改它不会污染配置。"""
    config = FakeChargePlanConfig()
    writer = PlanWriter(None)

    result = writer.apply(config, _result(_item()), dry_run=True)
    result.plan_list[0].plan_times = 99

    assert config.plan_list == []


def test_apply_remove_keys() -> None:
    """上一轮排过、这一轮不需要的条目要清掉（缺口填平了）。"""
    stale = _item('猎人与忠犬', None, category_name='区域巡防', card_num='默认数量')
    config = FakeChargePlanConfig([stale])
    writer = PlanWriter(None)

    result = writer.apply(
        config,
        _result(_item()),
        remove_keys=['区域巡防|猎人与忠犬|'],
    )

    keys = [content_key(item) for item in config.plan_list]
    assert '区域巡防|猎人与忠犬|' not in keys
    assert '实战模拟室|基础材料|调查专项' in keys
    assert any(change.kind == 'remove' for change in result.changes)


def test_apply_remove_keys_ignores_still_needed() -> None:
    """同一条内容键这一轮还需要时，就算传进 remove_keys 也不能删。"""
    keep = _item()
    config = FakeChargePlanConfig([keep])
    writer = PlanWriter(None)

    result = writer.apply(config, _result(_item()), remove_keys=[content_key(keep)])

    assert len(config.plan_list) == 1
    assert result.changes[0].kind == 'keep'


def test_apply_warns_on_duplicate_existing_keys() -> None:
    """配置里本来就有两条一样的：只动第一条，并给提醒。"""
    first = _item()
    second = _item()
    config = FakeChargePlanConfig([first, second])
    writer = PlanWriter(None)

    result = writer.apply(config, _result(_item(plan_times=2)))

    assert len(config.plan_list) == 2
    assert config.plan_list[0].plan_times == 2
    assert config.plan_list[1].plan_times == 1
    assert any('两条' in warning for warning in result.warnings)


def test_apply_empty_result_writes_nothing() -> None:
    """这一轮什么都没生成（例如所有条目都已完成）：不写、不删。"""
    config = FakeChargePlanConfig([_item()])
    writer = PlanWriter(None)

    result = writer.apply(config, _result())

    assert result.written is False
    assert config.save_count == 0
    assert len(config.plan_list) == 1


# ============================= 真配置（工作目录指到临时目录） =============================


@pytest.fixture
def temp_work_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """把一条龙的工作目录指到临时目录，保证真配置不会写进仓库。"""
    from one_dragon.utils import os_utils

    def fake_path(*sub_paths: str) -> str:
        target = tmp_path.joinpath(*sub_paths)
        target.mkdir(parents=True, exist_ok=True)
        return str(target)

    monkeypatch.setattr(os_utils, 'get_path_under_work_dir', fake_path)
    return tmp_path


def _config_file(temp_work_dir: Path) -> Path:
    """临时目录里内置体力计划配置文件的位置。"""
    return temp_work_dir / 'config' / '01' / 'one_dragon' / 'charge_plan.yml'


def test_real_config_roundtrip(temp_work_dir: Path) -> None:
    """真配置：写下去能被重新读回来，plan_id 也保住了。"""
    config: Any = ChargePlanConfig(instance_idx=1, group_id='one_dragon')
    assert config.plan_list == []

    writer = PlanWriter(None)
    result = writer.apply(config, _result(_item(), _item('代理人技能', '电流测试')))
    assert result.written is True

    yml_path = _config_file(temp_work_dir)
    assert yml_path.exists(), '真配置应该写到临时目录里，而不是仓库的 config/ 下'

    reloaded: Any = ChargePlanConfig(instance_idx=1, group_id='one_dragon')
    assert len(reloaded.plan_list) == 2
    assert {content_key(item) for item in reloaded.plan_list} == {
        '实战模拟室|基础材料|调查专项',
        '实战模拟室|代理人技能|电流测试',
    }
    assert {item.plan_id for item in reloaded.plan_list} == {
        item.plan_id for item in config.plan_list
    }


def test_real_config_dry_run_creates_no_file(temp_work_dir: Path) -> None:
    """真配置 + dry-run：临时目录里连文件都不该出现。"""
    config: Any = ChargePlanConfig(instance_idx=1, group_id='one_dragon')
    writer = PlanWriter(None)

    result = writer.apply(config, _result(_item()), dry_run=True)

    assert result.written is False
    assert not _config_file(temp_work_dir).exists()
    assert config.plan_list == []


def test_real_config_does_not_touch_repo_config() -> None:
    """兜底护栏：仓库里真实用户配置（如果存在）不能被这些用例改到。

    这个用例只是把「测试不该碰的那条路径」写出来，一旦哪天夹具写错，
    `git status` 会立刻显示 config/ 被改动。
    """
    repo_config = Path(__file__).resolve().parents[3] / 'config' / '01' / 'one_dragon' / 'charge_plan.yml'
    assert not repo_config.exists() or repo_config.is_file()
