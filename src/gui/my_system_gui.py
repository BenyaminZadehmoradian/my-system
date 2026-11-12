#!/usr/bin/python3
"""My System — desktop app of my-system: health, long runs and their results, storage,
projects, backups, AI guard, reports, inventory and notifications. Follows the desktop
light/dark setting. It only reads system state; buttons run the my-system commands."""
import csv
import hashlib
import html
import json
import os
import re
import shutil
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from PyQt6.QtCore import QDateTime, QPoint, QPointF, QProcess, QProcessEnvironment, QRect, QRectF, QSize, Qt, QTimer, QUrl
from PyQt6.QtGui import (QBrush, QColor, QDesktopServices, QFont, QIcon, QLinearGradient, QPainter,
                         QPainterPath, QPalette, QPen)
from PyQt6.QtWidgets import (
    QAbstractItemView, QApplication, QCheckBox, QComboBox, QDateTimeEdit, QDialog, QFileDialog, QFormLayout, QFrame,
    QSpinBox, QDoubleSpinBox, QGridLayout, QHBoxLayout, QHeaderView, QLayout,
    QLabel, QLineEdit, QListWidget, QListWidgetItem, QMainWindow, QMessageBox, QPlainTextEdit,
    QPushButton, QScrollArea, QSizePolicy, QSplitter, QStackedWidget, QTableWidget, QTableWidgetItem,
    QTextBrowser, QVBoxLayout, QWidget,
)

HOME = Path.home()
BASE = Path(os.environ.get("MY_SYSTEM", HOME / "My_System"))
PROFILE = "--profile" in sys.argv or bool(os.environ.get("MS_GUI_PROFILE"))
REPORTS, SCRIPTS, STATE = BASE / "reports", BASE / "bin", BASE / "state"
sys.path.insert(0, str(BASE / "lib"))
from mysys import config as CFG, detect as DET, gitstat as GIT, probes as PR, watcher as W  # noqa: E402


# ======================= theme =======================
def is_dark():
    if os.environ.get("MY_SYSTEM_THEME") in ("dark", "light"):
        return os.environ["MY_SYSTEM_THEME"] == "dark"
    try:
        r = subprocess.run(["gsettings", "get", "org.gnome.desktop.interface", "color-scheme"],
                           capture_output=True, text=True, errors="replace", timeout=2)
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
                v_ = hi - (hi - lo) * k / 4
                lab = f"{v_:.0f}" if hi - lo >= 20 else f"{v_:.2f}" if hi - lo >= 0.2 else f"{v_:.3f}"
                p.drawText(QRectF(0, y - 8, 30, 16), Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter, lab)
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


class FastTable(QTableWidget):
    """A table that stays quick with thousands of rows and says so when it is empty.

    Qt's ResizeToContents measures a whole column again for every cell that is set, so filling N rows cost N²:
    300 queue jobs took 35 s per refresh and the window looked frozen ("not responding"). Here the columns are
    plain (Interactive) while the table is filled and are fitted once afterwards; a column you resize by hand
    keeps your width."""
    MAX_W = 460

    def __init__(self, headers, stretch=0, empty="Nothing to show yet"):
        super().__init__(0, len(headers))
        self.stretch_col, self.empty_text = stretch, empty
        self.manual, self._fitting, self._fit_pending = set(), False, False
        self.setHorizontalHeaderLabels(headers)
        self.verticalHeader().hide()
        self.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.setAlternatingRowColors(True)
        self.setShowGrid(False)
        self.setWordWrap(False)
        self.setHorizontalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self.verticalHeader().setDefaultSectionSize(30)
        h = self.horizontalHeader()
        h.setHighlightSections(False)
        for i in range(len(headers)):
            h.setSectionResizeMode(i, QHeaderView.ResizeMode.Stretch if i == stretch else QHeaderView.ResizeMode.Interactive)
        h.sectionResized.connect(self._resized)

    def _resized(self, col, _old, _new):
        # only a drag with the mouse counts as "you chose this width" (layout changes fire the same signal)
        if not self._fitting and QApplication.mouseButtons() & Qt.MouseButton.LeftButton:
            self.manual.add(col)

    def setRowCount(self, n):                 # every fill starts here: fit the columns once, after the fill
        super().setRowCount(n)
        if not self._fit_pending:
            self._fit_pending = True
            QTimer.singleShot(0, self.fit)

    def fit(self):
        self._fit_pending = False
        self._fitting = True
        try:
            h = self.horizontalHeader()
            for i in range(self.columnCount()):
                if i == self.stretch_col or i in self.manual or self.isColumnHidden(i):
                    continue
                w = max(self.sizeHintForColumn(i), h.sectionSizeHint(i)) + 10
                self.setColumnWidth(i, max(44, min(self.MAX_W, w)))
        except RuntimeError:                  # the table was deleted meanwhile (window closed)
            pass
        finally:
            self._fitting = False

    def paintEvent(self, e):
        super().paintEvent(e)
        if self.rowCount() == 0 and self.empty_text:
            p = QPainter(self.viewport())
            p.setPen(QColor(T["muted"]))
            p.drawText(self.viewport().rect().adjusted(16, 16, -16, -16),
                       Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop | Qt.TextFlag.TextWordWrap, self.empty_text)
            p.end()

    def keep_selection(self, fill, key_col=0, role=Qt.ItemDataRole.UserRole):
        """Run fill() (which rebuilds the rows) and keep what was selected and where the view was scrolled to."""
        def key(r):
            it = self.item(r, key_col)
            return None if it is None else (it.data(role) if it.data(role) is not None else it.text())
        keep = {key(i.row()) for i in self.selectedItems()}
        pos = self.verticalScrollBar().value()
        self.blockSignals(True)
        try:
            fill()
            if keep:
                sm, model = self.selectionModel(), self.model()
                for r in range(self.rowCount()):
                    if key(r) in keep:
                        sm.select(model.index(r, 0), sm.SelectionFlag.Select | sm.SelectionFlag.Rows)
            self.verticalScrollBar().setValue(pos)
        finally:
            self.blockSignals(False)


def table(headers, stretch=0, empty="Nothing to show yet"):
    return FastTable(headers, stretch, empty)


class FlowLayout(QLayout):
    """Buttons in a row that wraps to the next line when the window is narrow (instead of making the page wider
    than the window, which cut off the table and the buttons at the right)."""

    def __init__(self, spacing=6):
        super().__init__()
        self.items, self.sp = [], spacing
        self.setContentsMargins(0, 0, 0, 0)

    def addItem(self, it):
        self.items.append(it)

    def count(self):
        return len(self.items)

    def itemAt(self, i):
        return self.items[i] if 0 <= i < len(self.items) else None

    def takeAt(self, i):
        return self.items.pop(i) if 0 <= i < len(self.items) else None

    def expandingDirections(self):
        return Qt.Orientation(0)

    def hasHeightForWidth(self):
        return True

    def heightForWidth(self, w):
        return self._layout(QRect(0, 0, w, 0), False)

    def setGeometry(self, r):
        super().setGeometry(r)
        self._layout(r, True)

    def sizeHint(self):
        return self.minimumSize()

    def minimumSize(self):
        s = QSize(0, 0)
        for it in self.items:
            s = s.expandedTo(it.minimumSize())
        return s

    def _layout(self, rect, apply):
        x, y, row_h = rect.x(), rect.y(), 0
        for it in self.items:
            hint = it.sizeHint()
            if x + hint.width() > rect.right() + 1 and row_h > 0:
                x, y, row_h = rect.x(), y + row_h + self.sp, 0
            if apply:
                it.setGeometry(QRect(QPoint(x, y), hint))
            x += hint.width() + self.sp
            row_h = max(row_h, hint.height())
        return y + row_h - rect.y()


def later(owner, fn, ms=250):
    """A slot that runs fn once, `ms` after the last call: typing in a filter refills a big table once, not per key."""
    t = QTimer(owner)
    t.setSingleShot(True)
    t.setInterval(ms)
    t.timeout.connect(fn)
    return lambda *_: t.start()


def run_rc(cmd, timeout=30):
    """(exit code, output) of a helper command for a dialog. Never raises: a command that hangs or is missing
    gives a message (exit code 124 / 127) instead of an error that would stop the handler half-way."""
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, errors="replace", timeout=timeout,
                           env=dict(os.environ, MS_GUI="1"))
        return r.returncode, (r.stdout or "") + (r.stderr or "")
    except subprocess.TimeoutExpired:
        return 124, (f"No answer within {timeout} s: {' '.join(str(c) for c in cmd[-4:])}\n"
                     "(the machine or a server is busy; try again in a moment)")
    except OSError as e:
        return 127, f"Could not run {cmd[0]}: {e}"


def run_text(cmd, timeout=30):
    return run_rc(cmd, timeout)[1]


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


class BrokenPage(Page):
    """Stands in for a page whose construction failed, so the rest of the app still opens."""

    def __init__(self, win, cls, err):
        super().__init__(win)
        self.title, self.icon = getattr(cls, "title", cls.__name__), getattr(cls, "icon", "")
        lay = QVBoxLayout(self)
        lay.addLayout(header(self.title, "This page could not be opened"))
        msg = QLabel(f"{type(err).__name__}: {err}\n\nThe other pages work. Details: {STATE / 'gui_errors.log'}")
        msg.setWordWrap(True)
        msg.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        lay.addWidget(msg)
        lay.addStretch(1)

    def nav_status(self):
        return "could not open", "bad"


# ======================= pages =======================
class Bar(QWidget):
    """Thin rounded fill bar (0-100 %)."""
    def __init__(self, h=6):
        super().__init__()
        self.pct, self.level = 0, "ok"
        self.setFixedHeight(h)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

    def set(self, pct, level):
        self.pct, self.level = max(0, min(100, pct or 0)), level
        self.update()

    def paintEvent(self, e):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        r = QRectF(self.rect())
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(T["grid"]))
        p.drawRoundedRect(r, r.height() / 2, r.height() / 2)
        p.setBrush(QColor(tone(self.level) if self.level != "ok" else T["accent_bg"]))
        p.drawRoundedRect(QRectF(r.x(), r.y(), r.width() * self.pct / 100, r.height()), r.height() / 2, r.height() / 2)


def _lab(text, obj=None, style=None, wrap=False):
    l_ = QLabel(text)
    if obj:
        l_.setObjectName(obj)
    if style:
        l_.setStyleSheet(style)
    l_.setWordWrap(wrap)
    return l_


def _card():
    f = QFrame()
    f.setObjectName("card")
    lay = QVBoxLayout(f)
    lay.setContentsMargins(16, 14, 16, 14)
    lay.setSpacing(6)
    return f, lay


def _clear(lay):
    while lay.count():
        it = lay.takeAt(0)
        if it.widget():
            it.widget().deleteLater()
        elif it.layout():
            _clear(it.layout())


