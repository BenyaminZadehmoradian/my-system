#!/usr/bin/python3
"""My System — desktop app of my-system: health, long runs and their results, storage,
projects, backups, AI guard, reports, inventory and notifications. Follows the desktop
light/dark setting. It only reads system state; buttons run the my-system commands."""
import csv
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

from PyQt6.QtCore import QPointF, QProcess, QProcessEnvironment, QRectF, QSize, Qt, QTimer, QUrl
from PyQt6.QtGui import (QBrush, QColor, QDesktopServices, QFont, QIcon, QLinearGradient, QPainter,
                         QPainterPath, QPalette, QPen)
from PyQt6.QtWidgets import (
    QAbstractItemView, QApplication, QCheckBox, QDialog, QFrame, QGridLayout, QHBoxLayout, QHeaderView,
    QLabel, QLineEdit, QListWidget, QListWidgetItem, QMainWindow, QMessageBox, QPlainTextEdit,
    QPushButton, QScrollArea, QSizePolicy, QSplitter, QStackedWidget, QTableWidget, QTableWidgetItem,
    QTextBrowser, QVBoxLayout, QWidget,
)

HOME = Path.home()
BASE = Path(os.environ.get("MY_SYSTEM", HOME / "My_System"))
REPORTS, SCRIPTS, STATE = BASE / "reports", BASE / "bin", BASE / "state"
sys.path.insert(0, str(BASE / "lib"))
from mysys import config as CFG, detect as DET, gitstat as GIT, probes as PR, watcher as W  # noqa: E402


# ======================= theme =======================
def is_dark():
    if os.environ.get("MY_SYSTEM_THEME") in ("dark", "light"):
        return os.environ["MY_SYSTEM_THEME"] == "dark"
    try:
        r = subprocess.run(["gsettings", "get", "org.gnome.desktop.interface", "color-scheme"],
                           capture_output=True, text=True, timeout=2)
        return "dark" in r.stdout
    except (OSError, subprocess.TimeoutExpired):
        return False


DARK = False
T = {}


def set_theme(dark):
    global DARK
    DARK = dark
    T.clear()
    if dark:
        T.update(bg="#1e1e1e", side="#242424", surface="#2d2d2d", surface2="#363636", border="#3d3d3d",
                 text="#f2f2f2", muted="#a0a0a0", accent="#78aeed", accent_bg="#3584e4",
                 ok="#57e389", warn="#f8e45c", warn2="#ffa348", bad="#ff7b63", grid="#3a3a3a")
    else:
        T.update(bg="#fafafa", side="#ebebed", surface="#ffffff", surface2="#f3f3f5", border="#dedede",
                 text="#1f1f1f", muted="#6e6e73", accent="#1c71d8", accent_bg="#3584e4",
                 ok="#26a269", warn="#c88800", warn2="#c64600", bad="#c01c28", grid="#ececec")


def stylesheet():
    return f"""
    QMainWindow, QWidget#page, QScrollArea, QScrollArea > QWidget > QWidget {{ background:{T['bg']}; color:{T['text']}; }}
    QWidget {{ color:{T['text']}; font-size:13px; }}
    QListWidget#nav {{ background:{T['side']}; border:none; padding:10px 8px; outline:0; }}
    QListWidget#nav::item {{ border-radius:8px; margin:1px 0; }}
    QListWidget#nav::item:selected {{ background:{T['surface2'] if DARK else '#dcdce0'}; }}
    QListWidget#nav::item:hover:!selected {{ background:{T['surface'] if DARK else '#e2e2e5'}; }}
    QFrame#card {{ background:{T['surface']}; border:1px solid {T['border']}; border-radius:12px; }}
    QLabel#h1 {{ font-size:22px; font-weight:700; }}
    QLabel#h2 {{ font-size:15px; font-weight:600; padding-top:6px; }}
    QLabel#muted, QLabel#cardTitle {{ color:{T['muted']}; }}
    QLabel#cardTitle {{ font-size:12px; font-weight:600; letter-spacing:0.3px; }}
    QLabel#cardValue {{ font-size:26px; font-weight:700; }}
    QLabel#cardDetail {{ color:{T['muted']}; font-size:12px; }}
    QLabel#pill {{ border-radius:10px; padding:3px 10px; font-size:12px; font-weight:600; }}
    QLabel#banner {{ border-radius:10px; padding:10px 14px; font-size:13px; }}
    QPushButton {{ background:{T['surface2']}; border:1px solid {T['border']}; border-radius:8px;
                  padding:6px 14px; font-weight:600; }}
    QPushButton:hover {{ border-color:{T['accent']}; }}
    QPushButton#primary {{ background:{T['accent_bg']}; color:white; border:none; }}
    QPushButton#primary:hover {{ background:{T['accent']}; }}
    QPushButton#danger {{ color:{T['bad']}; }}
    QPlainTextEdit, QTextBrowser, QLineEdit, QTableWidget, QListWidget#plain {{
        background:{T['surface']}; border:1px solid {T['border']}; border-radius:10px;
        selection-background-color:{T['accent_bg']}; selection-color:white; }}
    QLineEdit {{ padding:6px 10px; }}
    QTableWidget {{ gridline-color:{T['grid']}; alternate-background-color:{T['surface2']}; }}
    QHeaderView::section {{ background:{T['surface2']}; color:{T['muted']}; border:none;
        border-bottom:1px solid {T['border']}; padding:6px; font-weight:600; font-size:12px; }}
    QSplitter::handle {{ background:transparent; }}
    QScrollBar:vertical {{ background:transparent; width:10px; }}
    QScrollBar::handle:vertical {{ background:{T['border']}; border-radius:5px; min-height:30px; }}
    QScrollBar::add-line, QScrollBar::sub-line {{ height:0; }}
    QLabel#foot {{ color:{T['muted']}; font-size:11px; }}
    """


def tone(level):
    return {"ok": T["ok"], "warn": T["warn2"], "bad": T["bad"], "info": T["accent"]}.get(level, T["muted"])


# ======================= data helpers =======================
def read(path, default=""):
    try:
        return Path(path).read_text(errors="replace")
    except OSError:
        return default


def kv(path):
    out = {}
    for line in read(path).splitlines():
        k, _, v = line.partition("=")
        if k.strip():
            out[k.strip()] = v.strip().strip('"')
    return out


def human(n):
    for u in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1024:
            return f"{n:.0f} {u}" if u in ("B", "KB") else f"{n:.1f} {u}"
        n /= 1024
    return f"{n:.1f} PB"


def ago(ts):
    if not ts:
        return "never"
    d = time.time() - float(ts)
    if d < 90:
        return "just now"
    if d < 3600:
        return f"{int(d // 60)} min ago"
    if d < 172800:
        return f"{int(d // 3600)} h ago"
    return f"{int(d // 86400)} days ago"


def dur(sec):
    sec = int(sec)
    return f"{sec // 3600}h {sec % 3600 // 60:02d}m" if sec >= 3600 else f"{sec // 60}m {sec % 60:02d}s"


def disk(path):
    try:
        if path != "/" and not os.path.ismount(path):
            return None
        u = shutil.disk_usage(path)
        return u.used, u.free, u.total
    except OSError:
        return None


def report_days():
    if not REPORTS.is_dir():
        return []
    return sorted((p for p in REPORTS.iterdir() if p.is_dir() and not p.is_symlink() and p.name[:2] == "20"),
                  reverse=True)


def warnings_of(day):
    return [l for l in read(Path(day) / "summary.txt").splitlines() if l.startswith("WARN")]


def jsonl(path, limit=500):
    rows = []
    for line in read(path).splitlines()[-limit:]:
        try:
            rows.append(json.loads(line))
        except ValueError:
            pass
    return rows


def audit_rows(limit=800):
    d = REPORTS / "ai_audit"
    files = sorted(d.glob("*.jsonl")) if d.is_dir() else []
    rows = []
    for f in files[-2:]:
        rows += jsonl(f, limit)
    return rows[-limit:]


def metrics(days=7):
    return W.read_metrics(days)


def col(rows, key):
    out = []
    for r in rows:
        try:
            out.append(float(r[key]))
        except (KeyError, ValueError, TypeError):
            pass
    return out


