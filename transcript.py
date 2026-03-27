#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = []
# ///
"""
Convert a Claude Code transcript into a static HTML chat page.

Usage:
    uv run transcript.py transcript.txt -o output.html
"""
from __future__ import annotations

import argparse
import html
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass
class Block:
    kind: str  # "prose", "tool", "subtask", "system"
    lines: list[str] = field(default_factory=list)

    def text(self) -> str:
        return "\n".join(self.lines)


@dataclass
class Turn:
    role: str  # "user", "agent", "system", "header"
    blocks: list[Block] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------

_TOOL_RE = re.compile(r"^(Bash|Read|Write|Edit|Update|Glob|Grep|Skill|Agent|Explore)\(")
_SUBTASK_RE = re.compile(r"^\s+[├└│]")


def parse_transcript(text: str) -> list[Turn]:
    lines = text.splitlines()
    turns: list[Turn] = []
    i = 0

    # --- header ---
    header_lines: list[str] = []
    while i < len(lines):
        s = lines[i].strip()
        if s.startswith("❯") or s.startswith("⏺"):
            break
        header_lines.append(lines[i])
        i += 1
    if header_lines:
        turns.append(Turn(role="header", blocks=[Block(kind="prose", lines=header_lines)]))

    # --- main conversation ---
    current_agent: Turn | None = None

    def flush_agent():
        nonlocal current_agent
        if current_agent and current_agent.blocks:
            turns.append(current_agent)
        current_agent = None

    while i < len(lines):
        line = lines[i]
        stripped = line.strip()

        # user
        if stripped.startswith("❯"):
            flush_agent()
            first = re.sub(r"^❯\s?", "", stripped)
            ulines = [first]
            i += 1
            while i < len(lines):
                nxt_s = lines[i].strip()
                if nxt_s == "" or nxt_s.startswith(("❯", "⏺", "✻", "⎿")):
                    break
                if _SUBTASK_RE.match(lines[i]):
                    break
                ulines.append(nxt_s)
                i += 1
            while i < len(lines) and lines[i].strip().startswith("⎿"):
                i += 1
            turns.append(Turn(role="user", blocks=[Block(kind="prose", lines=ulines)]))
            continue

        # system
        if stripped.startswith("✻"):
            flush_agent()
            turns.append(Turn(role="system", blocks=[
                Block(kind="system", lines=[re.sub(r"^✻\s?", "", stripped)])
            ]))
            i += 1
            continue

        # agent block
        if stripped.startswith("⏺"):
            if current_agent is None:
                current_agent = Turn(role="agent")
            first = re.sub(r"^⏺\s?", "", stripped)
            blines = [first]
            i += 1
            while i < len(lines):
                nxt_s = lines[i].strip()
                if nxt_s.startswith(("❯", "⏺", "✻")):
                    break
                blines.append(lines[i])
                i += 1
            kind = "tool" if _TOOL_RE.match(first) else "prose"
            current_agent.blocks.append(Block(kind=kind, lines=blines))
            continue

        # sub-tasks
        if _SUBTASK_RE.match(line):
            if current_agent is None:
                current_agent = Turn(role="agent")
            slines = [line]
            i += 1
            while i < len(lines):
                nxt_s = lines[i].strip()
                if nxt_s.startswith(("❯", "⏺", "✻")):
                    break
                if not _SUBTASK_RE.match(lines[i]) and nxt_s:
                    break
                slines.append(lines[i])
                i += 1
            current_agent.blocks.append(Block(kind="subtask", lines=slines))
            continue

        i += 1

    flush_agent()
    return turns


# ---------------------------------------------------------------------------
# Renderer helpers
# ---------------------------------------------------------------------------

def _esc(t: str) -> str:
    return html.escape(t)


def _tool_summary(first_line: str) -> tuple[str, str]:
    """Extract tool name and short arg description from a tool call first line."""
    m = re.match(r"^(Bash|Read|Write|Edit|Update|Glob|Grep|Skill|Agent|Explore)\((.*)$", first_line)
    if not m:
        return "Tool", first_line
    name = m.group(1)
    args = m.group(2).rstrip(")")
    # clean up the args for display
    args = args.strip()
    if len(args) > 90:
        args = args[:90] + "…"
    return name, args


def _tool_output_lines(block: Block) -> list[str]:
    """Extract output lines from a tool block (lines after ⎿)."""
    out: list[str] = []
    capturing = False
    for ln in block.lines[1:]:
        s = ln.strip()
        if s.startswith("⎿"):
            capturing = True
            out.append(re.sub(r"^⎿\s?", "", s))
        elif capturing:
            out.append(ln)
    return out


