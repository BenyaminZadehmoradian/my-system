"""Which AMD/Xilinx tool versions each project may use (queue jobs and AI assistants).

Rules live in state/tool_access.json (edited in the GUI page "Tool access" or with `ms tools`):

  {"default": ["2025.2"],                                   # every project without its own rule
   "projects": {"/home/me/MCGEMA": ["2025.2", "14.7"]}}     # this folder and everything below it

A version "2025.2" also allows its updates (2025.2.1). ["*"] = every version. Before the file exists, the default
comes from XILINX_ALLOWED in my_system.conf (empty = no restriction).

A version that is not allowed for a project:
  - a queue job (in that folder) whose command names it is refused at once by ms q add;
  - a job that runs it anyway (e.g. from a script) is stopped within seconds, with the reason in ms q show;
  - the AI guard blocks shell commands that name it (judged by the folder the AI works in);
  - the AI system map lists the rules.
Only the user can change the rules: the AI guard blocks `ms tools allow|deny|default|reset`.
"""
import json
import os
import re
import sys

from . import config

FILE = config.STATE / "tool_access.json"
XILINX_LABELS = re.compile(r"vivado|vitis|ise\b|ise_|xilinx|sdk|petalinux|model_composer", re.I)
# a version-named folder of a Xilinx tool anywhere in a path: .../Vivado/2023.2/..., .../Vitis_HLS/2023.2, /opt/Xilinx/14.7
PATH_VER = re.compile(r"/(?:Xilinx|AMD|Vivado|Vitis|Vitis_HLS|Vivado_HLS|SDK|ISE|Apps|PetaLinux)/+(\d{4}\.\d(?:\.\d)?|1\d\.\d)(?=/|\s|$|['\"])",
                      re.I)
# settings scripts and tool names with a version in them: Vivado_2023.2, vitis_hls_2023.2
NAME_VER = re.compile(r"\b(?:vivado|vitis|vitis_hls|ise|sdk)[_-](\d{4}\.\d(?:\.\d)?|1\d\.\d)\b", re.I)


# ---------------------------------------------------------------- rules
def rules():
    try:
        r = json.loads(FILE.read_text())
        d = r["default"] if isinstance(r.get("default"), list) else ["*"]      # [] = nothing allowed (not "all")
        return {"default": list(d), "projects": {k: list(v) for k, v in (r.get("projects") or {}).items()}}
    except (OSError, ValueError):
        return {"default": config.words("XILINX_ALLOWED") or ["*"], "projects": {}}


def _from_ai():
    try:
        from . import watcher
        return watcher.ai_parent(os.getpid())
    except Exception:
        return None


def save(r):
    """Write the rules. Refused when an AI assistant started this process: only the user changes them."""
    who = _from_ai()
    if who:
        raise PermissionError(f"refused: {who} (an AI assistant) may not change tool access; the user does this in "
                              "My System > Tool access or with ms tools in their own terminal")
    FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(r, indent=1, sort_keys=True))
    os.replace(tmp, FILE)


def family(ver):
    """2025.2.1 -> 2025.2 (an update belongs to its release); 14.7 -> 14.7."""
    m = re.match(r"(\d{4}\.\d)", ver or "")
    return m.group(1) if m else ver


def project_of(cwd, r=None):
    """The project folder whose own rule applies to `cwd` (longest match), or None."""
    if not cwd:
        return None
    r = r or rules()
    c = os.path.realpath(os.path.expanduser(cwd))
    best = None
    for p in r["projects"]:
        rp = os.path.realpath(os.path.expanduser(p))
        if (c == rp or c.startswith(rp.rstrip("/") + "/")) and (best is None or len(rp) > len(best[1])):
            best = (p, rp)
    return best[0] if best else None


def allowed_for(cwd=None):
    r = rules()
    p = project_of(cwd, r)
    return r["projects"][p] if p else r["default"]


def active():
    r = rules()
    return r["default"] != ["*"] or any(v != ["*"] for v in r["projects"].values())


def allowed(ver, cwd=None, prefixes=None):
    pre = prefixes if prefixes is not None else allowed_for(cwd)
    if not ver or "*" in pre:
        return True
    return any(ver == p or ver.startswith(p + ".") for p in pre)


