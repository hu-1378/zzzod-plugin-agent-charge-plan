"""特训目标体力计划插件的常量。

这里只放插件身份和运行行为，展示信息（版本、作者、简介、封面）都在 plugin.json。
APP_ID 必须和插件目录名、plugin.json 的 id 一致；APP_NAME 必须和 plugin.json 的 name 一致。
"""

APP_ID = "agent_charge_plan"
APP_NAME = "特训目标体力计划"

# 不进一条龙默认列表，只在应用列表里单独选。
DEFAULT_GROUP = False

# 跑完要发通知。
NEED_NOTIFY = True