def _render_single_tool(block: Block) -> str:
    name, args = _tool_summary(block.lines[0])
    output = _tool_output_lines(block)
    output_html = ""
    if output:
        output_text = _esc("\n".join(output)).strip()
        if output_text:
            output_html = f'<pre class="tool-output">{output_text}</pre>'

    summary = f'<span class="tool-name">{_esc(name)}</span>'
    if args:
        summary += f'<span class="tool-args">{_esc(args)}</span>'

    return (
        f'<details class="tool-item">'
        f'<summary class="tool-item-summary">{summary}</summary>'
        f'{output_html}</details>'
    )


def _render_tool_group(tools: list[Block]) -> str:
    """Render a group of consecutive tool calls."""
    if len(tools) == 1:
        return f'<div class="tool-group">{_render_single_tool(tools[0])}</div>'

    inner = "\n".join(_render_single_tool(t) for t in tools)
    n = len(tools)
    return (
        f'<details class="tool-group-outer">'
        f'<summary class="tool-group-summary">{n} tool calls</summary>'
        f'<div class="tool-group-inner">{inner}</div>'
        f'</details>'
    )


PROSE_COLLAPSE = 12


def _render_prose(block: Block) -> str:
    text = block.text().strip()
    if not text:
        return ""
    lines = text.split("\n")
    if len(lines) <= PROSE_COLLAPSE:
        return f'<div class="prose">{_esc(text)}</div>'
    visible = _esc("\n".join(lines[:PROSE_COLLAPSE]))
    hidden = _esc("\n".join(lines[PROSE_COLLAPSE:]))
    n = len(lines) - PROSE_COLLAPSE
    return (
        f'<div class="prose">{visible}'
        f'<details class="more"><summary class="more-toggle">'
        f'{n} more lines</summary>'
        f'<div class="more-content">{hidden}</div></details></div>'
    )


def _render_subtask(block: Block) -> str:
    text = _esc(block.text().strip())
    return (
        f'<details class="tool-group-outer">'
        f'<summary class="tool-group-summary">Sub-agents</summary>'
        f'<pre class="tool-output">{text}</pre></details>'
    )


def _render_agent_turn(turn: Turn) -> str:
    """Render agent turn: group consecutive tool blocks, render prose normally."""
    segments: list[str] = []
    tool_buffer: list[Block] = []

    def flush_tools():
        nonlocal tool_buffer
        if tool_buffer:
            segments.append(_render_tool_group(tool_buffer))
            tool_buffer = []

    for block in turn.blocks:
        if block.kind == "tool":
            tool_buffer.append(block)
        elif block.kind == "subtask":
            flush_tools()
            segments.append(_render_subtask(block))
        else:
            flush_tools()
            r = _render_prose(block)
            if r:
                segments.append(r)

    flush_tools()
    return "\n".join(segments)


# ---------------------------------------------------------------------------
# Full page render
# ---------------------------------------------------------------------------

def render_html(turns: list[Turn], title: str = "Transcript") -> str:
    parts: list[str] = []

    for turn in turns:
        if turn.role == "header":
            t = _esc(turn.blocks[0].text()) if turn.blocks else ""
            parts.append(f'<div class="header"><pre>{t}</pre></div>')
            continue

        if turn.role == "system":
            t = _esc(turn.blocks[0].text()) if turn.blocks else ""
            parts.append(f'<div class="sys-row"><span class="sys">{t}</span></div>')
            continue

        if turn.role == "user":
            t = _esc(turn.blocks[0].text()) if turn.blocks else ""
            parts.append(
                f'<div class="row row-l"><div class="bubble bubble-u">'
                f'<div class="label label-u">You</div>'
                f'<div class="body">{t}</div>'
                f'</div></div>'
            )
            continue

        # agent
        inner = _render_agent_turn(turn)
        if not inner.strip():
            continue
        parts.append(
            f'<div class="row row-r"><div class="bubble bubble-a">'
            f'<div class="label label-a">Claude</div>'
            f'<div class="body">{inner}</div>'
            f'</div></div>'
        )

    return _PAGE.replace("{{TITLE}}", _esc(title)).replace("{{BODY}}", "\n".join(parts))


_PAGE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{{TITLE}}</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
:root{
  --bg:#0d1117; --surface:#161b22; --border:#30363d;
  --text:#e6edf3; --muted:#8b949e;
  --u-bg:#172a45; --u-border:#1f6feb; --u-label:#58a6ff;
  --a-bg:#161b22; --a-border:#30363d; --a-label:#c084fc;
  --tool-bg:#0d1117; --tool-border:#21262d;
  --accent:#c084fc;
}
body{
  font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Helvetica,Arial,sans-serif;
  font-size:14px; line-height:1.55; background:var(--bg); color:var(--text);
  padding:20px 12px; max-width:920px; margin:0 auto;
}

