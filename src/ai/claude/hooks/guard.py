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
    (re.compile(r"(^|[;&|(\s/])ms\s+(pause|resume|unlock|lock)\b"), "only the user may pause, resume, unlock or lock my-system"),
    (re.compile(r"(^|[;&|(\s/])ms\s+tools\s+(allow|deny|default|reset)\b"),
     "only the user may change which tool versions a project may use"),
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


# A heredoc body is only skipped when it is plainly written to a file (cat/tee > file) and nothing on that
# command line could execute it (no pipe, no shell or interpreter anywhere on the line). Everything else is checked.
DATA_SINKS = {"cat", "tee"}
INTERP = {"bash", "sh", "zsh", "dash", "ksh", "fish", "ssh", "perl", "ruby", "node", "tclsh", "vivado", "xargs", "env",
          "nice", "nohup", "time", "timeout", "eval", "exec", "source", ".", "sudo", "su", "doas", "pkexec", "systemd-run",
          "docker", "podman", "flock", "setsid", "script", "watch", "parallel", "tsp", "ts", "vrun", "ms", "make"}
HEREDOC = re.compile(r"(?<!<)<<(-?)\s*(['\"]?)([A-Za-z_][\w.-]*)\2")


def _executes(line):
    if "|" in line.replace("||", "") or "`" in line or "$(" in line:
        return True
    for w in re.split(r"[\s;&()]+", line):
        b = os.path.basename(w.strip("'\""))
        if b in INTERP or b.startswith("python") or b.startswith("pypy"):
            return True
    return False


def strip_heredocs(cmd):
    """Drop heredoc bodies that are only file data (cat > f <<E ... E), so text inside a file being written
    never trips a check."""
    lines, out, i = cmd.split("\n"), [], 0
    while i < len(lines):
        line = lines[i]
        out.append(line)
        i += 1
        for m in HEREDOC.finditer(line):
            seg = re.split(r"\|\||&&|[;|&(]", line[:m.start()])[-1].split()
            seg = [w for w in seg if not re.match(r"^\w+=", w)]
            prog = os.path.basename(seg[0].strip("'\"")) if seg else ""
            body = []
            while i < len(lines):
                end = lines[i].lstrip("\t") if m.group(1) else lines[i]
                if end == m.group(3):
                    break
                body.append(lines[i])
                i += 1
            if i < len(lines):
                term = lines[i]
                i += 1
            else:
                term = None
            # the rest of the command (after the heredoc line); a body written to a file that is then run, or
            # followed by anything that executes, is checked like any other command
            rest = line[m.end():] + "\n" + "\n".join(lines[i:])
            target = re.search(r">>?\s*['\"]?([^\s'\";|&]+)", line)
            used_later = False
            if target:                      # the written file is itself run as a command (./x.sh, /tmp/x.sh)
                name = os.path.basename(target.group(1))
                for seg in re.split(r"\|\||&&|[;|&\n]", rest):
                    w = seg.strip().split()
                    if w and os.path.basename(w[0].strip("'\"")) == name:
                        used_later = True
            if prog not in DATA_SINKS or _executes(line) or _executes(rest) or used_later:
                out += body
            if term is not None:
                out.append(term)
    return "\n".join(out)


CWD = None                     # the folder the AI works in (from the hook input): per-project tool rules
SLURM = {"sbatch", "srun", "salloc", "scancel", "scontrol", "sattach", "sbcast"}


def remote_hosts():
    """ssh aliases and host names of the servers in config/remotes.conf (shared clusters)."""
    hosts = set()
    try:
        import configparser
        cp = configparser.ConfigParser(inline_comment_prefixes=("#",), interpolation=None)
        cp.read(MS / "config" / "remotes.conf")
        for s in cp.sections():
            for k in ("ssh", "head"):
                v = cp[s].get(k, "").strip()
                if v:
                    hosts.add(v.split("@")[-1])
            hosts.add(s)
    except Exception:
        pass
    try:                                   # real host names behind the aliases (~/.ssh/config)
        txt = open(os.path.join(HOME, ".ssh", "config")).read()
        cur = set()
        for line in txt.splitlines():
            w = line.strip().split()
            if len(w) >= 2 and w[0].lower() == "host":
                cur = set(w[1:])
            elif len(w) >= 2 and w[0].lower() == "hostname" and cur & hosts:
                hosts.add(w[1])
    except OSError:
        pass
    return {h for h in hosts if h and h not in ("localhost", "127.0.0.1")}


