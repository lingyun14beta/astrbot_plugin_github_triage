"""GitHub 审阅助手：把新 issue / PR 变成能直接发的评论草稿。

流程：轮询发现新条目 → 通知订阅会话 → ``/gh t <编号>`` 抓取并让模型出草稿 →
``/gh post <编号>`` 人工确认后发布（``dry_run`` 默认开启，发布要显式放行）。

装饰器（指令、定时任务）全部留在本模块：AstrBot 用 ``star_map[handler.handler_module_path]``
索引元数据，放到子模块会让钩子静默失效。
"""

from __future__ import annotations

import logging
import re
import time
from functools import wraps
from typing import Any

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.star import Context, Star, StarTools

from .gh import analyze, fetch, publish, skills
from .gh.client import GitHubClient, GitHubError
from .gh.skills import SkillMissing
from .gh.state import StateStore
from .gh.workspace import PullWorkspace, WorkspaceError

PLUGIN_NAME = "astrbot_plugin_github_triage"
LOG = "[gh-triage]"
POLL_JOB_NAME = "github-triage-poll"
# 每个仓库单轮抓取的条数上限：再多的条目靠「不推进水位 + 下一轮重扫」补齐
POLL_PAGE_SIZE = 50
# UMO 的第二段，AstrBot 的 MessageType 只有这三个取值
MESSAGE_TYPES = ("GroupMessage", "FriendMessage", "OtherMessage")

URL_RE = re.compile(r"github\.com/([^/\s]+)/([^/\s]+)/(?:pull|issues)/(\d+)")
# 两组都必须是捕获组：解析处统一按 (repo 前半, repo 后半, 编号) 取 group(1..3)
SHORT_RE = re.compile(r"^([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)#(\d+)$")


def dedupe_running(func):
    """同一条目标还在处理中时直接回话，避免连点开出多个 worktree 与多次模型调用。"""

    @wraps(func)
    async def wrapper(self, event, *args, **kwargs):
        target = kwargs.get("target", args[0] if args else "")
        key = str(target or "").strip().lower()
        running = getattr(self, "_running_targets", None)
        if running is None:
            running = set()
            self._running_targets = running
        if key and key in running:
            yield event.plain_result(f"{target} 正在处理中，等它出结果再试。")
            return
        if key:
            running.add(key)
        try:
            async for result in func(self, event, *args, **kwargs):
                yield result
        finally:
            running.discard(key)

    return wrapper


def requires_enabled(func):
    """插件被配置关闭时统一回一句，不进入业务逻辑 —— 写操作（发布）也一并挡住。

    用 ``functools.wraps`` 保留原签名：AstrBot 用 ``inspect.signature`` 解析指令参数
    （见 ``astrbot/core/star/filter/command.py``），签名必须原样可见。
    """

    @wraps(func)
    async def wrapper(self, event, *args, **kwargs):
        if not getattr(self, "enabled", True):
            yield event.plain_result("插件已在配置中关闭（enabled），/gh 指令不响应。")
            return
        async for result in func(self, event, *args, **kwargs):
            yield result

    return wrapper


