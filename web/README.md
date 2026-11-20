# 体力计划本地配置页

在浏览器里编辑 `config/<实例号>/<组名>/charge_plan.yml` 的小工具，**不启动一条龙 GUI、不加载 OCR / YOLO 模型**。

## 为什么单独做这一页

一条龙 GUI 和 backend server 启动慢，是因为它们要先初始化 `ZContext`（加载 OCR / YOLO 模型）。
体力计划配置本身就是一份纯文本 YAML，直接在文件层面读写就够了：

- `import zzz_od.application.charge_plan.charge_plan_config` 约 0.3 秒，不加载 `PySide6`；
- `CompendiumService().reload()` 只读 `assets/game_data/compendium_data.yml`，约 0.01 秒；
- 所以从敲下命令到能在页面里读写配置，约 1 秒。

本服务**不碰 `ZContext`**，也不 import `PySide6`。

## 文件

| 文件 | 作用 |
|---|---|
| `server.py` | 后端。只用标准库（`http.server` / `json` / `yaml`），只监听 `127.0.0.1` |
| `index.html` | 单页前端，原生 JS，无 CDN、无框架，断网可用 |
| `启动配置页.bat` | 双击启动：优先 `.venv\Scripts\python.exe`，没有就 `uv run python`，起来后自动开浏览器 |
| `README.md` | 本文 |

## 启动

双击 `启动配置页.bat`，或者在仓库根目录执行：

```powershell
.\.venv\Scripts\python.exe plugins\agent_charge_plan\web\server.py
# 没有现成 venv 时
uv run python plugins\agent_charge_plan\web\server.py
```

`.bat` 本体是**纯 ASCII**（中文全在 `server.py` 里打印）。cmd.exe 用系统 OEM 代码页解析批处理，
UTF-8 中文会让它把命令行拆坏（实测会把 `python server.py` 拆成只跑 `python`，直接进 REPL），
所以不要往这个 bat 里加中文。

启动后控制台会打印一行地址，直接点它，或手工打开：

```
配置页地址: http://127.0.0.1:23099/
```

命令行参数：

| 参数 | 说明 |
|---|---|
| `--port 23099` | 起始端口，默认 23099。被占用会自动 +1 重试，最多连续试 30 个端口 |
| `--root "D:\path\to\zzz-od"` | 仓库根目录（含 `config/` 与 `src/`）。不传时从本文件位置逐级向上找 |
| `--no-browser` | 启动后不自动打开浏览器（脚本 / 远程调试时用） |

## 页面能做什么

- 顶部三个下拉：实例号 / 组 / 配置文件，选完自动加载；
- 只读一行显示的**配置文件绝对路径**，点一下复制；
- 每条体力计划一张卡片：分类、副本类型、副本名、卡片数量、计划次数、已运行次数、自动战斗配置、预备编队，加上移 / 下移 / 删除；
  - 副本名下拉只在该「副本类型」真的有可选副本时出现（实战模拟室就是这样）；
  - 卡片数量下拉只在「实战模拟室」出现；
- 顶部「全局选项」里可以改 `restore_charge`（恢复电量）；
- 「要养的角色」区块：把 `agent_charge_plan.yml` 里 `known_agent_list`（游戏里扫出来的角色名）列成复选框，
  勾选结果写回同一个文件的 `batch_agent_names`；配「全选 / 清空 / 只保留已勾选」三个按钮；
  名单为空时给明确提示，让用户先回游戏里扫描；
- 底部：新增一条计划、保存、**复制配置摘要**（把当前计划压成几行纯文本贴给别人看，含已勾选角色）；
- 保存成功顶部出现绿条，形如
  `已写入 config/01/one_dragon/charge_plan.yml（备份 .backup/charge_plan.20261120_221500.yml）`；
- 保存失败顶部出现红条，写明**第几条计划的哪个字段**错，并把那张卡片标红。

## 扫描 → 勾选 → 批量生成

游戏只能设一个特训目标，所以「批量配计划」分三段，浏览器只负责中间那段：

1. **扫描**（在游戏里，不在本页）：一条龙里跑一次插件应用，打开「只扫描代理人名单」（`scan_only`），
   它会读游戏里「特训目标」弹窗的代理人网格，把名字按顺序写进
   `config/<实例>/one_dragon/agent_charge_plan.yml` 的 `known_agent_list`。
   Web 页**不扫描** —— 浏览器碰不到游戏画面。
2. **勾选**（在本页）：「要养的角色」区块会列出 `known_agent_list` 里的每个角色，
   勾选结果存在页面状态里，点底部「保存到配置文件」时写进同一份 YAML 的 `batch_agent_names`。
   - 「全选」= 全勾上；「清空」= 全部取消；
   - 「只保留已勾选」= 列表只显示已勾选的角色（按钮变成「显示全部角色」可恢复），方便核对最终名单；
   - 名单为空 / 文件不存在时，页面上直接提示「角色列表还是空的 —— 先在游戏里跑一次「扫描代理人列表」，再回来勾选」。