# ======================= widgets =======================
class Chart(QWidget):
    """Smooth area/line chart drawn with QPainter (no extra dependencies)."""

    def __init__(self, height=46, threshold=None, fixed=None, axis=False):
        super().__init__()
        self.values, self.color = [], QColor("#3584e4")
        self.threshold, self.fixed, self.axis = threshold, fixed, axis
        self.setMinimumHeight(height)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed if not axis else QSizePolicy.Policy.Expanding)

    def set(self, values, color):
        self.values, self.color = values[-400:], QColor(color)
        self.update()

    def paintEvent(self, _e):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        r = QRectF(self.rect()).adjusted(2, 4, -2, -4)
        if self.axis:
            r.adjust(34, 4, -6, -16)
            p.setPen(QPen(QColor(T["grid"]), 1))
        if len(self.values) < 2:
            p.setPen(QColor(T["muted"]))
            p.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, "collecting data…")
            return
        lo, hi = self.fixed if self.fixed else (min(self.values), max(self.values))
        if hi - lo < 1e-9:
            lo, hi = lo - 1, hi + 1
        n = len(self.values)

        def pt(i, v):
            return QPointF(r.left() + r.width() * i / (n - 1), r.bottom() - r.height() * (v - lo) / (hi - lo))

        if self.axis:
            p.setFont(QFont(p.font().family(), 8))
            for k in range(5):
                y = r.top() + r.height() * k / 4
                p.setPen(QPen(QColor(T["grid"]), 1))
                p.drawLine(QPointF(r.left(), y), QPointF(r.right(), y))
                p.setPen(QColor(T["muted"]))
                p.drawText(QRectF(0, y - 8, 30, 16), Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
                           f"{hi - (hi - lo) * k / 4:.0f}")
        path = QPainterPath(pt(0, self.values[0]))
        for i in range(1, n):
            a, b = pt(i - 1, self.values[i - 1]), pt(i, self.values[i])
            mx = (a.x() + b.x()) / 2
            path.cubicTo(QPointF(mx, a.y()), QPointF(mx, b.y()), b)
        fill = QPainterPath(path)
        fill.lineTo(QPointF(r.right(), r.bottom()))
        fill.lineTo(QPointF(r.left(), r.bottom()))
        fill.closeSubpath()
        g = QLinearGradient(0, r.top(), 0, r.bottom())
        c1, c2 = QColor(self.color), QColor(self.color)
        c1.setAlpha(90 if DARK else 70)
        c2.setAlpha(0)
        g.setColorAt(0, c1)
        g.setColorAt(1, c2)
        p.fillPath(fill, QBrush(g))
        p.setPen(QPen(self.color, 2))
        p.drawPath(path)
        if self.threshold is not None and lo <= self.threshold <= hi:
            y = pt(0, self.threshold).y()
            pen = QPen(QColor(T["bad"]), 1, Qt.PenStyle.DashLine)
            p.setPen(pen)
            p.drawLine(QPointF(r.left(), y), QPointF(r.right(), y))
        last = pt(n - 1, self.values[-1])
        p.setBrush(self.color)
        p.setPen(Qt.PenStyle.NoPen)
        p.drawEllipse(last, 3.5, 3.5)


class Card(QFrame):
    def __init__(self, title, chart=False, threshold=None, fixed=None):
        super().__init__()
        self.setObjectName("card")
        lay = QVBoxLayout(self)
        lay.setContentsMargins(16, 14, 16, 12)
        lay.setSpacing(2)
        self.t = QLabel(title.upper())
        self.t.setObjectName("cardTitle")
        self.v = QLabel("–")
        self.v.setObjectName("cardValue")
        self.d = QLabel("")
        self.d.setObjectName("cardDetail")
        self.d.setWordWrap(True)
        lay.addWidget(self.t)
        lay.addWidget(self.v)
        lay.addWidget(self.d)
        self.chart = Chart(threshold=threshold, fixed=fixed) if chart else None
        if self.chart:
            lay.addSpacing(4)
            lay.addWidget(self.chart)
        lay.addStretch()

    def set(self, value, detail="", level=None, series=None):
        self.v.setText(value)
        self.v.setStyleSheet(f"color:{tone(level)};" if level else "")
        self.d.setText(detail)
        if self.chart is not None and series is not None:
            self.chart.set(series, tone(level) if level in ("warn", "bad") else T["accent_bg"])


def pill(text, level):
    lb = QLabel(text)
    lb.setObjectName("pill")
    c = QColor(tone(level))
    lb.setStyleSheet(f"background:rgba({c.red()},{c.green()},{c.blue()},40); color:{tone(level)};")
    return lb


def set_pill(lb, text, level):
    c = QColor(tone(level))
    lb.setText(text)
    lb.setStyleSheet(f"background:rgba({c.red()},{c.green()},{c.blue()},40); color:{tone(level)};")


def banner(lb, text, level):
    c = QColor(tone(level))
    lb.setText(text)
    lb.setStyleSheet(f"background:rgba({c.red()},{c.green()},{c.blue()},30); "
                     f"border:1px solid rgba({c.red()},{c.green()},{c.blue()},90);")


def btn(text, slot, kind=None):
    b = QPushButton(text)
    if kind:
        b.setObjectName(kind)
    b.setCursor(Qt.CursorShape.PointingHandCursor)
    b.clicked.connect(slot)
    return b


def mono(w):
    f = QFont("monospace")
    f.setStyleHint(QFont.StyleHint.Monospace)
    f.setPointSize(10)
    w.setFont(f)
    return w


def table(headers, stretch=0):
    t = QTableWidget(0, len(headers))
    t.setHorizontalHeaderLabels(headers)
    t.verticalHeader().hide()
    t.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
    t.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
    t.setAlternatingRowColors(True)
    t.setShowGrid(False)
    t.verticalHeader().setDefaultSectionSize(30)
    h = t.horizontalHeader()
    for i in range(len(headers)):
        h.setSectionResizeMode(i, QHeaderView.ResizeMode.Stretch if i == stretch else QHeaderView.ResizeMode.ResizeToContents)
    return t


def cell(text, level=None, bold=False):
    it = QTableWidgetItem(str(text))
    if level:
        it.setForeground(QColor(tone(level)))
    if bold:
        f = it.font()
        f.setBold(True)
        it.setFont(f)
    return it


def header(title, subtitle=""):
    box = QVBoxLayout()
    h = QLabel(title)
    h.setObjectName("h1")
    box.addWidget(h)
    if subtitle:
        s = QLabel(subtitle)
        s.setObjectName("muted")
        s.setWordWrap(True)
        box.addWidget(s)
    return box


class Page(QWidget):
    title = ""
    icon = ""
    fallback = ""

    def __init__(self, win):
        super().__init__()
        self.setObjectName("page")
        self.win = win

    def refresh(self):
        pass

    def nav_status(self):
        """(subtitle, level) shown under the sidebar entry."""
        return "", None


