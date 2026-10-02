"""拼 prompt 并调模型，产出评论草稿。"""

from __future__ import annotations

from typing import Any

from astrbot.api import ToolSet

from . import skills
from . import tools as gh_tools

SYSTEM_PROMPT_LIMIT = 40000
SECTION_LABELS = {
    "blocking": "阻断项（🔴 [blocking]，必须改才能合并；每条带 文件:行号 + 改法）",
    "important": "重要问题（🟡 [important]，应该改；每条带 文件:行号 + 改法）",
    "verdict": "结论（可发 / 需修改，并给出最关键的理由）",
    "strengths": "优点（只写可核实的实现质量，不写客套话）",
    "minor": "次要问题（🟢 级，每条带 文件:行号）",
    "questions": "待确认的问题（用问句，指向具体代码）",
    "security": "安全清单（按 security-review-guide 逐项核对，用 [x]/[ ] 勾选）",
    "test_coverage": "测试覆盖（跑了什么、没跑什么，不要谎称跑过）",
}

DEFAULT_DISCLOSURE = (
    "> 🤖 本评论由 AI 生成，结论已基于仓库代码核实；如有出入，以维护者判断为准。"
)

TOOLS_HINT = (
    "\n\n# 你手上的工具（本地检出已就绪）\n"
    "- `gh_read_file`：读文件的行区间 —— 用来看改动所在函数的完整逻辑；\n"
    "- `gh_search_code`：按正则搜代码 —— 用来找调用方、找配置键引用；\n"
    "- `gh_list_dir`：看目录结构，确认新文件位置与命名。\n"
    "动手顺序：先读改动所在函数 → 再搜它的调用方与相关实现 → 然后才下结论。"
    "评论里的 `文件:行号` 必须来自你实际读到的内容，不要凭 diff 猜位置。"
)


def build_tools(
    root: str, *, allow_commands: bool = False, python: str = ""
) -> ToolSet:
    """按 workspace root 造只读工具集；命令执行需显式开启。"""
    toolset = ToolSet()
    toolset.add_tool(gh_tools.ReadFileTool(root=root))
    toolset.add_tool(gh_tools.SearchCodeTool(root=root))
    toolset.add_tool(gh_tools.ListDirTool(root=root))
    if allow_commands:
        toolset.add_tool(gh_tools.RunCheckTool(root=root, python=python))
    return toolset


def _format_skill_name(kind: str) -> str:
    return "gh-issue-format" if kind == "issue" else "gh-code-review"


def build_system_prompt(
    *,
    kind: str,
    changed_paths: list[str],
    include_sections: list[str],
    diff_truncated: bool = False,
    signals: list[str] | None = None,
    tools_enabled: bool = False,
) -> str:
    """把内置说明书拼成 system prompt：流程 skill + 对应格式 skill + 按需挑的参考文件。

    Raises:
        SkillMissing: 流程 skill（``gh-triage/SKILL.md``）不存在。
    """
    parts = [skills.read_skill(skills.ROOT_SKILL)]

    format_skill = _format_skill_name(kind)
    if format_skill in skills.list_skills():
        parts.append(f"\n\n# 输出格式（{format_skill}）\n")
        parts.append(skills.read_skill(format_skill))

    if kind == "pr":
        for ref in skills.pick_references(changed_paths):
            body = skills.read_reference("gh-code-review", ref)
            if body:
                # 标题与正文放同一个 part：超限时整段丢弃，不会留半截
                parts.append(f"\n\n# 参考：{ref}\n{body}")

    if include_sections:
        wanted = [
            SECTION_LABELS[key] for key in include_sections if key in SECTION_LABELS
        ]
        parts.append(
            "\n\n# 本次要求的章节\n" + "\n".join(f"- {item}" for item in wanted)
        )
    if diff_truncated:
        parts.append("\n\ndiff 已被截断：不要在结论里断言未看到的部分。")

    if tools_enabled:
        parts.append(TOOLS_HINT)
    if signals:
        parts.append(
            "\n\n# 自动扫描命中的信号（候选，不是结论）\n"
            + "\n".join(f"- {item}" for item in signals)
            + "\n逐条核实：成立才写进评论，不成立的不要提。"
        )

    prompt = "".join(parts)
    if len(prompt) <= SYSTEM_PROMPT_LIMIT:
        return prompt
    # 超限时优先丢掉「参考」段，别把某份参考文件切成半截
    slim = [part for part in parts if not part.startswith("\n\n# 参考：")]
    note = "\n\n（提示：部分参考资料因长度超出上限被省略。）"
    text = "".join(slim) + note
    return text[:SYSTEM_PROMPT_LIMIT] if len(text) > SYSTEM_PROMPT_LIMIT else text


def build_user_prompt(
    digest_markdown: str, *, repo: str, number: int, kind: str
) -> str:
    label = "issue" if kind == "issue" else "PR"
    return (
        f"下面是 {repo}#{number} 这条 {label} 的完整材料。\n\n"
        f"{digest_markdown}\n\n"
        "请按前面的说明书核查，并**只输出可直接发布的评论正文**（Markdown）。"
        "不要输出解释、不要复述材料、不要写「已检查 A/B/C」这类过程说明。"
        "每条断言都要能指到文件与行号，没有证据的不要写。"
    )


async def make_draft(
    context: Any,
    *,
    chat_provider_id: str,
    kind: str,
    digest_markdown: str,
    repo: str,
    number: int,
    changed_paths: list[str] | None = None,
    include_sections: list[str] | None = None,
    diff_truncated: bool = False,
    signals: list[str] | None = None,
    event: Any = None,
    workspace_root: str | None = None,
    allow_commands: bool = False,
    max_steps: int = 12,
    local_python: str = "",
) -> str:
    """产出评论草稿。

    给了 ``workspace_root`` 与 ``event`` 时走 ``tool_loop_agent``（模型能读文件、搜代码），
    否则退回一次 ``llm_generate``（只看 diff）。失败抛异常，由调用方提示。
    """
    use_tools = bool(workspace_root) and event is not None
    system_prompt = build_system_prompt(
        kind=kind,
        changed_paths=list(changed_paths or []),
        include_sections=list(include_sections or []),
        diff_truncated=diff_truncated,
        signals=list(signals or []),
        tools_enabled=use_tools,
    )
    user_prompt = build_user_prompt(
        digest_markdown, repo=repo, number=number, kind=kind
    )

    if use_tools:
        response = await context.tool_loop_agent(
            event=event,
            chat_provider_id=chat_provider_id,
            prompt=user_prompt,
            system_prompt=system_prompt,
            tools=build_tools(
                str(workspace_root),
                allow_commands=allow_commands,
                python=local_python,
            ),
            max_steps=int(max_steps),
        )
    else:
        response = await context.llm_generate(
            chat_provider_id=chat_provider_id,
            prompt=user_prompt,
            system_prompt=system_prompt,
        )
    text = getattr(response, "completion_text", None) or str(response or "")
    return text.strip()