# ---------------------------------------------------------------- what is installed
def installs():
    """[(label, root, version)] of installed Xilinx/AMD tools."""
    try:
        from . import detect
        tools = detect.load_state().get("tools", [])
    except Exception:
        tools = []
    out = []
    for label, root in tools:
        if not XILINX_LABELS.search(label):
            continue
        m = re.search(r"(\d{4}\.\d(?:\.\d)?|1\d\.\d)", label + " " + root)
        out.append((label, os.path.realpath(root), m.group(1) if m else ""))
    return out


def projects():
    """Detected project folders (git repositories) plus folders that already have a rule."""
    try:
        from . import detect
        ps = list(detect.load_state().get("projects", []))
    except Exception:
        ps = []
    for p in rules()["projects"]:
        if p not in ps:
            ps.append(p)
    return sorted(set(ps), key=lambda p: os.path.basename(p.rstrip("/")).lower())


def resolve_project(name):
    """A project folder from a path or a (case-insensitive) folder name."""
    p = os.path.realpath(os.path.expanduser(name))
    if os.path.isdir(p):
        return p
    hits = [x for x in projects() if os.path.basename(x.rstrip("/")).lower() == name.lower()]
    if len(hits) == 1:
        return hits[0]
    if not hits:
        raise ValueError(f"no project named '{name}' (give its folder path; projects: ms tools)")
    raise ValueError(f"several projects named '{name}': " + ", ".join(hits) + " — give the folder path")


# ---------------------------------------------------------------- checks
def _check(text, pre, roots):
    for root, ver in roots:
        if root and root in text and not allowed(ver, prefixes=pre):
            return ver
    for rx in (PATH_VER, NAME_VER):
        for m in rx.finditer(text):
            if not allowed(m.group(1), prefixes=pre):
                return m.group(1)
    return None


RUN_VARS = re.compile(r"^(PATH|LD_LIBRARY_PATH|XILINX\w*|VIVADO\w*|VITIS\w*)$")
SETTINGS = re.compile(r"settings(32|64)?\.(c?sh)\b|\.settings64-")


def run_strings(text, cwd=None):
    """The parts of a command line that decide which tool runs: programs, `source`d files, settings64 scripts,
    and PATH / XILINX* values. File names that are only read or listed (ls, cat, diff, python x.py DIR) do not count."""
    try:
        from . import cmdparse
        cmds = cmdparse.commands(text, cwd)
    except Exception:
        return [text]
    out = []
    for cm in cmds:
        out += [v for k, v in cm.assign.items() if RUN_VARS.match(k)]
        a = cm.argv
        if not a:
            continue
        out.append(a[0])
        if cm.prog in ("source", ".") and len(a) > 1:
            out.append(a[1])
        elif cm.prog == "export":
            out += [x.split("=", 1)[1] for x in a[1:] if "=" in x and RUN_VARS.match(x.split("=", 1)[0])]
        elif cm.prog in ("bash", "sh", "zsh", "dash", "tclsh", "python", "python3", "perl") and len(a) > 1 \
                and not a[1].startswith("-"):
            out.append(a[1])                                # the script it runs
        out += [x for x in a[1:] if SETTINGS.search(x)]
    return out


def check_text(text, cwd=None):
    """Version that `text` (a command line) runs and that is not allowed in folder `cwd`, or None."""
    if not text or not active():
        return None
    pre = allowed_for(cwd)
    if "*" in pre:
        return None
    roots = [(root, ver) for _, root, ver in installs() if ver]
    for part in run_strings(text, cwd):
        v = _check(part, pre, roots)
        if v:
            return v
    return None


def check_proc(pid, cwd=None):
    """Version that process `pid` runs (its program or the script it runs) and that is not allowed, or None."""
    if not active():
        return None
    parts = []
    try:
        parts.append(os.readlink(f"/proc/{pid}/exe"))
    except OSError:
        pass
    try:
        args = [a.decode(errors="replace") for a in open(f"/proc/{pid}/cmdline", "rb").read().split(b"\0")[:3] if a]
    except OSError:
        args = []
    if args:
        parts.append(args[0])                               # the program
        if os.path.basename(args[0]) in ("sh", "bash", "dash", "zsh", "tclsh", "wish", "python", "python3", "perl") \
                and len(args) > 1 and not args[1].startswith("-"):
            parts.append(args[1])                           # the script it runs (Xilinx wrappers are shell scripts)
    if not parts:
        return None
    pre = allowed_for(cwd)
    if "*" in pre:
        return None
    roots = [(root, ver) for _, root, ver in installs() if ver]
    for part in parts:
        v = _check(part, pre, roots)
        if v:
            return v
    return None


