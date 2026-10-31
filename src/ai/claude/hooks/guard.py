#!/usr/bin/python3
"""Claude Code PreToolUse hook (registered in the root-owned managed policy).

1. Logs every tool call to <my-system>/reports/ai_audit/<YYYY-MM>.jsonl
2. Blocks shell commands and file edits that would write to protected paths
   (config/protected_paths.txt + detected toolchains/venvs + the backup repository),
   and a few always-dangerous commands. Exit code 2 = blocked; stderr is the reason.
"""
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

HOME = str(Path.home())
MS = Path(os.environ.get("MY_SYSTEM", Path(HOME) / "My_System"))
sys.path.insert(0, str(MS / "lib"))
try:
    from mysys import config, detect
except Exception:              # the guard must work even if the library is broken
    config = detect = None

FALLBACK = ["/etc", "/opt", "/tools", "/usr", "/boot", "/var/lib", f"{HOME}/.bashrc", f"{HOME}/.profile",
            f"{HOME}/.ssh", f"{HOME}/.claude", str(MS)]

HARD = [
    (re.compile(r"(^|[;&|(\s])(sudo|su|pkexec|doas)\s"), "root commands are not allowed"),
    (re.compile(r"(^|[;&|(\s])(kill|pkill|killall|skill)\s"), "killing processes is not allowed (long runs may be active)"),
    (re.compile(r"\bsystemctl\b.*\b(stop|restart|disable|mask|kill|poweroff|reboot)\b"), "stopping services is not allowed"),
    (re.compile(r"(^|[;&|(\s])(reboot|shutdown|poweroff|halt)(\s|$)"), "rebooting is not allowed"),
    (re.compile(r"\bcrontab\s+(-r|-e|\S+\s*$)"), "changing crontab is not allowed"),
    (re.compile(r"\bpip3?\s+install\b.*--break-system-packages"), "installing into system Python is not allowed"),
    (re.compile(r"(^|[;&|(\s])(mkfs\S*|fdisk|parted|wipefs|dd\s+.*of=/dev/)"), "disk tools are not allowed"),
]
DEST_LAST = {"cp", "rsync", "install", "ln", "scp"}
ANY_ARG = {"mv", "rm", "rmdir", "chmod", "chown", "chgrp", "chattr", "truncate", "touch", "mkdir", "unlink",
           "shred", "patch", "tee", "dd", "setfacl"}


def protected():
    out = []
    if config:
        out += config.lst("protected_paths.txt")
        try:
            d = detect.load_state()
            out += [p for _, p in d.get("tools", [])] + [v["path"] for v in d.get("venvs", [])]
        except Exception:
            pass
        try:
            r = config.backup_repo()
            if r:
                out.append(r)
        except Exception:
            pass
    out = out or FALLBACK
    out.append(str(MS))
    return sorted({os.path.realpath(os.path.expanduser(p)).rstrip("/") for p in out if p}, key=len, reverse=True)


def expand(cmd):
    cmd = cmd.replace("${HOME}", HOME).replace("$HOME", HOME)
    return re.sub(r"(^|[\s=:'\"(])~(?=/|\s|$)", lambda m: m.group(1) + HOME, cmd)


def hit(text, prot):
    for p in prot:
        if re.search(re.escape(p) + r"(/|\b|$)", text):
            return p
    return None


# programs that would *execute* a heredoc body; for anything else (cat > f, tee f, ...) the body is only file data
INTERP = {"bash", "sh", "zsh", "dash", "ksh", "fish", "ssh", "python", "python3", "perl", "ruby", "node", "tclsh",
          "vivado", "xargs", "env", "nice", "nohup", "time", "eval", "exec", "source", "."}
HEREDOC = re.compile(r"(?<!<)<<(-?)\s*(['\"]?)([A-Za-z_][\w.-]*)\2")


