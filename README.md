# astrbot_plugin_github_triage

把 GitHub 上的新 issue / PR 变成**一条能直接发布的评论草稿**，你过目、点头，它才发出去。

```
轮询关注仓库 → 抓正文与 diff → 建 worktree 让模型读代码 → 按内置 Skill 出草稿 → 你确认 → 发布
```

- **只看不发**：`dry_run` 默认开，发布要显式加 `--force`；
- **零第三方依赖**：只用 AstrBot 本体已带的 `aiohttp`，没有 `requirements.txt`；
- **不做越界的事**：写操作只有「评论」和「review」两种，不点批准、不合并、不打标签、不关 PR。

适用 AstrBot `>=4.14.0`（下限由 `context.cron_manager.add_basic_job` 与 `context.tool_loop_agent` 决定，
见 `metadata.yaml` 的 `astrbot_version`）。已在 AstrBot `4.29.0-beta.1` 上验证。

## 目录

- [适合谁](#适合谁)
- [安装](#安装)
- [快速开始](#快速开始)
- [指令](#指令)
- [配置](#配置)
- [工作流程](#工作流程)
- [核心设计](#核心设计)
- [内置 Skill](#内置-skill)
- [安全边界](#安全边界)
- [测试](#测试)
- [已知边界](#已知边界)

## 适合谁

- 手上有几个上游仓库要盯，issue / PR 一多就审不过来的人；
- 想让模型先做**一次带证据的初筛**（结论 + `文件:行号` + 分级），自己只做最后拍板的人；
- 不想让 bot 自动跑去别人仓库里发言的人。

反过来，如果你想要的是一句话就让 bot 自动评论，这个插件会让你多一步确认 —— 这是刻意的。

## 安装

在 WebUI 的「插件」页搜索 `astrbot_plugin_github_triage` 安装，或者：

```bash
cd AstrBot/data/plugins
git clone https://github.com/lingyun14beta/astrbot_plugin_github_triage
```

装完在「插件」页重载一次即可。

可选依赖：`gh_run_check` 要跑 `ruff` / `pytest`，需要**待审仓库自己**装好（见 `allow_local_commands` 与 `local_python`）。

## 快速开始

1. 在插件配置里填 **`github_token`**（建议只给 `public_repo`）→ **`repos`** 加一条 `owner/repo`
   → **`chat_provider_id`** 选一个模型。这三样是 `/gh t` 能跑起来的最小集合。
2. 在群里或私聊发 `/gh watch on`，把当前会话加进通知列表。
3. `/gh fetch` 立刻拉一次；或者打开 `poll_enabled` 让它按 `poll_cron` 自己跑。
4. 收到通知后 `/gh t <编号或链接>` 出草稿，看完 `/gh post <编号> --force` 发出去。

想让它读代码再下结论（推荐）：在 `local_paths` 里配上 `owner/repo` → 本地 clone 的绝对路径。

## 指令

| 指令 | 作用 |
| --- | --- |
| `/gh t <目标>` | 抓取并分析，产出评论草稿（只发回会话，不发 GitHub） |
| `/gh show <目标>` | 查看该条目已存的草稿 |
| `/gh list` | 列出所有待发草稿，末尾附已发布条数 |
| `/gh post <目标> [--force] [--review] [--changes]` | 发布草稿；`dry_run` 开着时必须加 `--force` |
| `/gh fetch` | 立刻轮询一次关注仓库 |
| `/gh watch on\|off\|status` | 订阅 / 取消 / 查看本会话的新条目通知 |
| `/gh config` | 查看生效配置：token 归属账号、API 剩余额度、本地仓库与工具开关（不显示 token 本身） |
| `/gh help` | 列出可用子指令 |

**目标写法**三种都认：`10200`（仅当只配了一个仓库）、`owner/repo#10200`、完整 GitHub 链接。

**`/gh post` 的 flag**：

- `--force`：越过 `dry_run`；
- `--review`：这条 PR 走正式 review（`POST /pulls/{n}/reviews`），覆盖 `review_mode`；
- `--changes`：走 review，且 event 用 `REQUEST_CHANGES`。

不发 `/gh post` 就不会有任何写操作；草稿一直留在插件 KV 里，`/gh show` 随时能翻出来。

## 配置

| 项 | 默认 | 说明 |
| --- | --- | --- |
| `enabled` | 开 | 总开关。关掉后所有 `/gh` 子指令（含 `/gh post`）一律不响应 |
| `github_token` | 空 | 空则匿名访问（60 次/小时，按出口 IP）；配了是 5000 次/小时。建议只给 `public_repo` |
| `repos` | 空 | 关注列表，每条含 `owner/repo` 与是否监听 issue / PR |
| `notify_targets` | 空 | 新条目推送到哪些会话。一般不用手填，用 `/gh watch on` |
| `poll_enabled` / `poll_cron` | 关 / `*/10 * * * *` | 定时轮询。关掉仍可用 `/gh fetch` 手动拉 |
| `chat_provider_id` | 空 | 分析用模型。留空时 `/gh t` 会提示先配 |
| `dry_run` | **开** | 开着时所有发布动作都要 `--force` |
| `review_mode` / `review_event` | 关 / `COMMENT` | PR 走正式 review 还是普通评论 |
| `include_sections` | `blocking, important, verdict` | 评论要哪些章节：阻断项 / 重要问题 / 结论 / 优点 / 次要问题 / 待确认问题 / 安全清单 / 测试覆盖 |
| `disclosure` | 内置 | 发布时自动补到末尾的 AI 声明，留空则不加 |
| `local_paths` | 空 | `owner/repo` → 本地 clone 路径（可选填远端名，默认 `origin`，本地是 fork 就填 `upstream`）。配了才能让模型读代码 |
| `enable_tools` | 开 | 让模型用工具（读文件 / 搜代码 / 看目录），仅 PR 且配了 `local_paths` 时生效 |
| `allow_local_commands` | **关** | 允许模型跑白名单命令（`ruff check` / `ruff format --check` / `pytest` / `git show` / `git log`） |
| `local_python` | 空 | 跑 `ruff` / `pytest` 用哪个解释器；留空用 AstrBot 自己的。填待审仓库虚拟环境的 python 路径更准 |
| `tool_max_steps` | 12 | 工具循环步数上限。调大更细，也更慢更贵 |
| `max_diff_chars` | 60000 | 送进模型的 diff 上限，超出按文件截断并在草稿里注明 |
| `max_items_per_poll` | 5 | 单次轮询最多处理多少条新条目 |
| `log_level` | info | 插件专属 logger 的级别（AstrBot ≥ 4.26.8 才有独立级别；更低版本上不会改全局级别） |

## 工作流程

1. **轮询**：按 cron 拉关注仓库的 open 条目。增量按 `updated_at`（排序也用 `updated`，否则刚被更新的
   老条目会排在窗口之外），命中未处理过的就推送到订阅会话。单仓库一轮最多取 50 条，**取满时不推进水位**，
   下一轮继续从同一窗口里取，避免窗口外的条目被永久跳过。
2. **抓取**：issue 取正文 + 评论 + 模板里未填的字段（GitHub 表单渲染出的 `_No response_`）；PR 取元数据 +
   改动文件 + 提交 + 完整 diff。改动文件超分页上限时会在材料里标注「只列出前 N 个」，免得模型以为列全了。
3. **自动扫描**：在 diff 新增行里扫高危信号（`except → pass`、捕获 `BaseException`、`shell=True`、
   `os.system`、`eval/exec`、关闭 TLS 校验、`noqa`/`type: ignore`、删断言），按改动文件名扫（改测试文件、
   改依赖清单、改 CI 配置）。这些只作为**候选**列给模型，写进评论前必须逐条核实。
4. **本地核查**（配了 `local_paths` 且是 PR）：见下节「PR head 怎么取」。
5. **产出**：按内置 Skill 的格式生成草稿，存进插件 KV。
6. **确认后才发**：`/gh post` 先过一遍**发布前自检** —— 正文为空、疑似 token / 密钥 → 直接拦下；
   缺 `文件:行号`、含绝对措辞（"完全"、"所有"…）→ 提醒但不拦。通过才真的发出去。

## 核心设计

**默认姿态是只看不发。** `/gh post` 不是"再确认一下"，是"明确下令"：`dry_run` 默认开，加 `--force` 才越得过去。

**PR head 怎么取。** 本地核查不直接在你的工作区里 checkout，而是：

- 用 `git worktree` 而不是 clone —— 省磁盘、快，共享同一个对象库；
- PR head 取到**本 PR 专属的私有 ref**（`refs/gh-triage/pr-N`）再检出成 detached HEAD。
  不用 `FETCH_HEAD`：那是仓库级共享状态，并发下会被另一次 fetch 插队，可能检出成另一条 PR 的代码且不报错；
- worktree 落在仓库**外面**（同级 `.gh-triage-worktrees/`），不污染工作区、不动你的分支；
- 退出时 `worktree remove --force` + `prune` + 删私有 ref，用完不留痕；
- 配置的远端取不到 PR ref 时（本地 clone 是自己的 fork 的常见情形）自动退回 `upstream` 再试一次。

**模型能看多远。** 工具只在 worktree 里活动，读文件 / 搜代码 / 列目录都走同一条路径校验：`resolve()` 之后
必须落在检出目录内，越界直接拒绝。行数、命中数、文件大小、输出长度都有上限，避免把上下文撑爆。

**为什么草稿必须带 `文件:行号`。** 评论里的每条断言都要能指回代码。发布前自检会提醒缺位置引用的 PR 评论，
含绝对措辞的也会被点出来 —— 静态审查加一次验证，最容易被打脸的就是绝对断言。

## 内置 Skill

插件自带的说明书，跟着插件走（**不做** `data/skills` 回退，也不读你本地的 skill）：

- `skills/gh-triage/SKILL.md` —— 流程说明书，仓库无关：怎么判类型、怎么找根因、怎么用 worktree、哪些事不许做；
- `skills/gh-issue-format/SKILL.md` —— issue 评论格式与建议标签口径（bug / feature 两套模板）；
- `skills/gh-code-review/` —— PR 审查清单与输出格式（🔴🟡🟢 分级、按语言/SDK 挑参考文件）。
  这是 [NefelibataBIGR/code-review-skill-codex](https://github.com/NefelibataBIGR/code-review-skill-codex)
  的内置副本（MIT © 2026 Nefelibata，`LICENSE` 原样随附），为适配 AstrBot 做了本地化改动：
  目录与 frontmatter 的 `name` 改为 `gh-code-review`，`description` 补了中文说明与「PR 评论的输出格式以本 skill 为准」。

`gh-triage` 缺失时插件会在启动日志里警告，`/gh t` 也会直接说明缺的是哪份，而不是报一句模型调用失败。

## 安全边界

- **默认不发**：`dry_run` 默认开；`/gh post` 要显式加 `--force`；
- **不点批准、不合并、不打标签、不关 PR**：写操作只有评论与 review 两种；
- **非协作者的 `REQUEST_CHANGES` 不计入合并门槛**，需要真的拦住请用协作者账号；
- **工具只能看见 worktree**：所有路径 `resolve()` 后必须落在检出目录内，越界直接拒绝；
- **没有 shell**：`gh_run_check` 是白名单 argv，`shell=False`，带超时（180s）与输出上限；
- **`allow_local_commands` 默认关**：`pytest` 会执行**待审 PR 的测试代码**，只在你信任的仓库上开；
- **会往本地仓库写三样东西**：`git fetch` 连带的 `FETCH_HEAD`、私有 ref `refs/gh-triage/pr-N`（退出即删）、
  仓库同级的 `.gh-triage-worktrees/`（退出即删）。不改你的分支、不动你的工作区；
- **token 不落日志**：配置项标了 `secret`（AstrBot ≥ 4.28.0 会渲染成密码框，配置文件里仍是明文）；
  发布前自检还会拦掉正文里疑似泄露的凭证；
- **开关是硬的**：`enabled=false` 时全部 `/gh` 子指令一律不响应，`/gh post` 这种写操作也在内。

## 测试

```bash
python -m pytest -q          # 75 条用例
python -m ruff check .       # 代码检查
python -m ruff format --check .
```

用例分两层：`gh/` 里的纯逻辑（解析、状态、发布自检、路径边界、worktree 生命周期）不依赖 AstrBot，
其余用例需要 AstrBot 运行时（`astrbot.api` 与 core 的指令装饰器），找不到时整组跳过。
本地跑需要 AstrBot 的依赖时，把根目录指过去：

```bash
ASTRBOT_ROOT=/path/to/AstrBot python -m pytest -q
```

## 已知边界

- 只处理 **issue 与 PR**，不碰 release / discussion / 安全公告；
- **worktree 里只有 PR head 那一份快照**：模型看到的是 PR 的代码，读不到你主分支上的其他文件，
  也读不到 PR 没碰过的仓库文档（比如 `AGENTS.md`）。跨分支的对比要在材料里说清楚；
- issue 的「根因」需要本地阅读代码，而 issue 没有 PR head 可检出 —— 这种场景走静态核查，
  评论里的行号建议带版本锚点；
- 轮询增量按 `updated_at`，被编辑的老条目会重新出现一次（靠已处理表挡，上限 500 条）；
- 无 ETag 条件请求；429 / 5xx 只退避一次重试，仍失败的留到下一轮；
- 轮询靠「全部成功 + 窗口没取满才推进水位，配合已处理表」避免漏报，代价是某仓库长期 403、
  或单仓库一轮超过 50 条更新时会反复重扫同一窗口（已处理表挡重复通知，不会重复打扰你）；
- 只做单页拉取：单个 PR 改动文件超过 100 个（GitHub 分页上限）时，列出的文件是前 100 个，
  材料里会标注，diff 本身仍是完整的。
