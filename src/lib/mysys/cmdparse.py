"""Split a shell command line into the simple commands it runs (quote-aware), for the AI guard and version checks.

  commands("cd hw && timeout -s KILL 2h vivado -mode batch", cwd)  ->
      [Cmd(argv=["cd", "hw"], ...), Cmd(argv=["vivado", "-mode", "batch"], cwd=".../hw", ...)]

Follows `bash -c STR`, `eval ...`, `$( ... )` / backticks, and strips wrappers (env, nice, nohup, timeout, flock,
xargs, stdbuf, setsid, taskset, unbuffer, time, command, exec, watch). Leading VAR=value assignments are kept.
Never raises: unparseable text falls back to splitting on whitespace.
"""
import os
import re
import shlex

SHELLS = {"bash", "sh", "zsh", "dash", "ksh"}
SEPS = {";", "&&", "||", "|", "&", "(", ")", "|&", ";;", "{", "}"}
# wrapper -> options that take a value (the wrapped command follows)
WRAPPERS = {"env": set(), "nice": {"-n"}, "ionice": {"-c", "-n"}, "nohup": set(), "time": set(), "command": set(),
            "exec": set(), "stdbuf": {"-i", "-o", "-e"}, "setsid": set(), "unbuffer": set(), "xvfb-run": {"-a", "-s"},
            "timeout": {"-s", "-k", "--signal", "--kill-after"}, "flock": {"-w", "-E", "-c"}, "watch": {"-n", "-d"},
            "xargs": {"-n", "-P", "-I", "-L", "-s", "-d", "-a", "-E"}, "taskset": set(), "chrt": set(), "nocache": set()}
ASSIGN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
SUBST = re.compile(r"\$\(([^()]*)\)|`([^`]*)`")


class Cmd:
    __slots__ = ("argv", "assign", "cwd")

    def __init__(self, argv, assign, cwd):
        self.argv, self.assign, self.cwd = argv, assign, cwd

    @property
    def prog(self):
        return os.path.basename(self.argv[0]) if self.argv else ""

    def __repr__(self):
        return f"Cmd({self.argv!r}, cwd={self.cwd!r})"


def _tokens(text):
    lex = shlex.shlex(text.replace("\n", " ; "), posix=True, punctuation_chars=";&|(){}")
    lex.whitespace_split = True
    try:
        return list(lex)
    except ValueError:                                     # unbalanced quotes
        return text.replace("\n", " ; ").split()


def _strip_wrappers(argv):
    """Remove wrapper programs and their options: ['timeout', '-s', 'KILL', '2h', 'vivado'] -> ['vivado']."""
    for _ in range(8):
        if not argv:
            return argv
        w = os.path.basename(argv[0])
        if w not in WRAPPERS:
            return argv
        i, takes = 1, WRAPPERS[w]
        while i < len(argv) and (argv[i].startswith("-") or (w == "env" and ASSIGN.match(argv[i]))):
            i += 2 if argv[i] in takes and "=" not in argv[i] else 1
        if w == "timeout" and i < len(argv):
            i += 1                                         # the duration
        elif w == "flock" and i < len(argv):
            i += 1                                         # the lock file
        elif w == "taskset" and i < len(argv) and not argv[i - 1].startswith("-c"):
            i += 1                                         # the CPU mask
        elif w == "nice" and i < len(argv) and re.fullmatch(r"-?\d+", argv[i]):
            i += 1
        argv = argv[i:]
    return argv


def commands(text, cwd=None, depth=0):
    """[Cmd] for every simple command in `text` (including inside bash -c, eval, $(...))."""
    out = []
    if not text or depth > 4:
        return out
    for m in SUBST.finditer(text):                         # command substitutions run too
        out += commands(m.group(1) or m.group(2) or "", cwd, depth + 1)
    cur, assign = [], {}
    cdir = cwd

    def flush():
        nonlocal cur, assign, cdir
        argv = _strip_wrappers(cur)
        if argv:
            prog = os.path.basename(argv[0])
            if prog == "cd" and len(argv) > 1 and cdir is not None:
                cdir = os.path.normpath(os.path.join(cdir, os.path.expanduser(argv[1])))
            out.append(Cmd(argv, dict(assign), cdir))
            if prog in SHELLS and "-c" in argv[1:]:
                k = argv.index("-c", 1)
                if k + 1 < len(argv):
                    out.extend(commands(argv[k + 1], cdir, depth + 1))
            elif prog == "eval":
                out.extend(commands(" ".join(argv[1:]), cdir, depth + 1))
        elif assign:
            out.append(Cmd([], dict(assign), cdir))
        cur, assign = [], {}

    for t in _tokens(text):
        if t in SEPS:
            flush()
            continue
        if not cur and ASSIGN.match(t):
            k, _, v = t.partition("=")
            assign[k] = v
            continue
        cur.append(t)
    flush()
    return out