def spec_grid(rows, cols=1):
    w = QWidget()
    gl = QGridLayout(w)
    gl.setContentsMargins(0, 0, 0, 0)
    gl.setHorizontalSpacing(14)
    gl.setVerticalSpacing(7)
    per = max(1, (len(rows) + cols - 1) // cols)
    for i, (k, v) in enumerate(rows):
        c, r = divmod(i, per)
        gl.addWidget(_lab(k, style=f"color:{T['muted']}; font-size:12px;"), r, c * 2, Qt.AlignmentFlag.AlignTop)
        vl = _lab(v, style="font-size:13px;", wrap=True)
        vl.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        gl.addWidget(vl, r, c * 2 + 1)
    for c in range(cols):
        gl.setColumnStretch(c * 2 + 1, 1)
    return w


class OverviewPage(Page):
    """One honest sentence, health tiles, all activity in one table, and this computer's specification."""
    title, icon, fallback = "Overview", "computer", "computer"

    def __init__(self, win):
        super().__init__(win)
        self.lay = QVBoxLayout(self)
        self.lay.setSpacing(12)
        top = QHBoxLayout()
        self.ban = _lab("", "banner", wrap=True)
        self.ban.setTextFormat(Qt.TextFormat.RichText)
        top.addWidget(self.ban, 1)
        col_ = QVBoxLayout()
        col_.addWidget(btn("Run health check", win.run_check, "primary"))
        col_.addWidget(btn("Open folder", lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(str(BASE)))))
        col_.addStretch()
        top.addLayout(col_)
        self.lay.addLayout(top)
        self.tiles = QHBoxLayout()
        self.tiles.setSpacing(10)
        self.lay.addLayout(self.tiles)
        self.lay.addWidget(_lab("ACTIVITY", "cardTitle"))
        self.tab = table(["State", "Job", "Where", "Time", "Details"], 2)
        self.tab.setMinimumHeight(260)
        self.lay.addWidget(self.tab, 1)
        self.spec_box, self.spec_lay = _card()
        self.lay.addWidget(self.spec_box)
        self._level = None

    def refresh(self):
        from mysys import brief as BR, overview as OV
        h = OV.health()
        act = OV.activity(3)
        att = BR.attention()
        for w in act["waiting"]:
            if w["long"]:
                att = att + [f"{w['name']} has waited {w['waited']} — {w['why']}"]
        head, sentence, level = OV.overall(h, act, att)
        self._level = (head, level)
        txt = f"<span style='font-size:20px; font-weight:700'>{html.escape(head)}</span><br>{html.escape(sentence)}"
        if att:
            txt += "<br>" + "<br>".join("–  " + html.escape(a) for a in att[:4])
        banner(self.ban, txt, level)
        _clear(self.tiles)
        for x in h:
            f, l2 = _card()
            l2.setContentsMargins(12, 10, 12, 10)
            l2.addWidget(_lab(x["label"].upper(), "cardTitle", wrap=True))
            l2.addWidget(_lab(x["value"], style=f"font-size:18px; font-weight:700; color:{tone(x['level'])};"))
            b = Bar(5)
            b.set(x["pct"] if x["pct"] is not None else (100 if x["level"] == "ok" else 0), x["level"])
            l2.addWidget(b)
            l2.addWidget(_lab(x["detail"], "cardDetail", wrap=True))
            l2.addStretch()
            self.tiles.addWidget(f, 1)
        rows = [("RUNNING", "info", x["name"] + (" (paused)" if x["paused"] else ""), x["where"], x["elapsed"],
                 ", ".join(b for b in [x["source"], f"{x['ram']:.1f} GB" if x["ram"] else "",
                                       (f"{x['progress']:.0f}%" if x.get("progress") is not None else ""),
                                       (("~" + x["left"] + " left") if x.get("left") and x["left"][0].isdigit() else x.get("left") or "")] if b))
                for x in act["running"]]
        rows += [("WAITING", "warn" if x["long"] else None, x["name"], x["where"], x["waited"], x["why"]) for x in act["waiting"]]
        rows += [(x["result"], {"OK": "ok", "WARN": "warn", "FAIL": "bad"}.get(x["result"]), x["name"], "", x["when"], x["detail"])
                 for x in act["finished"]]
        if not rows:
            rows = [("", None, "Nothing running, nothing waiting.", "", "", "")]
        self.tab.setRowCount(len(rows))
        for i, (st, lv, n, wh, t, d) in enumerate(rows):
            for j, v in enumerate((cell(st, lv, True), cell(n, bold=True), cell(wh), cell(t), cell(d))):
                self.tab.setItem(i, j, v)
        _clear(self.spec_lay)
        self.spec_lay.addWidget(_lab("THIS COMPUTER", "cardTitle"))
        try:
            rows_s = OV.specs() + [("Running for", OV.uptime_text())]
        except Exception as e:      # the page must never break because one probe fails
            rows_s = [("Specification", f"not available ({e})")]
        self.spec_lay.addWidget(spec_grid(rows_s, cols=2))

    def nav_status(self):
        if self._level:
            head, level = self._level
            return head.lower(), level
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
            c_ = cores.get(j.get("key"))
            self.live.setItem(i, 4, cell("–" if c_ is None else f"{c_:.1f}"))
            self.live.setItem(i, 5, cell(human(j["rss"])))
            self.live.setItem(i, 6, cell(", ".join(map(str, j["pids"][:3]))))
        if shutil.which("tsp"):
            self.queue.setPlainText(run_text(["tsp"], 5).strip() or "queue empty")
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
        return run_text(["systemctl", "--user", "is-active", unit], 5).strip() in ("active", "activating")

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
        try:
            (STATE / "backup_force").write_text("1\n")      # runs even while backups are paused
        except OSError:
            pass
        run_text(["systemctl", "--user", "start", "--no-block", "my-system-backup.service"], 10)
        self.out.setPlainText("Backup started in the background (lowest priority)…")
        QTimer.singleShot(2000, self.refresh)

    def verify_now(self):
        try:
            (STATE / "backup_force").write_text("1\n")      # runs even while backups are paused
        except OSError:
            pass
        run_text(["systemctl", "--user", "start", "--no-block", "my-system-verify.service"], 10)
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
        self.filter.textChanged.connect(later(self, self.refresh))
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


class ToolAccessPage(Page):
    """Which Vivado / Vitis / ISE versions each project may use (queue jobs and AI assistants)."""
    title, icon, fallback = "Tool access", "applications-engineering", "preferences-system"
    NICE = {"ise": "ISE", "vitis_hls": "Vitis HLS", "vivado": "Vivado/Vitis", "xilinx_sdk": "SDK"}

    def __init__(self, win):
        super().__init__(win)
        lay = QVBoxLayout(self)
        lay.addLayout(header("Tool access", "Which AMD/Xilinx tool versions each project may use. A queue job or an "
                                            "AI command that uses a version not ticked for its project is refused "
                                            "or stopped. The first row is the default for every project without its "
                                            "own ticks. A version like 2025.2 includes its updates (2025.2.1)."))
        row = QHBoxLayout()
        self.search = QLineEdit()
        self.search.setPlaceholderText("find project…")
        self.search.setMaximumWidth(240)
        self.search.textChanged.connect(self.filter)
        row.addWidget(self.search)
        self.only_own = QCheckBox("only projects with their own rule")
        self.only_own.toggled.connect(self.filter)
        row.addWidget(self.only_own)
        row.addStretch()
        row.addWidget(btn("Follow default", self.follow_default))
        row.addWidget(btn("Undo changes", self.refresh))
        row.addWidget(btn("Save", self.save, "primary"))
        lay.addLayout(row)
        self.state = _lab("", "muted")
        lay.addWidget(self.state)
        self.tab = QTableWidget(0, 1)
        self.tab.verticalHeader().hide()
        self.tab.setAlternatingRowColors(True)
        self.tab.setShowGrid(False)
        self.tab.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.tab.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.tab.verticalHeader().setDefaultSectionSize(30)
        self.tab.itemChanged.connect(self.changed)
        lay.addWidget(self.tab, 1)
        self.dirty = False
        self.loading = False

    def vmod(self):
        from mysys import versions as V
        return V

    def columns(self):
        V = self.vmod()
        cols = {}
        for label, root, ver in V.installs():
            if not ver:
                continue
            fam = V.family(ver)
            base = re.sub(r"[_-]?\d.*$", "", label)
            name = f"{self.NICE.get(base, base)} {ver}"
            cols.setdefault(fam, []).append((name, root))
        return sorted(cols.items(), key=lambda kv: [int(x) for x in re.findall(r"\d+", kv[0])], reverse=True)

    def refresh(self):
        if self.dirty and self.sender() is None:      # periodic refresh: keep unsaved ticks
            return
        V = self.vmod()
        self.loading = True
        try:
            self._fill(V)
        finally:
            self.loading = False           # an error while filling must not leave the page deaf to clicks

    def _fill(self, V):
        r = V.rules()
        self.cols = self.columns()
        heads = ["Project"] + [" / ".join(n for n, _ in names) for _, names in self.cols] + ["Rule"]
        self.tab.setColumnCount(len(heads))
        self.tab.setHorizontalHeaderLabels(heads)
        h = self.tab.horizontalHeader()
        h.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        for i in range(1, len(heads)):
            h.setSectionResizeMode(i, QHeaderView.ResizeMode.ResizeToContents)
        for i, (fam, names) in enumerate(self.cols):
            self.tab.horizontalHeaderItem(i + 1).setToolTip("\n".join(root for _, root in names))
        projs = sorted(V.projects(), key=lambda p: (p not in r["projects"], os.path.basename(p.rstrip("/")).lower()))
        self.tab.setRowCount(1 + len(projs))
        rows = [(None, r["default"], True)] + [(p, r["projects"][p] if p in r["projects"] else r["default"], p in r["projects"])
                                               for p in projs]
        hm = str(HOME)
        for i, (p, pre, own) in enumerate(rows):
            it = cell("All other projects (default)" if p is None else p.replace(hm, "~", 1), None, p is None or own)
            it.setData(Qt.ItemDataRole.UserRole, p)
            it.setToolTip(p or "every project without its own rule")
            self.tab.setItem(i, 0, it)
            for c, (fam, _) in enumerate(self.cols):
                ck = QTableWidgetItem()
                ck.setFlags(Qt.ItemFlag.ItemIsUserCheckable | Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable)
                ck.setCheckState(Qt.CheckState.Checked if V.allowed(fam, prefixes=pre) else Qt.CheckState.Unchecked)
                self.tab.setItem(i, c + 1, ck)
            self.tab.setItem(i, len(self.cols) + 1, cell("default" if p is None else ("own rule" if own else "follows default"),
                                                         "info" if own and p else None))
        self.loading = False
        self.dirty = False
        self.filter()
        self.state.setText(f"{len(r['projects'])} project(s) with their own rule · saved in {V.FILE}")

    def filter(self):
        q = self.search.text().strip().lower()
        own = self.only_own.isChecked()
        last = self.tab.columnCount() - 1
        for i in range(1, self.tab.rowCount()):
            name = (self.tab.item(i, 0).text() if self.tab.item(i, 0) else "").lower()
            rule = self.tab.item(i, last).text() if self.tab.item(i, last) else ""
            self.tab.setRowHidden(i, bool(q and q not in name) or (own and rule == "follows default"))

    def ticks(self, i):
        return [fam for c, (fam, _) in enumerate(self.cols)
                if self.tab.item(i, c + 1) and self.tab.item(i, c + 1).checkState() == Qt.CheckState.Checked]

    def changed(self, it):
        if self.loading or it.column() == 0:
            return
        self.dirty = True
        last = self.tab.columnCount() - 1
        self.loading = True
        if it.row() > 0:
            self.tab.setItem(it.row(), last, cell("own rule (unsaved)", "warn"))
        else:                                     # default changed: rows that follow it change too
            d = self.ticks(0)
            for i in range(1, self.tab.rowCount()):
                if self.tab.item(i, last).text() == "follows default":
                    for c, (fam, _) in enumerate(self.cols):
                        self.tab.item(i, c + 1).setCheckState(Qt.CheckState.Checked if fam in d else Qt.CheckState.Unchecked)
            self.tab.setItem(0, last, cell("default (unsaved)", "warn"))
        self.loading = False
        self.state.setText("Unsaved changes — press Save.")

    def follow_default(self):
        i = self.tab.currentRow()
        if i <= 0:
            return
        last = self.tab.columnCount() - 1
        self.loading = True
        d = self.ticks(0)
        for c, (fam, _) in enumerate(self.cols):
            self.tab.item(i, c + 1).setCheckState(Qt.CheckState.Checked if fam in d else Qt.CheckState.Unchecked)
        self.tab.setItem(i, last, cell("follows default (unsaved)", "warn"))
        self.loading = False
        self.dirty = True

    def save(self):
        V = self.vmod()
        allf = [fam for fam, _ in self.cols]
        old = V.rules()

        def keep(prev):                  # versions in a rule that are not installed (no column) stay as they were
            return [v for v in prev if v != "*" and V.family(v) not in allf]
        d = self.ticks(0)
        r = {"default": ["*"] if allf and set(d) == set(allf) and old["default"] == ["*"] else d + keep(old["default"]),
             "projects": {}}
        last = self.tab.columnCount() - 1
        for i in range(1, self.tab.rowCount()):
            p = self.tab.item(i, 0).data(Qt.ItemDataRole.UserRole)
            rule = self.tab.item(i, last).text()
            t = self.ticks(i)
            if rule.startswith("follows default") or (set(t) == set(d) and rule != "own rule"):
                continue
            r["projects"][p] = t + keep(old["projects"].get(p, []))
        if not d and QMessageBox.question(self, "Save", "No version is ticked in the default row: every project without "
                                          "its own rule will be blocked from all AMD/Xilinx tools. Save anyway?") \
                != QMessageBox.StandardButton.Yes:
            return
        try:
            V.save(r)
        except PermissionError as e:
            QMessageBox.warning(self, "Tool access", str(e))
            return
        try:
            from mysys import aictx
            aictx.write()
        except Exception:
            pass
        self.dirty = False
        self.refresh()
        self.state.setText(f"Saved · {len(r['projects'])} project(s) with their own rule")

    def nav_status(self):
        try:
            V = self.vmod()
            r = V.rules()
            if not V.active():
                return "no limits", None
            return f"default {'/'.join(r['default'])}" + (f" · {len(r['projects'])} own" if r["projects"] else ""), None
        except Exception:
            return "", None


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


