# astrbot_plugin_github_triage

[![CI](https://github.com/lingyun14beta/astrbot_plugin_github_triage/actions/workflows/ci.yml/badge.svg)](https://github.com/lingyun14beta/astrbot_plugin_github_triage/actions/workflows/ci.yml)

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
- [让它读代码再下结论（推荐）](#让它读代码再下结论推荐)
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
2. 把通知会话填进配置项 **`notify_targets`**：先到目标群里发一次 `/sid`，把它输出的 `UMO`
   那一行复制进来 —— 格式是 `platform_id:MessageType:session_id`，例如 `aiocqhttp:GroupMessage:123456`
   （连 `/sid` 输出的「」一起复制也行，插件会剥掉）。
   要推给多个会话就加多条；写法不对的条目会被跳过，并在日志里点名。
3. `/gh fetch` 立刻拉一次；或者打开 `poll_enabled` 让它按 `poll_cron` 自己跑。
4. 收到通知后 `/gh t <编号或链接>` 出草稿，看完 `/gh post <编号> --force` 发出去。

到这里 `/gh t` 已经能用了，但它只看得到 diff。想让它读代码再下结论（推荐）：见下一节。

## 让它读代码再下结论（推荐）

不配 `local_paths` 时，插件只把 issue 正文和 PR 的 diff 交给模型 —— 模型看不到改动所在函数的完整逻辑，
也没法搜调用方，结论只能建立在 diff 那几行上。配上本地仓库后，插件会把 PR head 检出成一个独立
worktree，让模型用 `gh_read_file` / `gh_search_code` / `gh_list_dir` 自己去读。

### 一步：准备一份 clone

随便 clone 一份 **上游仓库**（不用是你的 fork）：

```bash
mkdir D:\code && cd D:\code
git clone https://github.com/AstrBotDevs/AstrBot
```

**必须是完整的 clone，路径填到根目录** —— 也就是这个目录下能直接看到 `.git`：

```
D:\code\AstrBot\          ← 填这个
D:\code\AstrBot\.git      ← 有这个才算
D:\code\AstrBot\astrbot\  ← 不要填这种子目录
```

填错只会退化成静态审查（会话里会回一句「本地还原失败，退回静态审查」），不会影响其他功能。

### 二步：在配置里映射

插件配置 → **`local_paths`** → 添加一条：

| 字段 | 填什么 | 例子 |
| --- | --- | --- |
| `repo` | 关注的仓库，`owner/repo` | `AstrBotDevs/AstrBot` |
| `path` | 上一步 clone 的**根目录**绝对路径 | `D:\code\AstrBot` |
| `remote` | 从哪个远端取 PR head，默认 `origin` | `origin` |

几个容易踩的点：

- **`remote` 填哪个**：如果这份 clone 是你自己的 fork，`origin` 指向 fork，那 PR 的 head 通常只在
  上游上 —— 这时要么把 `remote` 填成 `upstream`（前提是你给这个 clone 加过 `upstream` 远端），
  要么干脆按上一步直接 clone 上游；
- **路径写法**：绝对路径或 `~` 都行，反斜杠也可以；但**不认** `%USERPROFILE%`、`$env:...` 这类
  环境变量，填了会被当成字面路径；
- **父目录要可写**：worktree 建在这个 clone 的**父目录**下的 `.gh-triage-worktrees/`，
  用完自动删除。比如 `path` 填 `D:\code\AstrBot`，过程目录就是
  `D:\code\.gh-triage-worktrees\AstrBotDevs__AstrBot-10328\`；
- **`repo` 大小写不敏感**：插件会把 `repos` 与 `local_paths` 两边的仓库名都归一成小写再比对，
  所以 `AstrBotDevs/AstrBot` 配在 `repos`、`astrbotdevs/astrbot` 配在 `local_paths` 也能对上
  （GitHub 侧本来就不区分大小写，抓取与显示不受影响）；
- **多个仓库**就加多条，每条各配自己的 clone；
- **想让模型跑检查命令**（可选）：再打开 `allow_local_commands`，并在 `local_python` 填这份待审仓库
  自己虚拟环境的 python 路径 —— 检查命令要用它来跑，留空会用 AstrBot 的解释器，`ruff` / `pytest`
  常常因为缺少依赖报一堆 import 错。注意 `pytest` 会执行待审 PR 的测试代码，只在你信任的仓库上开。

### 三步：验证配通了

配完在聊天里发：

1. `/gh config` —— 「本地仓库」那一行应该显示 `AstrBotDevs/AstrBot→D:\code\AstrBot`，
   工具应该是「开」；
2. `/gh t <一个 PR 编号>` —— 抓取后应该多一条消息：`已把改动还原到独立 worktree，模型可用工具查看代码 …`。
   出现这条就说明 worktree 建起来了、模型拿到了工具。

如果只看到「PR 代码还原失败，这次退回静态审查」，那句话会带**你填的路径**和**原始错误**，
按原始错误分流：

| 原始错误 | 往哪查 |
| --- | --- |
| `不是 git 仓库：<路径>` | 路径填错了一层（要填 clone 根目录），或者跑插件的机器没装 `git` |
| `git fetch 失败：<git 输出>` | `local_paths` 里配的远端名不对（默认 `origin`），或机器上网络不通 |
| `git worktree add 失败：<git 输出>` | 父目录不可写、磁盘满，或这份 clone 是浅克隆缺对象 |

> 这条消息只在「配了 `local_paths` + 审的是 PR + `enable_tools` 开着」时才会出现 —— 同时满足这三条，
> 就说明配置本身是生效的，只是还原那一步失败了。

> 注意 worktree 里**只有 PR head 那一份快照**：模型读到的是这个 PR 的代码，读不到你主分支上的其他文件，
> 也读不到 PR 没碰过的仓库文档（比如 `AGENTS.md`）。

## 指令

| 指令 | 作用 |
| --- | --- |
| `/gh t <目标>` | 抓取并分析，产出评论草稿（只发回会话，不发 GitHub） |
| `/gh show <目标>` | 查看该条目已存的草稿 |
| `/gh list` | 列出所有待发草稿，末尾附已发布条数 |
| `/gh post <目标> [--force] [--review] [--changes]` | 发布草稿；`dry_run` 开着时必须加 `--force` |
| `/gh fetch` | 立刻轮询一次关注仓库 |
| `/gh config` | 查看生效配置：token 归属账号、API 剩余额度、通知会话、本地仓库与工具开关（不显示 token 本身） |
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
| `notify_targets` | 空 | 新条目推送到哪些会话。填 AstrBot 的 UMO（`platform_id:MessageType:session_id`），在目标会话发 `/sid` 即可拿到；写法不对的条目会被跳过并在日志里点名 |
| `poll_enabled` / `poll_cron` | 关 / `*/10 * * * *` | 定时轮询。关掉仍可用 `/gh fetch` 手动拉 |
| `chat_provider_id` | 空 | 分析用模型。留空时 `/gh t` 会提示先配 |
| `dry_run` | **开** | 开着时所有发布动作都要 `--force` |
| `review_mode` / `review_event` | 关 / `COMMENT` | PR 走正式 review 还是普通评论 |
| `include_sections` | `blocking, important, verdict` | 评论要哪些章节：阻断项 / 重要问题 / 结论 / 优点 / 次要问题 / 待确认问题 / 安全清单 / 测试覆盖 |
| `disclosure` | 内置 | 发布时自动补到末尾的 AI 声明，留空则不加 |
| `local_paths` | 空 | `owner/repo` → 本地 clone 路径（可选填远端名，默认 `origin`，本地是 fork 就填 `upstream`）。**要填 clone 的根目录**（该目录下能直接看到 `.git`），填子目录识别不到仓库、会退回静态审查；绝对路径或 `~` 都行，但不认 `%USERPROFILE%` 这类环境变量。配了才能让模型读代码 |
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
- worktree 落在仓库**外面**（同级 `.gh-triage-worktrees/`，即你那个 clone 的父目录下，所以父目录要可写），不污染工作区、不动你的分支；
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
python -m pytest -q          # 104 条用例
python -m ruff check .       # 代码检查
python -m ruff format --check .
```

CI（`.github/workflows/ci.yml`）把 ruff 钉在 `0.15.22` —— 与 AstrBot 本体的 `.pre-commit-config.yaml` 一致；
本地也建议用同一个版本，否则新版本启用的新规则会让 CI 先红。

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

## 许可

本插件以 [MIT 协议](LICENSE) 发布：

```
Copyright (c) 2026 lingyun14beta
```

第三方内容按各自的协议随附，转载或再分发时请一并保留：

- `skills/gh-code-review/`：取自 [code-review-skill-codex](https://github.com/NefelibataBIGR/code-review-skill-codex)
  的内置副本，MIT © 2026 Nefelibata，原协议文本在 `skills/gh-code-review/LICENSE`；
  本副本为适配 AstrBot 做过本地化改动（目录名、frontmatter 的 `name` 与 `description`）。