3. **批量生成**（在游戏里）：插件按 `batch_agent_names` 逐个把特训目标切过去、读养成缺口、生成体力计划。

保存分两次写：先写 `charge_plan.yml`（体力计划），再写 `agent_charge_plan.yml`（角色名单）。
只有角色名单真的变了才会写第二份，两份各自有 `.backup/` 备份；`agent_charge_plan.yml` 不存在时跳过角色那一步。

## 接口

所有接口都在 `127.0.0.1` 上，不做鉴权（只监听本机，也不需要鉴权）。

### `GET /`

返回 `index.html`。

### `GET /api/meta`

```jsonc
{
  "root": "D:\\path\\to\\zzz-od",
  "config_dir": "D:\\path\\to\\zzz-od\\config",
  "instances": ["01", "02", "03", "04"],
  "groups": { "01": ["app_run_record", "one_dragon"] },
  "apps": { "01": { "one_dragon": ["charge_plan.yml", "coffee.yml"] } },
  "default": { "instance": "01", "group": "one_dragon", "app": "charge_plan" },
  "charge_plan": {
    "category_list": [{ "label": "实战模拟室", "value": "实战模拟室" }],
    "mission_type_map": { "实战模拟室": [{ "label": "基础材料", "value": "基础材料" }] },
    "mission_map": { "实战模拟室": { "基础材料": [{ "label": "代理人经验", "value": "调查专项" }] } },
    "card_num_list": [{ "label": "默认数量", "value": "默认数量" }, { "label": "5张卡片", "value": "5" }],
    "restore_charge_list": [{ "label": "不使用", "value": "不使用" }]
  },
  "agent_charge_plan": {
    "app": "agent_charge_plan",
    "instances": {
      "01": {
        "group": "one_dragon",
        "exists": true,
        "rel_path": "config/01/one_dragon/agent_charge_plan.yml",
        "known_agent_list": ["安比", "妮可", "比利"],
        "batch_agent_names": ["安比", "比利"]
      }
    }
  }
}
```

`agent_charge_plan.instances[实例号]` 是「要养的角色」区块的数据源：
`known_agent_list` 来自游戏里的扫描，`batch_agent_names` 是本页写回的勾选结果；
`exists=false` 表示这个实例还没有 `agent_charge_plan.yml`（页面上会提示先扫描）。

注意 `label` 和 `value` 不总一样：分类「恶名狩猎」的显示名是「恶名狩猎 深度追猎」，但存进 YAML 的是 `恶名狩猎`；
卡片数量「5张卡片」存的是 `'5'`。前端存的是 `value`。

### `GET /api/config?instance=01&group=one_dragon&app=charge_plan`

返回该 YAML 的**完整内容**（原样 JSON，不丢字段，包括 `history_list` 这类本页不编辑的键）：

```jsonc
{
  "ok": true,
  "instance": "01",
  "group": "one_dragon",
  "app": "charge_plan",
  "path": "D:\\path\\to\\zzz-od\\config\\01\\one_dragon\\charge_plan.yml",
  "rel_path": "config/01/one_dragon/charge_plan.yml",
  "plan_count": 10,
  "data": { "plan_list": [ ... ], "history_list": [ ... ], "skip_plan": false }
}
```

### `POST /api/config`

```json
{ "instance": "01", "group": "one_dragon", "app": "charge_plan", "data": { "plan_list": [ ... ] } }
```

同一个接口也写角色名单（换 `app`，这是另一份文件）：

```json
{ "instance": "01", "group": "one_dragon", "app": "agent_charge_plan",
  "data": { "batch_agent_names": ["安比", "比利"] } }
```

`batch_agent_names` 会去空白、去重、保留顺序；不是数组 / 含非字符串 / 含空名字都会 400，
名字不在当前 `known_agent_list` 里只在 `warnings` 里提示（不拒绝 —— 名单可能是旧扫描的）。

写回规则：

1. **先备份**：当前文件原样复制到 `config/<实例>/<组>/.backup/<app>.<YYYYmmdd_HHMMSS>.yml`，同名已存在时追加 `_1`、`_2`；
2. **原子写**：先写同目录的临时文件、`fsync`，再 `os.replace` 覆盖正式文件；
3. **只改 `data` 里给的键**：`data` 里没出现的键（如 `history_list`、`skip_plan`）原样保留；单条计划里用户手工加的其它键也保留；
4. **写前校验**：每条计划都过 `ChargePlanConfig.validate_item(ctx, item)`（`ctx` 只用到 `.compendium_service`，所以传一个只带这个属性的轻量对象即可），
   校验不过直接返回 400，**不写任何文件**：

```jsonc
{
  "ok": false,
  "error": "第 3 条计划：category 不存在的分类 不合法(合法: [...])",
  "errors": [{ "index": 2, "field": "category_name", "message": "category 不存在的分类 不合法(...)" }]
}
```

成功时返回写入路径、备份路径与提示语：

