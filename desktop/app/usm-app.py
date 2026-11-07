#!/usr/bin/env python3
"""USM, the app: a live view of RAM, swap and USM, and the VRAM tier and guard switches. GTK 4 + libadwaita.

Optional: `usm dash` shows the same in a terminal. The app reads what `usm dash` reads (/proc, /sys, /run/usm-*,
the logger's files; usm_dash.Sampler) and needs no root. The switches run /usr/local/lib/usm/usm-helper through
pkexec, as the GNOME menu does.

It draws with the CPU and keeps off the GPU: a context on the NVIDIA card counts as another program using the GPU,
and the VRAM tier would give VRAM back whenever the window opens. USM_APP_GPU=1 lets GTK use the GPU;
USM_APP_STILL=1 keeps the traffic dots still (they move only while the window is in front anyway).

  usm-app.py [--range 60|300|900|3600] [--screenshot FILE [--size WxH] [--scale N] [--wait S]]
"""
import ctypes
import math
import os
import sys
import time

HERE = os.path.dirname(os.path.realpath(__file__))
for _d in (HERE, os.path.join(HERE, "..", "..", "tools")):       # installed: next to usm_dash.py; the repository
    if os.path.exists(os.path.join(_d, "usm_dash.py")):
        sys.path.insert(0, os.path.abspath(_d))
        break


def _keep_off_the_gpu():
    """Before GTK opens the display: draw with the CPU, no GL or Vulkan."""
    if os.environ.get("USM_APP_GPU"):
        return
    os.environ.setdefault("GSK_RENDERER", "cairo")
    try:
        minor = ctypes.CDLL("libgtk-4.so.1").gtk_get_minor_version()
    except OSError:
        minor = 16
    if minor >= 16:
        os.environ.setdefault("GDK_DISABLE", "gl,vulkan")
    else:
        os.environ.setdefault("GDK_DEBUG", "gl-disable,vulkan-disable")


_keep_off_the_gpu()
import gi  # noqa: E402

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("Gsk", "4.0")
gi.require_version("Graphene", "1.0")
from gi.repository import Adw, Gdk, Gio, GLib, Graphene, Gsk, Gtk, Pango  # noqa: E402

import usm_dash as D  # noqa: E402
from usm_log import app_name, read_desktop  # noqa: E402

APP_ID = "com.kha333n.USM"
LIB = os.path.dirname(os.path.abspath(D.__file__))
HELPER = "/usr/local/lib/usm/usm-helper"
TOOL = os.path.join(LIB, "usm-tool.py")
VERSION = (D.read(os.path.join(LIB, "VERSION")) or D.read(os.path.join(HERE, "..", "..", "VERSION"))).strip()
RANGES = ((60, "1m"), (300, "5m"), (900, "15m"), (3600, "1h"))
HISTORY_S = 3600
MONO = "JetBrains Mono,Ubuntu Sans Mono,DejaVu Sans Mono,monospace"
COL = {"lime": "#a3ff12", "green": "#76b900", "cyan": "#22d3ee", "blue": "#60a5fa", "amber": "#f5a524",
       "coral": "#ff6b7a", "text": "#e6edf3", "muted": "#8b98a9", "panel": "#0d131d"}
H, V = Gtk.Orientation.HORIZONTAL, Gtk.Orientation.VERTICAL
CENTER = Gtk.Align.CENTER


# ---------------------------------------------------------------------------------- drawing helpers
def rgba(color, a=1.0):
    c = Gdk.RGBA()
    c.parse(COL.get(color, color))
    c.alpha = a
    return c


def pt(x, y):
    return Graphene.Point().init(x, y)


def rect(x, y, w, h):
    return Graphene.Rect().init(x, y, w, h)


def stops(*pairs):
    out = []
    for off, col in pairs:
        s = Gsk.ColorStop()
        s.offset, s.color = off, col
        out.append(s)
    return out


def stroke(snap, path, width, color, dash=None):
    s = Gsk.Stroke.new(width)
    s.set_line_join(Gsk.LineJoin.ROUND)
    s.set_line_cap(Gsk.LineCap.ROUND)
    if dash:
        s.set_dash(dash)
    snap.append_stroke(path, s, color)


def fill(snap, path, color):
    snap.append_fill(path, Gsk.FillRule.WINDING, color)


def fill_fade(snap, path, x0, x1, y_strong, y_weak, color, a_strong, a_weak):
    """Fill path with color, from a_strong at y_strong to a_weak at y_weak."""
    snap.push_fill(path, Gsk.FillRule.WINDING)
    top, bottom = min(y_strong, y_weak), max(y_strong, y_weak)
    snap.append_linear_gradient(rect(x0, top - 1, x1 - x0, bottom - top + 2), pt(0, y_strong), pt(0, y_weak),
                                stops((0, rgba(color, a_strong)), (1, rgba(color, a_weak))))
    snap.pop()


def circle(x, y, r):
    b = Gsk.PathBuilder.new()
    b.add_circle(pt(x, y), r)
    return b.to_path()


def rounded(x, y, w, h, r):
    rr = Gsk.RoundedRect()              # keep it: init_from_rect() returns a view into this struct, not a copy
    rr.init_from_rect(rect(x, y, w, h), r)
    return rr


def polyline(pts_):
    b = Gsk.PathBuilder.new()
    b.move_to(*pts_[0])
    for p in pts_[1:]:
        b.line_to(*p)
    return b


def text(snap, widget, s, x, y, size=8.5, color=None, align=0.0, valign=0.0, bold=False, mono=False):
    lay = widget.create_pango_layout(s)
    fd = Pango.FontDescription.from_string(MONO) if mono else widget.get_pango_context().get_font_description().copy()
    fd.set_size(int(size * Pango.SCALE))
    fd.set_weight(Pango.Weight.BOLD if bold else Pango.Weight.NORMAL)
    lay.set_font_description(fd)
    w, h = lay.get_pixel_size()
    snap.save()
    snap.translate(pt(round(x - w * align), round(y - h * valign)))
    snap.append_layout(lay, color or rgba("muted"))
    snap.restore()
    return w, h


def text_size(widget, s, size=8.5, bold=False, mono=False):
    lay = widget.create_pango_layout(s)
    fd = Pango.FontDescription.from_string(MONO) if mono else widget.get_pango_context().get_font_description().copy()
    fd.set_size(int(size * Pango.SCALE))
    fd.set_weight(Pango.Weight.BOLD if bold else Pango.Weight.NORMAL)
    lay.set_font_description(fd)
    return lay.get_pixel_size()


# ---------------------------------------------------------------------------------- formatting
def fmt_size(mb):
    if mb is None:
        return "–"
    return f"{mb / 1024:.1f} GiB" if abs(mb) >= 1024 else f"{mb:.0f} MiB"


def fmt_rate(mbs):
    if mbs is None:
        return "–"
    if mbs >= 100:
        return f"{mbs:.0f} MiB/s"
    if mbs >= 1:
        return f"{mbs:.1f} MiB/s"
    kb = mbs * 1024
    return f"{kb:.0f} KiB/s" if kb >= 0.5 else "0"


def fmt_lat(us, mark=""):
    if us is None:
        return "–"
    m = {"~": "~", "<": "< "}.get(mark, "")
    if us < 1000:
        return f"{m}{us:.0f} µs"
    if us < 100000:
        return f"{m}{us / 1000:.1f} ms"
    return f"{m}{us / 1000:.0f} ms"


def fmt_pct(v):
    if v is None:
        return "–"
    return "0%" if v < 0.05 else f"{v:.0f}%" if v >= 10 else f"{v:.1f}%"


def ago(s):
    s = max(0, int(s))
    return "now" if s < 2 else f"{s} s ago" if s < 120 else f"{s // 60} min ago"


def label(text_="", classes=(), xalign=0.0, **kw):
    lb = Gtk.Label(label=text_, xalign=xalign, **kw)
    for c in classes:
        lb.add_css_class(c)
    return lb


def box(orientation=H, spacing=0, classes=(), children=(), **kw):
    b = Gtk.Box(orientation=orientation, spacing=spacing, **kw)
    for c in classes:
        b.add_css_class(c)
    for ch in children:
        b.append(ch)
    return b


