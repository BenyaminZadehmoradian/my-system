"""Turn parts of my-system off — for a while or until you turn them on again.

  ms pause FEATURE|all [--for 2h | --until 08:00]     ms resume FEATURE|all     ms paused

state/paused/<feature> holds the end time (epoch seconds; 0 = until resumed). Shell scripts check the same files.
Only you can do this: the AI guard blocks `ms pause` / `ms resume` for assistants. The AI guard itself cannot be
paused here (it is a root-owned policy; remove it with uninstall.sh if you really want to).
"""
import os
import time

from . import config

DIR = config.STATE / "paused"
FEATURES = {
    "notifications": "all messages (desktop and phone); they are still written to the log",
    "phone": "messages to the phone only (desktop stays on)",
    "status-report": "the scheduled status report (typing 'status' in ntfy still works)",
    "watcher": "run tracking and alerts every minute (disk, RAM, temperature, crashes, finished runs)",
    "daily": "the daily check and its reports",
    "backup": "the nightly backup and the monthly restore test",
    "queue": "starting new queue jobs (running jobs continue)",
    "remote": "sending jobs to remote servers (Titan); they wait here instead",
}


def _file(f):
    return DIR / f


def until(feature):
    """None = not paused; 0 = paused until resumed; else epoch seconds when it ends."""
    for f in (feature, "all"):
        try:
            v = float(_file(f).read_text().strip() or 0)
        except (OSError, ValueError):
            continue
        if v and v <= time.time():
            try:
                _file(f).unlink()
            except OSError:
                pass
            continue
        return v
    return None


def paused(feature):
    return until(feature) is not None


def parse_end(args):
    """--for 90m|2h|1d  or  --until 08:00 / '2026-11-02 08:00'  -> epoch, or 0 (until resumed)."""
    from . import queue
    if "--for" in args:
        v = args[args.index("--for") + 1]
        return time.time() + queue.parse_duration(v)
    if "--until" in args:
        i = args.index("--until")
        v = " ".join(args[i + 1:i + 3]) if i + 2 < len(args) and ":" in args[i + 2] else args[i + 1]
        return queue.parse_when(v)
    return 0


def _refuse_ai():
    try:
        from . import watcher
        who = watcher.ai_parent(os.getpid())
    except Exception:
        who = None
    if who:
        raise ValueError(f"refused: {who} (an AI assistant) may not pause or resume my-system; only the user can")


def set_pause(feature, end=0):
    _refuse_ai()
    if feature != "all" and feature not in FEATURES:
        raise ValueError(f"unknown feature '{feature}' (one of: all, {', '.join(FEATURES)})")
    DIR.mkdir(parents=True, exist_ok=True)
    _file(feature).write_text(str(int(end)) + "\n")


def resume(feature):
    _refuse_ai()
    if feature == "all":
        for f in DIR.glob("*") if DIR.exists() else []:
            f.unlink()
        return
    if feature not in FEATURES:
        raise ValueError(f"unknown feature '{feature}' (one of: all, {', '.join(FEATURES)})")
    try:
        _file(feature).unlink()
    except OSError:
        pass


def active():
    """[(feature, end)] currently paused ('all' first)."""
    out = []
    for f in ["all"] + list(FEATURES):
        try:
            v = float(_file(f).read_text().strip() or 0)
        except (OSError, ValueError):
            continue
        if v and v <= time.time():
            continue
        out.append((f, v))
    return out


def describe(end):
    if not end:
        return "until you turn it on"
    lt = time.localtime(end)
    day = "" if time.strftime("%Y-%m-%d", lt) == time.strftime("%Y-%m-%d") else time.strftime("%a %d %b ", lt)
    return f"until {day}{time.strftime('%H:%M', lt)}"


def summary():
    """One line for reports: 'paused: queue until 08:00, phone until you turn it on'."""
    a = active()
    return ", ".join(f"{f} {describe(e)}" for f, e in a)


HELP = """ms pause — turn parts of my-system off for a while

  ms pause FEATURE [--for 2h | --until 08:00]   (no option: until you turn it on again)
  ms pause all [--for …]                         everything below
  ms resume FEATURE | all                        turn it on again now
  ms paused                                      what is off and until when

FEATURE:
""" + "\n".join(f"  {k:<14} {v}" for k, v in FEATURES.items()) + """

The AI guard cannot be paused here. Only you can pause or resume (AI assistants are blocked from it).
"""


def cli(cmd, a):
    if cmd == "paused":
        act = active()
        if not act:
            print("nothing is paused")
        for f, e in act:
            print(f"{f:<14} paused {describe(e)}" + (f"  — {FEATURES[f]}" if f in FEATURES else "  — everything"))
        return 0
    if not a or a[0] in ("-h", "--help", "help"):
        print(HELP)
        return 0 if a else 2
    feature = a[0]
    try:
        if cmd == "pause":
            end = parse_end(a[1:])
            set_pause(feature, end)
            print(f"{feature} paused {describe(end)}" + ("" if feature == "all" else f" — {FEATURES[feature]}"))
            print(f"turn it on again: ms resume {feature}")
        else:
            resume(feature)
            print(f"{feature} is on again" if feature != "all" else "everything is on again")
    except ValueError as e:
        print(e)
        return 2
    return 0
