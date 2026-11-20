"""特训目标体力计划插件的常量。

这里只放插件身份和运行行为，展示信息（版本、作者、简介、封面）都在 plugin.json。
APP_ID 必须和插件目录名、plugin.json 的 id 一致；APP_NAME 必须和 plugin.json 的 name 一致。
"""

APP_ID = "agent_charge_plan"
APP_NAME = "特训目标体力计划"

# 进一条龙的默认应用组：可以在「一条龙」里按顺序和其他应用一起跑。
# 想单独跑的时候，应用列表里也能直接选它。
DEFAULT_GROUP = True

# 跑完要发通知。
NEED_NOTIFY = True