class SystemPage(Page):
    title, icon, fallback = "System report", "computer", "video-display"

    def __init__(self, win):
        super().__init__(win)
        self._shown = None
        lay = QVBoxLayout(self)
        lay.addLayout(header("System report", "The whole machine on one page: hardware, disk health, memory, cooling, "
                                              "OS and security, software, projects, backup, what changed and what to do. "
                                              "Made automatically on the 1st of every month."))
        split = QSplitter()
        left = QWidget()
        ll = QVBoxLayout(left)
        ll.setContentsMargins(0, 0, 0, 0)
        ll.addWidget(btn("Create report now", self.create, "primary"))
        ll.addWidget(btn("Hardware check now", self.hardware))
        self.list = QListWidget()
        self.list.setObjectName("plain")
        self.list.currentItemChanged.connect(self.show)
        ll.addWidget(self.list, 1)
        self.view = QTextBrowser()
        self.view.setOpenExternalLinks(False)
        split.addWidget(left)
        split.addWidget(self.view)
        split.setSizes([200, 940])
        lay.addWidget(split, 1)
        self.setMinimumHeight(640)

    def refresh(self):
        from mysys import sysreport as SR
        dates = [d for d, _ in SR.list_reports()]
        if dates == [self.list.item(i).text() for i in range(self.list.count())]:
            return
        cur = self.list.currentItem().text() if self.list.currentItem() else None
        self.list.blockSignals(True)
        self.list.clear()
        for d, p in SR.list_reports():
            it = QListWidgetItem(d)
            it.setData(Qt.ItemDataRole.UserRole, str(p))
            self.list.addItem(it)
        self.list.blockSignals(False)
        if not dates:
            self.view.setHtml(f"<p style='color:{T['muted']}'>No system report yet — press <b>Create report now</b> "
                              "(takes up to a minute).</p>")
            return
        match = [self.list.item(i) for i in range(self.list.count()) if self.list.item(i).text() == cur]
        self.list.setCurrentItem(match[0] if match else self.list.item(0))

    def show(self, item, _p=None):
        if not item:
            return
        from mysys import sysreport as SR
        p = Path(item.data(Qt.ItemDataRole.UserRole))
        try:
            snap = json.loads(p.read_text())
        except (OSError, ValueError):
            snap = {}
        if snap.get("sections"):
            meta = {k: snap.get(k, "") for k in ("label", "time", "os", "version")}
            self.view.setHtml(SR.to_qt_html(meta, snap["sections"], dict(T, warn=T["warn2"])))
        else:   # reports made before 1.1.2 only have Markdown
            self.view.setMarkdown(read(p.with_suffix(".md"), "(report not found)"))
        self._shown = p

    def create(self):
        self.view.setHtml(f"<p style='color:{T['muted']}'>Building the system report (up to a minute)…</p>")
        self.list.clear()
        self.win.run_process(["/usr/bin/python3", str(SCRIPTS / "ms"), "report"], None, None, self.refresh)

    def hardware(self):
        self.list.clearSelection()
        try:
            out = subprocess.run(["/usr/bin/python3", str(SCRIPTS / "ms"), "hw"], capture_output=True, text=True, errors="replace",
                                 timeout=30).stdout
        except (OSError, subprocess.TimeoutExpired) as e:
            out = f"error: {e}"
        rows = []
        for line in out.splitlines():
            lv = "bad" if line.startswith("  !!") else "ok" if line.startswith("  ok") else "info" if line.startswith("  --") else None
            txt = html.escape(line.strip().lstrip("!-ok ").strip() if lv else line.strip())
            dot = f"<span style='color:{tone(lv)}'>●</span>&nbsp;" if lv else "&nbsp;&nbsp;&nbsp;"
            rows.append(f"<div style='margin:3px 0'>{dot}{txt}</div>")
        self.view.setHtml(f"<h2>Hardware now</h2><p style='color:{T['muted']}'>{time.strftime('%Y-%m-%d %H:%M')} · "
                          "disk health, memory modules and firmware come from the daily root collector</p>" + "".join(rows))

    def nav_status(self):
        from mysys import sysreport as SR
        r = SR.list_reports()
        return (r[0][0] if r else "none yet"), ("info" if r else None)


class SoftwarePage(Page):
    title, icon, fallback = "Software", "system-software-update", "package-x-generic"
    COLS = ["Advice", "Kind", "Name", "Installed", "New", "Used by", "What to do"]

    def __init__(self, win):
        super().__init__(win)
        self._sig = None
        lay = QVBoxLayout(self)
        lay.addLayout(header("Software", "Updates for apt, snap, flatpak and every Python environment — each with who uses it "
                                         "and what to do. Nothing is installed for you. Scanned daily."))
        top = QHBoxLayout()
        self.summary = QLabel("")
        self.summary.setObjectName("muted")
        self.summary.setWordWrap(True)
        top.addWidget(self.summary, 1)
        top.addWidget(btn("Scan now", self.scan, "primary"))
        top.addWidget(btn("Update plan", self.plan))
        top.addWidget(btn("Check vulnerabilities", self.audit))
        lay.addLayout(top)
        row = QHBoxLayout()
        self.q = QLineEdit()
        self.q.setPlaceholderText("Who uses…  (a project, Python package, library, tool, environment or Docker image) — press Enter")
        self.q.returnPressed.connect(self.who)
        row.addWidget(self.q, 1)
        self.proj = QComboBox()
        self.proj.setMinimumWidth(260)
        self.proj.activated.connect(lambda _: self.project())
        row.addWidget(QLabel("Project:"))
        row.addWidget(self.proj)
        lay.addLayout(row)
        split = QSplitter(Qt.Orientation.Vertical)
        self.tab = table(self.COLS, 5)
        self.tab.setSortingEnabled(True)
        split.addWidget(self.tab)
        self.view = mono(QPlainTextEdit())
        self.view.setReadOnly(True)
        split.addWidget(self.view)
        split.setSizes([420, 220])
        lay.addWidget(split, 1)
        self.setMinimumHeight(700)

    def refresh(self):
        from mysys import software as SW
        try:
            sig = SW.STATE.stat().st_mtime
        except OSError:
            sig = None
        if sig == self._sig:
            return
        self._sig = sig
        data = SW.load()
        if not data:
            self.summary.setText("No scan yet — press Scan now (a few minutes; it only reads).")
            return
        apt = [u for u in data["updates"] if u["kind"] == "apt"]
        sec = sum(1 for u in apt if u.get("security"))
        pip = sum(len(v.get("outdated") or []) for v in data["venvs"].values())
        eol = sum(1 for v in data["venvs"].values() if v.get("eol") or v.get("broken"))
        self.summary.setText(
            f"{len(apt)} apt updates ({sec} security) · {sum(1 for u in data['updates'] if u['kind'] == 'snap')} snap · "
            f"{sum(1 for u in data['updates'] if u['kind'] == 'flatpak')} flatpak · {pip} Python packages · "
            f"{eol} Python environment(s) to recreate · {len(data.get('restart_needed') or [])} program(s) to restart"
            + (" · reboot pending" if data.get("reboot_required") else "") + "\nscanned "
            + time.strftime("%Y-%m-%d %H:%M", time.localtime(data["time"])) + f" · {len(data['projects'])} projects mapped")
        rows = SW.advise(data)
        self.tab.setSortingEnabled(False)
        self.tab.setRowCount(len(rows))
        lvl = {"reboot": ("needs reboot", "bad", 0), "wait": ("after runs", "warn", 1), "test": ("test after", "info", 2),
               "ok": ("safe now", "ok", 3)}
        for i, r in enumerate(rows):
            t, lv, order = lvl[r["level"]]
            first = NumItem(order, "{:.0f}")
            first.setText(t)
            first.setForeground(QColor(tone(lv)))
            first.setTextAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
            f_ = first.font()
            f_.setBold(True)
            first.setFont(f_)
            for k, v in enumerate([first, cell(r["kind"]), cell(r["name"], bold=True), cell(r["old"]), cell(r["new"]),
                                   cell(r["used_by"]), cell(r["advice"])]):
                self.tab.setItem(i, k, v)
        self.tab.setSortingEnabled(True)
        self.tab.sortItems(0, Qt.SortOrder.AscendingOrder)
        cur = self.proj.currentText()
        self.proj.clear()
        self.proj.addItems(sorted(os.path.basename(p) for p in data["projects"]))
        if cur:
            self.proj.setCurrentText(cur)

    def scan(self):
        self.view.setPlainText("Scanning (reads package lists, Python environments and project folders; a few minutes)…")
        self.win.run_process(["/usr/bin/python3", str(SCRIPTS / "ms"), "software", "scan"], self.view, None,
                             lambda: (setattr(self, "_sig", None), self.refresh()))

    def audit(self):
        self.view.setPlainText("Checking Python packages against osv.dev (only names and versions are sent)…")
        self.win.run_process(["/usr/bin/python3", str(SCRIPTS / "ms"), "software", "audit"], self.view, None,
                             lambda: (setattr(self, "_sig", None), self.refresh()))

    def plan(self):
        self.view.setPlainText(run_text(["/usr/bin/python3", str(SCRIPTS / "ms"), "software", "plan"], 60))

    def who(self):
        q = self.q.text().strip()
        if q:
            self.view.setPlainText(run_text(["/usr/bin/python3", str(SCRIPTS / "ms"), "software", "uses", q], 60))

    def project(self):
        self.view.setPlainText(run_text(["/usr/bin/python3", str(SCRIPTS / "ms"), "software", "project",
                                         self.proj.currentText()], 60))

    def nav_status(self):
        from mysys import software as SW
        data = SW.load()
        if not data:
            return "not scanned", None
        sec = sum(1 for u in data["updates"] if u.get("security"))
        n = len(data["updates"]) + sum(len(v.get("outdated") or []) for v in data["venvs"].values())
        return (f"{n} updates" + (f" · {sec} security" if sec else "")), ("warn" if sec else "info" if n else None)


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
        lay.addLayout(header("Notifications", "Everything appears on this desktop and in the list below. Your phone gets "
                                              "only what needs you — failures, warnings, disk/RAM/crash alerts — plus the "
                                              "status report and the weekly summary. Successful runs are in the status report. "
                                              "To get successes on the phone too: PHONE_MIN_PRIO=low in my_system.conf."))
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
        card2 = QFrame()
        card2.setObjectName("card")
        c2 = QVBoxLayout(card2)
        c2.setContentsMargins(18, 16, 18, 16)
        t2 = QLabel("STATUS REPORT TO THE PHONE")
        t2.setObjectName("cardTitle")
        c2.addWidget(t2)
        self.brief_info = QLabel()
        self.brief_info.setWordWrap(True)
        self.brief_info.setObjectName("muted")
        c2.addWidget(self.brief_info)
        r2 = QHBoxLayout()
        r2.addWidget(btn("Preview", self.brief_preview))
        r2.addWidget(btn("Send status report now", self.brief_send, "primary"))
        r2.addStretch()
        c2.addLayout(r2)
        lay.addWidget(card2)
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
        times = " ".join(CFG.words("PHONE_REPORT_TIMES"))
        self.brief_info.setText(
            "How the machine is, what is running, what is waiting and the last 3 finished runs. "
            + (f"Sent automatically at {times}" if times else "Not sent automatically (PHONE_REPORT_TIMES is empty)")
            + ". Any time: type  status  as a message in your topic in the ntfy app — the report arrives within a minute. "
              "Change the times in config/my_system.conf (PHONE_REPORT_TIMES).")
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

    def brief_preview(self):
        text = run_text(["/usr/bin/python3", str(SCRIPTS / "ms"), "brief"], 45)
        d = QDialog(self)
        d.setWindowTitle("Status report (as it appears on the phone)")
        d.resize(520, 640)
        l_ = QVBoxLayout(d)
        v = mono(QPlainTextEdit())
        v.setReadOnly(True)
        v.setPlainText(text)
        l_.addWidget(v, 1)
        l_.addWidget(btn("Close", d.close))
        d.exec()

    def brief_send(self):
        self.win.run_process(["/usr/bin/python3", str(SCRIPTS / "ms"), "brief", "--send"], None, None, self.refresh)

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
        from mysys import pause as PZ
        if PZ.paused("notifications"):
            return f"all paused {PZ.describe(PZ.until('notifications'))}", "warn"
        if PZ.paused("phone"):
            return f"phone paused {PZ.describe(PZ.until('phone'))}", "warn"
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