def set_kind(widget, kinds, kind):
    """One of kinds as the widget's CSS class."""
    for k in kinds:
        if k != kind:
            widget.remove_css_class(k)
    if kind:
        widget.add_css_class(kind)


# ---------------------------------------------------------------------------------- widgets
class Chart(Gtk.Widget):
    """A chart of the window's time range, one column per pixel at most. kind:
    "stack"   the series stacked upward;  "overlay" each series from 0;
    "mirror"  the series with up=True stacked upward (pages going out), the others downward (pages coming back);
    "log"     lines on a log scale (latency).
    A series is {"name", "color", "get": f(index, record) -> value or None, "up"}."""
    L, R, T, B = 56, 10, 12, 22

    def __init__(self, win, kind, series, fmt, height=180, top=None, min_top=1.0, empty="", axis_fmt=None):
        super().__init__()
        self.win, self.kind, self.series, self.fmt = win, kind, series, fmt
        self.axis_fmt = axis_fmt or fmt
        self.fixed_top, self.min_top, self.empty = top, min_top, empty
        self.set_size_request(300, height)
        self.set_hexpand(True)
        self.hover = None
        self.cols = []
        m = Gtk.EventControllerMotion.new()
        m.connect("motion", lambda _c, x, y: self._hover((x, y)))
        m.connect("leave", lambda _c: self._hover(None))
        self.add_controller(m)

    def _hover(self, p):
        self.hover = p
        self.queue_draw()

    def _columns(self, L, R):
        """[(t, x, [value per series])], one per record or per pixel column; mean for stacks, max for the rest."""
        win, n = self.win, len(self.series)
        span = max(win.t1 - win.t0, 1)
        width = max(R - L, 1)
        per_px = len(win.view) > width
        mean = self.kind in ("stack", "mirror")
        cols, key0 = [], None
        for i, r in win.view:
            x = L + (r["t"] - win.t0) / span * width
            if x < L - 2:
                continue
            vals = [s["get"](i, r) for s in self.series]
            key = int(x - L) if per_px else None
            if per_px and cols and key == key0:
                c = cols[-1]
                c[0], c[1] = r["t"], x
                for k, v in enumerate(vals):
                    if v is None:
                        continue
                    if mean:
                        c[3][k] += v
                        c[4][k] += 1
                    else:
                        c[2][k] = v if c[2][k] is None else max(c[2][k], v)
                continue
            key0 = key
            cols.append([r["t"], x, list(vals), [v or 0.0 for v in vals], [1 if v is not None else 0 for v in vals]])
        out = []
        for t, x, vals, sums, cnt in cols:
            if mean and per_px:
                vals = [sums[k] / cnt[k] if cnt[k] else None for k in range(n)]
            out.append((t, x, vals))
        return out

    def _runs(self, cols):
        """Stretches without gaps (the PC asleep, the app paused)."""
        runs, cur = [], []
        gap = max(3.5, 2.5 * (self.win.t1 - self.win.t0) / max(len(cols), 1))
        for j, c in enumerate(cols):
            if cur and c[0] - cols[cur[-1]][0] > gap:
                runs.append(cur)
                cur = []
            cur.append(j)
        if cur:
            runs.append(cur)
        return runs

    def do_snapshot(self, snap):
        w, h = self.get_width(), self.get_height()
        if w < 80 or h < 60:
            return
        L, R, T, B = self.L, w - self.R, self.T, h - self.B
        self.cols = cols = self._columns(L, R)
        runs = self._runs(cols)
        grid = rgba("#ffffff", 0.07)
        if self.kind == "log":
            Y = self._draw_log(snap, cols, runs, L, R, T, B, grid)
        else:
            Y = self._draw_linear(snap, cols, runs, L, R, T, B, grid)
        self._x_axis(snap, L, R, B)
        if not any(v is not None and v > 0 for c in cols for v in c[2]) and self.empty:
            text(snap, self, self.empty, (L + R) / 2, (T + B) / 2, size=9.5, color=rgba("text", 0.4), align=0.5,
                 valign=0.5)
        if self.hover and cols and Y:
            self._draw_hover(snap, cols, L, R, T, B, Y)

    # -- linear charts
    def _draw_linear(self, snap, cols, runs, L, R, T, B, grid):
        mirror = self.kind == "mirror"
        up = [k for k, s in enumerate(self.series) if s.get("up", True) or not mirror]
        down = [k for k, s in enumerate(self.series) if mirror and not s.get("up", True)]

        def total(c, ks):
            return sum(c[2][k] or 0 for k in ks) if self.kind != "overlay" else max([c[2][k] or 0 for k in ks] + [0])
        peak = max([total(c, up) for c in cols] + [total(c, down) for c in cols] + [0])
        top = self.fixed_top or D.nice(max(peak, self.min_top))
        zero = (T + B) / 2 if mirror else B
        scale = (zero - T) / top

        # grid and scale
        for y in ((T, zero, B) if mirror else (T, (T + B) / 2, B)):
            g = Gsk.PathBuilder.new()
            g.move_to(L, y)
            g.line_to(R, y)
            stroke(snap, g.to_path(), 1, grid if y != zero or not mirror else rgba("#ffffff", 0.16),
                   dash=None if (y == B and not mirror) or (mirror and y == zero) else [2, 4])
        if mirror:
            for y, s in ((T, self.axis_fmt(top)), (zero, "0"), (B, self.axis_fmt(top))):
                text(snap, self, s, L - 8, y, size=8, align=1, valign=0.5, mono=True)
            text(snap, self, "out ↑", L + 6, T + 2, size=7.5, color=rgba("text", 0.35), bold=True)
            text(snap, self, "in ↓", L + 6, B - 2, size=7.5, color=rgba("text", 0.35), bold=True, valign=1)
        else:
            for y, v in ((T, top), ((T + B) / 2, top / 2), (B, 0)):
                text(snap, self, self.axis_fmt(v) if v else "0", L - 8, y, size=8, align=1, valign=0.5, mono=True)

        def Y(v, k):
            return zero + v * scale if k in down else zero - v * scale
        for run in runs:
            if len(run) < 2:
                continue
            xs = [cols[j][1] for j in run]
            for ks in (up, down):
                base = [0.0] * len(run)
                for k in ks:
                    s = self.series[k]
                    vals = [cols[j][2][k] or 0.0 for j in run]
                    lower = base if self.kind != "overlay" else [0.0] * len(run)
                    upper = [a + b for a, b in zip(lower, vals)]
                    if max(vals) <= 0:
                        base = upper
                        continue
                    pu = [(x, Y(v, k)) for x, v in zip(xs, upper)]
                    pl = [(x, Y(v, k)) for x, v in zip(xs, lower)]
                    b = polyline(pu)
                    for p in reversed(pl):
                        b.line_to(*p)
                    b.close()
                    y_far = T if k not in down else B
                    fill_fade(snap, b.to_path(), L, R, y_far, zero, s["color"], 0.42, 0.06)
                    seg = []
                    for n_, v in enumerate(vals + [0.0]):
                        near = v > 0 or (n_ + 1 < len(vals) and vals[n_ + 1] > 0) or (n_ and vals[n_ - 1] > 0)
                        if n_ < len(vals) and near:
                            seg.append(pu[n_])
                            continue
                        if len(seg) >= 2:
                            stroke(snap, polyline(seg).to_path(), 1.6, rgba(s["color"]))
                        seg = []
                    base = upper
        return Y

    # -- log chart (latency)
    def _draw_log(self, snap, cols, runs, L, R, T, B, grid):
        vals = [v for c in cols for v in c[2] if v is not None and v > 0]
        lo = math.floor(math.log10(min(vals))) if vals else 1
        hi = math.ceil(math.log10(max(vals))) if vals else 3
        if hi - lo < 2:
            hi = lo + 2
        span = hi - lo

        def Y(v, _k=None):
            return B - (math.log10(max(v, 10 ** lo)) - lo) / span * (B - T)
        for d in range(lo, hi + 1):
            y = Y(10 ** d)
            g = Gsk.PathBuilder.new()
            g.move_to(L, y)
            g.line_to(R, y)
            stroke(snap, g.to_path(), 1, grid, dash=None if d == lo else [2, 4])
            us = 10 ** d
            text(snap, self, f"{us / 1000:g} ms" if us >= 1000 else f"{us:g} µs", L - 8, y, size=8, align=1,
                 valign=0.5, mono=True)
        for k, s in enumerate(self.series):
            for run in runs:
                seg = []
                for j in run + [None]:
                    v = cols[j][2][k] if j is not None else None
                    if v is not None and v > 0:
                        seg.append((cols[j][1], Y(v)))
                        continue
                    if len(seg) >= 2:
                        b = polyline(seg)
                        b.line_to(seg[-1][0], B)
                        b.line_to(seg[0][0], B)
                        b.close()
                        fill_fade(snap, b.to_path(), L, R, T, B, s["color"], 0.14, 0.0)
                        stroke(snap, polyline(seg).to_path(), 1.7, rgba(s["color"]))
                    elif len(seg) == 1:
                        fill(snap, circle(seg[0][0], seg[0][1], 1.8), rgba(s["color"]))
                    seg = []
            last = next(((c[1], c[2][k]) for c in reversed(cols) if c[2][k]), None)
            if last and self.win.t1 - cols[-1][0] < 5 and last[0] >= cols[-1][1] - 2:
                fill(snap, circle(last[0], Y(last[1]), 6), rgba(s["color"], 0.18))
                fill(snap, circle(last[0], Y(last[1]), 2.8), rgba(s["color"]))
        return Y

    # -- time axis and hover
    def _x_axis(self, snap, L, R, B):
        win = self.win
        span = win.t1 - win.t0
        step = {60: 15, 300: 60, 900: 300, 3600: 900}.get(int(round(span)), max(60, int(span / 5)))
        k = 1
        text(snap, self, "now", R, B + 6, size=8, align=1, mono=True)
        while k * step < span - step * 0.4:
            x = R - k * step / span * (R - L)
            s = f"−{k * step}s" if step < 60 else f"−{k * step // 60}m"
            text(snap, self, s, x, B + 6, size=8, align=0.5, mono=True)
            k += 1

    def _draw_hover(self, snap, cols, L, R, T, B, Y):
        hx, hy = self.hover
        if not L <= hx <= R:
            return
        j = min(range(len(cols)), key=lambda i: abs(cols[i][1] - hx))
        t, x, vals = cols[j]
        g = Gsk.PathBuilder.new()
        g.move_to(x, T)
        g.line_to(x, B)
        stroke(snap, g.to_path(), 1, rgba("#ffffff", 0.35), dash=[3, 3])
        rows = []
        mirror = self.kind == "mirror"
        acc = {True: 0.0, False: 0.0}
        for k, s in enumerate(self.series):
            v = vals[k]
            up = s.get("up", True) or not mirror
            if v is None:
                continue
            y_val = v if self.kind in ("log", "overlay") else acc[up] + v
            if self.kind not in ("log", "overlay"):
                acc[up] += v
            if v > 0 or self.kind != "log":
                fill(snap, circle(x, Y(y_val, k), 3.2), rgba(s["color"]))
            rows.append((s, (self.fmt(v) if self.kind != "log" else fmt_lat(v))))
        if not rows:
            return
        head = f"{time.strftime('%H:%M:%S', time.localtime(t))} · {ago(self.win.t1 - t)}"
        hw, hh = text_size(self, head, 8.5, bold=True)
        lines = [(s, f"{s['name']}  {v}") for s, v in rows]
        lw = max([text_size(self, ln, 8.5, mono=True)[0] for _s, ln in lines] + [hw])
        bw, bh = lw + 34, hh + 8 + len(lines) * (hh + 3) + 10
        bx = x + 14 if x + 14 + bw < R else x - 14 - bw
        by = max(T, min(hy - bh / 2, B - bh))
        snap.push_rounded_clip(rounded(bx, by, bw, bh, 10))
        snap.append_color(rgba(COL["panel"], 0.94), rect(bx, by, bw, bh))
        snap.pop()
        snap.append_border(rounded(bx, by, bw, bh, 10), [1, 1, 1, 1], [rgba("#ffffff", 0.12)] * 4)
        text(snap, self, head, bx + 12, by + 8, size=8.5, color=rgba("text"), bold=True)
        y = by + 8 + hh + 6
        for s, ln in lines:
            fill(snap, circle(bx + 16, y + hh / 2, 3.2), rgba(s["color"]))
            text(snap, self, ln, bx + 26, y, size=8.5, color=rgba("text", 0.9), mono=True)
            y += hh + 3


