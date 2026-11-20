"""把生成的计划项写进内置体力计划的配置（upsert）。

拿配置的办法：`ctx.run_context.get_config(app_id='charge_plan', instance_idx=..., group_id=...)`。
它返回的是内置「体力计划」应用自己用的**同一个配置实例**，改完 `plan_list` 再 `save()`，
内置应用下次运行就会用到新计划，主仓一行都不用改。

去重靠内容键（`分类|mission_type|mission`），所以反复生成不会堆出重复项：

- 配置里已经有同一个副本 → 只更新次数和卡数，保留用户自己的已跑次数、等级、配队
- 配置里没有 → 追加一条新的
- 配置里其它计划（用户手工加的）→ 一条都不动

注意：实战模拟室的卡数是按手上电量推出来的，这里会覆盖用户手工设的卡数。
"""

from __future__ import annotations

from collections.abc import Iterable
from copy import copy
from typing import TYPE_CHECKING

from .plan_models import PlanBuildResult, PlanWriteChange, PlanWriteResult

if TYPE_CHECKING:
    from zzz_od.application.charge_plan.charge_plan_config import (
        ChargePlanConfig,
        ChargePlanItem,
    )
    from zzz_od.context.zzz_context import ZContext


#: 内置体力计划的应用 ID
CHARGE_PLAN_APP_ID = 'charge_plan'


def content_key(item: ChargePlanItem) -> str:
    """内容级去重键：同一个副本算同一条计划。

    实战模拟室必须带上 mission_name，否则 7 个属性芯片本会被当成同一条。
    """
    return f'{item.category_name}|{item.mission_type_name}|{item.mission_name or ""}'


def content_keys(items: Iterable[ChargePlanItem]) -> list[str]:
    """一批计划项的内容键，用来存成插件配置的 `last_generated_keys`。

    下一轮把它当 `remove_keys` 传回来，就能清掉「缺口已经填平」的旧条目，
    用户自己手工加的计划不在这个列表里，所以不会被清。
    """
    return [content_key(item) for item in items]


class PlanWriter:
    """把 PlanBuildResult 落到内置体力计划配置里。"""

    def __init__(self, ctx: ZContext) -> None:
        """只传上下文，配置实例按账号和应用组现取。"""
        self.ctx: ZContext = ctx

    def get_config(self, instance_idx: int, group_id: str) -> ChargePlanConfig:
        """拿内置体力计划的配置实例（和内置应用用的是同一个对象）。

        Args:
            instance_idx: 账号实例下标
            group_id: 应用组 ID

        Returns:
            内置体力计划的配置对象
        """
        return self.ctx.run_context.get_config(
            app_id=CHARGE_PLAN_APP_ID,
            instance_idx=instance_idx,
            group_id=group_id,
        )

    def write(
        self,
        result: PlanBuildResult,
        instance_idx: int,
        group_id: str,
        dry_run: bool = False,
        remove_keys: Iterable[str] | None = None,
    ) -> PlanWriteResult:
        """取配置再写，给应用流程用的入口。

        Args:
            result: 映射层的结果
            instance_idx: 账号实例下标
            group_id: 应用组 ID
            dry_run: 只预览不写入
            remove_keys: 要删掉的内容键（本插件上一轮排过、这一轮不需要的条目）

        Returns:
            写入结果
        """
        config = self.get_config(instance_idx, group_id)
        return self.apply(config, result, dry_run=dry_run, remove_keys=remove_keys)

    def apply(
        self,
        config: ChargePlanConfig,
        result: PlanBuildResult,
        dry_run: bool = False,
        remove_keys: Iterable[str] | None = None,
    ) -> PlanWriteResult:
        """把计划项 up/down 到配置里。

        Args:
            config: 内置体力计划的配置实例
            result: 映射层的结果
            dry_run: True 时只在副本上算一遍，配置一个字都不改
            remove_keys: 要清掉的内容键；当前结果里还需要的键会被忽略

        Returns:
            写入结果（dry-run 时 written 为 False，plan_list 是「假如写下去会变成什么」）
        """
        warnings: list[str] = []
        changes: list[PlanWriteChange] = []

        # 全程在副本上改，最后一次性换掉，dry-run 就直接扔掉副本
        working: list[ChargePlanItem] = [copy(item) for item in config.plan_list]

        keep_keys = {content_key(item) for item in result.plan_list}
        drop_keys = {key for key in (remove_keys or ()) if key not in keep_keys}
        if drop_keys:
            kept: list[ChargePlanItem] = []
            for item in working:
                key = content_key(item)
                if key in drop_keys:
                    changes.append(
                        PlanWriteChange(
                            kind='remove',
                            key=key,
                            plan_id=item.plan_id,
                            detail='这一轮不需要了',
                        )
                    )
                    continue
                kept.append(item)
            working = kept

        index_by_key: dict[str, int] = {}
        for idx, item in enumerate(working):
            key = content_key(item)
            if key in index_by_key:
                warnings.append(f'配置里本来就有两条 {key}，后一条没动')
                continue
            index_by_key[key] = idx

        changed = bool(drop_keys)
        for item in result.plan_list:
            key = content_key(item)
            existed = index_by_key.get(key)

            if existed is None:
                working.append(copy(item))
                index_by_key[key] = len(working) - 1
                changes.append(
                    PlanWriteChange(
                        kind='add',
                        key=key,
                        plan_id=item.plan_id,
                        plan_times=item.plan_times,
                        card_num=item.card_num,
                    )
                )
                changed = True
                continue

            current = working[existed]
            if current.plan_times == item.plan_times and current.card_num == item.card_num:
                changes.append(
                    PlanWriteChange(
                        kind='keep',
                        key=key,
                        plan_id=current.plan_id,
                        plan_times=current.plan_times,
                        card_num=current.card_num,
                    )
                )
                continue

            detail = (
                f'次数 {current.plan_times}→{item.plan_times}，'
                f'卡数 {current.card_num}→{item.card_num}'
            )
            current.plan_times = item.plan_times
            current.card_num = item.card_num
            changes.append(
                PlanWriteChange(
                    kind='update',
                    key=key,
                    plan_id=current.plan_id,
                    plan_times=item.plan_times,
                    card_num=item.card_num,
                    detail=detail,
                )
            )
            changed = True

        if dry_run:
            return PlanWriteResult(
                written=False,
                plan_list=working,
                changes=changes,
                warnings=warnings,
            )

        if changed:
            config.plan_list = working
            config.save()

        return PlanWriteResult(
            written=changed,
            plan_list=working,
            changes=changes,
            warnings=warnings,
        )
