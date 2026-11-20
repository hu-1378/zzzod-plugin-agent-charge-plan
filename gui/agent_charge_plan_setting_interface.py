"""特训目标体力计划的设置页（独立二级页面，像贝果计划那样）。

用 `SettingType.INTERFACE` 而不是悬浮卡片，是因为设置项比一屏能放下的多，
而且这块需要一个能写说明的地方（比如「角色名单要先在游戏里扫一次」）。
"""

from __future__ import annotations

from enum import Enum
from typing import TYPE_CHECKING

from PySide6.QtWidgets import QVBoxLayout, QWidget
from qfluentwidgets import CaptionLabel, FluentIcon, SubtitleLabel

from agent_charge_plan import agent_charge_plan_const
from one_dragon.base.config.config_item import ConfigItem
from one_dragon_qt.services.app_setting.app_setting_provider import GroupIdMixin
from one_dragon_qt.utils.config_utils import get_prop_adapter
from one_dragon_qt.widgets.setting_card.combo_box_setting_card import (
    ComboBoxSettingCard,
)
from one_dragon_qt.widgets.setting_card.spin_box_setting_card import SpinBoxSettingCard
from one_dragon_qt.widgets.setting_card.switch_setting_card import SwitchSettingCard
from one_dragon_qt.widgets.setting_card.text_setting_card import TextSettingCard
from one_dragon_qt.widgets.vertical_scroll_interface import VerticalScrollInterface

if TYPE_CHECKING:
    from zzz_od.context.zzz_context import ZContext


class GoalModeEnum(Enum):
    """目标模式：按什么来找活干。"""

    AGENT_STAGE = ConfigItem(label='按角色养成缺口', value='agent_stage')
    MATERIAL_FLOOR = ConfigItem(label='按材料库存下限', value='material_floor')
    BOTH = ConfigItem(label='两者都要', value='both')


class RunModeEnum(Enum):
    """运行模式：算出来之后是写配置还是自己刷。"""

    PLAN_ONLY = ConfigItem(label='只生成计划（交给内置体力计划去跑）', value='plan_only')
    EXECUTE = ConfigItem(label='自己刷（边跑边算，刷完自动切下一个角色）', value='execute')
    AUDIT = ConfigItem(label='全角色核对（只读不刷，按角色输出清单与截图）', value='audit')


class AgentChargePlanSettingInterface(VerticalScrollInterface, GroupIdMixin):
    """特训目标体力计划的设置页。"""

    def __init__(self, ctx: ZContext, parent: QWidget | None = None) -> None:
        """复用主窗口的导航与滚动容器。

        Args:
            ctx: 一条龙上下文
            parent: 父窗口
        """
        super().__init__(
            content_widget=None,
            object_name='agent_charge_plan_setting',
            nav_text_cn='特训目标体力计划设置',
            parent=parent,
        )
        self.ctx: ZContext = ctx
        self.config: object | None = None
        self._config_loaded: bool = False
        #: (设置卡片, 配置字段名) 配对，界面显示时统一初始化
        self._card_field_list: list[tuple[object, str]] = []

    def get_content_widget(self) -> QWidget:
        """搭出设置页的内容。

        Returns:
            内容组件
        """
        widget = QWidget(self)
        layout = QVBoxLayout(widget)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(12)

        title = SubtitleLabel('特训目标体力计划', widget)
        layout.addWidget(title)
        layout.addWidget(
            CaptionLabel(
                '读游戏自带「快捷手册-训练-特训目标」页的缺口，换算成体力计划。设置只对当前账号和应用组生效。',
                widget,
            )
        )

        run_mode_opt = ComboBoxSettingCard(
            icon=FluentIcon.PLAY,
            title='运行模式',
            content='「自己刷」会边跑边算，不用再开内置体力计划',
            options_enum=RunModeEnum,
            parent=widget,
        )
        layout.addWidget(run_mode_opt)
        self._card_field_list.append((run_mode_opt, 'run_mode'))

        goal_mode_opt = ComboBoxSettingCard(
            icon=FluentIcon.DOCUMENT,
            title='目标模式',
            content='按角色养成缺口、按材料库存下限，还是两者都要',
            options_enum=GoalModeEnum,
            parent=widget,
        )
        layout.addWidget(goal_mode_opt)
        self._card_field_list.append((goal_mode_opt, 'goal_mode'))

        target_agent_opt = TextSettingCard(
            icon=FluentIcon.PEOPLE,
            title='指定代理人',
            content='留空就用游戏里当前设的特训目标；批量名单非空时以批量名单为准',
            input_placeholder='例如：叶瞬光',
            parent=widget,
        )
        layout.addWidget(target_agent_opt)
        self._card_field_list.append((target_agent_opt, 'target_agent_name'))

        dry_run_opt = SwitchSettingCard(
            icon=FluentIcon.SYNC,
            title='只预览不写入',
            content='打开时只把结果写进日志，不动体力计划、也不真刷。确认没问题再关',
            parent=widget,
        )
        layout.addWidget(dry_run_opt)
        self._card_field_list.append((dry_run_opt, 'dry_run'))

        scan_only_opt = SwitchSettingCard(
            icon=FluentIcon.SEARCH,
            title='只扫描代理人名单',
            content='打开后跑一次，把游戏里所有代理人名字写进配置，供 Web 配置页勾选',
            parent=widget,
        )
        layout.addWidget(scan_only_opt)
        self._card_field_list.append((scan_only_opt, 'scan_only'))

        default_times_opt = SpinBoxSettingCard(
            icon=FluentIcon.HISTORY,
            title='默认次数',
            content='算不出「缺口 ÷ 单次产出」时，每条计划排几次',
            minimum=1,
            maximum=20,
            parent=widget,
        )
        layout.addWidget(default_times_opt)
        self._card_field_list.append((default_times_opt, 'default_plan_times'))

        drive_disc_opt = SwitchSettingCard(
            icon=FluentIcon.ZOOM,
            title='驱动盘也排',
            content='驱动盘没有数量概念（词条随机），默认不排',
            parent=widget,
        )
        layout.addWidget(drive_disc_opt)
        self._card_field_list.append((drive_disc_opt, 'include_drive_disc'))

        layout.addWidget(
            CaptionLabel(
                '要批量配置多个角色：先在游戏里跑一次「只扫描代理人名单」，'
                '再到 plugins/agent_charge_plan/web/ 双击「启动配置页.bat」勾选。',
                widget,
            )
        )
        layout.addStretch(1)
        return widget

    def on_interface_shown(self) -> None:
        """界面第一次显示时把配置灌进各个卡片。

        `group_id` 是框架在推入页面时才设好的，所以配置要等到这里才取。
        """
        super().on_interface_shown()
        if self._config_loaded:
            return

        self.config = self.ctx.run_context.get_config(
            app_id=agent_charge_plan_const.APP_ID,
            instance_idx=self.ctx.current_instance_idx,
            group_id=self.group_id,
        )
        for card, field_name in self._card_field_list:
            card.init_with_adapter(get_prop_adapter(self.config, field_name))
        self._config_loaded = True