class Ring(Gtk.Widget):
    """A ring gauge with a value in the middle."""

    def __init__(self, size=118, width=10):
        super().__init__()
        self.width, self.frac, self.value, self.sub = width, 0.0, "", ""
        self.colors = ("blue", "cyan")
        self.set_size_request(size, size)
        self.set_valign(CENTER)

    def set(self, frac, value, sub, colors):
        self.frac, self.value, self.sub, self.colors = max(0.0, min(1.0, frac)), value, sub, colors
        self.queue_draw()

    def do_snapshot(self, snap):
        w, h = self.get_width(), self.get_height()
        cx, cy = w / 2, h / 2
        r = min(w, h) / 2 - self.width / 2 - 2
        stroke(snap, circle(cx, cy, r), self.width, rgba("#ffffff", 0.07))
        f = self.frac
        if f > 0.004:
            b = Gsk.PathBuilder.new()
            if f >= 0.999:
                b.add_circle(pt(cx, cy), r)
            else:
                a = -math.pi / 2 + 2 * math.pi * f
                b.move_to(cx, cy - r)
                b.svg_arc_to(r, r, 0, f > 0.5, True, cx + r * math.cos(a), cy + r * math.sin(a))
            st = Gsk.Stroke.new(self.width)
            st.set_line_cap(Gsk.LineCap.ROUND)
            snap.push_stroke(b.to_path(), st)
            snap.append_linear_gradient(rect(0, 0, w, h), pt(w, 0), pt(0, h),
                                        stops((0, rgba(self.colors[0])), (1, rgba(self.colors[1]))))
            snap.pop()
        text(snap, self, self.value, cx, cy + 1, size=18, color=rgba("text"), align=0.5, valign=0.82, bold=True)
        text(snap, self, self.sub, cx, cy + 3, size=7.5, color=rgba("text", 0.5), align=0.5, bold=True)


class Bar(Gtk.Widget):
    """A thin rounded bar of coloured parts, [(fraction, colour)]."""

    def __init__(self, height=7):
        super().__init__()
        self.parts = []
        self.set_size_request(40, height)
        self.set_hexpand(True)
        self.set_valign(CENTER)

    def set(self, parts):
        self.parts = parts
        self.queue_draw()

    def do_snapshot(self, snap):
        w, h = self.get_width(), self.get_height()
        snap.push_rounded_clip(rounded(0, 0, w, h, h / 2))
        snap.append_color(rgba("#ffffff", 0.08), rect(0, 0, w, h))
        x = 0.0
        for f, col in self.parts:
            pw = max(0.0, min(1.0, f)) * w
            if 0 < f and pw < 3:
                pw = 3
            pw = min(pw, w - x)
            if pw >= 0.5:
                snap.append_linear_gradient(rect(x, 0, pw, h), pt(x, 0), pt(x + pw, 0),
                                            stops((0, rgba(col, 0.7)), (1, rgba(col, 1.0))))
            x += pw
        snap.pop()


