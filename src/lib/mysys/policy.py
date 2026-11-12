"""Builds the Claude Code managed policy (/etc/claude-code/managed-settings.json) from config."""
import json
import os

from . import config

BASH_DENY = ["sudo *", "su *", "pkexec *", "apt *", "apt-get *", "dpkg *", "snap *", "flatpak *", "systemctl *",
             "kill *", "pkill *", "killall *", "reboot*", "shutdown*", "poweroff*", "chown *", "chmod -R *",
             "rm -rf /", "rm -rf /*", "rm -rf ~", "rm -rf ~/", "rm -rf ~/*", "rm -rf $HOME", "rm -rf $HOME/",
             "rm -rf $HOME/*", "find / *", "locate *", "crontab *", "mkfs*"]
# (was "rm -rf ~*", which also refused removing a folder inside a project written as ~/project/tmp;
#  rm stays in BASH_ASK, and the guard blocks writes/deletes in protected paths)
BASH_ASK = ["pip install *", "pip3 install *", "python3 -m pip install *", "npm install *", "npm i *",
            "cargo install *", "git push *", "git reset --hard*", "rm *", "mv *", "docker *", "conda install *"]


def rule_path(p):
    """Claude Code path syntax: //abs for absolute paths, ~/ for home."""
    h = str(config.HOME)
    p = os.path.expanduser(p).rstrip("/")
    if p == h or p.startswith(h + "/"):
        return "~" + p[len(h):]
    return "/" + p if p.startswith("/") else p


def build():
    prot = config.lst("protected_paths.txt")
    deny = [f"Bash({b})" for b in BASH_DENY]
    for p in prot:
        rp = rule_path(p)
        e = os.path.expanduser(p)
        is_file = os.path.isfile(e) or (not os.path.exists(e) and os.path.basename(e).startswith("."))
        deny.append(f"Edit({rp})" if is_file else f"Edit({rp}/**)")
    for secret in ["~/.ssh/**", "~/.gnupg/**", f"{rule_path(str(config.STATE))}/borg-key-backup.txt",
                   f"{rule_path(str(config.STATE))}/ntfy_topic", "~/.netrc", "~/.config/gh/hosts.yml"]:
        deny.append(f"Read({secret})")
    guard = str(config.MS / "ai" / "claude" / "hooks" / "guard.py")
    return {
        "permissions": {
            "defaultMode": "default",
            "disableBypassPermissionsMode": "disable",
            # harmless and wanted commands run without a question (the guard hook still checks every one of them)
            "allow": [f"Read({rule_path(str(config.MS))}/**)", "Bash(git status)", "Bash(git diff *)",
                      "Bash(git log *)", "Bash(ls *)", "Bash(cd *)", "Bash(pwd)", "Bash(mkdir -p *)",
                      "Bash(ms status)", "Bash(ms results * --dry-run)", "Bash(ms q)", "Bash(ms q add *)",
                      "Bash(ms q show *)", "Bash(ms q log *)", "Bash(ms q explain *)", "Bash(ms q edit *)",
                      "Bash(ms q --help)", "Bash(ms flow check *)", "Bash(ms flow run *)", "Bash(ms flow example)",
                      "Bash(ms log check *)", "Bash(ms log compare *)", "Bash(ms log diff *)", "Bash(ms capacity*)", "Bash(ms remote list)", "Bash(ms remote status*)",
                      "Bash(ms tools)", "Bash(ms paused)", "Bash(ms replay *)", "Bash(ms ai)", "Bash(ms ai *)",
                      "Bash(ms q wait *)", "Bash(ms q list*)", "Bash(ms q history*)", "Bash(ms remote usage*)",
                      "Bash(ms log show *)", "Bash(ms log list*)", "Bash(ms log template *)",
                      "Bash(ms version)"],
            "ask": [f"Bash({a})" for a in BASH_ASK],
            "deny": list(dict.fromkeys(deny)),
        },
        "hooks": {"PreToolUse": [{"matcher": "*", "hooks": [
            {"type": "command", "command": f"/usr/bin/python3 {guard}", "timeout": 10}]}],
                  # every new, resumed or compacted session starts with the live state and the run/log template
                  "SessionStart": [{"hooks": [
                      {"type": "command", "command": f"{config.MS / 'bin' / 'ms'} ai status --hook", "timeout": 15}]}]},
    }


def write(path):
    with open(path, "w") as f:
        json.dump(build(), f, indent=2)