def _show(pre):
    return "every version" if "*" in pre else " / ".join(pre) or "none"


def message(ver, cwd=None):
    p = project_of(cwd) if cwd else None
    where = f"for project {_home(p)}" if p else "for this project"
    return (f"AMD/Xilinx tools version {ver} is not allowed {where}; allowed here: {_show(allowed_for(cwd))} "
            f"(set by the user: My System > Tool access, or ms tools)")


def _home(p):
    h = str(config.HOME)
    return p.replace(h, "~", 1) if p and p.startswith(h) else p


def map_lines():
    """For the AI system map."""
    if not active():
        return []
    r = rules()
    vers = sorted({family(v) for _, _, v in installs() if v})
    out = ["", "## AMD/Xilinx tool versions per project (set by the user; blocked otherwise)",
           f"- Every project without its own rule: **{_show(r['default'])}** (an entry like 2025.2 includes its updates, "
           f"e.g. 2025.2.1)."]
    for p, pre in sorted(r["projects"].items()):
        out.append(f"- `{_home(p)}` and its subfolders: **{_show(pre)}**")
    out.append("- Installed: " + ", ".join(f"`{_home(root)}` ({ver})" for _, root, ver in installs()) + ".")
    bad = [v for v in vers if not allowed(v, prefixes=r["default"])]
    if bad:
        out.append(f"- Do not run, source or write scripts for {', '.join(bad)} outside the projects above that allow it; "
                   "never change these rules (only the user can).")
    return out


# ---------------------------------------------------------------- command line
HELP = """ms tools — which AMD/Xilinx tool versions each project may use

  ms tools                               the rules, the installed versions and the projects
  ms tools default VER [VER ...]         versions every project may use (e.g. 2025.2; * = all)
  ms tools allow PROJECT VER [VER ...]   give one project (name or folder) its own list, adding these versions
  ms tools deny PROJECT VER [VER ...]    remove versions from a project's own list
  ms tools reset PROJECT                 the project follows the default again

A version like 2025.2 includes its updates (2025.2.1). The same rules are in the app: My System > Tool access.
Only you can change them (AI assistants are blocked from it).
"""


def cli(a):
    r = rules()
    if not a or a[0] in ("list", "show"):
        print(f"default (every other project): {_show(r['default'])}")
        for p, pre in sorted(r["projects"].items()):
            print(f"{_home(p)}: {_show(pre)}")
        print("\ninstalled: " + (", ".join(f"{ver} ({_home(root)})" for _, root, ver in installs()) or "none found"))
        return 0
    if a[0] in ("-h", "--help", "help"):
        print(HELP)
        return 0
    cmd, rest = a[0], a[1:]
    try:
        if cmd == "default":
            if not rest:
                raise ValueError("usage: ms tools default VER [VER ...]")
            r["default"] = ["*"] if "*" in rest else [family(v) for v in rest]
        elif cmd in ("allow", "deny", "reset"):
            if not rest:
                raise ValueError(f"usage: ms tools {cmd} PROJECT ...")
            p = resolve_project(rest[0])
            if cmd == "reset":
                r["projects"].pop(p, None)
            else:
                if len(rest) < 2:
                    raise ValueError(f"usage: ms tools {cmd} PROJECT VER [VER ...]")
                cur = list(r["projects"][p] if p in r["projects"] else r["default"])
                if "*" in cur and cmd == "deny":                # "everything except ..." = all installed versions
                    cur = sorted({family(v) for _, _, v in installs() if v})
                vs = [family(v) for v in rest[1:]]
                if cmd == "allow":
                    cur = ["*"] if "*" in vs else [x for x in cur if x != "*"] + [v for v in vs if v not in cur]
                else:
                    cur = [x for x in cur if x not in vs]
                r["projects"][p] = cur
        else:
            print(HELP)
            return 2
    except ValueError as e:
        print(e)
        return 2
    try:
        save(r)
    except PermissionError as e:
        print(e)
        return 1
    try:
        from . import aictx
        aictx.write()                          # AI assistants see the new rules at once
    except Exception:
        pass
    return cli([])


if __name__ == "__main__":
    sys.exit(cli(sys.argv[1:]))