class Link(Gtk.Widget):
    """The way between two tiers: dots run right for pages going out, left for pages coming back; more dots for
    more traffic. The window moves them (Window._animate), a few frames a second while it is in front."""

    def __init__(self, color):
        super().__init__()
        self.color, self.out, self.inn, self.phase = color, 0.0, 0.0, 0.0
        self.set_size_request(54, 64)
        self.set_valign(CENTER)

    @property
    def moving(self):
        return self.get_visible() and (self.out > 0.002 or self.inn > 0.002)

    def set_rates(self, out, inn):
        self.out, self.inn = out or 0.0, inn or 0.0
        self.queue_draw()

    @staticmethod
    def _dots(rate):
        return 0 if rate <= 0.002 else max(1, min(6, 1 + int(2 * math.log10(1 + rate * 8))))

    def do_snapshot(self, snap):
        w, h = self.get_width(), self.get_height()
        for lane, rate, y in ((0, self.out, h * 0.38), (1, self.inn, h * 0.62)):
            g = Gsk.PathBuilder.new()
            g.move_to(4, y)
            g.line_to(w - 4, y)
            stroke(snap, g.to_path(), 1.2, rgba(self.color, 0.22 if rate > 0.002 else 0.10), dash=[2, 4])
            a = Gsk.PathBuilder.new()                       # arrow head: out to the right, in to the left
            if lane == 0:
                a.move_to(w - 9, y - 3.5)
                a.line_to(w - 4, y)
                a.line_to(w - 9, y + 3.5)
            else:
                a.move_to(9, y - 3.5)
                a.line_to(4, y)
                a.line_to(9, y + 3.5)
            stroke(snap, a.to_path(), 1.4, rgba(self.color, 0.55 if rate > 0.002 else 0.18))
            n = self._dots(rate)
            for k in range(n):
                f = (self.phase * 0.6 + k / n) % 1.0
                x = 6 + f * (w - 16) if lane == 0 else w - 6 - f * (w - 16)
                a_ = math.sin(math.pi * f)
                fill(snap, circle(x, y, 5), rgba(self.color, 0.20 * a_))
                fill(snap, circle(x, y, 2.4), rgba(self.color, 0.95 * a_))


class TierNode(Gtk.Box):
    """One memory tier: name, a big figure, a bar of how full it is, and its traffic."""

    def __init__(self, key, name, color, caption):
        super().__init__(orientation=V, spacing=6)
        self.add_css_class("tier")
        self.add_css_class(key)
        self.set_hexpand(True)
        self.dot = label("●", (color,))
        self.name = label(name, ("tier-name",), hexpand=True)
        self.pill = label("", ("pill",), valign=CENTER, visible=False)
        self.append(box(H, 7, children=(self.dot, self.name, self.pill)))
        self.value = label("–", ("big",))
        self.caption = label(caption, ("caption",))
        self.append(box(V, 0, children=(self.value, self.caption), margin_top=2))
        self.bar = Bar(6)
        self.bar.set_margin_top(6)
        self.append(self.bar)
        self.usage = label("", ("tiny", "muted", "num"), ellipsize=Pango.EllipsizeMode.END)
        self.traffic = label("", ("tiny", "mono", "muted"), ellipsize=Pango.EllipsizeMode.END)
        self.append(self.usage)
        self.append(self.traffic)

    def show(self, value, bar, usage, traffic="", caption=None, pill=None, pill_kind="on", dim=False):
        self.value.set_text(value)
        if caption is not None:
            self.caption.set_text(caption)
        self.bar.set(bar)
        self.usage.set_text(usage)
        self.traffic.set_text(traffic)
        self.traffic.set_visible(bool(traffic))
        self.pill.set_visible(bool(pill))
        if pill:
            self.pill.set_text(pill)
            set_kind(self.pill, ("on", "warn", "off", "bad"), pill_kind)
        set_kind(self, ("dim",), "dim" if dim else None)


class Control(Gtk.Box):
    """A switch for the VRAM tier or the guard. Clicking runs usm-helper through pkexec; showing the current
    state never switches anything (the handler is blocked meanwhile)."""

    def __init__(self, win, title, verb_on, verb_off):
        super().__init__(orientation=H, spacing=12)
        self.add_css_class("control")
        self.win, self.verbs, self.busy = win, (verb_off, verb_on), False
        self.title = label(title, ("control-title",))
        self.sub = label("", ("small", "muted"), ellipsize=Pango.EllipsizeMode.END)
        self.append(box(V, 2, children=(self.title, self.sub), hexpand=True, valign=CENTER))
        self.pill = label("", ("pill",), valign=CENTER)
        self.spinner = Gtk.Spinner(valign=CENTER, visible=False)
        self.switch = Gtk.Switch(valign=CENTER)
        self.hid = self.switch.connect("state-set", self._on_state_set)
        for wdg in (self.pill, self.spinner, self.switch):
            self.append(wdg)
        if not os.path.exists(HELPER):
            self.switch.set_sensitive(False)
            self.switch.set_tooltip_text("USM is not installed here")

    def _on_state_set(self, sw, on):
        if self.busy:
            return True
        self.busy = True
        sw.set_sensitive(False)
        self.spinner.set_visible(True)
        self.spinner.start()
        self.sub.set_text("Switching on…" if on else "Switching off…")
        self.win.run_helper(self.verbs[1 if on else 0], self._done)
        return True                              # the switch shows the new state once it is there

    def _done(self, ok, out):
        self.busy = False
        self.spinner.stop()
        self.spinner.set_visible(False)
        self.switch.set_sensitive(True)
        if not ok:
            last = [ln for ln in out.splitlines() if ln.strip()][-1:] or ["it did not work"]
            self.win.toast(f"{self.title.get_text()}: {last[0][:160]}")
        self.win.refresh()

    def show(self, on, sub, pill, pill_kind):
        if self.busy:
            return
        with self.switch.handler_block(self.hid):
            self.switch.set_active(on)
            self.switch.set_state(on)
        self.sub.set_text(sub)
        self.pill.set_text(pill)
        self.pill.set_visible(bool(pill))
        set_kind(self.pill, ("on", "warn", "off", "bad"), pill_kind)


class ChartCard(Gtk.Box):
    def __init__(self, title, chart, legend, note=""):
        super().__init__(orientation=V, spacing=10)
        self.add_css_class("usm-card")
        self.values = {}
        head = box(H, 14)
        head.append(box(V, 2, children=[label(title, ("card-title",), ellipsize=Pango.EllipsizeMode.END)] +
                        ([label(note, ("tiny", "muted"), ellipsize=Pango.EllipsizeMode.END)] if note else []),
                        hexpand=True))
        for key, name, color in legend:
            v = label("", ("small", "mono", "num"))
            self.values[key] = v
            head.append(box(H, 6, children=(label("●", (color,)), label(name, ("small", "muted")), v),
                            valign=Gtk.Align.START))
        self.append(head)
        self.append(chart)
        self.chart = chart

    def set_value(self, key, s):
        self.values[key].set_text(s)


class AppRow(Gtk.Box):
    def __init__(self):
        super().__init__(orientation=H, spacing=12)
        self.icon = Gtk.Image(pixel_size=28, valign=CENTER)
        self.name = label("", ("app-name",), ellipsize=Pango.EllipsizeMode.END, hexpand=True)
        self.pill = label("PAUSED", ("pill", "warn"), visible=False)
        self.bar = Bar(6)
        self.value = label("", ("tiny", "mono", "muted", "num"), xalign=1.0)
        mid = box(V, 6, children=(box(H, 8, children=(self.name, self.pill, self.value)), self.bar), hexpand=True,
                  valign=CENTER)
        self.append(self.icon)
        self.append(mid)
        self.set_margin_top(4)
        self.set_margin_bottom(4)


