"""File lineage: which run produced a file, which runs used it (by content fingerprint).

state/lineage.jsonl — {"sha", "path", "size", "role": produced|consumed, "job", "run", "flow", "t"}
`ms lineage FILE` answers "where did this bitstream come from, and what used it?"
"""
import glob
import hashlib
import json
import os
import time

from . import config

DB = config.STATE / "lineage.jsonl"
MAX_HASH = 4 * 2**30          # files larger than this are fingerprinted by size+mtime+head/tail


def fingerprint(path):
    st = os.stat(path)
    h = hashlib.sha256()
    with open(path, "rb") as f:
        if st.st_size <= MAX_HASH:
            for b in iter(lambda: f.read(1 << 20), b""):
                h.update(b)
        else:
            h.update(f"{st.st_size}".encode())
            h.update(f.read(1 << 24))
            f.seek(-(1 << 24), 2)
            h.update(f.read())
    return h.hexdigest()


def record(job, patterns, role, run_id=None):
    out = []
    for pat in patterns or []:
        for f in sorted(glob.glob(os.path.join(job["cwd"], pat), recursive=True))[:500]:
            if not os.path.isfile(f):
                continue
            try:
                out.append({"sha": fingerprint(f), "path": os.path.abspath(f), "size": os.path.getsize(f), "role": role,
                            "job": job["id"], "run": run_id, "flow": (job.get("flow") or {}).get("id"),
                            "name": job["name"], "t": int(time.time())})
            except OSError:
                continue
    if out:
        DB.parent.mkdir(parents=True, exist_ok=True)
        with open(DB, "a") as fh:
            for r in out:
                fh.write(json.dumps(r) + "\n")
    return out


def query(path):
    sha = fingerprint(path)
    produced, consumed = [], []
    try:
        for line in open(DB):
            try:
                r = json.loads(line)
            except ValueError:
                continue
            if r.get("sha") == sha:
                (produced if r["role"] == "produced" else consumed).append(r)
    except OSError:
        pass
    return sha, produced, consumed