# ======================= pages =======================
class OverviewPage(Page):
    title, icon, fallback = "Overview", "utilities-system-monitor", "computer"

    def __init__(self, win):
        super().__init__(win)
        lay = QVBoxLayout(self)
        lay.setSpacing(14)
        top = QHBoxLayout()
        self.status = QLabel()
        self.status.setObjectName("h1")
        self.sub = QLabel()
        self.sub.setObjectName("muted")
        tl = QVBoxLayout()
        tl.addWidget(self.status)
        tl.addWidget(self.sub)
        top.addLayout(tl, 1)
        top.addWidget(btn("Open folder", lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(str(BASE)))))
        top.addWidget(btn("Run health check", win.run_check, "primary"))
        lay.addLayout(top)

        g = QGridLayout()
        g.setSpacing(12)
        self.c = {
            "ssd": Card("Root disk  /", True, threshold=CFG.num("SSD_WARN_PCT")),
            "mem": Card("Memory available", True),
            "temp": Card("CPU temperature", True, threshold=80),
            "load": Card("CPU load", True),
            "data": Card("Data disk"),
            "share": Card("Network share"),
            "backup": Card("Backup"),
            "jobs": Card("EDA jobs running"),
        }
        for i, k in enumerate(self.c):
            g.addWidget(self.c[k], i // 4, i % 4)
        lay.addLayout(g)

        h = QLabel("Needs attention")
        h.setObjectName("h2")
        lay.addWidget(h)
        self.att = QVBoxLayout()
        self.att.setSpacing(6)
        lay.addLayout(self.att)
        lay.addStretch()

    def refresh(self):
        m = metrics(7)
        d = disk("/")
        if d:
            pct = d[0] / d[2] * 100
            lv = "ok" if pct < 90 else "warn" if pct < 93 else "bad"
            fc = W.forecast_text()
            self.c["ssd"].set(f"{pct:.0f}%", f"{human(d[1])} free of {human(d[2])}\n{fc}", lv, col(m, "ssd_used_pct"))
        mi = PR.meminfo()
        if mi:
            av = mi.get("MemAvailable", 0)
            sw = mi.get("SwapTotal", 0) - mi.get("SwapFree", 0)
            self.c["mem"].set(human(av), f"of {human(mi['MemTotal'])} · swap used {human(sw)}",
                              "ok" if av > 8 * 2**30 else "warn", col(m, "mem_avail_gb"))
        t = PR.cpu_temp()
        self.c["temp"].set(f"{t} °C" if t is not None else "n/a", "fans handle up to ~85 °C",
                           None if t is None else "ok" if t < 82 else "warn" if t < 90 else "bad", col(m, "cpu_temp"))
        try:
            l1, l5, _ = os.getloadavg()
        except OSError:
            l1 = l5 = 0
        cpus = os.cpu_count() or 1
        self.c["load"].set(f"{l1 / cpus * 100:.0f}%", f"load {l1:.1f} / {cpus} threads (5-min {l5:.1f})",
                           "ok", col(m, "load1"))
        for key, lst in (("data", CFG.words("DATA_DISKS")), ("share", CFG.words("NET_SHARES"))):
            path = lst[0] if lst else ""
            if not path:
                self.c[key].set("none", "not configured")
                continue
            dd = disk(path)
            if dd:
                self.c[key].set(f"{human(dd[1])} free", f"{dd[0] / dd[2] * 100:.0f}% of {human(dd[2])} used · {path}", "ok")
            else:
                self.c[key].set("not mounted", path, "bad")
        b = kv(STATE / "backup_status")
        ls = float(b.get("last_success", 0) or 0)
        st, txt = CFG.backup_state()
        tone = {"ok": "ok", "running": "info", "first": "info", "off": "warn"}.get(st, "bad")
        self.c["backup"].set("running…" if st == "running" else ago(ls) if ls else txt.split(" (")[0], txt, tone)
        jobs = W.jobs()
        self.c["jobs"].set(str(len(jobs)), ", ".join(sorted({j["tool"] for j in jobs})) or "idle", "info" if jobs else None)
        self.c["jobs"].t.setText("TRACKED JOBS RUNNING")

        # attention list
        while self.att.count():
            w = self.att.takeAt(0).widget()
            if w:
                w.deleteLater()
        items = []
        days = report_days()
        if days:
            items += [(w_[6:], "warn") for w_ in warnings_of(days[0])]
        if Path("/var/run/reboot-required").exists() and not any("reboot" in i[0] for i in items):
            items.append(("Reboot required to finish installing updates", "warn"))
        blocked = [r for r in audit_rows(300) if r.get("decision") == "blocked"
                   and time.time() - time.mktime(time.strptime(r["t"], "%Y-%m-%d %H:%M:%S")) < 86400]
        if blocked:
            items.append((f"{len(blocked)} AI command(s) blocked in the last 24 h — see AI guard", "bad"))
        if not items:
            items = [("Nothing — everything looks fine.", "ok")]
        for text, lv in items:
            lb = QLabel(("●  " if lv != "ok" else "✓  ") + text)
            lb.setObjectName("banner")
            lb.setWordWrap(True)
            banner(lb, lb.text(), lv)
            self.att.addWidget(lb)
        bad = [i for i in items if i[1] != "ok"]
        self.status.setText("All good" if not bad else f"{len(bad)} thing{'s' if len(bad) > 1 else ''} need{'' if len(bad) > 1 else 's'} attention")
        self.status.setStyleSheet(f"color:{tone('ok' if not bad else 'warn')};")
        last = (days[0] / "summary.txt").stat().st_mtime if days and (days[0] / "summary.txt").exists() else 0
        self.sub.setText(f"{CFG.get('MACHINE_LABEL')} · {PR.os_name()} · kernel {os.uname().release} · last full check {ago(last)}")

    def nav_status(self):
        days = report_days()
        n = len(warnings_of(days[0])) if days else 0
        return (f"{n} warning{'s' if n != 1 else ''}" if n else "all good"), ("warn" if n else "ok")


class RunsPage(Page):
    title, icon, fallback = "Runs", "system-run", "media-playback-start"

    def __init__(self, win):
        super().__init__(win)
        lay = QVBoxLayout(self)
        lay.addLayout(header("Runs", "Long jobs recognised by the plugins (Vivado, HLS, simulators, …). Failures are "
                                     "reported at once, finished batches in one summary. Tip: queue jobs with  tsp vrun <command>"))
        h = QLabel("Running now")
        h.setObjectName("h2")
        lay.addWidget(h)
        self.live = table(["Tool", "Folder", "Started", "Running for", "CPU cores", "RAM", "PID"], 1)
        self.live.setMaximumHeight(200)
        lay.addWidget(self.live)
        row = QHBoxLayout()
        row.addWidget(btn("Open folder", self.open_folder))
        row.addWidget(btn("Open log", self.open_log))
        row.addStretch()
        lay.addLayout(row)
        h2 = QLabel("Queue (tsp)")
        h2.setObjectName("h2")
        lay.addWidget(h2)
        self.queue = mono(QPlainTextEdit())
        self.queue.setReadOnly(True)
        self.queue.setMaximumHeight(110)
        lay.addWidget(self.queue)
        h3 = QLabel("Finished")
        h3.setObjectName("h2")
        lay.addWidget(h3)
        self.hist = table(["When", "Tool / command", "Folder", "Duration", "Result"], 2)
        lay.addWidget(self.hist, 1)
        self.picked = None
        self.live.itemSelectionChanged.connect(lambda: self._pick(self.live, 1))
        self.hist.itemSelectionChanged.connect(lambda: self._pick(self.hist, 2))

    def _pick(self, t, c):
        it = t.item(t.currentRow(), c) if t.currentRow() >= 0 else None
        self.picked = it.data(Qt.ItemDataRole.UserRole) if it else self.picked

    def refresh(self):
        jobs = W.jobs()
        cores = {k: v.get("cores") for k, v in W.load_state().get("runs", {}).items()}
        self.live.setRowCount(len(jobs))
        now = time.time()
        for i, j in enumerate(sorted(jobs, key=lambda j: j["start"])):
            self.live.setItem(i, 0, cell(j["tool"], "info", True))
            it = cell(j["cwd"])
            it.setData(Qt.ItemDataRole.UserRole, j)
            self.live.setItem(i, 1, it)
            self.live.setItem(i, 2, cell(time.strftime("%a %H:%M", time.localtime(j["start"]))))
            self.live.setItem(i, 3, cell(dur(now - j["start"])))
            c_ = cores.get(j["key"])
            self.live.setItem(i, 4, cell("–" if c_ is None else f"{c_:.1f}"))
            self.live.setItem(i, 5, cell(human(j["rss"])))
            self.live.setItem(i, 6, cell(", ".join(map(str, j["pids"][:3]))))
        if shutil.which("tsp"):
            r = subprocess.run(["tsp"], capture_output=True, text=True)
            self.queue.setPlainText(r.stdout.strip() or "queue empty")
        else:
            self.queue.setPlainText("task-spooler not installed")
        rows = list(reversed(jsonl(REPORTS / "runs.jsonl", 200)))
        self.hist.setRowCount(len(rows))
        for i, r in enumerate(rows):
            if r.get("via") == "vrun":
                res, lv = ("OK", "ok") if r.get("rc") == 0 else (f"exit {r.get('rc')}", "bad")
                what = r.get("cmd", "")
            else:
                e = r.get("errors")
                res, lv = ("no errors in log", "ok") if e == 0 else (f"{e} ERROR(s)", "bad") if e else ("ended", None)
                what = r.get("tool", "")
            self.hist.setItem(i, 0, cell(r.get("t", "")))
            self.hist.setItem(i, 1, cell(what))
            it = cell(r.get("cwd", ""))
            it.setData(Qt.ItemDataRole.UserRole, r)
            self.hist.setItem(i, 2, it)
            self.hist.setItem(i, 3, cell(dur(r.get("seconds", 0))))
            self.hist.setItem(i, 4, cell(res, lv, True))

    def _selected(self):
        if self.picked is None:
            QMessageBox.information(self, "Runs", "Select a run first.")
        return self.picked

    def open_folder(self):
        j = self._selected()
        if j:
            QDesktopServices.openUrl(QUrl.fromLocalFile(j["cwd"]))

    def open_log(self):
        j = self._selected()
        if not j:
            return
        for n in ("vivado.log", "vitis_hls.log", f"{j.get('tool', '')}.log"):
            f = Path(j["cwd"]) / n
            if f.exists():
                QDesktopServices.openUrl(QUrl.fromLocalFile(str(f)))
                return
        if j.get("log"):
            QDesktopServices.openUrl(QUrl.fromLocalFile(j["log"]))

    def nav_status(self):
        n = len(W.jobs())
        return (f"{n} running" if n else "idle"), ("info" if n else None)


class StoragePage(Page):
    title, icon, fallback = "Storage", "drive-harddisk", "drive-harddisk"

    def __init__(self, win):
        super().__init__(win)
        lay = QVBoxLayout(self)
        lay.addLayout(header("Storage", "Root-disk usage over 30 days (dashed line = alert level) and the biggest "
                                        "folders. Folders on the root disk can be moved to the data disk with a link left behind."))
        self.fc = QLabel()
        self.fc.setObjectName("banner")
        lay.addWidget(self.fc)
        card = QFrame()
        card.setObjectName("card")
        cl = QVBoxLayout(card)
        self.chart = Chart(height=180, threshold=CFG.num("SSD_WARN_PCT"), axis=True)
        cl.addWidget(self.chart)
        lay.addWidget(card)
        self.tab = table(["Folder", "Size", "7-day change", "Disk", ""], 0)
        lay.addWidget(self.tab, 1)

    def refresh(self):
        m = metrics(30)
        ser = col(m, "ssd_used_pct")
        self.chart.fixed = (max(0, min(ser) - 5), 100) if ser else None
        self.chart.set(ser, T["accent_bg"])
        days, slope, n = W.forecast()
        txt = "Root disk forecast: " + W.forecast_text()
        banner(self.fc, txt, "bad" if days is not None and days < 14 else "ok" if slope is not None else "info")

        sizes = {}
        if (REPORTS / "sizes.csv").exists():
            with open(REPORTS / "sizes.csv") as f:
                for r in csv.DictReader(f):
                    sizes.setdefault(r["folder"], []).append(r)
        rows = []
        for folder, hist in sizes.items():
            cur = hist[-1]
            old = next((h for h in reversed(hist)
                        if time.mktime(time.strptime(h["date"], "%Y-%m-%d")) <= time.time() - 6.5 * 86400), None)
            ch = float(cur["gb"]) - float(old["gb"]) if old else None
            rows.append((folder, float(cur["gb"]), ch, cur["disk"], cur.get("kind", "dir")))
        rows.sort(key=lambda r: -r[1])
        self.tab.setRowCount(len(rows))
        for i, (folder, gb, ch, dsk, kind) in enumerate(rows):
            self.tab.setItem(i, 0, cell(folder + ("  →  HDD link" if kind == "link" else "")))
            self.tab.setItem(i, 1, cell(f"{gb:.1f} GB", None, True))
            self.tab.setItem(i, 2, cell("–" if ch is None else f"{ch:+.1f} GB",
                                        None if ch is None else "warn" if ch > 3 else None))
            self.tab.setItem(i, 3, cell(dsk, "warn" if dsk == "root" and gb > 20 else None))
            if dsk == "root" and folder.startswith("~/") and folder not in ("~/snap", "~/.cache") \
                    and CFG.get("BIG_FOLDER_DEST") and kind != "link":
                b = btn("Move to data disk", lambda _=False, f=folder: self.move(f))
                self.tab.setCellWidget(i, 4, b)
            else:
                self.tab.removeCellWidget(i, 4)
                self.tab.setItem(i, 4, cell(""))
        if not rows:
            self.tab.setRowCount(1)
            self.tab.setItem(0, 0, cell("Folder sizes appear after the next daily check."))

    def move(self, folder):
        path = os.path.expanduser(folder)
        if QMessageBox.question(self, "Move to data disk",
                                f"Move {folder} to {CFG.get('BIG_FOLDER_DEST')}/ and leave a link in its place?\n\n"
                                "It is refused automatically if any program is using it.") \
                != QMessageBox.StandardButton.Yes:
            return
        self.win.run_in_dialog(f"Moving {folder}", [str(SCRIPTS / "move_to_hdd.sh"), path, "--yes"])

    def nav_status(self):
        d = disk("/")
        if not d:
            return "", None
        pct = d[0] / d[2] * 100
        return f"root {pct:.0f}%", ("ok" if pct < 90 else "warn" if pct < 93 else "bad")


class BackupPage(Page):
    title, icon, fallback = "Backup", "document-save", "drive-removable-media"

    def __init__(self, win):
        super().__init__(win)
        lay = QVBoxLayout(self)
        lay.addLayout(header("Backup", "Encrypted borg backup, every night at 02:13. "
                                       "A restore test runs on the 1st of each month."))
        g = QGridLayout()
        g.setSpacing(12)
        self.c_last = Card("Last successful backup")
        self.c_new = Card("New data last time")
        self.c_test = Card("Restore test")
        for i, c in enumerate((self.c_last, self.c_new, self.c_test)):
            g.addWidget(c, 0, i)
        lay.addLayout(g)
        self.info = QLabel()
        self.info.setObjectName("muted")
        self.info.setWordWrap(True)
        self.info.setTextFormat(Qt.TextFormat.RichText)
        lay.addWidget(self.info)
        row = QHBoxLayout()
        row.addWidget(btn("Back up now", self.backup_now, "primary"))
        row.addWidget(btn("Run restore test", self.verify_now))
        row.addWidget(btn("List archives", self.list_archives))
        row.addWidget(btn("Browse / restore files (Vorta)", self.vorta))
        row.addStretch()
        lay.addLayout(row)
        self.out = mono(QPlainTextEdit())
        self.out.setReadOnly(True)
        lay.addWidget(self.out, 1)

    @staticmethod
    def active(unit):
        r = subprocess.run(["systemctl", "--user", "is-active", unit], capture_output=True, text=True)
        return r.stdout.strip() in ("active", "activating")

    def refresh(self):
        b = kv(STATE / "backup_status")
        v = kv(STATE / "verify_status")
        conf = kv(BASE / "config/backup.conf")
        running = self.active("my-system-backup.service")
        ls = float(b.get("last_success", 0) or 0)
        st, txt = CFG.backup_state()
        running = running or st == "running"
        if st == "off":
            self.c_last.set("not set up", "run: ms backup-setup", "warn")
        elif st in ("running", "first"):
            self.c_last.set("running…" if running else "no backup yet", txt, "info")
        else:
            self.c_last.set(ago(ls), txt, "ok" if st == "ok" else "bad")
        self.c_new.set(b.get("last_size", "–") or "–", b.get("last_archive", ""))
        if v:
            self.c_test.set(v.get("last_result", "?"), f"{ago(v.get('last_run'))} · {v.get('detail', '')}",
                            "ok" if v.get("last_result") == "ok" else "bad")
        else:
            self.c_test.set("not yet", "first test on the 1st of the month")
        srcs = [l for l in read(BASE / "config/backup_sources.txt").splitlines() if l.strip() and not l.startswith("#")]
        self.info.setText(f"Repository <code>{CFG.backup_repo() or 'not set (ms backup-setup)'}</code> (encrypted) · keeps "
                          f"{conf.get('KEEP_DAILY', '7')} daily, {conf.get('KEEP_WEEKLY', '4')} weekly, "
                          f"{conf.get('KEEP_MONTHLY', '6')} monthly<br>Folders: {' · '.join(srcs)}")
        if running or not self.out.toPlainText():
            logs = sorted((REPORTS / "backup").glob("*.log"), key=lambda p: p.stat().st_mtime)
            if logs:
                self.out.setPlainText(read(logs[-1])[-20000:])
                self.out.verticalScrollBar().setValue(self.out.verticalScrollBar().maximum())

    def backup_now(self):
        subprocess.run(["systemctl", "--user", "start", "--no-block", "my-system-backup.service"])
        self.out.setPlainText("Backup started in the background (lowest priority)…")
        QTimer.singleShot(2000, self.refresh)

    def verify_now(self):
        subprocess.run(["systemctl", "--user", "start", "--no-block", "my-system-verify.service"])
        self.out.setPlainText("Restore test started in the background — result appears here and on your phone.")

    def list_archives(self):
        conf = kv(BASE / "config/backup.conf")
        env = {"BORG_REPO": CFG.backup_repo(), "BORG_PASSCOMMAND": "secret-tool lookup " + conf.get("KEYRING_ATTR", "")}
        self.out.setPlainText("Reading archive list…")
        self.win.run_process(["borg", "list"], self.out, env)

    def vorta(self):
        if shutil.which("vorta"):
            subprocess.Popen(["vorta"], start_new_session=True)
        else:
            QMessageBox.information(self, "Vorta", "Vorta is not installed (sudo apt install vorta).")

    def nav_status(self):
        b = kv(STATE / "backup_status")
        if not b:
            return "not set up", "warn"
        ls = float(b.get("last_success", 0) or 0)
        if self.active("my-system-backup.service"):
            return "running…", "info"
        return ago(ls), ("ok" if ls and time.time() - ls < 48 * 3600 else "bad")


class GuardPage(Page):
    title, icon, fallback = "AI guard", "security-high", "dialog-password"

    def __init__(self, win):
        super().__init__(win)
        lay = QVBoxLayout(self)
        lay.addLayout(header("AI guard", "Every tool call an AI makes through Claude Code is checked and logged. "
                                         "Writes to protected paths (system, toolchains, environments, shell start-up "
                                         "files, my-system) are blocked."))
        row = QHBoxLayout()
        self.p_lock, self.p_pol, self.p_hook = pill("", None), pill("", None), pill("", None)
        for p in (self.p_lock, self.p_pol, self.p_hook):
            row.addWidget(p)
        row.addStretch()
        row.addWidget(btn("Show AI rules", self.show_rules))
        lay.addLayout(row)
        split = QSplitter(Qt.Orientation.Vertical)
        top = QWidget()
        tl = QVBoxLayout(top)
        tl.setContentsMargins(0, 0, 0, 0)
        fr = QHBoxLayout()
        h = QLabel("AI activity")
        h.setObjectName("h2")
        fr.addWidget(h)
        fr.addStretch()
        self.only_blocked = QCheckBox("only blocked")
        self.only_blocked.toggled.connect(self.refresh)
        self.filter = QLineEdit()
        self.filter.setPlaceholderText("search…")
        self.filter.setMaximumWidth(220)
        self.filter.textChanged.connect(self.refresh)
        fr.addWidget(self.only_blocked)
        fr.addWidget(self.filter)
        tl.addLayout(fr)
        self.audit = table(["Time", "Tool", "What", "Result"], 2)
        tl.addWidget(self.audit)
        bot = QWidget()
        bl = QVBoxLayout(bot)
        bl.setContentsMargins(0, 0, 0, 0)
        h2 = QLabel("Watched files")
        h2.setObjectName("h2")
        bl.addWidget(h2)
        self.watch = table(["File", "Status"], 0)
        bl.addWidget(self.watch)
        bl.addWidget(btn("I changed these on purpose — accept current state", self.accept))
        split.addWidget(top)
        split.addWidget(bot)
        split.setSizes([420, 260])
        lay.addWidget(split, 1)

    def refresh(self):
        locked = SCRIPTS.exists() and SCRIPTS.stat().st_uid == 0
        pol = Path("/etc/claude-code/managed-settings.json")
        hook = "guard.py" in read(pol)
        set_pill(self.p_lock, "My_System locked" if locked else "My_System UNLOCKED", "ok" if locked else "bad")
        set_pill(self.p_pol, "Claude policy installed" if pol.exists() else "Claude policy MISSING", "ok" if pol.exists() else "bad")
        set_pill(self.p_hook, "command guard active" if hook else "command guard MISSING", "ok" if hook else "bad")

        rows = list(reversed(audit_rows()))
        if self.only_blocked.isChecked():
            rows = [r for r in rows if r.get("decision") == "blocked"]
        q = self.filter.text().strip().lower()
        if q:
            rows = [r for r in rows if q in json.dumps(r).lower()]
        rows = rows[:400]
        self.audit.setRowCount(len(rows))
        for i, r in enumerate(rows):
            bl = r.get("decision") == "blocked"
            self.audit.setItem(i, 0, cell(r.get("t", "")))
            self.audit.setItem(i, 1, cell(r.get("tool", "")))
            it = cell(r.get("what", ""))
            it.setToolTip(f"{r.get('what', '')}\n\nfolder: {r.get('cwd', '')}\n{r.get('reason', '')}")
            self.audit.setItem(i, 2, it)
            self.audit.setItem(i, 3, cell("BLOCKED" if bl else "passed", "bad" if bl else "ok", bl))

        base = {}
        for line in read(STATE / "watched.sha256").splitlines():
            s, _, f = line.partition("  ")
            if f:
                base[f] = s
        files = [l.strip() for l in read(BASE / "config/watched_files.conf").splitlines() if l.strip() and not l.startswith("#")]
        self.watch.setRowCount(len(files))
        for i, f in enumerate(files):
            p = Path(f)
            if not p.exists():
                st, lv = ("removed", "bad") if f in base else ("not present", None)
            elif f not in base:
                st, lv = "no baseline", "warn"
            else:
                try:
                    same = hashlib.sha256(p.read_bytes()).hexdigest() == base[f]
                    st, lv = ("unchanged", "ok") if same else ("CHANGED", "bad")
                except OSError:
                    st, lv = "unreadable", "warn"
            self.watch.setItem(i, 0, cell(f))
            self.watch.setItem(i, 1, cell(st, lv, lv == "bad"))

    def show_rules(self):
        d = QDialog(self)
        d.setWindowTitle("AI rules (read-only)")
        d.resize(820, 680)
        l = QVBoxLayout(d)
        tb = QTextBrowser()
        tb.setMarkdown(read(STATE / "AGENTS.md", read(BASE / "ai/AI_RULES.md", "(rules not found)")))
        l.addWidget(tb)
        d.exec()

    def accept(self):
        if QMessageBox.question(self, "Accept changes", "Record the current content of all watched files as correct?") \
                == QMessageBox.StandardButton.Yes:
            self.win.run_process([str(SCRIPTS / "accept_changes.sh")], None, None, self.refresh)

    def nav_status(self):
        n = sum(1 for r in audit_rows(300) if r.get("decision") == "blocked"
                and time.time() - time.mktime(time.strptime(r["t"], "%Y-%m-%d %H:%M:%S")) < 86400)
        locked = SCRIPTS.exists() and SCRIPTS.stat().st_uid == 0
        if not locked:
            return "unlocked", "bad"
        return (f"{n} blocked today" if n else "protected"), ("bad" if n else "ok")


class ReportsPage(Page):
    title, icon, fallback = "Reports", "x-office-document", "text-x-generic"

    def __init__(self, win):
        super().__init__(win)
        lay = QVBoxLayout(self)
        lay.addLayout(header("Reports", "One health check per day (kept 60 days); a full system report on the 1st of every month."))
        split = QSplitter()
        left = QWidget()
        ll = QVBoxLayout(left)
        ll.setContentsMargins(0, 0, 0, 0)
        self.days = QListWidget()
        self.days.setObjectName("plain")
        self.days.currentItemChanged.connect(self.show)
        ll.addWidget(self.days)
        ll.addWidget(btn("History log", lambda: self.view.setPlainText(read(REPORTS / "history.log", "(empty)"))))
        ll.addWidget(btn("Hardware health", self.hardware))
        ll.addWidget(btn("Full system report", self.full_report, "primary"))
        ll.addWidget(btn("Open last full report", self.open_full))
        self.view = mono(QPlainTextEdit())
        self.view.setReadOnly(True)
        split.addWidget(left)
        split.addWidget(self.view)
        split.setSizes([220, 900])
        lay.addWidget(split, 1)

    def refresh(self):
        cur = self.days.currentItem().text() if self.days.currentItem() else None
        self.days.blockSignals(True)
        self.days.clear()
        for d in report_days():
            n = len(warnings_of(d))
            it = QListWidgetItem(f"{d.name}    {'⚠ ' + str(n) if n else '✓'}")
            it.setData(Qt.ItemDataRole.UserRole, str(d))
            it.setForeground(QColor(tone("warn" if n else "ok")))
            self.days.addItem(it)
        self.days.blockSignals(False)
        match = [self.days.item(i) for i in range(self.days.count()) if cur and self.days.item(i).text() == cur]
        self.days.setCurrentItem(match[0] if match else self.days.item(0))

    def show(self, item, _p=None):
        if item:
            self.view.setPlainText(read(Path(item.data(Qt.ItemDataRole.UserRole)) / "health.txt", "(no health.txt)"))

    def hardware(self):
        self.view.setPlainText("Reading hardware data…")
        self.win.run_process(["/usr/bin/python3", str(SCRIPTS / "ms"), "hw"], self.view)

    def full_report(self):
        self.view.setPlainText("Building the full system report (takes up to a minute)…")
        self.win.run_process(["/usr/bin/python3", str(SCRIPTS / "ms"), "report"], self.view, None, self.open_full)

    def open_full(self):
        f = REPORTS / "system" / "latest.html"
        if f.exists():
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(f.resolve())))
        else:
            self.view.setPlainText("No full report yet — press 'Full system report'.")