def strip_heredocs(cmd):
    """Drop heredoc bodies that are just data, so text inside a file being written never trips a check."""
    lines, out, i = cmd.split("\n"), [], 0
    while i < len(lines):
        line = lines[i]
        out.append(line)
        i += 1
        for m in HEREDOC.finditer(line):
            seg = re.split(r"\|\||&&|[;|&(]", line[:m.start()])[-1].split()
            seg = [w for w in seg if not re.match(r"^\w+=", w)]
            prog = os.path.basename(seg[0].strip("'\"")) if seg else ""
            prog = re.sub(r"[0-9.]+$", "", prog) if prog.startswith("python") else prog
            body = []
            while i < len(lines):
                end = lines[i].lstrip("\t") if m.group(1) else lines[i]
                if end == m.group(3):
                    break
                body.append(lines[i])
                i += 1
            if prog in INTERP or prog.startswith("python"):
                out += body
            if i < len(lines):
                out.append(lines[i])
                i += 1
    return "\n".join(out)


def check_bash(cmd, prot):
    c = strip_heredocs(expand(cmd))
    for rx, why in HARD:
        if rx.search(c):
            return why
    for seg in re.split(r"\|\||&&|[;|\n&]", c):
        seg = seg.strip()
        if not seg:
            continue
        for m in re.finditer(r"(?<![0-9&])>>?\s*(?!&)(['\"]?)([^\s'\";|&]+)\1", seg):
            p = hit(os.path.realpath(m.group(2)) if m.group(2).startswith("/") else m.group(2), prot)
            if p:
                return f"writing to protected path {p} is not allowed"
        words = [w.strip("'\"") for w in seg.split()]
        while words and re.match(r"^\w+=", words[0]):
            words.pop(0)
        if words and words[0] in ("env", "nice", "ionice", "nohup", "time", "command", "stdbuf"):
            words = [w for w in words[1:] if not w.startswith("-")]
        if not words:
            continue
        prog = os.path.basename(words[0])
        args = [w for w in words[1:] if not w.startswith("-")]
        target = None
        if prog in DEST_LAST and args:
            target = hit(args[-1], prot)
        elif prog in ANY_ARG or (prog == "sed" and re.search(r"\s-\w*i|--in-place", seg)) \
                or (prog == "perl" and re.search(r"\s-\w*i", seg)):
            target = hit(" ".join(args), prot)
        elif prog == "git" and re.search(r"\b(checkout|reset|clean|restore)\b", seg):
            target = hit(seg, prot)
        elif re.search(r"open\([^)]*['\"][wa]b?\+?['\"]", seg) or "write_text" in seg or "write_bytes" in seg:
            target = hit(seg, prot)
        if target:
            return f"writing to protected path {target} is not allowed"
    return None


def check_path(path, prot):
    if not path:
        return None
    p = os.path.realpath(expand(path))
    for pr in prot:
        if p == pr or p.startswith(pr + "/"):
            return f"editing {pr} is not allowed"
    return None


def summarize(tool, ti):
    if tool == "Bash":
        return ti.get("command", "")[:400]
    for k in ("file_path", "path", "notebook_path", "url", "pattern", "query"):
        if k in ti:
            return f"{k}={str(ti[k])[:300]}"
    return json.dumps(ti)[:300]


def log(entry):
    try:
        d = MS / "reports" / "ai_audit"
        d.mkdir(parents=True, exist_ok=True)
        with open(d / time.strftime("%Y-%m.jsonl"), "a") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except OSError:
        pass


def main():
    try:
        data = json.load(sys.stdin)
    except Exception:
        return 0
    tool = data.get("tool_name", "")
    ti = data.get("tool_input", {}) or {}
    reason = None
    if tool == "Bash" or tool in ("Edit", "Write", "MultiEdit", "NotebookEdit"):
        prot = protected()
        if tool == "Bash":
            reason = check_bash(ti.get("command", ""), prot)
        else:
            reason = check_path(ti.get("file_path") or ti.get("notebook_path"), prot)
    log({"t": time.strftime("%Y-%m-%d %H:%M:%S"), "session": str(data.get("session_id", ""))[:8],
         "cwd": data.get("cwd", ""), "tool": tool, "what": summarize(tool, ti),
         "decision": "blocked" if reason else "passed", "reason": reason or ""})
    if reason:
        n = MS / "bin" / "notify.sh"
        if n.exists():
            subprocess.Popen([str(n), "AI command blocked", f"{reason}\n{summarize(tool, ti)[:150]}", "default", "shield"],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
        print(f"Blocked by my-system guard: {reason}. Explain what you need and let the user do it.", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