```jsonc
{
  "ok": true,
  "path": "D:\\path\\to\\zzz-od\\config\\01\\one_dragon\\charge_plan.yml",
  "rel_path": "config/01/one_dragon/charge_plan.yml",
  "backup_rel_path": "config/01/one_dragon/.backup/charge_plan.20261120_221500.yml",
  "plan_count": 10,
  "warnings": [],
  "message": "已写入 config/01/one_dragon/charge_plan.yml（备份 .backup/charge_plan.20261120_221500.yml）"
}
```

YAML 落盘统一 `yaml.safe_dump(..., allow_unicode=True, sort_keys=False)` + UTF-8，字段顺序与内存顺序一致。

用 PowerShell 手测：

```powershell
$meta = Invoke-RestMethod http://127.0.0.1:23099/api/meta
Invoke-RestMethod 'http://127.0.0.1:23099/api/config?instance=01&group=one_dragon&app=charge_plan'
Invoke-RestMethod http://127.0.0.1:23099/api/config -Method Post -ContentType 'application/json; charset=utf-8' `
  -Body (@{ instance='01'; group='one_dragon'; app='charge_plan'; data=@{ plan_list=$meta | Out-Null } } | ConvertTo-Json -Depth 20)
```

（最后一条只是形态示例，实际请把读到的 `plan_list` 原样放回去。）

## 通知：只做页面内提示

用户提过「能不能设置一个通知，内容是配置文件 / 需要配置的提示」。这里只做页面内的两件事：

1. 「复制配置摘要」按钮：把当前计划压成 `分类/副本类型/副本名 × 次数` 的几行文本进剪贴板；
2. 顶部一行只读的配置文件绝对路径，可点击复制。

**没有**接 Windows 系统 toast，也没有接一条龙的通知通道 —— 那条路要 `ctx`，与「不启动脚本」的前提冲突。
保存结果也只在页面顶部用绿条 / 红条提示。

## 已知限制

- **只监听 `127.0.0.1`，没有登录鉴权、没有多用户** —— 设计上就是本机单人用；
- 只编辑 `plan_list` 与 `restore_charge`（`charge_plan.yml`），以及 `batch_agent_names`（`agent_charge_plan.yml`）；
  `history_list`、`tabular`、`known_agent_list` 等其他键只原样带过，不在页面上编辑；
- **角色名单只能勾选，不能在本页新增/删除角色** —— `known_agent_list` 由游戏里的扫描写入，页面只读它；
  想让名单变，就回游戏里再扫一次；
- 校验对「历史残留 `mission_name`」放行：老数据里 `mission_name` 常留着默认值 `调查专项`，而 `区域巡防` / `专业挑战室` / `恶名狩猎` 这些分类本身没有可选副本。
  `validate_item` 会把这种值判为非法，所以本服务在「该分类确实没有可选副本、其余字段都合法」时**保留原值并返回 `warnings`**，否则一条无损写回都做不到；
- 改完文件后，**正在运行**的一条龙不会自动重读，需要重启对应应用（或用 GUI 里的刷新）；
- 页面不读游戏数据以外的状态：`分类 / 副本类型 / 副本名 / 卡片数量 / 恢复电量` 全部来自 `CompendiumService`，如果游戏更新了数据表而仓库里的 `assets/game_data/compendium_data.yml` 没更新，下拉会是旧的；
- 插件如果是单独 clone 的仓库（不在一条龙仓库的 `plugins/agent_charge_plan/` 下），自动找根目录会失败，需要显式 `--root "D:\path\to\zzz-od"`；
- 端口固定从 `23099` 起试，被占用会自动 +1（`端口 23099 被占用（…），换 23100 再试`），想固定端口就传 `--port`。
  Windows 上必须关掉 `allow_reuse_address`（SO_REUSEADDR）：开着它时系统允许绑定**已被占用**的端口，
  占用检测不到、两个进程的请求还会串在一起；`server.py` 里已按平台处理（`LocalThreadingHTTPServer`）。

## 排错

| 现象 | 处理 |
|---|---|
| 控制台报「找不到仓库根目录」 | 用 `--root` 指定含 `config/` 与 `src/zzz_od/` 的目录 |
| 页面下拉全是「（无可选项）」 | `assets/game_data/compendium_data.yml` 缺失或格式变了，看控制台里的 `[警告] 预加载体力计划数据失败` |
| 保存报「配置文件不存在」 | `config/<实例>/<组>/<app>.yml` 没建过，先在 GUI 里创建一次该应用配置 |
| 「要养的角色」显示「角色列表还是空的」 | 还没在游戏里扫描过：跑一次 `scan_only`（只扫描代理人名单）把 `known_agent_list` 刷出来，然后刷新本页 |
| 勾选后保存，角色名单没变 | 只有「勾选结果和文件里的 `batch_agent_names` 不同」才会写第二份文件；另外确认 `agent_charge_plan.yml` 存在（页面第一行会显示它的路径） |
| 想把改动退回去 | 用 `.backup/<app>.<时间戳>.yml` 覆盖回正式文件即可 |
| 双击 bat 闪一下就没了 / 进了 python REPL | bat 里混进了中文，cmd 用 OEM 代码页解析会拆坏命令行；保持这个 bat 纯 ASCII |