class InventoryPage(Page):
    title, icon, fallback = "Inventory", "applications-engineering", "package-x-generic"

    def __init__(self, win):
        super().__init__(win)
        lay = QVBoxLayout(self)
        lay.addLayout(header("Inventory", "Tools, libraries and Python packages found on this machine "
                                          "(the same lists AI assistants are pointed to)."))
        split = QSplitter()
        self.files = QListWidget()
        self.files.setObjectName("plain")
        self.files.currentItemChanged.connect(self.show)
        right = QWidget()
        rl = QVBoxLayout(right)
        rl.setContentsMargins(0, 0, 0, 0)
        self.q = QLineEdit()
        self.q.setPlaceholderText("Search… (numpy, boost, vivado)")
        self.q.textChanged.connect(lambda _: self.show(self.files.currentItem()))
        self.view = mono(QPlainTextEdit())
        self.view.setReadOnly(True)
        rl.addWidget(self.q)
        rl.addWidget(self.view)
        split.addWidget(self.files)
        split.addWidget(right)
        split.setSizes([220, 900])
        lay.addWidget(split, 1)

    def refresh(self):
        inv = REPORTS / "latest" / "inventory"
        cur = self.files.currentItem().text() if self.files.currentItem() else "tools.txt"
        self.files.blockSignals(True)
        self.files.clear()
        for f in sorted(inv.glob("*.txt")) if inv.is_dir() else []:
            it = QListWidgetItem(f.name)
            it.setData(Qt.ItemDataRole.UserRole, str(f))
            self.files.addItem(it)
        self.files.blockSignals(False)
        m = self.files.findItems(cur, Qt.MatchFlag.MatchExactly)
        self.files.setCurrentItem(m[0] if m else self.files.item(0))

    def show(self, item, _p=None):
        if not item:
            self.view.setPlainText("No inventory yet — run a health check.")
            return
        text = read(item.data(Qt.ItemDataRole.UserRole))
        q = self.q.text().strip().lower()
        if q:
            text = "\n".join(l for l in text.splitlines() if q in l.lower())
        self.view.setPlainText(text)