class Gantt(QWidget):
    """Timeline of jobs: one bar per job from start to end, coloured by state."""
    COLORS = {"done": "ok", "cached": None, "running": "info", "suspended": "warn", "failed": "bad", "timeout": "bad",
              "cancelled": None, "skipped": None}

    def __init__(self):
        super().__init__()
        self.rows = []
        self.setMinimumHeight(160)

    def set(self, rows):
        self.rows = rows[-60:]
        self.setMinimumHeight(max(160, 22 + 16 * len(self.rows)))
        self.update()

    def paintEvent(self, _e):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        if not self.rows:
            p.setPen(QColor(T["muted"]))
            p.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, "no started jobs yet")
            return
        t0 = min(r[1] for r in self.rows)
        t1 = max(r[2] for r in self.rows)
        span = max(1.0, t1 - t0)
        lw = min(260, self.width() * 0.32)
        W = self.width() - lw - 10
        p.setFont(QFont(p.font().family(), 8))
        for i, (lab, a, b, st, *rest) in enumerate(self.rows):
            y = 4 + i * 16
            cr = rest[0] if rest else None
            if cr and cr < a:
                p.setPen(Qt.PenStyle.NoPen)
                p.setBrush(QColor(T["grid"]))
                x0 = lw + (max(cr, t0) - t0) / span * W
                p.drawRoundedRect(QRectF(x0, y + 5, max(2.0, lw + (a - t0) / span * W - x0), 5), 2, 2)
            p.setPen(QColor(T["text"]))
            p.drawText(QRectF(4, y, lw - 8, 14), Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, lab[:40])
            x = lw + (a - t0) / span * W
            w = max(3.0, (b - a) / span * W)
            c = QColor(tone(self.COLORS.get(st)) if self.COLORS.get(st) else T["muted"])
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(c)
            p.drawRoundedRect(QRectF(x, y + 2, w, 11), 3, 3)
        p.setPen(QColor(T["muted"]))
        yb = 6 + len(self.rows) * 16
        p.drawText(QRectF(lw, yb, 200, 14), Qt.AlignmentFlag.AlignLeft, time.strftime("%m-%d %H:%M", time.localtime(t0)))
        p.drawText(QRectF(self.width() - 210, yb, 200, 14), Qt.AlignmentFlag.AlignRight,
                   time.strftime("%m-%d %H:%M", time.localtime(t1)))


def server_line(jobs):
    """One line on how the servers are used right now: 'titan: 34 running · 41 of 72 cores · 12 waiting'."""
    try:
        from mysys import remote as RM
        out = []
        for n, r in RM.remotes().items():
            run = [j for j in jobs if j.get("remote") == n and j["state"] == "running"]
            wait = sum(1 for j in jobs if j.get("remote") == n and j["state"] == "queued")
            cap = str(r.get("max_cores") or "").strip()
            cores = sum(RM.slurm_cores(j) for j in run)
            out.append(f"{n}: {len(run)} running" + (f" · {cores} of {cap} cores" if cap else f" of {r['max_jobs']}")
                       + (f" · {wait} waiting for room there" if wait else ""))
        return "    ".join(out)
    except Exception:
        return ""


class ServersPage(Page):
    title, icon, fallback = "Servers", "network-server", "network-workgroup"
    GCOLS = ["Jobs like", "Jobs", "Reserved", "Used avg", "9 of 10 <", "Peak GB", "Used %", "Unused core-h",
             "Better request"]

    def __init__(self, win):
        super().__init__(win)
        lay = QVBoxLayout(self)
        lay.addLayout(header("Servers", "How much of the server your jobs use. The queue sends jobs there by itself; here you "
                                        "see whether the limits are reached, what your jobs reserved and what they really "
                                        "used, and why some jobs stayed on this computer."))
        cards = QHBoxLayout()
        self.c_run, self.c_cores, self.c_idle, self.c_eff = (Card("Running there"), Card("Cores reserved"),
                                                             Card("Idle CPUs on the server"), Card("Reserved cores really used"))
        for c in (self.c_run, self.c_cores, self.c_idle, self.c_eff):
            cards.addWidget(c)
        lay.addLayout(cards)
        self.bar = Bar(8)
        lay.addWidget(self.bar)
        self.hint = QLabel()
        self.hint.setObjectName("banner")
        self.hint.setWordWrap(True)
        lay.addWidget(self.hint)
        row = QHBoxLayout()
        self.days = QComboBox()
        for d, t in ((1, "last 24 hours"), (3, "last 3 days"), (7, "last 7 days"), (30, "last 30 days")):
            self.days.addItem(t, d)
        self.days.setCurrentIndex(1)
        self.days.currentIndexChanged.connect(lambda _: self.refresh())
        row.addWidget(QLabel("Measured use of finished jobs,"))
        row.addWidget(self.days)
        row.addStretch()
        row.addWidget(btn("Check connection", self.check))
        row.addWidget(btn("Ask the server now", self.ask))
        lay.addLayout(row)
        self.tab = table(self.GCOLS, 8, "No finished server job with measurements in this period.")
        self.tab.setSortingEnabled(True)
        lay.addWidget(self.tab, 2)
        h2 = QLabel("WHY JOBS STAYED ON THIS COMPUTER (last 7 days)")
        h2.setObjectName("cardTitle")
        lay.addWidget(h2)
        self.why = table(["Jobs", "Reason", "Example"], 1, "Every job that could go to the server went there.")
        lay.addWidget(self.why, 1)
        self.setMinimumHeight(720)

    def refresh(self):
        from mysys import queue as Qm, remote as RM
        rms = RM.remotes()
        if not rms:
            for c in (self.c_run, self.c_cores, self.c_idle, self.c_eff):
                c.set("–")
            banner(self.hint, "No server is set up. In a terminal:  ms remote setup titan   (then: ms remote check titan)", "info")
            self.tab.setRowCount(0)
            self.why.setRowCount(0)
            return
        jobs = Qm.all_jobs()
        name, r = next(iter(rms.items()))
        st = RM._cache(name)
        run = [j for j in jobs if j.get("remote") == name and j["state"] == "running"]
        wait = [j for j in jobs if j.get("remote") == name and j["state"] == "queued"]
        pend = sum(1 for j in run if j.get("remote_state") == "PENDING")
        cores = sum(RM.slurm_cores(j) for j in run)
        cap = str(r.get("max_cores") or "").strip()
        self.c_run.set(str(len(run)), f"of at most {r['max_jobs']} (max_jobs)"
                       + (f" · {pend} waiting in the server's own queue" if pend else "")
                       + (f" · {len(wait)} waiting here for room" if wait else ""), "info" if run else None)
        if cap:
            pct = cores / float(cap) * 100 if float(cap) else 0
            self.c_cores.set(f"{cores} / {cap}", "max_cores: the most your jobs may reserve together",
                             "warn" if pct >= 99 and wait else None)
            self.bar.set(pct, "warn" if pct >= 99 and wait else "ok")
        else:
            self.c_cores.set(str(cores), "no max_cores set: only the number of jobs is limited")
            self.bar.set(len(run) / max(1, int(r["max_jobs"])) * 100, "ok")
        age = time.time() - st.get("t", 0)
        if st.get("ok") and st.get("total_cpus"):
            self.c_idle.set(str(st.get("idle_cpus", "?")), f"of {st['total_cpus']} · asked {ago(st.get('t'))}",
                            "ok" if st.get("idle_cpus", 0) > 0 else "warn")
        else:
            self.c_idle.set("?", (st.get("error") or "not asked yet")[:90] if age < 3600 else "not asked recently",
                            "bad" if st.get("error") else None)
        u = RM.usage_summary(self.days.currentData() or 3, jobs)
        self.c_eff.set(f"{u['cpu_eff']}%" if u["cpu_eff"] is not None else "–",
                       f"{u['n']} jobs · {u['used_h']:.0f} of {u['reserved_h']:.0f} core-hours",
                       None if u["cpu_eff"] is None else "ok" if u["cpu_eff"] >= 70 else "warn" if u["cpu_eff"] >= 40 else "bad")
        # one plain sentence on what limits you right now
        if wait and cap and cores >= float(cap) - 1:
            waste = [x for x in u["groups"] if x["advice"]]
            banner(self.hint, f"{len(wait)} job(s) wait because your running jobs reserve {cores} of {cap} cores (max_cores)."
                   + (f" Recent jobs used only {u['cpu_eff']}% of what they reserved: asking for fewer cores per job "
                      f"(see \"Better request\" below) lets more run at once." if waste and (u["cpu_eff"] or 100) < 70
                      else " To run more at once, raise max_cores in Settings > remotes.conf (the server is shared)."), "warn")
        elif wait:
            banner(self.hint, f"{len(wait)} job(s) wait for room on the server: {wait[0].get('reason') or 'limit reached'}", "warn")
        elif not run:
            banner(self.hint, "Nothing of yours runs on the server right now. Jobs added to the queue go there by themselves "
                              "when they can run there.", "info")
        elif (u["cpu_eff"] or 100) < 60:
            banner(self.hint, f"The limits are not reached ({cores} of {cap or '∞'} cores reserved). Recent jobs used "
                              f"{u['cpu_eff']}% of the cores they reserved — the table shows what to ask for instead.", "info")
        else:
            banner(self.hint, f"The limits are not reached ({cores} of {cap or '∞'} cores reserved): more jobs could run there "
                              "now if they were in the queue.", "ok")

        def fill():
            self.tab.setSortingEnabled(False)
            self.tab.setRowCount(len(u["groups"]))
            for i, x in enumerate(u["groups"]):
                lvl = None if x["cpu_eff"] is None else "ok" if x["cpu_eff"] >= 70 else "warn" if x["cpu_eff"] >= 40 else "bad"
                vals = [cell(x["family"] + "…", bold=True), NumItem(x["n"], "{:.0f}"), NumItem(x["cores"], "{:.0f}"),
                        NumItem(x["used"], "{:.2f}"), NumItem(x["used_hi"], "{:.2f}"),
                        NumItem(x["peak_gb"] if x["peak_gb"] is not None else "", "{:.1f}"),
                        NumItem(x["cpu_eff"] if x["cpu_eff"] is not None else "", "{:.0f}", lvl),
                        NumItem(x["wasted_h"], "{:.1f}"), cell(x["advice"] or "fits", "info" if x["advice"] else None)]
                for k, it in enumerate(vals):
                    self.tab.setItem(i, k, it)
            self.tab.setSortingEnabled(True)
        self.tab.keep_selection(fill, role=Qt.ItemDataRole.DisplayRole)
        reasons = {}
        for j in jobs:
            note = j.get("prefer_note") or ""
            if not note.startswith("not sent: ") or j.get("remote") or (j.get("created") or 0) < time.time() - 7 * 86400:
                continue
            why = re.sub(r"(/[\w.+~-]+){2,}[^\s,)]*", "PATH", note[10:])
            why = re.sub(r"(PATH,? ?)+", "PATH ", why)
            why = re.sub(r"\d+", "N", why)[:150]
            d = reasons.setdefault(why, [0, f"#{j['id']} {j['name']}", note[10:]])
            d[0] += 1
        rows = sorted(reasons.items(), key=lambda kv: -kv[1][0])[:30]
        self.why.setRowCount(len(rows))
        for i, (why, (n, ex, full)) in enumerate(rows):
            self.why.setItem(i, 0, NumItem(n, "{:.0f}"))
            it = cell(why)
            it.setToolTip(full)
            self.why.setItem(i, 1, it)
            self.why.setItem(i, 2, cell(ex))

    def _name(self):
        from mysys import remote as RM
        return next(iter(RM.remotes()), None)

    def check(self):
        if self._name():
            self.win.run_in_dialog("Connection check", ["/usr/bin/python3", str(SCRIPTS / "ms"), "remote", "check", self._name()])

    def ask(self):
        if self._name():
            self.win.run_process(["/usr/bin/python3", str(SCRIPTS / "ms"), "remote", "status", self._name()], None, None, self.refresh)

    def nav_status(self):
        from mysys import queue as Qm, remote as RM
        rms = RM.remotes()
        if not rms:
            return "not set up", None
        n = next(iter(rms))
        jobs = Qm.all_jobs()
        run = sum(1 for j in jobs if j.get("remote") == n and j["state"] == "running")
        wait = sum(1 for j in jobs if j.get("remote") == n and j["state"] == "queued")
        if wait:
            return f"{run} running · {wait} waiting", "warn"
        return (f"{run} running on {n}", "info") if run else (f"{n}: idle", None)


