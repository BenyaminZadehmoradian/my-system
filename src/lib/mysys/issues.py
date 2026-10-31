"""Warnings and failures found in tool logs.

* Issues: CRITICAL WARNING / WARNING / ERROR lines (Vivado, Vitis, HLS, GCC/Clang style) are parsed into
  fingerprinted issues; compared with a reference run they become new / fixed / outstanding, and quality
  gates can turn a run into *warning* or *failed* (e.g. "no new critical warnings").
* Suppressions: config/warnings_ignore.txt — one regex per line; matching issues are ignored.
* Failure knowledge base: config/failure_kb.yaml plus built-in rules; a failed run's log is matched
  and the known cause and fix are attached to the job, the run log and the notification.
* JUnit XML: test counts and failing test names.
"""
import glob
import json
import os
import re
import xml.etree.ElementTree as ET

from . import config

LINE = re.compile(r"^(CRITICAL WARNING|WARNING|ERROR)\s*:\s*(\[[^\]]+\])?\s*(.*)$")
GCC = re.compile(r"^(\S+?):(\d+)(?::\d+)?:\s*(warning|error):\s*(.*?)(\s*\[(-W[^\]]+)\])?$")
SEV = {"CRITICAL WARNING": "critical", "WARNING": "warning", "ERROR": "error", "warning": "warning", "error": "error"}

BUILTIN_KB = [
    {"name": "Out of memory", "pattern": r"(Killed|Out of memory|std::bad_alloc|Cannot allocate memory|oom-kill)",
     "fix": "Run fewer jobs in parallel or raise mem_gb; check `ms q report` for the real peak."},
    {"name": "License not available", "pattern": r"(License checkout failed|No license|FLEXnet Licensing error|Common 17-345|license.*(not available|expired))",
     "fix": "All seats are in use or the license server is unreachable; define the license in config/resources.conf so the queue waits for a seat."},
    {"name": "Disk full", "pattern": r"(No space left on device|Disk quota exceeded)",
     "fix": "Free space or move the run folder to the data disk (ms move)."},
    {"name": "Missing file", "pattern": r"(No such file or directory|cannot open|couldn't read file|ERROR: \[Common 17-55\])",
     "fix": "A path in the script is wrong or a previous step did not produce its output."},
    {"name": "Placement failed", "pattern": r"\[Place 30-(640|99|487|838)\]",
     "fix": "Design does not fit or has illegal constraints: check utilization and pblocks/IO constraints."},
    {"name": "Routing failed", "pattern": r"\[Route 35-(1|7|39|54)\]|not legally routed",
     "fix": "Congestion: try another strategy (e.g. Congestion_SpreadLogic), floorplan, or reduce utilization."},
    {"name": "Timing not met", "pattern": r"(Timing constraints are not met|\[Timing 38-282\])",
     "fix": "WNS < 0: check the worst path (report_timing_summary); try Performance_Explore or pipelining."},
    {"name": "DRC error", "pattern": r"\[DRC [A-Z]+-\d+\]",
     "fix": "Bitstream DRC failed: read the DRC report; often unconstrained IO standards or locations (NSTD-1, UCIO-1)."},
    {"name": "HLS synthesis failed", "pattern": r"\[HLS 200-70\]|\[HLS 214-\d+\].*(error|failed)|csynth.*failed",
     "fix": "Read the first [HLS ...] error; unsupported constructs or pragmas are the usual cause."},
    {"name": "Segmentation fault", "pattern": r"(Segmentation fault|SIGSEGV|core dumped)",
     "fix": "Tool crash: rerun once; if repeatable, reduce threads (maxThreads 1) or update the tool."},
    {"name": "Python exception", "pattern": r"^Traceback \(most recent call last\)",
     "fix": "See the last lines of the traceback in the job output."},
    {"name": "Permission denied", "pattern": r"Permission denied",
     "fix": "The job tried to write somewhere it may not (protected path or read-only output)."},
]


def _norm(msg):
    m = re.sub(r"0x[0-9a-fA-F]+", "#", msg)
    m = re.sub(r"\d+(\.\d+)?", "#", m)
    m = re.sub(r"/[^\s'\"]+", "<path>", m)
    m = re.sub(r"\s+", " ", m).strip()
    return m[:200]


def suppressions():
    pats = []
    for line in config.lst("warnings_ignore.txt"):
        try:
            pats.append(re.compile(line))
        except re.error:
            pass
    return pats