class NotifyPage(Page):
    title, icon, fallback = "Notifications", "preferences-system-notifications", "mail-unread"

    def __init__(self, win):
        super().__init__(win)
        lay = QVBoxLayout(self)
        lay.addLayout(header("Notifications", "Alerts go to this desktop and, through ntfy, to your phone: "
                                              "failed runs and finished batches, disk filling fast, overload advice, "
                                              "backup problems, blocked AI commands, daily warnings, weekly summary."))
        card = QFrame()
        card.setObjectName("card")
        cl = QVBoxLayout(card)
        cl.setContentsMargins(18, 16, 18, 16)
        t = QLabel("PHONE SETUP")
        t.setObjectName("cardTitle")
        cl.addWidget(t)
        self.steps = QLabel()
        self.steps.setWordWrap(True)
        self.steps.setTextFormat(Qt.TextFormat.RichText)
        self.steps.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        cl.addWidget(self.steps)
        row = QHBoxLayout()
        row.addWidget(btn("Copy topic", self.copy))
        row.addWidget(btn("Send test notification", self.test, "primary"))
        self.enabled = QCheckBox("Send to phone")
        self.enabled.toggled.connect(self.toggle)
        row.addWidget(self.enabled)
        row.addStretch()
        cl.addLayout(row)
        lay.addWidget(card)
        h = QLabel("Recent notifications")
        h.setObjectName("h2")
        lay.addWidget(h)
        self.log = table(["Time", "Priority", "Title", "Message"], 3)
        lay.addWidget(self.log, 1)

    def topic(self):
        return read(STATE / "ntfy_topic").strip()

    def refresh(self):
        tp = self.topic()
        self.steps.setText(
            "1. Install the free <b>ntfy</b> app (Google Play / App Store / F-Droid).<br>"
            "2. Tap <b>+</b> → <i>Subscribe to topic</i> and enter exactly:<br>"
            f"<span style='font-family:monospace; font-size:16px; font-weight:700'>{tp or '(no topic yet — reinstall)'}</span><br>"
            "3. Press <b>Send test notification</b> below.<br>"
            f"<span style='color:{T['muted']}'>The topic name works like a password: anyone who knows it can read these "
            "alerts, so keep it private. Alerts contain only status text, never file contents.</span>")
        self.enabled.blockSignals(True)
        self.enabled.setChecked(bool(tp) and not (STATE / "ntfy_off").exists())
        self.enabled.blockSignals(False)
        rows = []
        for line in reversed(read(REPORTS / "notifications.log").splitlines()[-300:]):
            parts = line.split("|", 3)
            if len(parts) == 4:
                rows.append(parts)
        self.log.setRowCount(len(rows))
        for i, (t, pr, ti, msg) in enumerate(rows):
            lv = "bad" if pr in ("high", "urgent") else None
            self.log.setItem(i, 0, cell(t))
            self.log.setItem(i, 1, cell(pr, lv))
            self.log.setItem(i, 2, cell(ti, lv, True))
            self.log.setItem(i, 3, cell(msg))

    def copy(self):
        QApplication.clipboard().setText(self.topic())

    def test(self):
        self.win.run_process([str(SCRIPTS / "notify.sh"), "Test from My System",
                              "If you can read this on your phone, alerts work.", "default", "tada"],
                             None, None, self.refresh)

    def toggle(self, on):
        f = STATE / "ntfy_off"
        try:
            if on:
                f.unlink(missing_ok=True)
            else:
                f.write_text("off\n")
        except OSError:
            pass

    def nav_status(self):
        tp = self.topic()
        on = bool(tp) and not (STATE / "ntfy_off").exists()
        return ("phone on" if on else "phone off"), ("ok" if on else None)



