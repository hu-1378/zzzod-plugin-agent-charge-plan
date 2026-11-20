"""把特训目标恢复成进来时那个。

核对（audit）和边刷边算（execute）都会来回切角色，跑完得把游戏里的特训目标放回去，
不然用户会发现自己的设置被脚本改掉了。

单独放一个文件是因为这两条链路都要用，谁 import 谁都不合适。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from one_dragon.utils.log_utils import log

from .change_agent_plan_target import ChangeAgentPlanTarget

if TYPE_CHECKING:
    from zzz_od.context.zzz_context import ZContext


def restore_agent_target(ctx: ZContext, original_agent_name: str) -> tuple[bool, str]:
    """把特训目标恢复成进来时那个。

    进来时就没读到名字的话什么都不做——宁可不动，也不能瞎设一个。

    Args:
        ctx: 一条龙上下文
        original_agent_name: 进来时读到的特训目标名字，空字符串表示当时没有特训目标

    Returns:
        (是否已经恢复/本来就不用恢复, 给人看的一句话说明)
    """
    name = (original_agent_name or '').strip()
    if not name:
        log.info('进来时没读到特训目标，跳过恢复这一步')
        return True, '进来时没有特训目标，跳过恢复'

    result = ChangeAgentPlanTarget(ctx, target_agent_name=name, goto_first=True).execute()
    if result.success:
        log.info('特训目标已恢复为「%s」', name)
        return True, f'特训目标已恢复为「{name}」'

    log.warning('特训目标没能恢复成「%s」：%s，需要在游戏里手动改回来', name, result.status)
    return False, f'特训目标没能恢复成「{name}」：{result.status}'