def check_remote(c):
    hosts = None
    for seg in re.split(r"\|\||&&|[;|\n&()]", c):
        w = [x.strip("'\"") for x in seg.split()]
        while w and (re.match(r"^\w+=", w[0]) or w[0] in ("env", "nice", "nohup", "time", "command", "exec", "timeout")):
            w.pop(0)
            while w and (w[0].startswith("-") or w[0].isdigit()):
                w.pop(0)
        if not w:
            continue
        prog = os.path.basename(w[0])
        if prog in SLURM:
            return "Slurm commands are not allowed: submit with `ms q add` (the queue sends jobs to the server itself)"
        if prog not in ("ssh", "scp", "rsync", "sftp", "mosh", "sshfs"):
            continue
        hosts = remote_hosts() if hosts is None else hosts
        for a in w[1:]:
            h = a.split("@")[-1].split(":")[0]
            if h in hosts:
                return (f"direct access to the shared server {h} is not allowed: submit with `ms q add` "
                        "(status: ms remote list / ms remote status)")
    return None


# heavy EDA tools: an AI may start them only through the my-system queue (ms q add / ms flow run / queue_submit)
HEAVY = {"vivado", "vitis", "vitis-run", "vitis_hls", "vivado_hls", "v++", "xtclsh", "xst", "ngdbuild", "par", "bitgen",
         "trce", "xsim", "xelab", "quartus_sh", "quartus_map", "quartus_fit", "quartus_sta", "ise", "planahead"}
QUICK = {"-version", "--version", "-help", "--help", "-h", "-v"}
SHELLS = {"bash", "sh", "zsh", "dash", "source", ".", "exec", "xvfb-run"}
SCRIPT_INVOKE = re.compile(r"(^|[\s/\"'`(\[,])(vivado|vitis-run|vitis_hls|vivado_hls|v\+\+|xtclsh|xst|ngdbuild|bitgen|"
                           r"quartus_sh|xelab|xsim|par)(?=[\s\"',)\]])[^\n]{0,40}?(-mode|-source|-f\b|--mode|\.tcl|"
                           r"-intstyle|-w\b|--config|-c\b|-R\b|-runall|-tclbatch|\$|\{)", re.M)


def _script_runs_heavy(path):
    """True when a script file (sh/tcl/py/Makefile) the AI wants to run starts a heavy EDA tool."""
    try:
        p = path if os.path.isabs(path) else os.path.join(CWD or os.getcwd(), path)
        if not os.path.isfile(p) or os.path.getsize(p) > 2_000_000:
            return False
        txt = open(p, errors="replace").read()
    except OSError:
        return False
    return bool(SCRIPT_INVOKE.search(txt))


def check_heavy(c):
    try:
        if config and str(config.get("AI_HEAVY_VIA_QUEUE") or "1") == "0":
            return None
    except Exception:
        pass
    msg = ("{what}: an AI assistant must start Vivado/Vitis/ISE runs through the my-system queue, not directly, so they "
           "do not take the CPU other queued jobs wait for. Use: ms q add --name NAME [--cores N --mem GB] -- <command> "
           "(add --wait to wait for the result), a flow (ms flow run), or the queue_submit tool")
    for seg in re.split(r"\|\||&&|[;|\n&()]", c):
        w = [x.strip("'\"") for x in seg.split()]
        while w and (re.match(r"^\w+=", w[0]) or w[0] in ("env", "nice", "ionice", "nohup", "time", "command",
                                                           "timeout", "stdbuf", "setsid", "taskset")):
            w.pop(0)
            while w and (w[0].startswith("-") or w[0].isdigit() or re.match(r"^\d+[smhd]?$", w[0])):
                w.pop(0)
        if not w:
            continue
        prog = os.path.basename(w[0])
        if prog == "ms":                    # ms q add ... / ms flow run ...: through the queue
            continue
        if prog in HEAVY:
            if any(a in QUICK for a in w[1:]) and not any(a in ("-mode", "-source", "-f") for a in w[1:]):
                continue
            return msg.format(what=f"running {prog} directly")
        args = [a for a in w[1:] if not a.startswith("-")]
        if prog in SHELLS or prog.endswith((".sh", ".tcl", ".py", ".bash")) or prog in ("python", "python3", "make",
                                                                                         "tclsh", "perl"):
            if any(os.path.basename(a) in HEAVY for a in args[:4]):
                return msg.format(what=f"running {next(os.path.basename(a) for a in args if os.path.basename(a) in HEAVY)} directly")
            scripts = ([w[0]] if prog.endswith((".sh", ".tcl", ".py", ".bash")) else []) + \
                      [a for a in args[:3] if a.endswith((".sh", ".tcl", ".py", ".bash")) or a == "Makefile"]
            if prog == "make":
                scripts.append("Makefile")
            for sc in scripts:
                if _script_runs_heavy(sc):
                    return msg.format(what=f"{os.path.basename(sc)} starts Vivado/Vitis/ISE")
    return None


def check_bash(cmd, prot):
    c = strip_heredocs(expand(cmd))
    for rx, why in HARD:
        if rx.search(c):
            return why
    why = check_remote(c) or check_heavy(c)
    if why:
        return why
    try:
        from mysys import versions
        v = versions.check_text(c, CWD)
        if v:
            return versions.message(v, CWD)
    except Exception:
        pass
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
    global CWD
    CWD = data.get("cwd") or os.getcwd()
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
