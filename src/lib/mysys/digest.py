"""Weekly digest: one short message + reports/weekly/<year>-W<week>.md."""
import json
import time

from . import config, gitstat, watcher


def _jsonl(path):
    out = []
    try:
        for line in path.read_text().splitlines():
            try:
                out.append(json.loads(line))
            except ValueError:
                pass
    except OSError:
        pass
    return out


def build(days=7):
    since = time.time() - days * 86400

    def recent(r):
        try:
            return time.mktime(time.strptime(r.get("t", ""), "%Y-%m-%d %H:%M:%S")) >= since
        except ValueError:
            return False

    runs = [r for r in _jsonl(config.REPORTS / "runs.jsonl") if recent(r)]
    failed = [r for r in runs if (r.get("errors") or 0) > 0 or (r.get("rc") not in (None, 0))]
    hours = sum(r.get("seconds", 0) for r in runs) / 3600
    audit = []
    for f in sorted((config.REPORTS / "ai_audit").glob("*.jsonl"))[-2:]:
        audit += [r for r in _jsonl(f) if recent(r)]
    blocked = [r for r in audit if r.get("decision") == "blocked"]
    m = watcher.read_metrics(days)
    ssd = [float(r["ssd_used_pct"]) for r in m if r.get("ssd_used_pct")]
    b = config.kv_file(config.STATE / "backup_status")
    ls = float(b.get("last_success", 0) or 0)
    git = []
    try:
        git = json.loads((config.STATE / "git_status.json").read_text())
    except (OSError, ValueError):
        pass
    git_issues = [(g["path"], gitstat.problems(g)) for g in git if gitstat.problems(g)]

    lines = [f"# Week {time.strftime('%G-W%V')} — {config.get('MACHINE_LABEL')}", "",
             f"- Runs: {len(runs)} ({hours:.0f} h of compute), {len(failed)} failed",
             f"- Root disk: {ssd[0]:.1f}% → {ssd[-1]:.1f}%" if len(ssd) > 1 else "- Root disk: not enough data",
             f"- Forecast: {watcher.forecast_text()}",
             f"- Last backup: {time.strftime('%a %H:%M', time.localtime(ls)) if ls else 'never'}",
             f"- AI tool calls: {len(audit)}, blocked: {len(blocked)}",
             f"- Projects needing attention: {len(git_issues)}"]
    for p, issues in git_issues[:10]:
        lines.append(f"  - {p}: {', '.join(issues)}")
    if failed:
        lines += ["", "Failed runs:"] + [f"- {r.get('t')} {r.get('tool') or r.get('cmd')} in {r.get('cwd')}"
                                         for r in failed[:15]]
    short = (f"{len(runs)} runs ({len(failed)} failed) · disk "
             + (f"{ssd[-1]:.0f}%" if ssd else "?") + f" · backup {'ok' if ls and time.time() - ls < 50 * 3600 else 'BEHIND'}"
             + f" · {len(blocked)} AI blocks · {len(git_issues)} projects need attention")
    return "\n".join(lines) + "\n", short


def run():
    text, short = build()
    d = config.REPORTS / "weekly"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{time.strftime('%G-W%V')}.md").write_text(text)
    import os
    os.environ["MS_PHONE"] = "1"
    watcher.notify("Weekly summary", short, "low", "bar_chart")
    return text