class EventRow(Gtk.Box):
    KINDS = ((" stall from ", "dialog-warning-symbolic", "amber"), ("VRAM tier", "computer-chip-symbolic", "lime"),
             ("GPU memory", "video-display-symbolic", "lime"), ("swap on", "drive-harddisk-symbolic", "cyan"),
             ("swap off", "drive-harddisk-symbolic", "cyan"), ("woke up", "weather-clear-night-symbolic", "muted"),
             ("pause", "media-playback-pause-symbolic", "amber"), ("OOM", "process-stop-symbolic", "coral"),
             ("logger", "document-edit-symbolic", "faint"))

    def __init__(self):
        super().__init__(orientation=H, spacing=10)
        self.time = label("", ("event-time", "mono"), width_chars=8, valign=Gtk.Align.START)
        self.icon = Gtk.Image(pixel_size=14, valign=Gtk.Align.START, margin_top=2)
        self.text = label("", ("event-text",), wrap=True, hexpand=True, lines=2, ellipsize=Pango.EllipsizeMode.END,
                          max_width_chars=40)
        self.text.set_wrap_mode(Pango.WrapMode.WORD_CHAR)
        for wdg in (self.time, self.icon, self.text):
            self.append(wdg)
        self.set_margin_top(3)
        self.set_margin_bottom(3)
        self.kind = None

    def show(self, line):
        day, clock, rest = line[:10], line[11:19], line[20:]
        self.time.set_text(clock if day == time.strftime("%Y-%m-%d") else f"{day[5:]}")
        self.time.set_tooltip_text(line[:19])
        icon, kind = "dialog-information-symbolic", "muted"
        for key, ic, k in self.KINDS:
            if key in rest:
                icon, kind = ic, k
                break
        theme = Gtk.IconTheme.get_for_display(self.get_display())
        self.icon.set_from_icon_name(icon if theme.has_icon(icon) else "dialog-information-symbolic")
        set_kind(self.icon, ("amber", "lime", "cyan", "muted", "coral", "faint"), kind)
        self.text.set_text(rest)
        self.text.set_tooltip_text(rest if len(rest) > 90 else None)


# ---------------------------------------------------------------------------------- the window
def tier_state(ts, has_device):
    if not has_device:
        return "off"
    if ts.get("switched_off") == "1":
        return "spilled"
    if ts.get("vram_held") == "0":
        return "given back"
    if int(ts.get("given_back_mb", "0") or 0) > 0:
        return "lending"
    return "on"