class QueuePage(Page):
    title, icon, fallback = "Queue", "view-list", "view-list"
    COLS = ["ID", "State", "Where", "Prio", "Name", "Flow / step", "Mem GB", "Peak GB", "Cores", "CPU used", "Started",
            "Time", "Note"]

    def __init__(self, win):
        super().__init__(win)
        lay = QVBoxLayout(self)
        lay.addLayout(header("Queue", "Jobs start only when the CPU threads and RAM they need are free. When RAM runs low the "
                                      "newest low-priority job is paused and continues later. Add jobs with  ms q add -- CMD  "
                                      "or a flow file (steps, sweeps, dependencies)."))
        top = QHBoxLayout()
        self.status = QLabel()
        self.status.setObjectName("pill")
        top.addWidget(self.status)
        self.show_all = QCheckBox("show finished")
        self.show_all.toggled.connect(lambda _: self.refresh())
        top.addWidget(self.show_all)
        self.server = QLabel()
        self.server.setObjectName("muted")
        top.addWidget(self.server)
        self.server.setWordWrap(True)
        top.addStretch()
        lay.addLayout(top)
        row = FlowLayout()
        row.addWidget(btn("Run flow file…", self.add_flow, "primary"))
        row.addWidget(btn("Check flow…", self.check_flow))
        row.addWidget(btn("Schedules", self.show_schedules))
        for label, fn in (("Approve", "approve"), ("Hold", "hold"), ("Release", "release"), ("Pause", "suspend"),
                          ("Continue", "resume"), ("Retry", "retry"), ("Run again", "clone"), ("To front", "top"),
                          ("Cancel", "cancel")):
            row.addWidget(btn(label, lambda _=False, f=fn: self.act(f)))
        row.addWidget(btn("Priority +", lambda: self.prio(+1)))
        row.addWidget(btn("Priority −", lambda: self.prio(-1)))
        row2 = row
        row2.addWidget(btn("Details", self.details))
        row2.addWidget(btn("Output", self.output))
        row2.addWidget(btn("Warnings", self.warnings))
        row2.addWidget(btn("Flow report", self.flow_report))
        row2.addWidget(btn("Why waiting?", self.explain))
        row2.addWidget(btn("Change job…", self.edit_job))
        row2.addWidget(btn("Send input…", self.send_input))
        row2.addWidget(btn("Groups / flaky", self.groups))
        row2.addWidget(btn("Edit flow file…", self.edit_flow))
        row2.addWidget(btn("Clear finished", self.clear))
        lay.addLayout(row)
        split = QSplitter(Qt.Orientation.Vertical)
        self.tab = table(self.COLS, 12, "No jobs. Add one in a terminal with  ms q add -- COMMAND  or use \"Run flow file…\".\n"
                                         "Finished jobs older than 6 hours are hidden: tick \"show finished\".")
        self.tab.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.tab.setSortingEnabled(True)
        for c, tip in ((2, "Where the job runs: this computer or a server (and the node there)"),
                       (7, "Highest memory the job used. For a server job: measured by Slurm when it ends"),
                       (9, "Cores really used on average. Much lower than \"Cores\" = it reserved too much")):
            self.tab.horizontalHeaderItem(c).setToolTip(tip)
        split.addWidget(self.tab)
        bottom = QWidget()
        bl = QVBoxLayout(bottom)
        bl.setContentsMargins(0, 6, 0, 0)
        hl = QLabel("TIMELINE (last 7 days, or the selected job's flow)")
        hl.setObjectName("cardTitle")
        bl.addWidget(hl)
        sc = QScrollArea()
        sc.setWidgetResizable(True)
        sc.setFrameShape(QFrame.Shape.NoFrame)
        self.gantt = Gantt()
        sc.setWidget(self.gantt)
        bl.addWidget(sc, 1)
        split.addWidget(bottom)
        split.setSizes([380, 260])
        lay.addWidget(split, 1)
        self.tab.itemSelectionChanged.connect(self.timeline)
        self.setMinimumHeight(700)
        self._jobs = []

    def refresh(self):
        from mysys import queue as Qm
        st, d = Qm.daemon_state()
        set_pill(self.status, f"scheduler {st}" + (f": {d['error'][:60]}" if d and d.get("error") else ""),
                 "ok" if st == "running" else "bad")
        jobs = Qm.all_jobs()
        self.server.setText(server_line(jobs))
        if not self.show_all.isChecked():
            jobs = [j for j in jobs if j["state"] not in Qm.TERMINAL or time.time() - (j.get("ended") or 0) < 6 * 3600]
        self._jobs = jobs
        self.tab.keep_selection(lambda: self._fill(jobs, Qm))
        self.timeline()

    def _fill(self, jobs, Qm):
        self.tab.setSortingEnabled(False)
        self.tab.setRowCount(len(jobs))
        lv = {"running": "info", "done": "ok", "cached": None, "failed": "bad", "timeout": "bad", "suspended": "warn",
              "queued": None, "held": "warn", "skipped": None, "cancelled": None, "warning": "warn", "approval": "warn"}
        for i, j in enumerate(reversed(jobs)):
            e = Qm.efficiency(j)
            fl = j.get("flow") or {}
            if j.get("remote") and j.get("remote") not in ("here", "local", "auto"):
                where = j["remote"] + (f" · {j['remote_node']}" if j.get("remote_node") and j["state"] == "running" else "")
                if j["state"] == "running" and j.get("remote_state") and j["remote_state"] != "RUNNING":
                    where += f" ({j['remote_state'].lower()})"
            else:
                where = "here" if j.get("started") else ""
            vals = [NumItem(j["id"], "{:.0f}"), cell(j["state"], lv.get(j["state"]), j["state"] in ("failed", "timeout")),
                    cell(where, "info" if where not in ("", "here") else None),
                    NumItem(j.get("priority") or 0, "{:.0f}"), cell(j["name"], bold=True),
                    cell(f"{fl.get('name', '')} / {fl.get('step', '')}" if fl else ""),
                    NumItem(e["mem_req_gb"], "{:.1f}"), NumItem(e["mem_peak_gb"] or "", "{:.1f}"),
                    NumItem(e["cores_req"], "{:.1f}"), NumItem(e["cores_used"] if e["cores_used"] is not None else "", "{:.1f}"),
                    cell(time.strftime("%m-%d %H:%M", time.localtime(j["started"])) if j.get("started") else ""),
                    cell(self.hms(Qm.runtime(j)) if j.get("started") else ""),
                    cell(j.get("reason", "") or ((f"{j['progress']:.0f}% " if j.get("progress") is not None else "")
                                                 + (j.get("last_line") or "") if j["state"] == "running" else ""))]
            for k, it in enumerate(vals):
                it.setData(Qt.ItemDataRole.UserRole, j["id"])
                self.tab.setItem(i, k, it)
            if j.get("prefer_note") and not j.get("remote"):
                vals[2].setToolTip(j["prefer_note"])
        self.tab.setSortingEnabled(True)

    @staticmethod
    def hms(s):
        s = int(s)
        return f"{s // 3600}h{s % 3600 // 60:02d}m" if s >= 3600 else f"{s // 60}m{s % 60:02d}s"

    def selected(self):
        rows = {i.row() for i in self.tab.selectedItems()}
        return [self.tab.item(r, 0).data(Qt.ItemDataRole.UserRole) for r in sorted(rows) if self.tab.item(r, 0)]

    def timeline(self):
        from mysys import flow as F, queue as Qm
        sel = self.selected()
        jobs = self._jobs or Qm.all_jobs()
        if sel:
            j = Qm.load(sel[0])
            fid = (j or {}).get("flow", {}) and j["flow"].get("id")
            if fid:
                jobs = [x for x in Qm.all_jobs() if (x.get("flow") or {}).get("id") == fid]
        jobs = [j for j in jobs if time.time() - (j.get("started") or 0) < 7 * 86400]
        self.gantt.set(F.timeline_rows(jobs))

    def act(self, fn):
        from mysys import queue as Qm
        ids = self.selected()
        if fn == "cancel" and ids and QMessageBox.question(self, "Cancel jobs", f"Cancel {len(ids)} job(s)?") \
                != QMessageBox.StandardButton.Yes:
            return
        for i in ids:
            getattr(Qm, fn)(i)
        self.refresh()

    def _text_dialog(self, title, text, html_=False, chart=None):
        d = QDialog(self)
        d.setWindowTitle(title)
        d.resize(1000, 700)
        l_ = QVBoxLayout(d)
        if chart is not None:
            l_.addWidget(chart)
        v = QTextBrowser() if html_ else mono(QPlainTextEdit())
        if html_:
            v.setHtml(text)
        else:
            v.setReadOnly(True)
            v.setPlainText(text)
        l_.addWidget(v, 1)
        d.exec()

    def _ms(self, *args):
        return run_text(["/usr/bin/python3", str(SCRIPTS / "ms"), *args], 30)

    def details(self):
        from mysys import queue as Qm
        ids = self.selected()
        if not ids:
            return
        txt = self._ms("q", "show", str(ids[0]))
        chart = None
        f = Qm.Q / "series" / f"{ids[0]}.csv"
        if f.exists() and not (Qm.load(ids[0]) or {}).get("remote"):     # (a server job: that file is only the link)
            vals = []
            for row in f.read_text().splitlines()[1:]:
                try:
                    vals.append(float(row.split(",")[1]))
                except (ValueError, IndexError):
                    pass
            if len(vals) > 1:
                chart = Chart(height=160, axis=True)
                chart.set(vals, T["accent_bg"])
                txt = "RAM in GB over time (chart above)\n\n" + txt
        self._text_dialog(f"Job {ids[0]}", txt, chart=chart)

    def explain(self):
        ids = self.selected()
        if ids:
            self._text_dialog(f"Why job {ids[0]} is waiting", self._ms("q", "explain", str(ids[0])))

    def edit_job(self):
        from mysys import queue as Qm
        ids = self.selected()
        if not ids:
            return
        j = Qm.load(ids[0])
        if not j or j["state"] not in ("queued", "held", "approval"):
            QMessageBox.information(self, "Change job", "Only waiting jobs can be changed. Use \"Run again\" for others.")
            return
        d = QDialog(self)
        d.setWindowTitle(f"Change job {j['id']}")
        d.resize(720, 260)
        f = QFormLayout(d)
        cmd, name = QLineEdit(j["cmd"]), QLineEdit(j["name"])
        mem = QDoubleSpinBox()
        mem.setRange(0.1, 4096)
        mem.setDecimals(1)
        mem.setValue(float(j["mem_gb"]))
        cores = QDoubleSpinBox()
        cores.setRange(1, 1024)
        cores.setDecimals(0)
        cores.setValue(float(j["cores"]))
        prio = QSpinBox()
        prio.setRange(-100, 100)
        prio.setValue(int(j.get("priority") or 0))
        group = QLineEdit(j.get("group") or "")
        for lab, w in (("Command", cmd), ("Name", name), ("Memory GB", mem), ("Cores", cores), ("Priority", prio), ("Group", group)):
            f.addRow(lab, w)
        row = QHBoxLayout()
        row.addStretch()
        row.addWidget(btn("Cancel", d.reject))
        row.addWidget(btn("Save", d.accept, "primary"))
        f.addRow(row)
        if d.exec():
            ok, msg = Qm.edit(j["id"], cmd=cmd.text(), name=name.text(), mem_gb=mem.value(), cores=cores.value(),
                              priority=prio.value(), group=group.text().strip() or None)
            if not ok:
                QMessageBox.warning(self, "Change job", msg)
            self.refresh()

    def send_input(self):
        ids = self.selected()
        if not ids:
            return
        from PyQt6.QtWidgets import QInputDialog
        text, ok = QInputDialog.getText(self, "Send input", f"Line to type into job {ids[0]} (started with --stdin):")
        if ok:
            out = self._ms("q", "send", str(ids[0]), text)
            if out.strip():
                QMessageBox.information(self, "Send input", out)

    def groups(self):
        self._text_dialog("Groups and flaky jobs", "GROUPS (limits in config/groups.conf)\n" + self._ms("q", "groups")
                          + "\n\nFLAKY (failed, then passed on retry without changes)\n" + self._ms("q", "flaky"))

    def warnings(self):
        ids = self.selected()
        if ids:
            self._text_dialog(f"Warnings of job {ids[0]}", self._ms("q", "warnings", str(ids[0])))

    def flow_report(self):
        from mysys import flow as F, queue as Qm
        ids = self.selected()
        j = Qm.load(ids[0]) if ids else None
        fid = (j or {}).get("flow", {}) and j["flow"].get("id")
        if not fid:
            QMessageBox.information(self, "Flow report", "Select a job that belongs to a flow.")
            return
        h = F.report_html(fid)
        h = re.sub(r"<svg.*?</svg>", "", h, flags=re.S)
        try:
            _, specs, _ = F.check(F.load_flow(fid)["file"])
            h = h.replace("<h2>Steps</h2>", "<h2>Steps</h2><pre>" + html.escape(F.dag_text(specs)) + "</pre>")
        except Exception:
            pass
        self._text_dialog(f"Flow {fid}", h, html_=True)

    def edit_flow(self):
        f = self._pick()
        if not f:
            return
        d = QDialog(self)
        d.setWindowTitle(f)
        d.resize(1000, 760)
        l_ = QVBoxLayout(d)
        ed = mono(QPlainTextEdit())
        ed.setPlainText(read(f))
        out = mono(QPlainTextEdit())
        out.setReadOnly(True)
        out.setMaximumHeight(200)
        l_.addWidget(ed, 1)
        l_.addWidget(out)
        row = QHBoxLayout()

        def save():
            try:
                Path(f).write_text(ed.toPlainText())
                return True
            except OSError as e:
                out.setPlainText(f"cannot save: {e}")
                return False

        def check():
            if save():
                out.setPlainText(self._ms("flow", "check", f))

        def run():
            if save():
                res = self._ms("flow", "check", f)
                if res.startswith("Problems"):
                    out.setPlainText(res)
                    return
                out.setPlainText(self._ms("flow", "run", f, "--resume"))
                self.refresh()
        row.addWidget(btn("Reference", lambda: out.setPlainText(self._ms("flow", "reference"))))
        row.addStretch()
        row.addWidget(btn("Save and check", check))
        row.addWidget(btn("Save and run", run, "primary"))
        l_.addLayout(row)
        d.exec()

    def prio(self, d):
        from mysys import queue as Qm
        for i in self.selected():
            j = Qm.load(i)
            if j:
                Qm.set_priority(i, int(j.get("priority") or 0) + d)
        self.refresh()

    def output(self):
        from mysys import queue as Qm
        ids = self.selected()
        if not ids:
            return
        d = QDialog(self)
        d.setWindowTitle(f"Output of job {ids[0]}")
        d.resize(900, 600)
        l_ = QVBoxLayout(d)
        v = mono(QPlainTextEdit())
        v.setReadOnly(True)
        v.setPlainText(read(Qm.LOGS / f"{ids[0]}.log", "(no output yet)")[-200000:])
        v.verticalScrollBar().setValue(v.verticalScrollBar().maximum())
        l_.addWidget(v)
        d.exec()

    def clear(self):
        from mysys import queue as Qm
        Qm.clear(0)
        self.refresh()

    def _pick(self):
        f, _ = QFileDialog.getOpenFileName(self, "Flow file", str(HOME), "Flow (*.yaml *.yml *.toml *.json)")
        return f

    def check_flow(self, path=None):
        f = path or self._pick()
        if not f:
            return None
        rc, text = run_rc(["/usr/bin/python3", str(SCRIPTS / "ms"), "flow", "check", f], 60)
        d = QDialog(self)
        d.setWindowTitle("Flow plan")
        d.resize(1000, 600)
        l_ = QVBoxLayout(d)
        v = mono(QPlainTextEdit())
        v.setReadOnly(True)
        v.setPlainText(text)
        l_.addWidget(v)
        ok = rc == 0
        if path:
            row = QHBoxLayout()
            row.addStretch()
            row.addWidget(btn("Cancel", d.reject))
            if ok:
                row.addWidget(btn("Queue all jobs", d.accept, "primary"))
            l_.addLayout(row)
        return d.exec() == QDialog.DialogCode.Accepted and ok

    def add_flow(self):
        f = self._pick()
        if f and self.check_flow(f):
            self.win.run_process(["/usr/bin/python3", str(SCRIPTS / "ms"), "flow", "run", f, "--resume"], None, None, self.refresh)

    def show_schedules(self):
        text = run_text(["/usr/bin/python3", str(SCRIPTS / "ms"), "flow", "schedules"], 20)
        QMessageBox.information(self, "Scheduled flows", (text.strip() or "No schedules.") +
                                "\n\nAdd one in a terminal:\n  ms flow schedule FILE --cron \"17 2 * * *\"\n"
                                "  ms flow schedule FILE --idle\n  ms flow schedule FILE --on-commit ~/project")

    def nav_status(self):
        from mysys import queue as Qm
        js = Qm.all_jobs()
        run = sum(1 for j in js if j["state"] == "running")
        wait = sum(1 for j in js if j["state"] == "queued")
        st, _ = Qm.daemon_state()
        if st != "running" and (run or wait):
            return "scheduler stopped", "bad"
        if not run and not wait:
            return "empty", None
        return f"{run} running · {wait} waiting", "info"


