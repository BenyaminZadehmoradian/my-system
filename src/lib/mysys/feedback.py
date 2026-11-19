"""Reports from AI sessions (and you) about my-system itself — `ms feedback ...`.

A Claude session that works on a project and runs into a problem with my-system (the queue, the server, the rules,
the run log, a block it thinks is wrong, something missing) files a report instead of working around it. You decide:
`ms feedback accept ID "why"` (it will be improved) or `ms feedback reject ID "why"`. The answer is shown to the next
AI session in that project (`ms ai status`), so the same request is not made again. A change that fixes a report
names it in CHANGELOG.md as "(feedback #ID)"; installing that version marks the report done.

Stored as one JSON file per report in state/feedback/. Only you may accept, reject or mark done (the guard blocks
AI sessions from it)."""
import json
import os
import re
import subprocess
import time

from . import config

DIR = config.STATE / "feedback"
KINDS = ("bug", "wish", "rule", "question")
OPEN = ("open", "accepted")


def _path(fid):
    return DIR / f"{int(fid)}.json"


def load(fid):
    try:
        return json.loads(_path(fid).read_text())
    except (OSError, ValueError):
        return None


def save(f):
    DIR.mkdir(parents=True, exist_ok=True)
    tmp = _path(f["id"]).with_suffix(".tmp")
    tmp.write_text(json.dumps(f, indent=1))
    os.replace(tmp, _path(f["id"]))


def all_items():
    out = []
    if DIR.exists():
        for p in DIR.glob("*.json"):
            try:
                out.append(json.loads(p.read_text()))
            except (OSError, ValueError):
                pass
    return sorted(out, key=lambda f: f["id"])


def _project(cwd):
    try:
        from . import detect
        ps = [p for p in detect.load_state().get("projects", [])
              if cwd == p or cwd.startswith(p.rstrip("/") + "/")]
        if ps:
            return max(ps, key=len)
    except Exception:
        pass
    return cwd


def _version():
    try:
        from . import __version__
        return __version__
    except Exception:
        return ""


def _by():
    if os.environ.get("CLAUDECODE") or os.environ.get("MS_BY_AI"):
        return "ai"
    try:
        from . import watcher
        if watcher.ai_parent(os.getpid()):
            return "ai"
    except Exception:
        pass
    return "user"


def _job_context(jid):
    try:
        from . import queue
        j = queue.load(jid)
    except Exception:
        j = None
    if not j:
        return None
    return {k: j.get(k) for k in ("id", "name", "state", "reason", "last_line", "prefer_note", "start_note", "remote",
                                  "cores", "mem_gb", "cwd") if j.get(k) not in (None, "")}


def similar(title, items=None):
    """Open reports whose title shares most words with `title` (to add to them instead of filing again)."""
    w = set(re.findall(r"[a-z0-9]{3,}", title.lower()))
    if not w:
        return []
    out = []
    for f in items if items is not None else all_items():
        if f["status"] not in OPEN:
            continue
        o = set(re.findall(r"[a-z0-9]{3,}", f["title"].lower()))
        if o and len(w & o) / len(w | o) >= 0.5:
            out.append(f)
    return out


