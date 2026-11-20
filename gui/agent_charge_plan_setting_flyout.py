"""特训目标体力计划的设置卡片（悬浮弹窗）。

只放最常用的 4 项。材料库存目标（`material_goals`）是个列表，悬浮卡片放不下，
暂时改 `config/<账号下标>/<组>/agent_charge_plan.yml` 手写。
"""

from __future__ import annotations

from PySide6.QtWidgets import QVBoxLayout

from agent_charge_plan import agent_charge_plan_const
from one_dragon_qt.utils.config_utils import get_prop_adapter
from one_dragon_qt.widgets.app_setting.app_setting_flyout import AppSettingFlyout
from one_dragon_qt.widgets.setting_card.spin_box_setting_card import SpinBoxSettingCard
from one_dragon_qt.widgets.setting_card.switch_setting_card import SwitchSettingCard
from one_dragon_qt.widgets.setting_card.text_setting_card import TextSettingCard


class AgentChargePlanSettingFlyout(AppSettingFlyout):
    """特训目标体力计划的设置弹窗。"""

    def _setup_ui(self, layout: QVBoxLayout) -> None:
        """把 4 个设置项摆上去。

        Args:
            layout: 基类给的竖向布局
        """
        self.target_agent_opt = TextSettingCard(
            icon='',
            title='指定代理人',
            content='留空就用游戏里当前设的特训目标',
            input_placeholder='例如：叶瞬光',
            margins=self.card_margins,
        )
        layout.addWidget(self.target_agent_opt)

        self.dry_run_opt = SwitchSettingCard(
            icon='',
            title='只预览不写入',
            content='打开时只把结果写进日志，不动体力计划。确认没问题再关',
            margins=self.card_margins,
        )
        layout.addWidget(self.dry_run_opt)

        self.default_plan_times_opt = SpinBoxSettingCard(
            icon='',
            title='默认次数',
            content='算不出「缺口 ÷ 单次产出」时，每条计划排几次',
            minimum=1,
            maximum=20,
            margins=self.card_margins,
        )
        layout.addWidget(self.default_plan_times_opt)

        self.include_drive_disc_opt = SwitchSettingCard(
            icon='',
            title='驱动盘也排',
            content='驱动盘没有数量概念（词条随机），默认不排',
            margins=self.card_margins,
        )
        layout.addWidget(self.include_drive_disc_opt)

    def init_config(self) -> None:
        """打开弹窗时按当前配置初始化这 4 个控件。"""
        config = self.ctx.run_context.get_config(
            app_id=agent_charge_plan_const.APP_ID,
            instance_idx=self.ctx.current_instance_idx,
            group_id=self.group_id,
        )
        self.target_agent_opt.init_with_adapter(get_prop_adapter(config, 'target_agent_name'))
        self.dry_run_opt.init_with_adapter(get_prop_adapter(config, 'dry_run'))
        self.default_plan_times_opt.init_with_adapter(
            get_prop_adapter(config, 'default_plan_times')
        )
        self.include_drive_disc_opt.init_with_adapter(
            get_prop_adapter(config, 'include_drive_disc')
        )