class Window(Adw.ApplicationWindow):
    def __init__(self, app, span, size):
        super().__init__(application=app, title="USM")
        self.add_css_class("usm")
        self.set_default_size(*size)
        self.set_size_request(900, 560)
        self.span, self.uid, self.n = span, os.getuid(), 0
        self.s = D.Sampler(HISTORY_S)
        self.s.prefill(D.LOG_DIR)
        self.s.step()
        self.t1 = time.time()
        self.t0 = self.t1 - span
        self.view, self.vdev, self.ddev = [], None, None
        self._apps_cache, self._by_label = None, {}
        self.toasts = Adw.ToastOverlay()
        header, content = self._header(), self._content()
        if hasattr(Adw, "ToolbarView"):
            tv = Adw.ToolbarView()
            tv.add_top_bar(header)
            tv.set_content(content)
            self.toasts.set_child(tv)
        else:
            self.toasts.set_child(box(V, 0, children=(header, content)))
        self.set_content(self.toasts)
        self._anim = 0
        self.connect("notify::is-active", lambda *_: self._animate_check())
        self.refresh()
        GLib.timeout_add(1000, self._tick)

    # -- building
    def _header(self):
        hb = Adw.HeaderBar()
        layout = Gtk.Settings.get_default().get_property("gtk-decoration-layout") or ""
        if "icon" in layout:                         # the brand already shows it
            hb.set_decoration_layout(":".join(",".join(t for t in side.split(",") if t and t != "icon")
                                              for side in layout.split(":")))
        theme = Gtk.IconTheme.get_for_display(Gdk.Display.get_default())
        icon_file = os.path.join(HERE, f"{APP_ID}.svg")
        if theme.has_icon(APP_ID) or not os.path.exists(icon_file):
            icon = Gtk.Image(icon_name=APP_ID, pixel_size=24)
        else:
            icon = Gtk.Image.new_from_file(icon_file)
            icon.set_pixel_size(24)
        brand = box(H, 8, children=(icon, label("USM", ("brand-name",))), valign=CENTER)
        if VERSION:
            ver = VERSION.split("-")[-1] if "-" in VERSION else VERSION
            brand.append(label(ver, ("brand-version", "mono"), valign=CENTER, tooltip_text=f"USM {VERSION}"))
        hb.pack_start(brand)
        rng = box(H, 2, classes=("range",), valign=CENTER)
        first = None
        for secs, name in RANGES:
            b = Gtk.ToggleButton(label=name, tooltip_text=f"Charts: the last {name.replace('m', ' min').replace('1h', '1 hour')}")
            if first:
                b.set_group(first)
            first = first or b
            b.set_active(secs == self.span)
            b.connect("toggled", self._on_range, secs)
            rng.append(b)
        hb.set_title_widget(rng)
        menu = Gio.Menu()
        m1 = Gio.Menu()
        m1.append("Report: the last 24 hours", "app.report")
        m1.append("It felt slow just now (mark it)", "app.mark")
        m2 = Gio.Menu()
        m2.append("Website", "app.site")
        m2.append("About USM", "app.about")
        menu.append_section(None, m1)
        menu.append_section(None, m2)
        hb.pack_end(Gtk.MenuButton(icon_name="open-menu-symbolic", menu_model=menu, tooltip_text="Menu"))
        self.live_dot = box(H, 0, classes=("live-dot",), valign=CENTER)
        self.live = label("LIVE", ("live",), valign=CENTER)
        hb.pack_end(box(H, 7, children=(self.live_dot, self.live), margin_end=6, valign=CENTER))
        return hb

    def _section(self, idx, title, note=""):
        b = box(H, 10, margin_top=8)
        b.append(label(idx, ("section-index", "mono"), valign=CENTER))
        b.append(label(title, ("section-title",), valign=CENTER))
        if note:
            b.append(label(note, ("small", "faint"), valign=CENTER, hexpand=True, ellipsize=Pango.EllipsizeMode.END))
        return b

    @staticmethod
    def _flow(children, max_per_line=2):
        fb = Gtk.FlowBox(selection_mode=Gtk.SelectionMode.NONE, homogeneous=True, min_children_per_line=1,
                         max_children_per_line=max_per_line, column_spacing=18, row_spacing=18)
        fb.add_css_class("plain")
        for ch in children:
            fb.append(ch)
        i = 0
        while fb.get_child_at_index(i):
            fb.get_child_at_index(i).set_focusable(False)
            i += 1
        return fb

    def _content(self):
        page = box(V, 18, margin_top=6, margin_bottom=28, margin_start=28, margin_end=28)
        page.append(self._hero())
        page.append(self._section("01", "Memory tiers", "where pages go when RAM is full, and how fast they come back"))
        page.append(self._tiers())
        page.append(self._section("02", "Over time"))
        page.append(self._charts())
        page.append(self._section("03", "Apps and events"))
        page.append(self._lists())
        foot = label("", ("footer",), xalign=0.5, use_markup=True, margin_top=10)
        foot.set_markup(f"USM {GLib.markup_escape_text(VERSION)} · "
                        '<a href="https://usm.kha333n.com">usm.kha333n.com</a> · '
                        '<a href="https://github.com/kha333n/usm">GitHub</a> · reads only, no root')
        page.append(foot)
        clamp = Adw.Clamp(maximum_size=1320, tightening_threshold=1100)
        clamp.set_child(page)
        sc = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER, vexpand=True)
        sc.set_child(clamp)
        return sc

    def _hero(self):
        hero = box(H, 26, classes=("usm-card", "hero"))
        self.ring = Ring(122, 11)
        hero.append(self.ring)
        mid = box(V, 6, hexpand=True, valign=CENTER)
        self.h_caption = label("RIGHT NOW", ("caption",))
        self.h_dot = label("●", ("lime",), valign=CENTER)
        self.h_title = label("", ("headline",), wrap=True)
        self.h_sub = label("", ("subline",), wrap=True)
        self.h_more = label("", ("small", "muted"), wrap=True)
        mid.append(self.h_caption)
        mid.append(box(H, 10, children=(self.h_dot, self.h_title)))
        mid.append(self.h_sub)
        mid.append(self.h_more)
        hero.append(mid)
        ctl = box(V, 10, valign=CENTER, width_request=330)
        self.c_tier = Control(self, "VRAM swap tier", "tier-on", "tier-off")
        self.c_guard = Control(self, "Memory guard", "guard-on", "guard-off")
        ctl.append(self.c_tier)
        ctl.append(self.c_guard)
        hero.append(ctl)
        return hero

    def _tiers(self):
        card = box(H, 0, classes=("usm-card",))
        self.n_ram = TierNode("ram", "RAM", "blue", "IN USE")
        self.n_zswap = TierNode("zswap", "zswap", "cyan", "PACKED IN RAM")
        self.n_vram = TierNode("vram", "VRAM tier", "lime", "READ LATENCY")
        self.n_disk = TierNode("disk", "Disk swap", "coral", "READ LATENCY")
        self.l_zswap, self.l_vram, self.l_disk = Link("cyan"), Link("lime"), Link("coral")
        sg = Gtk.SizeGroup.new(Gtk.SizeGroupMode.HORIZONTAL)
        for wdg in (self.n_ram, self.l_zswap, self.n_zswap, self.l_vram, self.n_vram, self.l_disk, self.n_disk):
            card.append(wdg)
            if isinstance(wdg, TierNode):
                sg.add_widget(wdg)
        self._sizegroup = sg
        return card

    def _charts(self):
        def lat(dev_attr):
            def get(i, _r):
                dev = getattr(self, dev_attr)
                if not dev:
                    return None
                us, mark = self.s.latency_at(i, dev)
                return us if us is not None and mark != "<" else None
            return get

        def tier(name, direction):
            def get(_i, r):
                return self._tiers_of(r)[name][direction]
            return get
        self.ch_lat = ChartCard("Swap latency", Chart(self, "log", [
            {"name": "VRAM tier", "color": "lime", "get": lat("vdev")},
            {"name": "disk", "color": "coral", "get": lat("ddev")}], fmt_lat, empty="no swap reads in this time"),
            [("vram", "VRAM tier", "lime"), ("disk", "disk", "coral")],
            "average time to read a page back (log scale)")
        series = []
        for d, up in ((1, True), (0, False)):
            for name, title, color in (("zswap", "zswap", "cyan"), ("vram", "VRAM tier", "lime"), ("disk", "disk", "coral")):
                series.append({"name": f"{title} {'out' if up else 'in'}", "color": color, "get": tier(name, d), "up": up})
        self.ch_traffic = ChartCard("Swap traffic", Chart(self, "mirror", series, fmt_rate, min_top=1.0,
                                                          empty="nothing is swapping",
                                                          axis_fmt=lambda v: f"{v:g} MiB/s" if v >= 1 else fmt_rate(v)),
                                    [("zswap", "zswap", "cyan"), ("vram", "VRAM", "lime"), ("disk", "disk", "coral")],
                                    "MiB/s going out (up) and coming back (down), per tier")
        total = D.meminfo()["MemTotal"]
        self.ch_ram = ChartCard("RAM in use", Chart(self, "stack", [
            {"name": "in use", "color": "blue", "get": lambda _i, r: r["used_mb"]}], fmt_size, top=total),
            [("ram", "in use", "blue")], f"of {fmt_size(total)}")
        self.ch_wait = ChartCard("Waiting for memory", Chart(self, "overlay", [
            {"name": "some apps", "color": "amber", "get": lambda _i, r: r["stall"]},
            {"name": "everything", "color": "coral", "get": lambda _i, r: r["stall_all"]}], fmt_pct, min_top=5.0,
            empty="nothing waited for memory", axis_fmt=lambda v: f"{v:g}%"),
            [("some", "some apps", "amber"), ("all", "everything", "coral")], "% of each second (kernel PSI)")
        self.charts = (self.ch_lat, self.ch_traffic, self.ch_ram, self.ch_wait)
        return self._flow(self.charts)

    def _lists(self):
        apps = box(V, 6, classes=("usm-card",))
        apps.append(box(H, 8, children=(label("Apps", ("card-title",), hexpand=True),
                                        label("RAM", ("small", "blue")), label("+ swap", ("small", "lime")))))
        self.app_rows = [AppRow() for _ in range(8)]
        for r in self.app_rows:
            apps.append(r)
        self.apps_empty = label("No desktop apps found", ("empty",), visible=False)
        apps.append(self.apps_empty)
        evs = box(V, 4, classes=("usm-card",))
        evs.append(box(H, 8, children=(label("Events", ("card-title",), hexpand=True),
                                       label("from the logger", ("small", "faint")))))
        self.event_rows = [EventRow() for _ in range(11)]
        for r in self.event_rows:
            evs.append(r)
        self.events_empty = label("No events yet (is the logger on? usm status)", ("empty",), visible=False)
        evs.append(self.events_empty)
        return self._flow((apps, evs))

    # -- data
    def _tiers_of(self, r):
        t = r.get("_tiers")
        if t is None or r.get("_tiers_dev") != self.vdev:
            t = r["_tiers"] = D.Sampler.tiers(r, self.vdev)
            r["_tiers_dev"] = self.vdev
        return t

    def _on_range(self, button, secs):
        if button.get_active():
            self.span = secs
            self.refresh(full=False)

    def _tick(self):
        self.s.step()
        self.n += 1
        self.refresh()
        self._animate_check()
        return GLib.SOURCE_CONTINUE

    # Moving dots on the tier links: 12 frames a second, only while the window is in front (in the background
    # it costs nothing) and pages flow. Software drawing makes every frame count, so no frame-clock animation.
    ANIM_MS = 80

    def _animate_check(self):
        want = (self.is_active() and not os.environ.get("USM_APP_STILL")
                and Gtk.Settings.get_default().get_property("gtk-enable-animations")
                and any(lk.moving for lk in (self.l_zswap, self.l_vram, self.l_disk)))
        if want and not self._anim:
            self._anim = GLib.timeout_add(self.ANIM_MS, self._animate)
        elif not want and self._anim:
            GLib.source_remove(self._anim)
            self._anim = 0

    def _animate(self):
        t = time.monotonic()
        for lk in (self.l_zswap, self.l_vram, self.l_disk):
            if lk.moving:
                lk.phase = t
                lk.queue_draw()
        return GLib.SOURCE_CONTINUE

    def refresh(self, full=True):
        s = self.s
        self.t1 = time.time()
        self.t0 = self.t1 - self.span
        self.view = [(i, r) for i, r in enumerate(s.hist) if r["t"] >= self.t0 - 2]
        if full:
            self._update_state()
        # a long range moves less than a pixel a second: redraw when it shows (or when asked: range, hover)
        step = max(1, int(self.span / 300))
        if not full or self.n % step == 0:
            for c in self.charts:
                c.chart.queue_draw()

    def _recent(self, secs):
        return [r for _i, r in self.view if r["t"] >= self.t1 - secs] or ([self.s.hist[-1]] if self.s.hist else [])

    def _update_state(self):
        s = self.s
        sw = D.swaps()
        self.vdev = next((os.path.basename(n) for n, *_ in sw if n.startswith("/dev/ublkb")), None)
        disks = [(prio, D.device_of(n, k), size, used, n) for n, k, size, used, prio in sw
                 if not n.startswith("/dev/ublkb")]
        self.ddev = max(disks)[1] if disks else None
        ts, mi, gs = D.tier_stats(), D.meminfo(), D.guard_state()
        dk = read_desktop(self.uid) or {}
        li = len(s.hist) - 1
        last = s.hist[li] if li >= 0 else None
        # live dot: blinks with every sample, amber when the samples stop
        stale = not last or time.time() - last["t"] > 4
        for wdg in (self.live, self.live_dot):
            set_kind(wdg, ("stale",), "stale" if stale else None)
        self.live.set_text("PAUSED" if stale else "LIVE")
        # a blink per sample: two small repaints (a CSS transition would repaint at the screen's frame rate)
        if not os.environ.get("USM_APP_STILL") and self.is_active():
            self.live_dot.remove_css_class("dim")
            GLib.timeout_add(350, lambda: self.live_dot.add_css_class("dim") or False)

        total, avail = mi["MemTotal"], mi["MemAvailable"]
        used = total - avail
        frac = used / total if total else 0
        ring_col = ("coral", "amber") if frac > 0.92 else ("amber", "cyan") if frac > 0.8 else ("blue", "cyan")
        self.ring.set(frac, f"{frac * 100:.0f}%", "RAM IN USE", ring_col)
        rec = self._recent(10)
        stall = sum(r["stall"] for r in rec) / len(rec) if rec else 0
        moving = sum(r["swin"] + r["swout"] for r in rec) / len(rec) if rec else 0
        vlat = s.latency_at(li, self.vdev) if self.vdev and li >= 0 else (None, "")
        dlat = s.latency_at(li, self.ddev) if self.ddev and li >= 0 else (None, "")
        if stall >= 20:
            head, col, more = "Memory is very tight", "coral", f"Apps waited for memory {stall:.0f}% of the last 10 s."
        elif stall >= 3:
            head, col, more = "Memory is tight", "amber", f"Apps waited for memory {stall:.0f}% of the last 10 s."
        elif moving >= 2:
            head, col, more = "Swapping smoothly", "cyan", f"{fmt_rate(moving)} moving to and from swap; apps hardly wait."
        elif avail < total * 0.1:
            head, col, more = "RAM is nearly full", "amber", "What doesn't fit goes to swap."
        else:
            head, col, more = "All smooth", "lime", "No app is waiting for memory."
        self.h_title.set_text(head)
        set_kind(self.h_dot, ("lime", "cyan", "amber", "coral"), col)
        swapped = mi.get("SwapTotal", 0) - mi.get("SwapFree", 0)
        sub = f"{fmt_size(used)} of {fmt_size(total)} RAM in use · {fmt_size(swapped)} swapped out"
        if vlat[0] is not None:
            sub += f" · the VRAM tier answers in {fmt_lat(*vlat)}"
        self.h_sub.set_text(sub)
        bits = [more]
        if dk.get("lag_max_ms") is not None:
            bits.append(f"Desktop lag {dk.get('lag_max_ms')} ms.")
        if dk.get("focus_app"):
            bits.append(f"In front: {self._app_label(str(dk['focus_app']).removesuffix('.desktop'))}.")
        self.h_more.set_text("  ".join(bits))

        # the two switches
        state = tier_state(ts, bool(self.vdev))
        gb = int(ts.get("given_back_mb", "0") or 0)
        vram_mb = int(ts.get("vram_mb", "0") or 0)
        in_vram = int(ts.get("in_vram_mb", "0") or 0)
        live_mb = int(ts.get("live_mb", "0") or 0)
        tier_text = {"on": (f"{fmt_size(live_mb)} of data in {fmt_size(in_vram)} of VRAM", "", "on"),
                     "lending": (f"{fmt_size(gb)} of VRAM lent to other programs", "LENDING", "warn"),
                     "given back": ("All VRAM went back to other programs", "GIVEN BACK", "warn"),
                     "spilled": ("Data waits on disk until you switch it on", "SPILLED", "warn"),
                     "off": ("Off: the GPU's memory is all yours", "", "off")}[state]
        if ts.get("refused", "0") not in ("0", ""):
            tier_text = (f"{ts['refused']} writes refused (see usm status)", "CHECK", "bad")
        self.c_tier.show(state != "off", *tier_text)
        mode = gs.get("mode")
        frozen = gs.get("frozen_apps") or []
        if not mode:
            g = ("Off: apps are never paused", "", "off")
        elif frozen:
            g = (f"Paused: {', '.join(frozen)} (switch to it to resume)", f"PAUSED {len(frozen)}", "warn")
        elif mode == "observe":
            g = ("Watching only: log out and in once to finish setup", "WATCHING", "warn")
        elif mode == "protect":
            g = ("Keeps the desktop in RAM only", "", "on")
        elif mode == "waiting":
            g = ("Waiting for a desktop login", "WAITING", "off")
        else:
            g = ("Can pause a busy background app", "", "on")
        self.c_guard.show(bool(mode), *g)

        # memory tiers
        t3 = [self._tiers_of(r) for r in self._recent(3)]

        def avg(name, d):
            return sum(t[name][d] for t in t3) / len(t3) if t3 else 0.0
        self.n_ram.show(f"{frac * 100:.0f}%", [(frac, ring_col[0])],
                        f"{fmt_size(used)} of {fmt_size(total)} · {fmt_size(avail)} free")
        zon = D.read("/sys/module/zswap/parameters/enabled").strip() == "Y"
        self.n_zswap.set_visible(zon)
        self.l_zswap.set_visible(zon)
        if zon:
            pool = int(D.read("/sys/module/zswap/parameters/max_pool_percent") or 20) * total / 100
            zs, zd = mi.get("Zswap", 0), mi.get("Zswapped", 0)
            self.n_zswap.show(f"×{zd / zs:.1f}" if zs else "–", [(zs / pool if pool else 0, "cyan")],
                              f"{fmt_size(zd)} in {fmt_size(zs)} · pool {fmt_size(pool)}",
                              f"out {fmt_rate(avg('zswap', 1))} · in {fmt_rate(avg('zswap', 0))}",
                              caption="PACKED IN RAM")
            self.l_zswap.set_rates(avg("zswap", 1), avg("zswap", 0))
        vpill = {"on": None, "lending": ("LENDING", "warn"), "given back": ("GIVEN BACK", "warn"),
                 "spilled": ("SPILLED", "warn"), "off": ("OFF", "off")}[state]
        ratio = f" · data ×{live_mb / in_vram:.1f}" if in_vram >= 64 and live_mb > in_vram else ""
        self.n_vram.show(fmt_lat(*vlat) if state != "off" else "off",
                         [(in_vram / vram_mb if vram_mb else 0, "lime")],
                         f"{fmt_size(in_vram)} of {fmt_size(vram_mb)} VRAM{ratio}" if state != "off" else "not running",
                         f"out {fmt_rate(avg('vram', 1))} · in {fmt_rate(avg('vram', 0))}" if state != "off" else "",
                         pill=vpill[0] if vpill else None, pill_kind=vpill[1] if vpill else "on", dim=state == "off")
        self.l_vram.set_rates(avg("vram", 1), avg("vram", 0))
        dsize = sum(d[2] for d in disks)
        dused = sum(d[3] for d in disks)
        self.n_disk.show(fmt_lat(*dlat) if disks else "none", [(dused / dsize if dsize else 0, "coral")],
                         f"{fmt_size(dused)} of {fmt_size(dsize)} · {self.ddev}" if disks else "no disk swap",
                         f"out {fmt_rate(avg('disk', 1))} · in {fmt_rate(avg('disk', 0))}" if disks else "",
                         dim=not disks)
        self.l_disk.set_rates(avg("disk", 1), avg("disk", 0))

        # chart legends
        self.ch_lat.set_value("vram", fmt_lat(*vlat))
        self.ch_lat.set_value("disk", fmt_lat(*dlat))
        self.ch_ram.set_value("ram", fmt_size(used))
        self.ch_wait.set_value("some", fmt_pct(last["stall"] if last else None))
        self.ch_wait.set_value("all", fmt_pct(last["stall_all"] if last else None))
        if self.n % 3 == 0:
            self._update_apps()
        if self.n % 5 == 0:
            self._update_events()

    # -- apps and events
    def _app_lookup(self):
        if self._apps_cache is None:
            m = {}
            for info in Gio.AppInfo.get_all():
                keys = [(info.get_id() or "").removesuffix(".desktop")]
                exe = info.get_executable() or ""
                keys.append(os.path.basename(exe))
                wm = getattr(info, "get_startup_wm_class", None)
                if wm and wm():
                    keys.append(wm())
                for k in keys:
                    if k:
                        m.setdefault(k.lower(), info)
            self._apps_cache = m
        return self._apps_cache

    def _app_info(self, name):
        m = self._app_lookup()
        for k in (name, name.split(".")[1] if name.startswith("snap.") and name.count(".") >= 1 else "",
                  name.rsplit(".", 1)[-1]):
            if k and k.lower() in m:
                return m[k.lower()]
        return None

    def _app_label(self, name):
        info = self._app_info(name)
        if not info:
            return name
        lb = info.get_display_name()
        self._by_label[lb] = info
        return lb

    def _update_apps(self):
        base = f"/sys/fs/cgroup/user.slice/user-{self.uid}.slice/user@{self.uid}.service/app.slice"
        rows = []
        try:
            scopes = os.listdir(base)
        except OSError:
            scopes = []
        for c in scopes:
            if not (c.endswith(".scope") or c.endswith(".service")):
                continue
            p = f"{base}/{c}"
            try:
                cur = int(D.read(f"{p}/memory.current", "0") or 0) >> 20
                sw = int(D.read(f"{p}/memory.swap.current", "0") or 0) >> 20
            except ValueError:
                continue
            if cur + sw < 16:
                continue
            rows.append((cur + sw, cur, sw, D.read(f"{p}/cgroup.freeze").strip() == "1", app_name(c)))
        merged = {}
        for tot, cur, sw, fr, name in rows:                # one row per app (Chrome has many scopes, two ids)
            name = self._app_label(name)
            m = merged.setdefault(name, [0, 0, 0, False])
            m[0] += tot
            m[1] += cur
            m[2] += sw
            m[3] = m[3] or fr
        top = sorted(merged.items(), key=lambda x: -x[1][0])[:len(self.app_rows)]
        peak = top[0][1][0] if top else 1
        for row, item in zip(self.app_rows, top + [None] * len(self.app_rows)):
            row.set_visible(item is not None)
            if not item:
                continue
            name, (tot, cur, sw, fr) = item
            info = self._by_label.get(name)
            row.name.set_text(name)
            gicon = info.get_icon() if info else None
            if gicon:
                row.icon.set_from_gicon(gicon)
            else:
                row.icon.set_from_icon_name("application-x-executable-symbolic")
            row.bar.set([(cur / peak, "blue"), (sw / peak, "lime")])
            row.value.set_text(f"{fmt_size(cur)}" + (f" + {fmt_size(sw)}" if sw >= 1 else ""))
            row.pill.set_visible(fr)
        self.apps_empty.set_visible(not top)

    def _update_events(self):
        ev = [e for e in D.tail_events(40) if "logger st" not in e][-len(self.event_rows):]
        ev.reverse()
        for row, line in zip(self.event_rows, ev + [None] * len(self.event_rows)):
            row.set_visible(line is not None)
            if line:
                row.show(line)
        self.events_empty.set_visible(not ev)

    # -- actions
    def toast(self, msg):
        self.toasts.add_toast(Adw.Toast.new(msg))

    def run_helper(self, verb, done):
        self.run_cmd(["pkexec", HELPER, verb], done)

    @staticmethod
    def run_cmd(argv, done):
        try:
            p = Gio.Subprocess.new(argv, Gio.SubprocessFlags.STDOUT_PIPE | Gio.SubprocessFlags.STDERR_MERGE)
        except GLib.Error as e:
            done(False, e.message)
            return

        def fin(proc, res):
            try:
                _ok, out, _err = proc.communicate_utf8_finish(res)
                done(proc.get_successful(), (out or "").strip())
            except GLib.Error as e:
                done(False, e.message)
        p.communicate_utf8_async(None, None, fin)


