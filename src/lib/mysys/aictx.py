"""Builds the machine-specific part of the AI instructions every day.

  ai/AI_RULES.md            static, generic rules (root-owned, locked)
  state/SYSTEM_MAP.md       generated: hardware, disks, toolchains, venvs, projects, protected paths
  state/AGENTS.md           AI_RULES + SYSTEM_MAP in one file (for tools that read AGENTS.md)
"""
import json
import time
from pathlib import Path

from . import capacity, config, detect, probes


def _home(p):
    p = str(p)
    h = str(config.HOME)
    return "~" + p[len(h):] if p.startswith(h) else p


def tools_table():
    f = config.REPORTS / "latest" / "inventory" / "tools.txt"
    rows = []
    try:
        for line in f.read_text().splitlines():
            parts = line.split(None, 2)
            if len(parts) == 3 and not line.startswith("#") and parts[1].startswith("/"):
                rows.append(f"| {parts[0]} | `{_home(parts[1])}` | {parts[2][:70]} |")
    except OSError:
        pass
    return rows


def build():
    d = detect.load_state() or detect.write_state()
    c = config.load()
    mem = d.get("mem_total", 0) / 2**30
    lines = [f"# System map — {c.get('MACHINE_LABEL')} (generated {time.strftime('%Y-%m-%d %H:%M')})",
             "", "Generated automatically every day by my-system. Do not edit; it is overwritten.", "",
             "## Machine",
             f"- {d.get('product', '')} · {d.get('os', '')} · kernel {d.get('kernel', '')}",
             f"- {d.get('threads', '?')} CPU threads · {mem:.0f} GB RAM"]
    root = probes.disk("/")
    if root:
        lines.append(f"- Root disk `/`: {root[1] / 2**30:.0f} GB free of {root[2] / 2**30:.0f} GB "
                     f"({root[0] / root[2] * 100:.0f}% used) — keep big outputs off it")
    for dd in config.words("DATA_DISKS") or d.get("data_disks", []):
        x = probes.disk(dd)
        if x:
            lines.append(f"- Data disk `{dd}`: {x[1] / 2**40:.1f} TB free — put large build/run outputs here"
                         + (f" (e.g. `{c.get('BIG_FOLDER_DEST')}`)" if c.get("BIG_FOLDER_DEST") else ""))
    for sh in config.words("NET_SHARES") or d.get("net_shares", []):
        lines.append(f"- Network share `{sh}` (shared, may be slow; not for scratch data)")

    try:
        lines += ["", capacity.guide_md()]
    except Exception as e:  # never lose the whole map because one probe failed
        lines += ["", f"(capacity guide unavailable: {type(e).__name__})"]

    lines += ["", "## Toolchains (detected)", "| Name | Install root |", "|---|---|"]
    lines += [f"| {l} | `{_home(p)}` |" for l, p in d.get("tools", [])] or ["| (none found) | |"]
    extra = config.pairs("paths.conf")
    if extra:
        lines += ["", "Extra paths noted by the user:"] + [f"- {l}: `{_home(p)}`" for l, p in extra]

    tt = tools_table()
    if tt:
        lines += ["", "## Command-line tools (from the latest inventory)", "| Tool | Path | Version |", "|---|---|---|"] + tt

    lines += ["", "## Python environments", "Use these; never install into the system Python. A new project gets",
              "its own `.venv` inside the project folder (ask the user before installing packages).", ""]
    lines += [f"- `{_home(v['path'])}` — Python {v.get('python') or '?'}" for v in d.get("venvs", [])] or ["- (none)"]

    lines += ["", "## Projects (git repositories)"]
    gs = {}
    try:
        gs = {g["path"]: g for g in json.loads((config.STATE / "git_status.json").read_text())}
    except (OSError, ValueError):
        pass
    sw = {}
    try:
        from . import software
        sw = (software.load() or {}).get("projects", {})
    except Exception:
        pass
    for p in d.get("projects", []):
        g = gs.get(p, {})
        note = f" — branch {g['branch']}" if g.get("branch") else ""
        u = sw.get(p) or {}
        bits = []
        if u.get("venvs"):
            bits.append("env " + ", ".join(f"`{_home(v)}`" for v in u["venvs"]))
        if u.get("tools"):
            bits.append("tools " + ", ".join(f"{k}→{_home(v)}" for k, v in sorted(u["tools"].items())[:6]))
        lines.append(f"- `{_home(p)}`{note}" + (" — " + "; ".join(bits) if bits else ""))
    if not d.get("projects"):
        lines.append("- (none found)")

    lines += ["", "## Protected paths (writes are blocked automatically)"]
    lines += [f"- `{_home(p)}`" for p in config.lst("protected_paths.txt")]
    lines += ["", "## Where to look first",
              f"- Latest health report: `{_home(config.REPORTS / 'latest' / 'health.txt')}`",
              f"- Full tool/library inventory: `{_home(config.REPORTS / 'latest' / 'inventory')}/`",
              f"- Full system report (hardware, disks, memory, OS, software): `{_home(config.REPORTS / 'system' / 'latest.md')}`",
              f"- Past run results: `{_home(config.REPORTS / 'results.csv')}`",
              f"- Run-log format every new program must write: `{_home(config.MS / 'runlog' / 'RUNLOG_SPEC.md')}` "
              "(check with `ms log check <folder>`; list all runs with `ms log list`)"]
    return "\n".join(lines) + "\n"


def write():
    text = build()
    config.STATE.mkdir(parents=True, exist_ok=True)
    (config.STATE / "SYSTEM_MAP.md").write_text(text)
    rules = ""
    try:
        rules = (config.MS / "ai" / "AI_RULES.md").read_text()
    except OSError:
        pass
    (config.STATE / "AGENTS.md").write_text(rules.rstrip() + "\n\n" + text)
    return text


def path():
    return Path(config.STATE / "SYSTEM_MAP.md")