class GithubTriagePlugin(Star):
    """插件入口。"""

    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context, config)
        self.config = config

        self.enabled = bool(config.get("enabled", True))
        self.client = GitHubClient(str(config.get("github_token", "") or ""))
        self.state = StateStore(self)
        self.data_dir = StarTools.get_data_dir(PLUGIN_NAME)
        self.repos = self._read_repos()
        self.local_paths = self._read_local_paths()
        self.local_remotes = self._read_local_remotes()

        self._polling = False
        self._job_id: str | None = None

        level_name = str(config.get("log_level", "info")).upper()
        if getattr(logger, "name", "").startswith("astrbot.plugin."):
            logger.setLevel(getattr(logging, level_name, logging.INFO))

        # list_skills 只认带 SKILL.md 的目录，比 .is_dir() 更严：否则 SKILL.md 被删
        # 时这里不报警，等到 /gh t 才炸在 read_skill 上
        missing = [
            name for name in (skills.ROOT_SKILL,) if name not in skills.list_skills()
        ]
        if missing:
            logger.warning(
                f"{LOG} 内置 Skill 缺失：{missing}；请检查插件目录是否完整。"
            )

    # ---------- 生命周期 ----------

    async def initialize(self) -> None:
        """按配置注册轮询作业。"""
        if not (self.enabled and bool(self.config.get("poll_enabled", False))):
            return
        try:
            job = await self.context.cron_manager.add_basic_job(
                name=POLL_JOB_NAME,
                cron_expression=str(self.config.get("poll_cron", "*/10 * * * *")),
                handler=self._poll_job,
                description="GitHub 审阅助手：轮询关注仓库的新 issue / PR",
                payload={"reason": "cron"},
            )
            self._job_id = job.job_id
            logger.info(
                f"{LOG} 已注册轮询作业 {self._job_id}（{self.config.get('poll_cron')}）"
            )
        except Exception as exc:
            logger.error(f"{LOG} 注册轮询作业失败：{exc}")

    async def terminate(self) -> None:
        if self._job_id:
            try:
                await self.context.cron_manager.delete_job(self._job_id)
            except Exception as exc:
                logger.warning(f"{LOG} 删除轮询作业失败：{exc}")
            self._job_id = None

        await self.client.close()

    # ---------- 配置读取 ----------

    @staticmethod
    def _repo_key(raw: Any) -> str:
        """把配置里的仓库名归一成查表用的键。

        GitHub 的 owner/repo 大小写不敏感（同一个仓库怎么拼都是它），而配置里
        随手写成 `AstrBotDevs/AstrBot` 或 `astrbotdevs/astrbot` 都很常见；
        标题与 API 调用不受影响（GitHub 侧同样不敏感），这里只统一键的写法。

        Args:
            raw: 配置里填的仓库名。

        Returns:
            小写、去掉两端空格与斜杠的 `owner/repo`；写法不合规时返回空串。
        """
        repo = str(raw or "").strip().strip("/").lower()
        return repo if repo.count("/") == 1 else ""

    def _read_repos(self) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        for raw in self.config.get("repos") or []:
            if not isinstance(raw, dict):
                continue
            repo = self._repo_key(raw.get("repo", ""))
            if not repo:
                continue
            items.append(
                {
                    "repo": repo,
                    "issues": bool(raw.get("watch_issues", True)),
                    "prs": bool(raw.get("watch_prs", True)),
                }
            )
        return items

    def _read_local_paths(self) -> dict[str, str]:
        """本地 clone 映射：键与 `_read_repos` 用同一套归一化，大小写不同也能对上。"""
        mapping: dict[str, str] = {}
        for raw in self.config.get("local_paths") or []:
            if not isinstance(raw, dict):
                continue
            repo = self._repo_key(raw.get("repo", ""))
            path = str(raw.get("path") or "").strip()
            if repo and path:
                mapping[repo] = path
        return mapping

    def _read_local_remotes(self) -> dict[str, str]:
        """可选的远端名：本地是 fork 时用 upstream 取 PR head。"""
        mapping: dict[str, str] = {}
        for raw in self.config.get("local_paths") or []:
            if not isinstance(raw, dict):
                continue
            repo = self._repo_key(raw.get("repo", ""))
            remote = str(raw.get("remote") or "").strip()
            if repo and remote:
                mapping[repo] = remote
        return mapping

    def _chat_provider(self) -> str:
        return str(self.config.get("chat_provider_id", "") or "").strip()

    def _disclosure(self) -> str:
        value = self.config.get("disclosure", analyze.DEFAULT_DISCLOSURE)
        return str(value if value is not None else "").strip()

    def _review_mode(self) -> tuple[bool, str]:
        as_review = bool(self.config.get("review_mode", False))
        event = str(self.config.get("review_event", "COMMENT") or "COMMENT").upper()
        return as_review, event if event in (
            "COMMENT",
            "REQUEST_CHANGES",
            "APPROVE",
        ) else "COMMENT"

    # ---------- 工具方法 ----------

    @staticmethod
    def _parse_umo(raw: str) -> tuple[str, str, str] | None:
        """解析 unified_msg_origin，格式 ``platform_id:MessageType:session_id``。

        Args:
            raw: 待解析的会话标识。

        Returns:
            ``(platform_id, message_type, session_id)``；写法不合规时返回 None。
        """
        parts = str(raw or "").strip().split(":", 2)
        if len(parts) != 3 or not all(parts):
            return None
        if parts[1] not in MESSAGE_TYPES:
            return None
        return parts[0], parts[1], parts[2]

    @staticmethod
    def _clean_target(raw: Any) -> str:
        """清掉从聊天里复制 UMO 时容易带上的成对包裹（``/sid`` 输出是「…」）。"""
        text = str(raw or "").strip()
        for left, right in (("「", "」"), ("“", "”"), ("'", "'"), ('"', '"')):
            if len(text) >= 2 and text.startswith(left) and text.endswith(right):
                return text[1:-1].strip()
        return text

    @staticmethod
    def _workspace_help(local: str, exc: Exception) -> str:
        """worktree 建不起来时给用户的提示：先说清影响，再给可核对的排查方向。"""
        return (
            f"PR 代码还原失败，这次退回静态审查（只看 diff）：{exc}\n"
            f"本地路径：{local}\n"
            "排查：① 这个路径要填 clone 的根目录（目录下能直接看到 .git）；"
            "② 跑插件的机器装了 git 吗（提示「不是 git 仓库」时优先看这两条）；"
            "③ local_paths 里配的远端名在不在（默认 origin，"
            "提示「git fetch 失败」时优先看这条）。"
        )

    def _notify_targets(self) -> list[str]:
        """取配置里的通知会话，顺手剔掉空项与写法不对的项。"""
        configured = self.config.get("notify_targets") or []
        if not isinstance(configured, list):
            return []
        targets = []
        for raw in configured:
            if raw is None:
                continue
            item = self._clean_target(raw)
            if not item:
                continue
            if self._parse_umo(item) is None:
                logger.warning(
                    f"{LOG} notify_targets 里的这一项不是合法的 UMO，已跳过：{item}"
                    "（格式 platform_id:MessageType:session_id，可在目标会话发 /sid 获取）"
                )
                continue
            targets.append(item)
        return targets

    @staticmethod
    def _format_targets(targets: list[str]) -> str:
        """把通知会话渲染成可读列表。"""
        lines = []
        for item in targets:
            parsed = GithubTriagePlugin._parse_umo(item)
            kind = "群聊" if parsed[1] == "GroupMessage" else "私聊"
            lines.append(f"- {parsed[0]}｜{kind}｜{parsed[2]}")
        return "\n".join(lines)

    def _resolve_target(self, target: str) -> tuple[str, int]:
        """把 URL / owner/repo#n / 纯编号解析成 ``(repo, number)``。

        Args:
            target: 用户给的目标，三种写法都认。

        Returns:
            ``(owner/repo, 编号)``。

        Raises:
            ValueError: 写法无法识别，或纯编号但配置了多个仓库。
        """
        raw = str(target or "").strip()
        match = URL_RE.search(raw) or SHORT_RE.match(raw)
        if match:
            return f"{match.group(1)}/{match.group(2)}", int(match.group(3))
        if raw.isdigit():
            if len(self.repos) == 1:
                return self.repos[0]["repo"], int(raw)
            raise ValueError("配置了多个仓库，请用 owner/repo#编号 或完整链接指定。")
        raise ValueError("无法识别目标，请用编号、owner/repo#编号 或 GitHub 链接。")

    async def _kind(self, repo: str, number: int) -> tuple[str, dict[str, Any]]:
        """判类型，顺手把这次调用拿到的 issue 对象带回去，少烧一次配额。"""
        issue = await self.client.get_issue(repo, number)
        return ("pr" if issue.get("pull_request") else "issue"), issue

    async def _build_digest(
        self, repo: str, number: int, kind: str, issue: dict[str, Any] | None = None
    ) -> tuple[dict[str, Any], str]:
        if kind == "pr":
            digest = await fetch.pr_digest(
                self.client,
                repo,
                number,
                max_diff_chars=int(self.config.get("max_diff_chars", 60000)),
            )
        else:
            if issue is None:
                issue = await self.client.get_issue(repo, number)
            comments = await self.client.get_issue_comments(repo, number)
            digest = fetch.issue_digest(issue, comments)
        digest["repo"] = repo
        return digest, fetch.render_markdown(digest)

    async def _poll_job(self, reason: str = "cron") -> None:
        await self.poll_once(reason=reason)

    async def poll_once(self, *, reason: str = "manual") -> list[dict[str, Any]]:
        """拉一次新条目；发现新的就通知订阅会话。返回本轮新条目列表。"""
        if self._polling:
            logger.info(f"{LOG} 上一轮轮询未结束，跳过本次（{reason}）")
            return []
        if not self.repos:
            return []

        self._polling = True
        found: list[dict[str, Any]] = []
        failed = 0
        window_full = False
        limit = int(self.config.get("max_items_per_poll", 5))
        since_ts = await self.state.last_poll_at()
        since = (
            time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(since_ts))
            if since_ts
            else None
        )

        try:
            for entry in self.repos:
                repo = entry["repo"]
                try:
                    # 多要一条：用来判断窗口是否被 per_page 截断
                    items = await self.client.list_issues(
                        repo, since=since, per_page=POLL_PAGE_SIZE + 1
                    )
                except GitHubError as exc:
                    failed += 1
                    logger.warning(f"{LOG} 轮询 {repo} 失败：{exc} {exc.hint}")
                    continue
                if len(items) > POLL_PAGE_SIZE:
                    window_full = True

                for item in items[:POLL_PAGE_SIZE]:
                    is_pr = bool(item.get("pull_request"))
                    if is_pr and not entry["prs"]:
                        continue
                    if not is_pr and not entry["issues"]:
                        continue
                    number = int(item.get("number") or 0)
                    if not number or await self.state.is_seen(repo, number):
                        continue
                    found.append(
                        {
                            "repo": repo,
                            "number": number,
                            "kind": "pr" if is_pr else "issue",
                            "title": str(item.get("title") or ""),
                            "author": str((item.get("user") or {}).get("login") or ""),
                        }
                    )
                    await self.state.mark_seen(repo, number)
                    if len(found) >= limit:
                        break
                if len(found) >= limit:
                    break

            if failed:
                logger.warning(f"{LOG} {failed} 个仓库本轮抓取失败，水位保持不变")
            elif window_full:
                # 窗口被截断时不能把水位推到 now：窗口外的条目还排在水位之前，
                # 推过去就再也不会被返回。等窗口不再截断的那一轮再推进。
                logger.warning(
                    f"{LOG} 本轮窗口已满（每仓库 {POLL_PAGE_SIZE} 条），水位保持不变，"
                    "下一轮继续从同一窗口取。"
                )
            else:
                await self.state.set_last_poll_at(time.time())

            if found:
                await self._notify_new_items(found)
            logger.info(f"{LOG} 轮询完成（{reason}）：新条目 {len(found)} 条")
        finally:
            self._polling = False
        return found

    async def _notify_new_items(self, items: list[dict[str, Any]]) -> None:
        targets = self._notify_targets()
        if not targets:
            logger.info(f"{LOG} 有新条目但未配通知会话（notify_targets），跳过通知。")
            return
        logger.info(f"{LOG} 推送到 {len(targets)} 个会话")

        lines = ["【GitHub 更新】"]
        for item in items:
            label = "PR" if item["kind"] == "pr" else "issue"
            lines.append(
                f"#{item['number']} [{label}] {item['title']}（@{item['author']}）"
            )
            lines.append(f"  /gh t {item['repo']}#{item['number']} 出草稿")
        chain = MessageChain().message("\n".join(lines))
        for umo in targets:
            try:
                await self.context.send_message(umo, chain)
            except Exception as exc:
                logger.warning(f"{LOG} 推送到 {umo} 失败：{exc}")

    # ---------- 指令 ----------

    @filter.command_group("gh")
    def gh_group(self) -> None:
        """GitHub 审阅助手：把新 issue / PR 变成能直接发的评论草稿。"""

    @gh_group.command("help")
    @requires_enabled
    async def gh_help(self, event: AstrMessageEvent) -> None:
        """列出可用子指令。"""
        yield event.plain_result(
            "GitHub 审阅助手\n"
            "/gh t <编号>     抓取并分析，产出评论草稿（不发）\n"
            "/gh show <编号>  查看已存草稿\n"
            "/gh list         列出待发草稿\n"
            "/gh post <编号>  发布草稿（dry_run 开启时需加 --force）\n"
            "/gh fetch        立刻轮询一次\n"
            "/gh config       查看生效配置\n"
            "通知会话在插件配置的 notify_targets 里填：先到目标会话发 /sid，把输出的 UMO 抄进去"
        )

    @gh_group.command("t")
    @requires_enabled
    @dedupe_running
    async def gh_t(self, event: AstrMessageEvent, target: str) -> None:
        """抓取一条 issue / PR，让模型产出评论草稿。"""
        if not self.enabled:
            return
        provider = self._chat_provider()
        if not provider:
            yield event.plain_result(
                "未配置「分析用模型」（chat_provider_id），请先在插件配置里选择。"
            )
            return

        try:
            repo, number = self._resolve_target(target)
        except ValueError as exc:
            yield event.plain_result(str(exc))
            return

        yield event.plain_result(f"抓取 {repo}#{number} …")
        try:
            kind, issue = await self._kind(repo, number)
            digest, markdown = await self._build_digest(repo, number, kind, issue)
        except GitHubError as exc:
            yield event.plain_result(f"抓取失败：{exc} {exc.hint}")
            return

        local = self.local_paths.get(repo)
        use_tools = (
            bool(local) and kind == "pr" and bool(self.config.get("enable_tools", True))
        )

        async def _draft(root: str | None) -> str:
            return await analyze.make_draft(
                self.context,
                event=event,
                chat_provider_id=provider,
                kind=kind,
                digest_markdown=markdown,
                repo=repo,
                number=number,
                changed_paths=[f["filename"] for f in digest.get("files", [])],
                include_sections=[
                    str(s) for s in (self.config.get("include_sections") or [])
                ],
                diff_truncated=bool(digest.get("diff_truncated")),
                signals=list(digest.get("signals") or []),
                workspace_root=root,
                allow_commands=bool(self.config.get("allow_local_commands", False)),
                max_steps=int(self.config.get("tool_max_steps", 12)),
                local_python=str(self.config.get("local_python") or ""),
            )

        try:
            if use_tools:
                try:
                    async with PullWorkspace(
                        local,
                        repo,
                        number,
                        remote=self.local_remotes.get(repo, "origin"),
                    ) as ws:
                        yield event.plain_result(
                            "已把改动还原到独立 worktree，模型可用工具查看代码 …"
                        )
                        draft = await _draft(str(ws.root))
                except WorkspaceError as exc:
                    logger.warning(f"{LOG} 本地还原失败，退回静态审查：{exc}")
                    yield event.plain_result(self._workspace_help(local, exc))
                    draft = await _draft(None)
            else:
                draft = await _draft(None)
        except SkillMissing as exc:
            # 说明书跟着插件走，缺了就没法产出格式正确的草稿，直说比报模型错误有用
            logger.error(f"{LOG} 内置 Skill 缺失：{exc}")
            yield event.plain_result(
                f"内置 Skill 缺失，无法产出草稿：{exc}\n请重新安装插件或恢复 skills/ 目录。"
            )
            return
        except Exception as exc:
            logger.error(f"{LOG} 生成草稿失败：{exc}")
            yield event.plain_result(f"模型调用失败：{exc}")
            return

        await self.state.save_draft(
            repo, number, kind=kind, title=str(digest.get("title") or ""), body=draft
        )
        await self.state.mark_seen(repo, number)

        yield event.plain_result(
            f"GitHub 审阅助手｜{repo}#{number}（{'PR' if kind == 'pr' else 'issue'}）\n"
            f"—— 草稿 ——\n{draft}\n"
            f"—— 发布：/gh post {repo}#{number} ——"
        )

    @gh_group.command("show")
    @requires_enabled
    async def gh_show(self, event: AstrMessageEvent, target: str) -> None:
        """查看已存草稿。"""
        try:
            repo, number = self._resolve_target(target)
        except ValueError as exc:
            yield event.plain_result(str(exc))
            return
        draft = await self.state.get_draft(repo, number)
        if not draft:
            yield event.plain_result(f"{repo}#{number} 没有待发草稿。")
            return
        yield event.plain_result(
            f"{repo}#{number}（{draft.get('created_at')}）\n\n{draft.get('body')}"
        )

    @gh_group.command("list")
    @requires_enabled
    async def gh_list(self, event: AstrMessageEvent) -> None:
        """列出所有待发草稿。"""
        drafts = await self.state.list_drafts()
        if not drafts:
            yield event.plain_result("没有待发草稿。")
            return
        lines = [
            f"- {d['repo']}#{d['number']} [{d['kind']}] {d.get('title', '')[:40]}（{d.get('created_at')}）"
            for d in drafts
        ]
        published = await self.state.published()
        tail = ""
        if published:
            tail = f"\n（已发布过 {len(published)} 条）"
        yield event.plain_result("待发草稿：\n" + "\n".join(lines) + tail)

    @gh_group.command("post")
    @requires_enabled
    async def gh_post(
        self, event: AstrMessageEvent, target: str, flags: str = ""
    ) -> None:
        """发布草稿到 GitHub。flags 支持 --force / --review / --changes。"""
        raw_flags = str(flags or "")
        if bool(self.config.get("dry_run", True)) and "--force" not in raw_flags:
            yield event.plain_result(
                "dry_run 开启：加 --force 才真的发布（/gh post <编号> --force）。"
            )
            return

        try:
            repo, number = self._resolve_target(target)
        except ValueError as exc:
            yield event.plain_result(str(exc))
            return

        draft = await self.state.get_draft(repo, number)
        if not draft:
            yield event.plain_result(f"{repo}#{number} 没有待发草稿。")
            return

        as_review, review_event = self._review_mode()
        if "--review" in raw_flags:
            as_review = True
        if "--changes" in raw_flags:
            as_review = True
            review_event = "REQUEST_CHANGES"

        body = publish.ensure_disclosure(
            str(draft.get("body") or ""), self._disclosure()
        )
        blocks, warnings = publish.self_check(
            body, kind=str(draft.get("kind") or "issue")
        )
        if blocks:
            yield event.plain_result(
                "发布前自检未通过，已拦下：\n- " + "\n- ".join(blocks)
            )
            return
        try:
            result = await publish.publish(
                self.client,
                repo=repo,
                number=number,
                body=body,
                kind=str(draft.get("kind") or "issue"),
                as_review=as_review,
                event=review_event,
            )
        except GitHubError as exc:
            yield event.plain_result(f"发布失败：{exc} {exc.hint}")
            return

        await self.state.pop_draft(repo, number)
        url = str(result.get("html_url") or result.get("url") or "")
        mode = f"review({review_event})" if as_review else "评论"
        await self.state.record_published(
            repo, number, url=url, kind=str(draft.get("kind") or "issue"), mode=mode
        )
        if warnings:
            mode = f"{mode}；提醒：{'、'.join(warnings)}"
        yield event.plain_result(f"已发布（{mode}）：{repo}#{number}\n{url}")

    @gh_group.command("fetch")
    @requires_enabled
    async def gh_fetch(self, event: AstrMessageEvent) -> None:
        """立刻轮询一次关注仓库。"""
        if not self.repos:
            yield event.plain_result("还没有配置关注仓库（repos）。")
            return
        yield event.plain_result("开始轮询 …")
        found = await self.poll_once(reason="command")
        if not found:
            yield event.plain_result("本轮没有新条目。")
            return
        lines = [
            f"- {item['repo']}#{item['number']} [{item['kind']}] {item['title']}"
            for item in found
        ]
        yield event.plain_result(f"发现 {len(found)} 条新条目：\n" + "\n".join(lines))

    @gh_group.command("config")
    @requires_enabled
    async def gh_config(self, event: AstrMessageEvent) -> None:
        """查看生效配置（不显示 token）。"""
        token_state = "未配置（匿名 60 次/小时）"
        if self.client.token:
            try:
                viewer = await self.client.viewer()
                token_state = f"已配置（@{viewer.get('login')}）"
            except GitHubError as exc:
                token_state = f"已配置（校验失败：{exc}）"
        repos = "、".join(entry["repo"] for entry in self.repos) or "（未配置）"
        as_review, review_event = self._review_mode()
        local = (
            "、".join(f"{repo}→{path}" for repo, path in self.local_paths.items())
            or "（未配置，纯静态核查）"
        )
        tools_state = "开" if self.config.get("enable_tools", True) else "关"
        cmds = "允许" if self.config.get("allow_local_commands", False) else "不允许"
        rate = self.client.last_rate_remaining or "未知"
        notify = self._notify_targets()
        notify_state = (
            f"{len(notify)} 个\n{self._format_targets(notify)}"
            if notify
            else "未配置（在目标会话发 /sid 拿到 UMO，填进 notify_targets）"
        )
        yield event.plain_result(
            f"Token：{token_state}\n"
            f"仓库：{repos}\n"
            f"模型：{self._chat_provider() or '（未配置）'}\n"
            f"轮询：{'开' if self.config.get('poll_enabled') else '关'}（{self.config.get('poll_cron')}）\n"
            f"通知会话：{notify_state}\n"
            f"发布：dry_run={'开' if self.config.get('dry_run', True) else '关'}"
            f"｜PR {'走 review' if as_review else '走评论'}（{review_event}）\n"
            f"本地仓库：{local}\n"
            f"工具：{tools_state}（本地命令{cmds}）｜API 剩余额度：{rate}\n"
            f"内置 Skill：{'、'.join(skills.list_skills()) or '（缺失）'}"
        )
