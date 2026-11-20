"""特训目标体力计划的设置声明。

框架会扫插件根目录下的 `*_app_setting.py`，把这里登记的界面挂到运行卡片的齿轮上。
用 INTERFACE（独立二级页面）而不是悬浮卡片，和贝果计划一致。
"""

from one_dragon_qt.services.app_setting.app_setting_provider import (
    AppSettingProvider,
    SettingType,
)

from .agent_charge_plan_const import APP_ID


class AgentChargePlanAppSetting(AppSettingProvider):
    """告诉框架：这个应用有一个独立设置页面。"""

    app_id = APP_ID
    setting_type = SettingType.INTERFACE

    @staticmethod
    def get_setting_cls() -> type:
        """惰性导入界面类，避免启动时就把 Qt 控件拉起来。"""
        from .gui.agent_charge_plan_setting_interface import (
            AgentChargePlanSettingInterface,
        )

        return AgentChargePlanSettingInterface