class RunLogsPage(Page):
    title, icon, fallback = "Run logs", "text-x-script", "text-x-generic"
    COLS = ["Started", "Project", "Run", "Status", "Time", "Errors", "Warnings", "Key metrics"]

    def __init__(self, win):
        super().__init__(win)
        self._runs, self._sig, self._filled = [], None, 0.0
        lay = QVBoxLayout(self)
        lay.addLayout(header("Run logs", "Every run that writes the standard run log (msrun.jsonl) — from your scripts, "
                                         "Vivado/Tcl flows or `ms log run`. Select a run to see its timeline. "
                                         "Format: ~/My_System/runlog/RUNLOG_SPEC.md"))
        row = QHBoxLayout()
        self.q = QLineEdit()
        self.q.setPlaceholderText("Filter (project, run, status, metric)…")
        self.q.textChanged.connect(later(self, self.fill))
        self.count = QLabel()
        self.count.setObjectName("muted")
        row.addWidget(self.q, 1)
        row.addWidget(self.count)
        row.addWidget(btn("Search folders again", self.rescan))
        row.addWidget(btn("Check format", self.check))
        row.addWidget(btn("Compare runs", self.compare))
        row.addWidget(btn("Open folder", self.open_folder))
        lay.addLayout(row)
        split = QSplitter(Qt.Orientation.Vertical)
        self.tab = table(self.COLS, 7)
        self.tab.setSortingEnabled(True)
        self.tab.itemSelectionChanged.connect(self.detail)
        self.view = QTextBrowser()
        self.view.setOpenLinks(False)
        self.view.anchorClicked.connect(self.open_artifact)
        split.addWidget(self.tab)
        bottom = QSplitter(Qt.Orientation.Horizontal)
        bottom.addWidget(self.view)
        trend = QWidget()
        tl = QVBoxLayout(trend)
        tl.setContentsMargins(6, 0, 0, 0)
        tr = QHBoxLayout()
        lab = QLabel("TREND")
        lab.setObjectName("cardTitle")
        tr.addWidget(lab)
        self.metric = QComboBox()
        self.metric.currentTextChanged.connect(lambda _: self.draw_trend())
        tr.addWidget(self.metric, 1)
        tl.addLayout(tr)
        self.trend_info = QLabel("")
        self.trend_info.setObjectName("muted")
        tl.addWidget(self.trend_info)
        self.chart = Chart(height=180, axis=True)
        tl.addWidget(self.chart, 1)
        bottom.addWidget(trend)
        bottom.setSizes([620, 420])
        split.addWidget(bottom)
        split.setSizes([400, 320])
        lay.addWidget(split, 1)
        self.setMinimumHeight(720)

    def refresh(self):
        from mysys import runlog as RL
        paths = RL.known_paths()
        sig = tuple((p, os.path.getmtime(p)) for p in paths if os.path.exists(p))
        running = any(r["status"] == "running" for r in self._runs)
        # unchanged files: nothing to do; running runs only update their duration, once a minute is enough
        if sig == self._sig and not (running and time.time() - self._filled > 60):
            return
        self._sig = sig
        self._runs = RL.all_runs(2000)          # cheap: unchanged files come from the parse cache
        self.fill()
        self._filled = time.time()

    def fill(self):
        from mysys import runlog as RL
        q = self.q.text().strip().lower()
        runs = [r for r in self._runs if not q or q in " ".join(
            [r["project"], r["name"], r["status"], r["folder"], " ".join(map(str, r["metrics"]))]).lower()]
        self.count.setText(f"{len(runs)} runs")
        self.tab.setSortingEnabled(False)
        self.tab.setRowCount(len(runs))
        for i, r in enumerate(runs):
            st = time.strftime("%Y-%m-%d %H:%M", time.localtime(r["start_ts"])) if r["start_ts"] else "?"
            lv = {"ok": "ok", "failed": "bad", "died": "bad", "cancelled": "warn", "running": "info"}.get(r["status"])
            status = str(r.get("status") or "") + (f" {r['progress']:.0f}%" if r.get("status") == "running"
                                                   and isinstance(r.get("progress"), (int, float)) else "")
            mets = ", ".join(f"{k}={v}{r['units'].get(k, '')}" for k, v in list(r["metrics"].items())[:5])
            vals = [cell(st), cell(r["project"]), cell(r["name"], bold=True), cell(status, lv, lv == "bad"),
                    self.dur_item(r["seconds"], RL.fmt_seconds(r["seconds"])),
                    cell(r["errors"] or "", "bad" if r["errors"] else None), cell(r["warnings"] or "", "warn" if r["warnings"] else None),
                    cell(mets)]
            for j, it in enumerate(vals):
                it.setData(Qt.ItemDataRole.UserRole, i)
                self.tab.setItem(i, j, it)
        self.tab.setSortingEnabled(True)
        self._shown = runs
        names = sorted({str(k) for r in runs for k, v in (r.get("metrics") or {}).items()
                        if k is not None and isinstance(v, (int, float)) and not isinstance(v, bool)})
        cur = self.metric.currentText()
        self.metric.blockSignals(True)
        self.metric.clear()
        self.metric.addItems(names)
        if cur in names:
            self.metric.setCurrentText(cur)
        elif "wns_ns" in names:
            self.metric.setCurrentText("wns_ns")
        self.metric.blockSignals(False)
        self.draw_trend()

    def draw_trend(self):
        m = self.metric.currentText()
        pts = [(r["start_ts"] or 0, r["metrics"][m]) for r in getattr(self, "_shown", [])
               if isinstance(r["metrics"].get(m), (int, float)) and not isinstance(r["metrics"].get(m), bool)]
        pts.sort(key=lambda x: x[0])
        vals = [float(v) for _, v in pts]
        if vals:
            self.trend_info.setText(f"{len(vals)} runs (filter above) · last {vals[-1]:g} · best {max(vals):g} · worst {min(vals):g}")
        else:
            self.trend_info.setText("no numeric metric in these runs")
        self.chart.set(vals, T["accent_bg"])

    @staticmethod
    def dur_item(sec, text):
        it = NumItem(sec or 0)
        it.setText(text)
        return it

    def current(self):
        rows = self.tab.selectionModel().selectedRows() if self.tab.selectionModel() else []
        if not rows:
            return None
        it = self.tab.item(rows[0].row(), 0)
        idx = it.data(Qt.ItemDataRole.UserRole) if it else None
        return self._shown[idx] if idx is not None and idx < len(self._shown) else None

    def detail(self):
        from mysys import runlog as RL
        r = self.current()
        if not r:
            return
        def esc(x):
            return html.escape(str(x))
        lv = {"ok": "ok", "failed": "bad", "died": "bad", "cancelled": "warn", "running": "info"}.get(r["status"])
        h = [f"<h3>{esc(r['name'])} <span style='color:{tone(lv)}'>· {esc(r['status'])}</span></h3>",
             f"<p style='color:{T['muted']}'>{esc(r['folder'])} · {esc(r['cmd'])[:200]}"
             + (f" · git {esc(r['git'].get('commit'))}{' (dirty)' if r['git'].get('dirty') else ''}"
                if isinstance(r.get("git"), dict) else f" · git {esc(r['git'])}" if r.get("git") else "") + "</p>"]
        if r["first_error"]:
            h.append(f"<p style='color:{T['bad']}'><b>Error:</b> {esc(r['first_error'])}</p>")
        if r["params"]:
            h.append("<p><b>Parameters:</b> " + ", ".join(f"{esc(k)}={esc(v)}" for k, v in r["params"].items()) + "</p>")
        if r["metrics"]:
            h.append("<table cellspacing='0' cellpadding='4' border='1' style='border-collapse:collapse; "
                     f"border-color:{T['border']}'><tr><th align='left'>Metric</th><th align='left'>Value</th></tr>"
                     + "".join(f"<tr><td>{esc(k)}</td><td>{esc(v)} {esc(r['units'].get(k, ''))}</td></tr>" for k, v in r["metrics"].items())
                     + "</table>")
        if r["artifacts"]:
            h.append("<p><b>Files</b> (click to view): " + ", ".join(
                f"<a href='{QUrl.fromLocalFile(os.path.join(r['folder'], p)).toString()}'>{esc(p)}</a> ({esc(k)})"
                for p, k in r["artifacts"] if p) + "</p>")
        h.append(f"<pre style='font-family:monospace; font-size:12px'>{esc(RL.timeline(r['file'], r['run']))}</pre>")
        self.view.setHtml("".join(h))

    def open_artifact(self, url):
        f = url.toLocalFile()
        if not os.path.exists(f):
            QMessageBox.information(self, "File", f"{f}\n\ndoes not exist (any more).")
            return
        d = QDialog(self)
        d.setWindowTitle(f)
        d.resize(1000, 700)
        l_ = QVBoxLayout(d)
        ext = os.path.splitext(f)[1].lower()
        if ext in (".png", ".jpg", ".jpeg", ".gif", ".svg", ".bmp", ".webp"):
            v = QTextBrowser()
            v.setHtml(f"<img src='{QUrl.fromLocalFile(f).toString()}' style='max-width:100%'>")
        elif os.path.isdir(f):
            v = mono(QPlainTextEdit())
            v.setPlainText("\n".join(sorted(os.listdir(f))))
        else:
            with open(f, "rb") as fh:
                head = fh.read(400000)
            if b"\0" in head[:4000]:
                d.close()
                QDesktopServices.openUrl(QUrl.fromLocalFile(f))
                return
            v = mono(QPlainTextEdit())
            v.setPlainText(head.decode(errors="replace") + ("\n… (first 400 kB)" if len(head) == 400000 else ""))
        if isinstance(v, QPlainTextEdit):
            v.setReadOnly(True)
        l_.addWidget(v)
        row = QHBoxLayout()
        row.addStretch()
        row.addWidget(btn("Open with system app", lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(f))))
        l_.addLayout(row)
        d.exec()

    def rescan(self):
        self.view.setPlainText("Searching project folders for run logs…")
        self.win.run_process(["/usr/bin/python3", str(SCRIPTS / "ms"), "log", "index"], None, None,
                             lambda: (setattr(self, "_sig", None), self.refresh(), self.view.setPlainText("Search finished.")))

    def check(self):
        r = self.current()
        target = r["folder"] if r else None
        if not target:
            self.view.setPlainText("Select a run first.")
            return
        out = run_text(["/usr/bin/python3", str(SCRIPTS / "ms"), "log", "check", target], 60)
        self.view.setPlainText(out or "(no output)")

    def open_folder(self):
        r = self.current()
        if r:
            QDesktopServices.openUrl(QUrl.fromLocalFile(r["folder"]))

    def compare(self):
        """Params and metrics side by side (DVC exp show): runs of the selected run's folder, else the filtered list."""
        from mysys import runlog as RL
        r = self.current()
        runs = [x for x in self._shown if x["folder"] == r["folder"]] if r else list(getattr(self, "_shown", []))
        if len(runs) < 2:
            runs = list(getattr(self, "_shown", []))
        if not runs:
            return
        sort = self.metric.currentText() or None
        header, rows = RL.compare_table(runs[:500], sort, desc=True)
        d = QDialog(self)
        d.setWindowTitle(f"Compare {len(rows)} runs" + (f" — best {sort} first" if sort else ""))
        d.resize(1100, 640)
        l_ = QVBoxLayout(d)
        info = QLabel("Only columns that differ between the runs are shown (p: = parameter). Sorted by the trend metric.")
        info.setObjectName("muted")
        l_.addWidget(info)
        t = table(header, 0)
        t.setRowCount(len(rows))
        for i, row in enumerate(rows):
            for k, v in enumerate(row):
                t.setItem(i, k, NumItem(v, "{:.4g}") if isinstance(v, (int, float)) and not isinstance(v, bool) else cell(str(v)))
        t.setSortingEnabled(True)
        l_.addWidget(t, 1)
        b = QHBoxLayout()
        b.addStretch()

        def save():
            fn, _ = QFileDialog.getSaveFileName(d, "Save CSV", str(Path.home() / "runs_compare.csv"), "CSV (*.csv)")
            if fn:
                with open(fn, "w", newline="") as fh:
                    w = csv.writer(fh)
                    w.writerow(header)
                    w.writerows(rows)
        b.addWidget(btn("Save as CSV…", save))
        b.addWidget(btn("Close", d.close))
        l_.addLayout(b)
        d.exec()

    def nav_status(self):                       # runs in the background thread: no widgets here
        from mysys import runlog as RL
        runs = RL.all_runs(2000)
        bad = sum(1 for r in runs[:200] if r["status"] in ("failed", "died") and r["end_ts"]
                  and time.time() - r["end_ts"] < 86400)
        run = sum(1 for r in runs[:200] if r["status"] == "running")
        if bad:
            return f"{bad} failed today", "bad"
        return (f"{run} running" if run else f"{len(runs)} runs"), ("info" if run else None)


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
        self.q.textChanged.connect(later(self, self.refresh))
        self.only_timing = QCheckBox("only runs with timing")
        self.only_timing.toggled.connect(lambda _: self.refresh())
        self._csv, self._shown = (None, []), None
        self.count = QLabel()
        self.count.setObjectName("muted")
        row.addWidget(self.q, 1)
        row.addWidget(self.only_timing)
        row.addWidget(self.count)
        row.addWidget(btn("Open folder", self.open_folder))
        row.addWidget(btn("Open CSV", lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(str(REPORTS / "results.csv")))))
        lay.addLayout(row)
        self.tab = table([c[1] for c in self.COLS], -1,
                         "No results yet. They appear here when a Vivado / HLS run that my-system tracks finishes.")
        self.tab.setSortingEnabled(True)
        lay.addWidget(self.tab, 1)

    def rows(self):
        """results.csv, read again only when the file changed (it grows to several MB)."""
        f = REPORTS / "results.csv"
        try:
            st = f.stat()
        except OSError:
            return []
        key = (st.st_mtime_ns, st.st_size)
        if self._csv[0] != key:
            with open(f, newline="", errors="replace") as fh:
                self._csv = (key, list(csv.DictReader(fh)))
        return self._csv[1]

    def refresh(self):
        all_rows = self.rows()
        q = self.q.text().strip().lower()
        shown = (self._csv[0], q, self.only_timing.isChecked())
        if shown == self._shown:               # nothing changed since the table was filled
            return
        self._shown = shown
        rows = list(reversed(all_rows))
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
                    except (ValueError, TypeError):
                        pass
                    it = NumItem(v, "{:+.3f}", lv)
                elif k == "seconds":
                    it = NumItem(v, "{:.0f}")
                    try:
                        it.setText(dur(float(v)) if float(v) else "")
                    except (ValueError, TypeError):
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
        rc, rl = _card()
        rl.addWidget(_lab("REMOTE SERVERS (run jobs on a Slurm cluster such as Titan)", "cardTitle"))
        self.remote_info = _lab("", wrap=True)
        rl.addWidget(self.remote_info)
        rrow = QHBoxLayout()
        rrow.addWidget(btn("Check connection", lambda: self.remote("check")))
        rrow.addWidget(btn("Test with a small job", lambda: self.remote("test")))
        rrow.addWidget(btn("Status", lambda: self.remote("status")))
        rrow.addStretch()
        rl.addLayout(rrow)
        lay.addWidget(rc)
        from mysys import pause as PZ
        pc, pl = _card()
        pl.addWidget(_lab("PAUSE PARTS OF MY-SYSTEM (the AI guard always stays on)", "cardTitle"))
        self.pause_state = _lab("", wrap=True)
        pl.addWidget(self.pause_state)
        grid = QGridLayout()
        grid.setHorizontalSpacing(18)
        self.pause_box = {}
        for i, (f, d) in enumerate(PZ.FEATURES.items()):
            cb = QCheckBox(f)
            cb.setToolTip(d)
            self.pause_box[f] = cb
            grid.addWidget(cb, i // 4, i % 4)
        pl.addLayout(grid)
        prow = QHBoxLayout()
        self.pause_for = QComboBox()
        self.pause_for.addItems(["for 1 hour", "for 4 hours", "until tomorrow 08:00", "until I turn it on"])
        prow.addWidget(self.pause_for)
        prow.addWidget(btn("Pause selected", self.do_pause, "primary"))
        prow.addWidget(btn("Turn selected on", self.do_resume))
        prow.addWidget(btn("Turn everything on", lambda: self.do_resume(all_=True)))
        prow.addStretch()
        pl.addLayout(prow)
        lay.addWidget(pc)
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
        self._pause_refresh()
        try:
            from mysys import remote as RM
            rs = RM.remotes()
        except Exception:
            rs = {}
        if rs:
            lines = []
            for n, r in rs.items():
                c = RM._cache(n)
                st = "not checked yet" if not c else (f"reachable, {c.get('idle_cpus')} of {c.get('total_cpus')} CPUs idle"
                                                      if c.get("ok") else f"not reachable: {c.get('error', '')[:80]}")
                lines.append(f"{n}: ssh {r['ssh']} · account {r['account'] or '(not set)'} · automatic use: {r['offload']} · {st}")
            self.remote_info.setText("\n".join(lines) + "\nUse: ms q add --on " + next(iter(rs)) + " -- CMD   (or --on auto)")
        else:
            self.remote_info.setText("None configured. Set up: ms remote help (Titan: ms remote setup titan).")
        parts = []
        for f in sorted((BASE / "config").glob("*")):
            parts.append(f"──── {f.name} " + "─" * max(0, 60 - len(f.name)) + "\n" + read(f).rstrip())
        self.conf.setPlainText("\n\n".join(parts) or "(no config files)")

    def _pause_refresh(self):
        from mysys import pause as PZ
        act = dict(PZ.active())
        self.pause_state.setText(("Paused now: " + PZ.summary()) if act else "Everything is on.")
        self.pause_state.setStyleSheet(f"color:{tone('warn')};" if act else f"color:{tone('ok')};")
        for f, cb in self.pause_box.items():
            cb.setText(f + (f"  (paused {PZ.describe(act[f])})" if f in act else ""))

    def do_pause(self):
        from mysys import pause as PZ
        sel = [f for f, cb in self.pause_box.items() if cb.isChecked()]
        if not sel:
            QMessageBox.information(self, "Pause", "Tick what to pause first.")
            return
        i = self.pause_for.currentIndex()
        now = time.time()
        if i == 0:
            end = now + 3600
        elif i == 1:
            end = now + 4 * 3600
        elif i == 2:
            lt = time.localtime(now + 86400)
            end = time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, 8, 0, 0, 0, 0, -1))
        else:
            end = 0
        for f in sel:
            PZ.set_pause(f, end)
            self.pause_box[f].setChecked(False)
        self._pause_refresh()
        self.win.refresh_nav()

    def do_resume(self, all_=False):
        from mysys import pause as PZ
        if all_:
            PZ.resume("all")
        for f, cb in self.pause_box.items():
            if cb.isChecked() or all_:
                PZ.resume(f)
                cb.setChecked(False)
        self._pause_refresh()
        self.win.refresh_nav()

    def remote(self, what):
        from mysys import remote as RM
        names = list(RM.remotes())
        if not names:
            QMessageBox.information(self, "Remote servers", "No server configured yet.\n\nIn a terminal: ms unlock, then "
                                    "ms remote setup titan, edit config/remotes.conf, then ms lock. Details: ms remote help")
            return
        self.win.run_in_dialog(f"Remote {names[0]}: {what}", ["/usr/bin/python3", str(SCRIPTS / "ms"), "remote", what, names[0]])

    def redetect(self):
        self.win.run_in_dialog("Detection", ["/usr/bin/python3", str(SCRIPTS / "ms"), "detect", "--state"])

    def doctor(self):
        self.win.run_in_dialog("Self-check", ["/usr/bin/python3", str(SCRIPTS / "ms"), "doctor"])

    def nav_status(self):
        from mysys import pause as PZ
        act = PZ.active()
        if act:
            return f"{len(act)} paused", "warn"
        locked = SCRIPTS.exists() and SCRIPTS.stat().st_uid == 0
        return ("locked" if locked else "unlocked"), (None if locked else "warn")