def parse_text(text, sup=None):
    """[{sev, id, msg, fp}] unique by fingerprint, with count."""
    sup = sup if sup is not None else suppressions()
    found = {}
    for line in text.splitlines():
        s = re.sub(r"^\[[^\]]+\]\s+(?=(CRITICAL WARNING|WARNING|ERROR)\b)", "", line.strip())   # followed-log prefix
        m = LINE.match(s)
        if m:
            sev, tid, msg = SEV[m.group(1)], (m.group(2) or "").strip("[]"), m.group(3)
        else:
            g = GCC.match(s)
            if not g:
                continue
            sev, tid, msg = SEV[g.group(3)], g.group(6) or "", f"{g.group(4)} ({os.path.basename(g.group(1))})"
        if any(p.search(s) for p in sup):
            continue
        fp = f"{sev}|{tid}|{_norm(msg)}"
        if fp in found:
            found[fp]["count"] += 1
        else:
            found[fp] = {"sev": sev, "id": tid, "msg": msg[:300], "fp": fp, "count": 1}
    return list(found.values())


def summarize(issues):
    out = {"critical": 0, "warning": 0, "error": 0}
    for i in issues:
        out[i["sev"]] += 1
    return out


def compare(cur, ref):
    """new / fixed / outstanding lists of issues (by fingerprint)."""
    rf = {i["fp"] for i in ref or []}
    cf = {i["fp"] for i in cur}
    return {"new": [i for i in cur if i["fp"] not in rf], "fixed": [i for i in ref or [] if i["fp"] not in cf],
            "outstanding": [i for i in cur if i["fp"] in rf]}


def gate(cmp, cur, rules):
    """Apply quality gates. rules e.g. {"new_critical": 0, "total_critical": 20, "new_warning": 50, "level": "failed"}.
    Returns (ok, [messages])."""
    msgs = []
    tot = summarize(cur)
    new = summarize(cmp["new"]) if cmp else tot
    for k, limit in (rules or {}).items():
        if k == "level":
            continue
        kind, _, sev = k.partition("_")
        val = (new if kind == "new" else tot).get(sev)
        if val is not None and val > int(limit):
            msgs.append(f"{val} {kind} {sev} issue(s) > {limit}")
    return not msgs, msgs


# ---------------------------------------------------------------- failure knowledge base
def kb():
    rules = list(BUILTIN_KB)
    p = config.CONF / "failure_kb.yaml"
    if p.exists():
        try:
            import yaml
            extra = yaml.safe_load(p.read_text()) or []
            rules = [r for r in extra if isinstance(r, dict) and r.get("pattern")] + rules
        except Exception:
            pass
    return rules


def diagnose(text):
    """First matching known cause: {name, fix, line} or None. Later lines matter more (tools print the cause last)."""
    lines = text.splitlines()[-20000:]
    for r in kb():
        try:
            rx = re.compile(r["pattern"], re.M | re.I)
        except re.error:
            continue
        for line in reversed(lines):
            if rx.search(line):
                return {"name": r.get("name", "?"), "fix": r.get("fix", ""), "line": line.strip()[:300]}
    return None


# ---------------------------------------------------------------- JUnit
def junit(paths, cwd):
    tot = fail = err = skip = 0
    failing = []
    for pat in paths or []:
        for f in glob.glob(os.path.join(cwd, pat), recursive=True):
            try:
                root = ET.parse(f).getroot()
            except (ET.ParseError, OSError):
                continue
            for tc in root.iter("testcase"):
                tot += 1
                name = f"{tc.get('classname', '')}.{tc.get('name', '')}".strip(".")
                if tc.find("failure") is not None:
                    fail += 1
                    failing.append(name)
                elif tc.find("error") is not None:
                    err += 1
                    failing.append(name)
                elif tc.find("skipped") is not None:
                    skip += 1
    if not tot:
        return None
    return {"tests_total": tot, "tests_failed": fail, "tests_error": err, "tests_skipped": skip,
            "tests_passed": tot - fail - err - skip, "failing": failing[:50]}


# ---------------------------------------------------------------- per-job issue store
STORE = config.STATE / "queue" / "issues"


def save(jid, issues):
    STORE.mkdir(parents=True, exist_ok=True)
    (STORE / f"{jid}.json").write_text(json.dumps(issues))


def load(jid):
    try:
        return json.loads((STORE / f"{jid}.json").read_text())
    except (OSError, ValueError):
        return None
