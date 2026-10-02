# 更新日志

> AstrBot 会在插件详情页读取本文件（依次尝试 `CHANGELOG.md` → `changelog.md` → `CHANGELOG` → `changelog`），
> 按 Markdown 渲染。约定：发版时 `metadata.yaml` 的 `version` 与本文件最新一节标题保持一致。

## v0.1.2

通知会话只保留配置项一个入口：**去掉 `/gh watch`**，UMO 由用户自己在目标会话发 `/sid` 取得后填进配置。

### 变更

- **移除 `/gh watch`**：原先让 bot 自己记下当前会话，等于同一份状态存在插件 KV 与配置项两处，
  「命令写的列表覆盖配置」这条规则既不直观、又让取消订阅取消不干净。现在只有一个入口：
  在目标会话发 `/sid`，把输出里的 `UMO` 一行填进配置项 `notify_targets`
- **`notify_targets` 会校验 UMO**：填 `platform_id:MessageType:session_id`（例如
  `aiocqhttp:GroupMessage:123456`），`MessageType` 取 `GroupMessage` / `FriendMessage` / `OtherMessage`；
  空项与写法不对的条目直接跳过，并在日志里点名是哪一个
- **`/sid` 输出的「」会被剥掉**：连书名号或引号一起复制也能用
- **`/gh config` 的「通知会话」直接列出解析后的平台 / 群聊私聊 / 会话 ID**，不再只是计数

## v0.1.1

修掉几处会让功能静默失效的问题；发布姿态与配置项都没变。

### 修复

- **`owner/repo#编号` 写法必崩**：`SHORT_RE` 的括号嵌套让编号落到了第 3 组，解析处取 `group(3)`
  直接 `IndexError: no such group` —— README 与 `/gh help` 都写着这种写法，实测却完全不可用。
  现在改成三个捕获组（owner / repo / 编号），并补上解析用例
- **老 issue 更新后再也发现不了**：`list_issues` 用 `sort=created` 配 `since`，而 `since` 过滤的是
  `updated_at` —— 刚被更新的老条目排在窗口之外，进不了视野。改用 `sort=updated&direction=desc`，
  这才真的对上 README 承诺的「按 `updated_at` 增量」
- **窗口截断会永久丢条目**：单仓库一轮只取 50 条，取满就把水位推到 `now`，窗口外的条目下一轮不再
  返回。现在窗口取满时不推进水位（多取一条用来判断是否取满），下一轮重扫同一窗口补齐
- **Skill 缺失时不再以模型报错收场**：`read_skill` 的异常原先落在 try 之外，`/gh t` 会抛
  `SkillMissing`；`SKILL.md` 被删也躲过启动自检（自检原先只看目录在不在）。现在自检按
  `SKILL.md` 判定，缺失时回一句能看懂的提示

### 说明

- 补上 MIT 协议（`LICENSE`，Copyright © 2026 lingyun14beta）。插件内置的 `skills/gh-code-review/`
  是第三方内容，按 MIT © 2026 Nefelibata 随附原协议文本，`README` 的「许可」一节写明了这一点
- CI 把 ruff 钉在 `0.15.22`（与 AstrBot 本体的 `.pre-commit-config.yaml` 一致）。首次推送时因为没钉版本，
  CI 装到的 ruff 启用了 `I001` / `SIM117` / `PYI034` 等额外规则，17 个错误让「代码检查」这一步直接失败，
  而同一份代码在本机 `0.15.22` 上是全绿的 —— 钉住版本后两边才可比