# ======================= sidebar =======================
class HistoryPage(Page):
    title, icon, fallback = "History", "document-open-recent", "document-open-recent"

    def __init__(self, win):
        super().__init__(win)
        lay = QVBoxLayout(self)
        lay.addLayout(header("History", "What the machine was doing at any minute of the last days: load, free RAM, swap, "
                                        "temperature, memory/disk stalls, the biggest programs and the jobs that ran. "
                                        "After an unexpected stop (power loss, freeze) you get a notification with the cause."))
        row = QHBoxLayout()
        self.when = QDateTimeEdit(QDateTime.currentDateTime().addSecs(-1800))
        self.when.setDisplayFormat("yyyy-MM-dd HH:mm")
        self.when.setCalendarPopup(True)
        self.span = QSpinBox()
        self.span.setRange(1, 720)
        self.span.setValue(15)
        self.span.setSuffix(" min around it")
        row.addWidget(QLabel("Time"))
        row.addWidget(self.when)
        row.addWidget(self.span)
        row.addWidget(btn("Show", self.show_time, "primary"))
        row.addWidget(btn("Last hour", lambda: self._run(["replay", "60m", "--span", "60m", "--brief"])))
        row.addWidget(btn("Unexpected stops", lambda: self._run(["replay", "crashes"])))
        row.addStretch()
        lay.addLayout(row)
        self.view = mono(QPlainTextEdit())
        self.view.setReadOnly(True)
        lay.addWidget(self.view, 1)
        self._shown = False

    def _run(self, args):
        self.view.setPlainText(run_text(["/usr/bin/python3", str(SCRIPTS / "ms"), *args], 30))

    def show_time(self):
        self._run(["replay", self.when.dateTime().toString("yyyy-MM-dd HH:mm"), "--span", f"{self.span.value()}m"])

    def refresh(self):
        if not self._shown:
            self._shown = True
            self._run(["replay", "60m", "--span", "60m", "--brief"])

    def nav_status(self):
        try:
            lines = (REPORTS / "crashes.log").read_text().splitlines()
        except OSError:
            return "recording every minute", None
        last = lines[-1].split("\t")[0] if lines else ""
        return (f"last unexpected stop {last}", "warn") if last else ("recording every minute", None)