class NumItem(QTableWidgetItem):
    """Table cell that sorts numerically (empty cells last)."""

    def __init__(self, v, fmt="{:g}", level=None):
        try:
            self.v = float(v)
            txt = fmt.format(self.v)
        except (TypeError, ValueError):
            self.v = float("inf")
            txt = "" if v in (None, "") else str(v)
        super().__init__(txt)
        self.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        if level:
            self.setForeground(QColor(tone(level)))

    def __lt__(self, other):
        return self.v < getattr(other, "v", float("inf"))


class ResultsPage(Page):
    title, icon, fallback = "Results", "x-office-spreadsheet", "accessories-calculator"
    COLS = [("time", "When"), ("tool", "Tool"), ("batch", "Batch"), ("run", "Run"), ("status", "Status"),
            ("wns_ns", "WNS ns"), ("tns_ns", "TNS ns"), ("fmax_mhz", "Fmax MHz"), ("clock_ns", "Clock ns"),
            ("latency_cycles", "Latency"), ("lut", "LUT"), ("ff", "FF"), ("bram", "BRAM"), ("dsp", "DSP"),
            ("power_w", "Power W"), ("seconds", "Time")]

    def __init__(self, win):
        super().__init__(win)
        lay = QVBoxLayout(self)
        lay.addLayout(header("Results", "Timing, resources and power extracted automatically from every finished "
                                        "run (Vivado reports, HLS csynth, nextpnr/VPR logs). Click a column to sort. "
                                        "Older runs:  ms results <folder>"))
        row = QHBoxLayout()
        self.q = QLineEdit()
        self.q.setPlaceholderText("Filter (batch, run, tool, status)…")
        self.q.textChanged.connect(self.refresh)
        self.only_timing = QCheckBox("only runs with timing")
        self.only_timing.toggled.connect(self.refresh)
        self.count = QLabel()
        self.count.setObjectName("muted")
        row.addWidget(self.q, 1)
        row.addWidget(self.only_timing)
        row.addWidget(self.count)
        row.addWidget(btn("Open folder", self.open_folder))
        row.addWidget(btn("Open CSV", lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(str(REPORTS / "results.csv")))))
        lay.addLayout(row)
        self.tab = table([c[1] for c in self.COLS], -1)
        self.tab.setSortingEnabled(True)
        lay.addWidget(self.tab, 1)

    def rows(self):
        f = REPORTS / "results.csv"
        if not f.exists():
            return []
        with open(f) as fh:
            return list(csv.DictReader(fh))

    def refresh(self):
        rows = list(reversed(self.rows()))
        q = self.q.text().strip().lower()
        if q:
            rows = [r for r in rows if q in " ".join(str(r.get(k, "")) for k in ("batch", "run", "tool", "status", "folder")).lower()]
        if self.only_timing.isChecked():
            rows = [r for r in rows if r.get("wns_ns") or r.get("fmax_mhz") or r.get("clock_ns")]
        rows = rows[:2000]
        self.count.setText(f"{len(rows)} runs")
        self.tab.setSortingEnabled(False)
        self.tab.setRowCount(len(rows))
        for i, r in enumerate(rows):
            for j, (k, _) in enumerate(self.COLS):
                v = r.get(k, "")
                if k == "status":
                    it = cell(v, "bad" if v == "failed" else "ok" if v == "ok" else None, v == "failed")
                elif k in ("wns_ns", "tns_ns"):
                    lv = None
                    try:
                        lv = "bad" if float(v) < 0 else "ok"
                    except ValueError:
                        pass
                    it = NumItem(v, "{:+.3f}", lv)
                elif k == "seconds":
                    it = NumItem(v, "{:.0f}")
                    try:
                        it.setText(dur(float(v)) if float(v) else "")
                    except ValueError:
                        pass
                elif k in ("time", "tool", "batch", "run"):
                    it = cell(v)
                else:
                    it = NumItem(v)
                if j == 3:
                    it.setData(Qt.ItemDataRole.UserRole, r.get("folder", ""))
                    it.setToolTip(r.get("folder", ""))
                self.tab.setItem(i, j, it)
        self.tab.setSortingEnabled(True)

    def open_folder(self):
        row = self.tab.currentRow()
        it = self.tab.item(row, 3) if row >= 0 else None
        if it and it.data(Qt.ItemDataRole.UserRole):
            QDesktopServices.openUrl(QUrl.fromLocalFile(it.data(Qt.ItemDataRole.UserRole)))

    def nav_status(self):
        f = REPORTS / "results.csv"
        if not f.exists():
            return "no runs yet", None
        n = max(0, sum(1 for _ in open(f)) - 1)
        return f"{n} runs", None