- 仓库迁到 [lingyun14beta/astrbot_plugin_github_triage](https://github.com/lingyun14beta/astrbot_plugin_github_triage)：
  `metadata.yaml` 的 `author` 与 `repo` 一并改到该账号下。**插件 ID 随之变为
  `lingyun14beta/astrbot_plugin_github_triage`**，插件的 KV（待发草稿、已处理表、通知列表、轮询水位）
  与 `data/plugin_data/` 目录都会从新位置重新开始 —— 升级到本版后旧数据不会自动搬过来，
  需要按新入口重新配置通知会话（见 v0.1.2）。
- CI 的 `ruff format --check .` 之前会在 `tests/` 上报 8 个文件，已重新格式化（`ruff check` 保持全绿）
- `metadata.yaml` 的 `support_platforms` 去掉了 AstrBot 本体并未注册的 `vocechat`
- 补齐内置 skill 的出处：[NefelibataBIGR/code-review-skill-codex](https://github.com/NefelibataBIGR/code-review-skill-codex)
  （MIT © 2026 Nefelibata），README 与 `skills/gh-code-review/SKILL.md` 都写明了本副本做过哪些本地化改动

## v0.1.0

从"人工审 PR"的流程固化而来：把一条 issue / PR 变成能直接发布的评论草稿，人工确认后才发。

### 新增

- **指令**：`/gh` 指令组 —— `t`（抓取并产出草稿）、`show`、`list`、`post`、`fetch`、`watch on|off|status`、`config`
- **轮询与通知**：`context.cron_manager.add_basic_job()` 注册作业，按 `updated_at` 增量发现新条目，
  推送到订阅会话（`/gh watch on`），已处理表走插件 KV（上限 500 条）
- **抓取**：issue 取正文 / 评论 / 模板未填字段；PR 取元数据 / 改动文件 / 提交 / 完整 diff（带截断标注）
- **自动扫描**：diff 新增行扫高危信号 —— `except → pass`、捕获 `BaseException`、`shell=True`、
  `os.system`、`eval/exec`、关闭 TLS 校验、`noqa` 绕过、新增断言、删除断言；
  按改动文件名扫 —— 改动测试文件、改动依赖或构建清单、改动 CI 配置。结果作为**候选**交给模型核实
- **本地核查**：`gh/workspace.py` 为 PR 建 detached worktree（`FETCH_HEAD`，落在仓库外，
  退出即 `remove` + `prune`）；`gh/tools.py` 提供 `gh_read_file` / `gh_search_code` / `gh_list_dir` /
  `gh_run_check`（白名单命令，默认关闭，可用 `local_python` 指定解释器）；
  PR head 取到本 PR 专属的私有 ref（`refs/gh-triage/pr-N`），并发审多条 PR 不会串代码；
  remote 取不到自动退回 `upstream`（本地是 fork 的场景）
- **工具循环**：配了本地路径的 PR 走 `context.tool_loop_agent`，其余退回单次 `context.llm_generate`
- **发布**：普通评论或 PR review（`COMMENT` / `REQUEST_CHANGES`），自动补 AI 声明；
  发布前自检拦下空正文与疑似凭证，提醒缺失的位置引用与绝对措辞
- **客户端稳健性**：复用同一个 `aiohttp` session（不再每请求建连）；429 / 5xx 按 `Retry-After` 退避一次再试；
  `/gh config` 显示 API 剩余额度
- **轮询水位**：任一仓库抓取失败时不推进 `last_poll_at`，避免它这期间的新条目凭空消失
- **发布记账**：发布的评论 id / 链接记入 KV，`/gh list` 显示已发条数
- **硬开关**：`enabled=false` 时全部 `/gh` 子指令（含发布）一律不响应
- **省调用 / 防重复**：issue 判类型与建档复用同一次 API 调用；同一条目标在途时 `/gh t` 不重复开工；
  PR 改动文件超分页上限时材料里明确标注；`/gh config` 显示 token 归属账号
- **内置 Skill**：`gh-triage`（流程）、`gh-issue-format`（issue 格式与标签口径）、
  `gh-code-review`（PR 审查清单，MIT © 2026 Nefelibata 的内置副本，含 LICENSE）
- **工程**：WebUI 配置页 16 项（含 `secret`、`template_list`、`_special: select_provider`、checkbox、slider）、
  `tests/`（68 条用例）、`pytest.ini`、`.github/workflows/ci.yml`、`ruff check` 全绿

### 说明

- 支持 AstrBot `>=4.14.0`；插件零第三方依赖，只用本体已带的 `aiohttp`
- 默认姿态是**只看不发**：`dry_run` 默认开，`/gh post` 需要显式 `--force`
- 有意不做的：自动点赞 / 打标签 / 合并 / 关闭，以及任何形式的任意命令执行