def add(title, text="", kind="bug", jobs=(), cwd=None, by=None):
    """File a report; returns (report, existing) — existing is a similar open report it was added to instead."""
    title = " ".join(title.split())[:160]
    if not title:
        raise ValueError("a report needs a title")
    kind = kind if kind in KINDS else "bug"
    cwd = os.path.abspath(cwd or os.getcwd())
    ctx = [c for c in (_job_context(j) for j in jobs) if c]
    by = by or _by()
    now = time.time()
    same = similar(title)
    if same:                                   # the same problem again: one more voice, not a second report
        f = same[0]
        f.setdefault("votes", []).append({"t": now, "by": by, "cwd": cwd, "project": _project(cwd),
                                          "text": text[:4000], "jobs": ctx})
        save(f)
        return f, True
    DIR.mkdir(parents=True, exist_ok=True)
    fid = max([f["id"] for f in all_items()] + [0]) + 1
    f = {"id": fid, "t": now, "kind": kind, "title": title, "text": text[:8000], "by": by, "cwd": cwd,
         "project": _project(cwd), "jobs": ctx, "version": _version(), "status": "open", "answer": "",
         "answered": None, "votes": []}
    save(f)
    try:
        subprocess.Popen([str(config.MS / "bin" / "notify.sh"), f"Report #{fid} from {'an AI session' if by == 'ai' else 'you'}",
                          f"{kind}: {title} — ms feedback show {fid}", "low", ""],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except OSError:
        pass
    return f, False


def answer(fid, status, text=""):
    f = load(fid)
    if not f:
        return None
    f.update(status=status, answer=text.strip()[:2000], answered=time.time())
    save(f)
    return f


def resolve_from_changelog(path=None):
    """Mark accepted/open reports done when CHANGELOG.md of the installed version names them '(feedback #N)'."""
    p = path or (config.MS / "CHANGELOG.md")
    try:
        text = open(p).read()
    except OSError:
        return []
    done = []
    ver = None
    for line in text.splitlines():
        m = re.match(r"##\s+(\d+\.\d+\.\d+)", line)
        if m:
            ver = m.group(1)
        for n in re.findall(r"feedback #(\d+)", line):
            f = load(int(n))
            if f and f["status"] in OPEN:
                f.update(status="done", answer=(f.get("answer") + " · " if f.get("answer") else "") + f"fixed in {ver}",
                         answered=time.time(), fixed_in=ver)
                save(f)
                done.append(f["id"])
    return done


def _age(t):
    s = max(0, time.time() - (t or 0))
    return f"{s / 86400:.0f} d" if s >= 86400 else f"{s / 3600:.0f} h" if s >= 3600 else f"{max(1, s / 60):.0f} min"


def line(f):
    n = len(f.get("votes") or [])
    return (f"#{f['id']:<4} {f['status']:<9} {f['kind']:<8} {f['title'][:70]}"
            + (f"  (+{n})" if n else "") + f"  — {f['by']}, {_age(f['t'])} ago")


def show_text(f):
    L = [line(f), f"  project: {f['project']}", f"  folder:  {f['cwd']}", f"  my-system {f.get('version', '')}"]
    if f.get("text"):
        L += ["", *("  " + x for x in f["text"].splitlines())]
    for c in f.get("jobs") or []:
        L.append(f"  job #{c.get('id')} {c.get('name')} {c.get('state')}: {(c.get('reason') or '')[:150]}")
        for k in ("last_line", "prefer_note", "start_note"):
            if c.get(k):
                L.append(f"      {k}: {str(c[k])[:200]}")
    for v in f.get("votes") or []:
        L.append(f"  + {v['by']} in {v['project']}, {_age(v['t'])} ago" + (f": {v['text'][:300]}" if v.get("text") else ""))
    if f.get("answer") or f["status"] not in ("open",):
        L += ["", f"  answer ({f['status']}): {f.get('answer') or '-'}"]
    return "\n".join(L)


def ai_lines(cwd=None, days=30):
    """For `ms ai status`: open reports, and answers of the last `days` days for this project first."""
    items = all_items()
    if not items:
        return []
    proj = _project(os.path.abspath(cwd or os.getcwd()))
    now = time.time()
    L = []
    opn = [f for f in items if f["status"] in OPEN]
    if opn:
        L.append(f"reports about my-system still open: {len(opn)} — before filing, see `ms feedback list` (add to one "
                 "with `ms feedback add` and the same title)")
    ans = [f for f in items if f["status"] in ("rejected", "done", "accepted") and f.get("answered")
           and now - f["answered"] < days * 86400]
    ans.sort(key=lambda f: (f["project"] != proj, -(f["answered"] or 0)))
    for f in ans[:4]:
        L.append(f"  #{f['id']} {f['status']}: {f['title'][:60]} — {(f.get('answer') or '')[:110]}")
    return L


HELP = """ms feedback — reports about my-system itself (from AI sessions and from you)

  ms feedback add [--kind bug|wish|rule|question] [--job ID ...] "TITLE" [-- DETAILS ...]
                                 file a report (a similar open one gets your details added instead)
  ms feedback list [--all]       open and accepted reports (--all: also answered ones)
  ms feedback show ID            the report, its context (jobs, folder), who else hit it, the answer
  ms feedback accept ID "WHY"    you: it will be improved          (AI sessions may not)
  ms feedback reject ID "WHY"    you: it will not be changed, and why (shown to later AI sessions)
  ms feedback done ID ["NOTE"]   you: it is fixed (also automatic: CHANGELOG "(feedback #ID)")

Report problems with my-system (queue, server, rules, run log, a block you think is wrong, something missing) —
not bugs of the project you work on."""


def cli(a):
    if not a or a[0] in ("-h", "--help", "help"):
        print(HELP)
        return 0
    sub, rest = a[0], a[1:]
    if sub == "add":
        kind, jobs, words, details = "bug", [], [], []
        i = 0
        while i < len(rest):
            x = rest[i]
            if x == "--":
                details = rest[i + 1:]
                break
            if x == "--kind" and i + 1 < len(rest):
                kind, i = rest[i + 1], i + 2
                continue
            if x == "--job" and i + 1 < len(rest):
                jobs += [int(y) for y in rest[i + 1].split(",") if y.strip().isdigit()]
                i += 2
                continue
            words.append(x)
            i += 1
        if kind not in KINDS:
            print(f"--kind must be one of: {', '.join(KINDS)}")
            return 2
        try:
            f, same = add(" ".join(words), " ".join(details), kind, jobs)
        except ValueError as e:
            print(f"ms feedback: {e}")
            return 2
        print(f"added to report #{f['id']} (same problem: {f['title']})" if same else
              f"report #{f['id']} filed — the user decides; see the answer later with: ms feedback show {f['id']}")
        return 0
    if sub in ("list", "ls"):
        items = [f for f in all_items() if "--all" in rest or f["status"] in OPEN]
        if not items:
            print("no open reports" + ("" if "--all" in rest else " (--all: also answered ones)"))
        for f in items:
            print(line(f))
        return 0
    if sub == "show":
        f = load(rest[0]) if rest and rest[0].isdigit() else None
        if not f:
            print("usage: ms feedback show ID")
            return 2
        print(show_text(f))
        return 0
    if sub in ("accept", "reject", "done"):
        if not rest or not rest[0].isdigit():
            print(f"usage: ms feedback {sub} ID \"WHY\"")
            return 2
        if sub == "reject" and len(rest) < 2:
            print("say why it is rejected: the answer is shown to later AI sessions, so they do not ask again")
            return 2
        f = answer(int(rest[0]), {"accept": "accepted", "reject": "rejected", "done": "done"}[sub], " ".join(rest[1:]))
        if not f:
            print(f"no report #{rest[0]}")
            return 1
        print(line(f))
        return 0
    if sub == "_changelog":                      # install.sh
        n = resolve_from_changelog()
        if n:
            print(f"reports fixed by this version: {', '.join('#' + str(x) for x in n)}")
        return 0
    print(HELP)
    return 2
