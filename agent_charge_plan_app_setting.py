"""特训目标体力计划的设置声明。

框架会扫插件根目录下的 `*_app_setting.py`，把这里登记的界面挂到运行卡片的齿轮上。
"""

from one_dragon_qt.services.app_setting.app_setting_provider import (
    AppSettingProvider,
    SettingType,
)

from .agent_charge_plan_const import APP_ID


class AgentChargePlanAppSetting(AppSettingProvider):
    """告诉框架：这个应用有一个悬浮设置卡片。"""

    app_id = APP_ID
    setting_type = SettingType.FLYOUT

    @staticmethod
    def get_setting_cls() -> type:
        """惰性导入界面类，避免启动时就把 Qt 控件拉起来。"""
        from .gui.agent_charge_plan_setting_flyout import AgentChargePlanSettingFlyout

        return AgentChargePlanSettingFlyout