class NavItem(QWidget):
    def __init__(self, page):
        super().__init__()
        lay = QHBoxLayout(self)
        lay.setContentsMargins(14, 4, 10, 4)
        lay.setSpacing(10)
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
        lay.addLayout(col_, 1)
        lay.addWidget(self.dot)

    def set(self, text, level):
        self.sub.setText(text)
        self.sub.setVisible(bool(text))
        self.dot.setVisible(level in ("warn", "bad", "info"))
        self.dot.setStyleSheet(f"color:{tone(level)}; background:transparent; font-size:10px;")


# ======================= main window =======================
class Main(QMainWindow):
    GROUPS = [("WORK", [OverviewPage, RunsPage, QueuePage, ServersPage, RunLogsPage, ResultsPage, HistoryPage]),
              ("THIS COMPUTER", [SystemPage, StoragePage, SoftwarePage, InventoryPage, ProjectsPage]),
              ("PROTECTION", [BackupPage, GuardPage, ToolAccessPage]),
              ("MESSAGES & SETTINGS", [ReportsPage, NotifyPage, SettingsPage])]
    PAGES = [c for _, cs in GROUPS for c in cs]

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
        self.nav.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        sl.addWidget(self.nav, 1)
        self.theme_btn = btn("☾  Dark" if not DARK else "☀  Light", self.toggle_theme)
        self.theme_btn.setStyleSheet("margin:0 12px;")
        sl.addWidget(self.theme_btn)

        self.stack = QStackedWidget()
        self.pages, self.navitems, self.row_page = [], [], {}
        self.prof = {}                     # page -> {"init": s, "refresh": [s], "nav": [s]}  (ms gui --profile)
        self.slow = {}                     # page index -> seconds its last refresh took
        self.ticks = 0
        self.nav_pool = ThreadPoolExecutor(max_workers=1)
        self.nav_future = None
        group_of = {c: g for g, cs in self.GROUPS for c in cs}
        last_group = None
        for cls in self.PAGES:
            if group_of.get(cls) != last_group:
                last_group = group_of.get(cls)
                hd = QListWidgetItem(last_group)
                hd.setFlags(Qt.ItemFlag.NoItemFlags)
                f = hd.font()
                f.setPointSizeF(max(7.0, f.pointSizeF() * 0.78))
                f.setBold(True)
                hd.setFont(f)
                hd.setForeground(QColor(T["muted"]))
                hd.setSizeHint(QSize(200, 30 if self.nav.count() else 22))
                hd.setTextAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignBottom)
                self.nav.addItem(hd)
            t_init = time.time()
            try:
                page = cls(self)
            except Exception as e:           # one page that cannot be built must not stop the app from opening
                import traceback
                log_error(f"page {cls.__name__} could not be built", traceback.format_exc())
                page = BrokenPage(self, cls, e)
            self.prof.setdefault(cls.__name__, {"init": 0.0, "refresh": [], "nav": []})["init"] = time.time() - t_init
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
            it.setSizeHint(QSize(200, 40))
            self.row_page[self.nav.count()] = len(self.pages) - 1
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
        self.nav.setCurrentRow(min(self.row_page))
        self.closed = False
        QTimer.singleShot(0, self.refresh_nav)       # the window shows first; the sidebar fills in when ready
        self.poll_timer = QTimer(self)
        self.poll_timer.timeout.connect(self.apply_nav)
        self.poll_timer.start(200)
        self.tick_timer = QTimer(self)
        self.tick_timer.timeout.connect(self.tick)
        self.tick_timer.start(15000)

    def switch(self, row):
        if row not in self.row_page:
            return
        self.stack.setCurrentIndex(self.row_page[row])
        self.refresh(force=True)

    SLOW_S = 0.7                         # a page whose refresh takes longer is refreshed once a minute, not every 15 s

    def refresh(self, force=False):
        if getattr(self, "closed", False):
            return
        i = self.stack.currentIndex()
        slow = self.slow.get(i, 0) > self.SLOW_S
        if slow and not force and self.ticks % 4:
            return
        page = self.pages[i]
        t0 = time.time()
        try:
            page.refresh()
        except Exception as e:  # never let one bad file kill the app
            import traceback
            log_error(f"refresh of {type(page).__name__}", traceback.format_exc())
            self.foot.setText(f"Could not update this page: {type(e).__name__}: {str(e)[:140]}  ·  details in {GUI_ERRORS}")
            self.slow[i] = 0
            return
        took = time.time() - t0
        self.slow[i] = took
        self.prof.setdefault(type(page).__name__, {"init": 0.0, "refresh": [], "nav": []})["refresh"].append(took)
        every = "every minute (this page is slow to update)" if took > self.SLOW_S else "every 15 s"
        self.foot.setText(f"Updated {time.strftime('%H:%M:%S')}  ·  refreshes {every}")

    def refresh_nav(self):
        """Sidebar statuses are computed in a background thread, so a slow one never freezes the window."""
        if self.closed or (self.nav_future is not None and not self.nav_future.done()):
            return
        pages = list(self.pages)

        def work():
            out = []
            for p in pages:
                t0 = time.time()
                try:
                    v = p.nav_status()
                except Exception:
                    v = ("", None)
                out.append((v, time.time() - t0))
            return out
        try:
            self.nav_future = self.nav_pool.submit(work)
        except RuntimeError:               # the pool was shut down (the window is closing)
            return
        self.nav_future.applied = False

    def apply_nav(self):
        f = self.nav_future
        if f is None or not f.done() or f.applied:
            return
        f.applied = True
        try:
            res = f.result()
        except Exception:
            return
        for (v, took), p, ni in zip(res, self.pages, self.navitems):
            ni.set(*v)
            self.prof.setdefault(type(p).__name__, {"init": 0.0, "refresh": [], "nav": []})["nav"].append(took)

    def tick(self):
        if self.closed:
            return
        self.ticks += 1
        self.refresh()
        self.refresh_nav()
        if PROFILE:
            self.write_profile()

    def write_profile(self):
        """ms gui --profile: how long each page takes (state/gui_profile.txt)."""
        rows = []
        for name, d in self.prof.items():
            rf, nv = d["refresh"], d["nav"]
            rows.append((max(rf + nv + [d["init"]]), name, d["init"], max(rf) if rf else 0, len(rf), max(nv) if nv else 0))
        lines = [f"my-system GUI profile {time.strftime('%F %T')} (seconds; worst of each)",
                 f"{'page':24s} {'start':>7s} {'refresh':>8s} {'(n)':>4s} {'sidebar':>8s}"]
        for _, name, ini, rf, n, nv in sorted(rows, reverse=True):
            lines.append(f"{name:24s} {ini:7.2f} {rf:8.2f} {n:4d} {nv:8.2f}")
        try:
            (STATE / "gui_profile.txt").write_text("\n".join(lines) + "\n")
        except OSError:
            pass

    def closeEvent(self, e):
        # stop everything that would call back into this window: a timer firing after the close used to end the
        # whole app ("cannot schedule new futures after shutdown"), e.g. 15 s after switching the theme
        self.closed = True
        for t in (self.poll_timer, self.tick_timer):
            t.stop()
        if PROFILE:
            self.write_profile()
        self.nav_pool.shutdown(wait=False, cancel_futures=True)
        super().closeEvent(e)

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
        self.deleteLater()

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
            if not self.closed:
                self.tick()

        def failed(err):
            if err == QProcess.ProcessError.FailedToStart:        # finished() never comes in this case
                if output is not None:
                    output.appendPlainText(f"\nCould not start {cmd[0]} — is it installed?")
                if p in self.procs:
                    self.procs.remove(p)
                if done:
                    done()

        p.readyRead.connect(out)
        p.finished.connect(fin)
        p.errorOccurred.connect(failed)
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


GUI_ERRORS = STATE / "gui_errors.log"
_last_error = {"t": 0.0, "n": 0}


def log_error(kind, text):
    """Append one problem of the app to state/gui_errors.log (kept under ~300 kB). Never raises."""
    try:
        STATE.mkdir(parents=True, exist_ok=True)
        if GUI_ERRORS.exists() and GUI_ERRORS.stat().st_size > 300_000:
            GUI_ERRORS.write_text(GUI_ERRORS.read_text(errors="replace")[-150_000:])
        with open(GUI_ERRORS, "a") as f:
            f.write(f"=== {time.strftime('%F %T')} {kind}\n{text.rstrip()}\n")
    except OSError:
        pass


def install_crash_guards():
    """PyQt6 ends the whole program when a Python error escapes from a button, timer or paint handler
    (it calls qFatal). With an excepthook installed the error is logged and shown in the footer, and the app
    keeps running. Hard crashes inside Qt/C code leave their stack in state/gui_crash.log (faulthandler)."""
    import faulthandler
    import threading
    import traceback

    def hook(etype, value, tb):
        if issubclass(etype, KeyboardInterrupt):
            QApplication.quit()
            return
        text = "".join(traceback.format_exception(etype, value, tb))
        log_error("error", text)
        sys.stderr.write(text)
        _last_error["t"], _last_error["n"] = time.time(), _last_error["n"] + 1
        try:
            w = getattr(QApplication.instance(), "_main", None)
            if w is not None:
                where = traceback.extract_tb(tb)[-1] if tb else None
                at = f" ({where.name}, line {where.lineno})" if where else ""
                w.foot.setText(f"Problem: {etype.__name__}: {str(value)[:140]}{at}  ·  the app keeps running; "
                               f"details in {GUI_ERRORS}")
        except Exception:
            pass
    sys.excepthook = hook
    threading.excepthook = lambda a: hook(a.exc_type, a.exc_value, a.exc_traceback)
    try:
        STATE.mkdir(parents=True, exist_ok=True)
        crash = STATE / "gui_crash.log"
        if crash.exists() and crash.stat().st_size > 0:        # the previous start ended in a hard crash: keep it
            log_error("hard crash of the previous start (stack from faulthandler)", crash.read_text(errors="replace")[-6000:])
        install_crash_guards.fh = open(crash, "w")
        faulthandler.enable(file=install_crash_guards.fh, all_threads=True)
    except OSError:
        pass


def main():
    install_crash_guards()
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