class App(Adw.Application):
    def __init__(self, opts):
        flags = getattr(Gio.ApplicationFlags, "DEFAULT_FLAGS", Gio.ApplicationFlags.FLAGS_NONE)
        if opts["screenshot"]:
            flags |= Gio.ApplicationFlags.NON_UNIQUE
        super().__init__(application_id=APP_ID, flags=flags)
        self.opts, self.win = opts, None

    def do_startup(self):
        Adw.Application.do_startup(self)
        Adw.StyleManager.get_default().set_color_scheme(Adw.ColorScheme.FORCE_DARK)
        css = Gtk.CssProvider()
        css.load_from_path(os.path.join(HERE, "usm-app.css"))
        # above the user's own gtk.css too (themes put one there): the app has its own look
        Gtk.StyleContext.add_provider_for_display(Gdk.Display.get_default(), css,
                                                  Gtk.STYLE_PROVIDER_PRIORITY_USER + 100)
        for name, fn in (("about", self._about), ("report", self._report), ("mark", self._mark),
                         ("site", lambda *_: Gtk.UriLauncher.new("https://usm.kha333n.com").launch(self.win, None, None)),
                         ("quit", lambda *_: self.quit())):
            a = Gio.SimpleAction.new(name, None)
            a.connect("activate", fn)
            self.add_action(a)
        self.set_accels_for_action("app.quit", ["<Control>q", "<Control>w"])

    def do_activate(self):
        if not self.win:
            self.win = Window(self, self.opts["range"], self.opts["size"])
        self.win.present()
        if self.opts["screenshot"]:
            GLib.timeout_add(int(self.opts["wait"] * 1000), self._screenshot)

    def _screenshot(self):
        win, scale = self.win, self.opts["scale"]
        w, h = win.get_width(), win.get_height()
        snap = Gtk.Snapshot.new()
        snap.scale(scale, scale)
        Gtk.WidgetPaintable.new(win).snapshot(snap, w, h)
        node = snap.to_node()
        tex = win.get_renderer().render_texture(node, rect(0, 0, w * scale, h * scale))
        tex.save_to_png(self.opts["screenshot"])
        print(f"saved {self.opts['screenshot']} ({int(w * scale)}x{int(h * scale)})")
        self.quit()
        return False

    def _about(self, *_):
        kw = dict(application_name="USM", application_icon=APP_ID, version=VERSION, developer_name="Usman Khan",
                  website="https://usm.kha333n.com", issue_url="https://github.com/kha333n/usm/issues",
                  license_type=Gtk.License.MIT_X11,
                  comments="Your GPU's spare memory as fast swap, and a guard that keeps the desktop responsive.\n\n"
                           "The code was written by Claude (Anthropic's AI coding agent) under the author's direction.")
        if hasattr(Adw, "AboutDialog"):
            Adw.AboutDialog(**kw).present(self.win)
        else:
            Adw.AboutWindow(transient_for=self.win, **kw).present()

    def _report(self, *_):
        win = Adw.Window(transient_for=self.win, modal=True, title="Report: the last 24 hours",
                         default_width=900, default_height=680)
        win.add_css_class("usm")
        text_ = label("Reading the logs…", ("report",), selectable=True, valign=Gtk.Align.START,
                      margin_top=14, margin_bottom=18, margin_start=20, margin_end=20)
        sc = Gtk.ScrolledWindow(vexpand=True)
        sc.set_child(text_)
        hb = Adw.HeaderBar()
        if hasattr(Adw, "ToolbarView"):
            tv = Adw.ToolbarView()
            tv.add_top_bar(hb)
            tv.set_content(sc)
            win.set_content(tv)
        else:
            win.set_content(box(V, 0, children=(hb, sc)))
        win.present()
        Window.run_cmd([sys.executable, TOOL, "report", "24"], lambda ok, out: text_.set_text(out or "no report"))

    def _mark(self, *_):
        Window.run_cmd([sys.executable, TOOL, "mark", "marked from the USM app"],
                       lambda ok, out: self.win.toast("Marked: it felt slow just now. Shows in the report."
                                                      if ok else out[-160:]))