class ProjectsPage(Page):
    title, icon, fallback = "Projects", "folder-development", "folder"

    def __init__(self, win):
        super().__init__(win)
        lay = QVBoxLayout(self)
        lay.addLayout(header("Projects", "Every git repository found in your home folder and on the data disks: "
                                         "uncommitted work, unpushed commits, missing remote, and whether it is in the backup."))
        row = QHBoxLayout()
        row.addWidget(btn("Refresh now", self.rescan, "primary"))
        row.addWidget(btn("Open folder", self.open_folder))
        row.addStretch()
        lay.addLayout(row)
        self.tab = table(["Project", "Branch", "Changed", "Unpushed", "Remote", "Backup", "Last commit", "Needs attention"], 7)
        self.tab.setSortingEnabled(True)
        lay.addWidget(self.tab, 1)

    def data(self):
        try:
            return json.loads((STATE / "git_status.json").read_text())
        except (OSError, ValueError):
            return []

    def refresh(self):
        rows = self.data()
        self.tab.setSortingEnabled(False)
        self.tab.setRowCount(len(rows))
        h = str(HOME)
        for i, g in enumerate(rows):
            probs = GIT.problems(g)
            name = g["path"].replace(h, "~")
            it = cell(name, None, bool(probs))
            it.setData(Qt.ItemDataRole.UserRole, g["path"])
            self.tab.setItem(i, 0, it)
            self.tab.setItem(i, 1, cell(g.get("branch", "")))
            self.tab.setItem(i, 2, NumItem(g.get("changed", 0), "{:.0f}", "warn" if g.get("changed") else None))
            self.tab.setItem(i, 3, NumItem(g.get("ahead", 0), "{:.0f}", "warn" if g.get("ahead") else None))
            self.tab.setItem(i, 4, cell("yes" if g.get("has_remote") else "NO", None if g.get("has_remote") else "bad"))
            self.tab.setItem(i, 5, cell("yes" if g.get("backed_up") else "NO", "ok" if g.get("backed_up") else "bad"))
            self.tab.setItem(i, 6, cell(ago(g["last_commit"]) if g.get("last_commit") else "–"))
            pi = cell(", ".join(probs) or "—", "warn" if probs else "ok")
            pi.setToolTip("\n".join(probs))
            self.tab.setItem(i, 7, pi)
        self.tab.setSortingEnabled(True)
        if not rows:
            self.tab.setRowCount(1)
            self.tab.setItem(0, 0, cell("No data yet — press Refresh now."))

    def rescan(self):
        self.win.run_process(["/usr/bin/python3", str(SCRIPTS / "ms"), "git"], None, None, self.refresh)

    def open_folder(self):
        row = self.tab.currentRow()
        it = self.tab.item(row, 0) if row >= 0 else None
        if it and it.data(Qt.ItemDataRole.UserRole):
            QDesktopServices.openUrl(QUrl.fromLocalFile(it.data(Qt.ItemDataRole.UserRole)))

    def nav_status(self):
        rows = self.data()
        n = sum(1 for g in rows if GIT.problems(g))
        return (f"{n} need attention" if n else f"{len(rows)} ok"), ("warn" if n else None)


class SettingsPage(Page):
    title, icon, fallback = "Settings", "preferences-system", "emblem-system"

    def __init__(self, win):
        super().__init__(win)
        lay = QVBoxLayout(self)
        lay.addLayout(header("Settings", "Configuration is locked so no program (or AI) can change it. To edit: run "
                                         "ms unlock in a terminal, edit the files below, then ms lock."))
        row = QHBoxLayout()
        row.addWidget(btn("Open config folder", lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(str(BASE / "config")))))
        row.addWidget(btn("Re-detect now", self.redetect))
        row.addWidget(btn("Self-check (doctor)", self.doctor))
        row.addStretch()
        lay.addLayout(row)
        split = QSplitter(Qt.Orientation.Vertical)
        self.det = mono(QPlainTextEdit())
        self.det.setReadOnly(True)
        self.conf = mono(QPlainTextEdit())
        self.conf.setReadOnly(True)
        split.addWidget(self.det)
        split.addWidget(self.conf)
        split.setSizes([260, 420])
        lay.addWidget(split, 1)

    def refresh(self):
        d = DET.load_state()
        self.det.setPlainText("Detected (refreshed daily)\n\n" + (DET.summary(d) if d else "not yet — press Re-detect now") +
                              "\n\nToolchains:\n" + "\n".join(f"  {l:26} {p}" for l, p in d.get("tools", [])) +
                              "\n\nPython environments:\n" + "\n".join(f"  {v['path']}  ({v.get('python') or '?'})"
                                                                       for v in d.get("venvs", [])))
        parts = []
        for f in sorted((BASE / "config").glob("*")):
            parts.append(f"──── {f.name} " + "─" * max(0, 60 - len(f.name)) + "\n" + read(f).rstrip())
        self.conf.setPlainText("\n\n".join(parts) or "(no config files)")

    def redetect(self):
        self.win.run_in_dialog("Detection", ["/usr/bin/python3", str(SCRIPTS / "ms"), "detect", "--state"])

    def doctor(self):
        self.win.run_in_dialog("Self-check", ["/usr/bin/python3", str(SCRIPTS / "ms"), "doctor"])

    def nav_status(self):
        locked = SCRIPTS.exists() and SCRIPTS.stat().st_uid == 0
        return ("locked" if locked else "unlocked"), (None if locked else "warn")