/* header */
.header{background:var(--surface);border:1px solid var(--border);border-radius:10px;padding:14px 18px;margin-bottom:24px}
.header pre{white-space:pre-wrap;font-family:'SF Mono','Fira Code',Consolas,monospace;font-size:12px;color:var(--muted);line-height:1.4}

/* rows */
.row{display:flex;margin-bottom:14px}
.row-l{justify-content:flex-start}
.row-r{justify-content:flex-end}

/* bubbles */
.bubble{max-width:82%;padding:10px 14px;border-radius:14px;overflow-wrap:break-word;word-break:break-word}
.bubble-u{background:var(--u-bg);border:1px solid var(--u-border);border-bottom-left-radius:4px}
.bubble-a{background:var(--a-bg);border:1px solid var(--a-border);border-bottom-right-radius:4px}

.label{font-size:10px;font-weight:700;text-transform:uppercase;letter-spacing:.07em;margin-bottom:5px}
.label-u{color:var(--u-label)}
.label-a{color:var(--a-label)}

.body{white-space:pre-wrap;font-size:12.5px;line-height:1.5;font-family:'SF Mono','Fira Code',Consolas,monospace}

/* prose sections inside agent */
.prose{margin:4px 0;padding:6px 0}
.prose:first-child{padding-top:0;margin-top:0}
.body > .prose + .tool-group,
.body > .prose + .tool-group-outer,
.body > .tool-group + .prose,
.body > .tool-group-outer + .prose{border-top:1px solid var(--border);padding-top:8px;margin-top:8px}

/* system */
.sys-row{display:flex;justify-content:center;margin:8px 0}
.sys{color:var(--muted);font-size:11px;font-style:italic;padding:3px 14px;border-radius:99px;border:1px solid var(--border);background:var(--surface)}

/* tool group (multiple) */
.tool-group-outer{margin:6px 0;border:1px solid var(--tool-border);border-radius:8px;background:var(--tool-bg);overflow:hidden}
.tool-group-summary{
  padding:8px 12px;cursor:pointer;font-size:12px;color:var(--muted);
  user-select:none;list-style:none;font-weight:600;
}
.tool-group-summary::-webkit-details-marker{display:none}
.tool-group-summary::before{content:'▶ ';font-size:9px}
details[open]>.tool-group-summary::before{content:'▼ '}
.tool-group-inner{border-top:1px solid var(--tool-border);padding:4px}

/* single tool (or inside group) */
.tool-group{margin:6px 0}
.tool-item{border:1px solid var(--tool-border);border-radius:6px;background:var(--tool-bg);overflow:hidden;margin:3px 0}
.tool-item-summary{
  padding:6px 10px;cursor:pointer;font-family:'SF Mono','Fira Code',Consolas,monospace;
  font-size:11.5px;color:var(--muted);user-select:none;list-style:none;
  white-space:nowrap;overflow:hidden;text-overflow:ellipsis;
}
.tool-item-summary::-webkit-details-marker{display:none}
.tool-item-summary::before{content:'▶ ';font-size:8px}
details[open]>.tool-item-summary::before{content:'▼ '}
.tool-name{color:var(--accent);font-weight:600;margin-right:6px}
.tool-args{color:var(--muted)}
.tool-output{
  margin:0;padding:8px 10px;font-family:'SF Mono','Fira Code',Consolas,monospace;
  font-size:11px;line-height:1.4;color:var(--muted);
  border-top:1px solid var(--tool-border);white-space:pre-wrap;
  max-height:300px;overflow-y:auto;
}

/* collapsible prose */
.more{display:inline}
.more-toggle{display:block;cursor:pointer;color:var(--accent);font-size:11px;user-select:none;list-style:none;padding:3px 0}
.more-toggle::-webkit-details-marker{display:none}
.more-toggle:hover{text-decoration:underline}
details[open]>.more-toggle{font-size:0;padding:0;height:0;overflow:hidden}
.more-content{white-space:pre-wrap}
</style>
</head>
<body>
{{BODY}}
</body>
</html>
"""


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(description="Convert Claude Code transcript to HTML")
    ap.add_argument("input", help="Path to transcript text file")
    ap.add_argument("-o", "--output", default=None, help="Output HTML file (default: <input>.html)")
    ap.add_argument("-t", "--title", default="Claude Code Transcript", help="Page title")
    args = ap.parse_args()

    path = Path(args.input)
    if not path.exists():
        print(f"Error: {path} not found", file=sys.stderr)
        sys.exit(1)

    turns = parse_transcript(path.read_text(encoding="utf-8"))
    out = Path(args.output) if args.output else path.with_suffix(".html")
    out.write_text(render_html(turns, title=args.title), encoding="utf-8")

    nu = sum(1 for t in turns if t.role == "user")
    na = sum(1 for t in turns if t.role == "agent")
    print(f"Written to {out} ({nu} user, {na} agent turns)")


if __name__ == "__main__":
    main()