def parse_args(argv):
    o = {"range": 300, "screenshot": None, "size": (1300, 940), "scale": 1.0, "wait": 3.0}
    it = iter(argv)
    try:
        for a in it:
            if a == "--range":
                o["range"] = int(next(it))
            elif a == "--screenshot":
                o["screenshot"] = os.path.abspath(next(it))
            elif a == "--size":
                w, h = next(it).lower().split("x")
                o["size"] = (int(w), int(h))
            elif a == "--scale":
                o["scale"] = float(next(it))
            elif a == "--wait":
                o["wait"] = float(next(it))
            elif a in ("-h", "--help"):
                print(__doc__)
                sys.exit(0)
            elif a == "--version":
                print(f"USM app {VERSION}")
                sys.exit(0)
            else:
                raise ValueError(a)
    except (StopIteration, ValueError) as e:
        sys.exit(f"usm-app: bad option {e} (see --help)")
    if o["range"] not in [r for r, _ in RANGES]:
        sys.exit("usm-app: --range 60, 300, 900 or 3600")
    return o


def main():
    opts = parse_args(sys.argv[1:])
    if os.geteuid() == 0 and not opts["screenshot"]:
        sys.exit("usm-app: run it as your desktop user, not as root (it needs no root)")
    sys.exit(App(opts).run([sys.argv[0]]))


if __name__ == "__main__":
    main()