# ======================= sidebar =======================
class NavItem(QWidget):
    def __init__(self, page):
        super().__init__()
        lay = QHBoxLayout(self)
        lay.setContentsMargins(10, 7, 10, 7)
        lay.setSpacing(10)
        ic = QIcon.fromTheme(page.icon, QIcon.fromTheme(page.fallback))
        self.icon = QLabel()
        if not ic.isNull():
            self.icon.setPixmap(ic.pixmap(QSize(20, 20)))
        else:
            self.icon.setText("•")
        self.icon.setFixedWidth(22)
        col_ = QVBoxLayout()
        col_.setSpacing(0)
        self.name = QLabel(page.title)
        self.name.setStyleSheet("font-weight:600; font-size:13px; background:transparent;")
        self.sub = QLabel("")
        self.sub.setStyleSheet(f"font-size:11px; color:{T['muted']}; background:transparent;")
        col_.addWidget(self.name)
        col_.addWidget(self.sub)
        self.dot = QLabel("●")
        self.dot.setStyleSheet("background:transparent;")
        lay.addWidget(self.icon)
        lay.addLayout(col_, 1)
        lay.addWidget(self.dot)

    def set(self, text, level):
        self.sub.setText(text)
        self.dot.setVisible(level in ("warn", "bad", "info"))
        self.dot.setStyleSheet(f"color:{tone(level)}; background:transparent; font-size:10px;")


# ======================= main window =======================
class Main(QMainWindow):
    PAGES = [OverviewPage, RunsPage, ResultsPage, StoragePage, ProjectsPage, BackupPage, GuardPage,
             ReportsPage, InventoryPage, NotifyPage, SettingsPage]

    def __init__(self):
        super().__init__()
        self.setWindowTitle("My System")
        self.setWindowIcon(QIcon.fromTheme("utilities-system-monitor"))
        self.resize(1240, 820)
        self.procs = []
        central = QWidget()
        h = QHBoxLayout(central)
        h.setContentsMargins(0, 0, 0, 0)
        h.setSpacing(0)

        side = QWidget()
        side.setObjectName("side")
        side.setStyleSheet(f"#side {{ background:{T['side']}; }}")
        side.setFixedWidth(230)
        sl = QVBoxLayout(side)
        sl.setContentsMargins(0, 18, 0, 10)
        brand = QLabel("  My System")
        brand.setStyleSheet("font-size:17px; font-weight:800; padding:0 14px 8px 14px; background:transparent;")
        host = QLabel(f"    {CFG.get('MACHINE_LABEL')} · {PR.product()}")
        host.setStyleSheet(f"font-size:11px; color:{T['muted']}; padding:0 14px 10px 14px; background:transparent;")
        sl.addWidget(brand)
        sl.addWidget(host)
        self.nav = QListWidget()
        self.nav.setObjectName("nav")
        self.nav.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        sl.addWidget(self.nav, 1)
        self.theme_btn = btn("☾  Dark" if not DARK else "☀  Light", self.toggle_theme)
        self.theme_btn.setStyleSheet("margin:0 12px;")
        sl.addWidget(self.theme_btn)

        self.stack = QStackedWidget()
        self.pages, self.navitems = [], []
        for cls in self.PAGES:
            page = cls(self)
            wrap = QScrollArea()
            wrap.setWidgetResizable(True)
            wrap.setFrameShape(QFrame.Shape.NoFrame)
            inner = QWidget()
            inner.setObjectName("page")
            il = QVBoxLayout(inner)
            il.setContentsMargins(28, 22, 28, 18)
            il.addWidget(page)
            wrap.setWidget(inner)
            self.stack.addWidget(wrap)
            self.pages.append(page)
            it = QListWidgetItem()
            ni = NavItem(page)
            it.setSizeHint(QSize(200, 46))
            self.nav.addItem(it)
            self.nav.setItemWidget(it, ni)
            self.navitems.append(ni)
        self.nav.currentRowChanged.connect(self.switch)

        body = QVBoxLayout()
        body.setContentsMargins(0, 0, 0, 0)
        body.addWidget(self.stack, 1)
        self.foot = QLabel()
        self.foot.setObjectName("foot")
        self.foot.setContentsMargins(28, 0, 28, 8)
        body.addWidget(self.foot)
        bw = QWidget()
        bw.setObjectName("page")
        bw.setLayout(body)
        h.addWidget(side)
        h.addWidget(bw, 1)
        self.setCentralWidget(central)
        self.nav.setCurrentRow(0)
        self.refresh_nav()
        t = QTimer(self)
        t.timeout.connect(self.tick)
        t.start(15000)

    def switch(self, row):
        self.stack.setCurrentIndex(row)
        self.refresh()

    def refresh(self):
        try:
            self.pages[self.stack.currentIndex()].refresh()
        except Exception as e:  # never let one bad file kill the app
            self.foot.setText(f"refresh error: {e}")
            return
        self.foot.setText(f"{BASE}  ·  updated {time.strftime('%H:%M:%S')}  ·  refreshes every 15 s")

    def refresh_nav(self):
        for p, ni in zip(self.pages, self.navitems):
            try:
                ni.set(*p.nav_status())
            except Exception:
                ni.set("", None)

    def tick(self):
        self.refresh()
        self.refresh_nav()

    def toggle_theme(self):
        os.environ["MY_SYSTEM_THEME"] = "light" if DARK else "dark"
        set_theme(not DARK)
        apply_theme(QApplication.instance())
        geo, row = self.saveGeometry(), self.nav.currentRow()
        w = Main()
        w.restoreGeometry(geo)
        w.nav.setCurrentRow(row)
        w.show()
        QApplication.instance()._main = w
        self.close()

    def run_process(self, cmd, output=None, env=None, done=None):
        p = QProcess(self)
        p.setProcessChannelMode(QProcess.ProcessChannelMode.MergedChannels)
        if env:
            e = QProcessEnvironment.systemEnvironment()
            for k, v in env.items():
                e.insert(k, v)
            p.setProcessEnvironment(e)
        buf = []

        def out():
            buf.append(bytes(p.readAll()).decode(errors="replace"))
            if output is not None:
                output.setPlainText("".join(buf)[-50000:])
                output.verticalScrollBar().setValue(output.verticalScrollBar().maximum())

        def fin(code, _s):
            out()
            if output is not None and code != 0:
                output.appendPlainText(f"\n(exit code {code})")
            if p in self.procs:
                self.procs.remove(p)
            if done:
                done()
            self.tick()

        p.readyRead.connect(out)
        p.finished.connect(fin)
        self.procs.append(p)
        p.start(cmd[0], cmd[1:])

    def run_in_dialog(self, title, cmd):
        d = QDialog(self)
        d.setWindowTitle(title)
        d.resize(760, 440)
        l = QVBoxLayout(d)
        o = mono(QPlainTextEdit())
        o.setReadOnly(True)
        o.setPlainText("Starting… (low priority — can take a while while Vivado is busy)\n")
        l.addWidget(o)
        l.addWidget(btn("Close", d.close))
        d.show()
        self._dlg = d
        self.run_process(cmd, o)

    def run_check(self):
        self.run_in_dialog("Health check", [str(SCRIPTS / "run_daily.sh")])


def apply_theme(app):
    app.setStyle("Fusion")
    pal = QPalette()
    for role, key in ((QPalette.ColorRole.Window, "bg"), (QPalette.ColorRole.Base, "surface"),
                      (QPalette.ColorRole.AlternateBase, "surface2"), (QPalette.ColorRole.Text, "text"),
                      (QPalette.ColorRole.WindowText, "text"), (QPalette.ColorRole.Button, "surface2"),
                      (QPalette.ColorRole.ButtonText, "text"), (QPalette.ColorRole.Highlight, "accent_bg"),
                      (QPalette.ColorRole.ToolTipBase, "surface"), (QPalette.ColorRole.ToolTipText, "text"),
                      (QPalette.ColorRole.PlaceholderText, "muted")):
        pal.setColor(role, QColor(T[key]))
    pal.setColor(QPalette.ColorRole.HighlightedText, QColor("white"))
    app.setPalette(pal)
    app.setStyleSheet(stylesheet())


def main():
    app = QApplication(sys.argv)
    app.setApplicationName("My System")
    app.setDesktopFileName("my-system")
    set_theme(is_dark())
    apply_theme(app)
    app._main = Main()
    app._main.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
