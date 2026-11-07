"""Keeps my-system's own files from growing forever (run by the daily job: `ms housekeeping`).

  reports/notifications.log   rotated above NOTIFY_LOG_MAX_MB (3 gzipped copies kept)
  reports/ai_audit/*.jsonl    months older than AUDIT_KEEP_MONTHS removed
  reports/metrics.csv         rows older than 400 days removed
  state/queue/{logs,series,outputs}   files of jobs that were cleared from the queue
  state/history               per-minute record: gzip finished days, keep HISTORY_KEEP_DAYS
"""
import gzip
import os
import shutil
import time

from . import config


def rotate(path, max_mb, keep=3):
    try:
        if path.stat().st_size < max_mb * 2**20:
            return False
    except OSError:
        return False
    for i in range(keep, 0, -1):
        src = path.with_name(f"{path.name}.{i}.gz")
        if i == keep:
            src.unlink(missing_ok=True)
        elif src.exists():
            src.replace(path.with_name(f"{path.name}.{i + 1}.gz"))
    with open(path, "rb") as a, gzip.open(path.with_name(f"{path.name}.1.gz"), "wb") as b:
        shutil.copyfileobj(a, b)
    path.write_text("")
    return True


def run(verbose=False):
    done = []
    if rotate(config.REPORTS / "notifications.log", float(config.num("NOTIFY_LOG_MAX_MB") or 5)):
        done.append("notifications.log rotated")
    months = int(config.num("AUDIT_KEEP_MONTHS") or 12)
    lt = time.localtime()
    limit = (lt.tm_year * 12 + lt.tm_mon - 1) - months
    for f in (config.REPORTS / "ai_audit").glob("????-??.jsonl"):
        try:
            y, m = int(f.name[:4]), int(f.name[5:7])
        except ValueError:
            continue
        if y * 12 + m - 1 < limit:
            f.unlink()
            done.append(f"removed {f.name}")
    mf = config.REPORTS / "metrics.csv"
    try:
        rows = mf.read_text().splitlines()
        cut = time.strftime("%Y-%m-%d", time.localtime(time.time() - 400 * 86400))
        keep = rows[:1] + [r for r in rows[1:] if r[:10] >= cut]
        if len(keep) < len(rows):
            mf.write_text("\n".join(keep) + "\n")
            done.append(f"metrics.csv: {len(rows) - len(keep)} old rows removed")
    except OSError:
        pass
    try:
        from . import queue
        ids = {str(p.stem) for p in queue.JOBS.glob("*.json")}
        n = 0
        for d, pat in ((queue.LOGS, "*.log"), (queue.Q / "series", "*.csv"), (queue.Q / "outputs", "*.env"),
                       (queue.LOGS, "*.in")):
            for f in d.glob(pat):
                if f.stem.split(".")[0] not in ids and time.time() - f.stat().st_mtime > 86400:
                    f.unlink(missing_ok=True)
                    n += 1
        if n:
            done.append(f"{n} file(s) of cleared jobs removed")
    except (OSError, ImportError):
        pass
    try:                                           # temporary folders of server tests left by older versions
        from . import remote
        import shutil
        for d in remote.STATE.glob("ms-remote-test-*"):
            if d.is_dir() and time.time() - d.stat().st_mtime > 86400:
                shutil.rmtree(d, ignore_errors=True)
                done.append(f"removed {d.name}")
    except (OSError, ImportError):
        pass
    from . import history
    history.housekeeping()
    if verbose:
        print("\n".join(done) or "nothing to clean")
    return done
