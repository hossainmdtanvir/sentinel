"""Custom-drawn HUD widgets: an arc gauge and a segmented bar.

They draw with GTK's own GPU renderer (GSK paths), so no extra Python packages are needed, and they ease
smoothly between values on the frame clock.
"""

import math

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
gi.require_version("Gsk", "4.0")
gi.require_version("Graphene", "1.0")
from gi.repository import Gdk, GLib, Graphene, Gsk, Gtk  # noqa: E402

ANIM_SECONDS = 0.35
ANIM_MIN_STEP = 0.04    # smaller changes jump straight to the new value: one redraw instead of a whole animation


def rgba(rgb, alpha=1.0):
    c = Gdk.RGBA()
    c.red, c.green, c.blue, c.alpha = rgb[0], rgb[1], rgb[2], alpha
    return c


def stroke(width, round_cap=False):
    s = Gsk.Stroke.new(width)
    if round_cap:
        s.set_line_cap(Gsk.LineCap.ROUND)
    return s


def arc(cx, cy, r, a0, a1):
    """Clockwise arc path from angle a0 to a1 (radians, screen coordinates)."""
    b = Gsk.PathBuilder.new()
    b.move_to(cx + r * math.cos(a0), cy + r * math.sin(a0))
    steps = max(1, math.ceil((a1 - a0) / math.pi))     # pieces of at most 180° keep the arc flags unambiguous
    for k in range(1, steps + 1):
        a = a0 + (a1 - a0) * k / steps
        b.svg_arc_to(r, r, 0, False, True, cx + r * math.cos(a), cy + r * math.sin(a))
    return b.to_path()


def circle(cx, cy, r):
    b = Gsk.PathBuilder.new()
    b.add_circle(Graphene.Point().init(cx, cy), r)
    return b.to_path()


class _Animated(Gtk.Widget):
    """Eases a displayed fraction towards its target on the frame clock, then stops ticking."""

    def __init__(self, width, height):
        super().__init__()
        self._size = (width, height)
        self.shown = 0.0
        self._from = self._target = 0.0
        self._start = None
        self._tick_id = None
        self.rgb = (0.133, 0.827, 0.933)

    def do_measure(self, orientation, for_size):
        n = self._size[0] if orientation == Gtk.Orientation.HORIZONTAL else self._size[1]
        return n, n, -1, -1

    def set_value(self, fraction, rgb):
        fraction = max(0.0, min(1.0, fraction or 0.0))
        if abs(fraction - self._target) < ANIM_MIN_STEP and self._tick_id is None:
            if fraction != self.shown or rgb != self.rgb:
                self.shown = self._target = fraction
                self.queue_draw()
            self.rgb = rgb
            return
        self._from, self._target, self._start = self.shown, fraction, None
        self.rgb = rgb
        if self._tick_id is None:
            self._tick_id = self.add_tick_callback(self._tick)

    def _tick(self, _widget, clock):
        now = clock.get_frame_time() / 1e6
        if self._start is None:
            self._start = now
        t = min(1.0, (now - self._start) / ANIM_SECONDS)
        self.shown = self._from + (self._target - self._from) * (1 - (1 - t) ** 3)
        self.queue_draw()
        if t >= 1.0:
            self._tick_id = None
            return GLib.SOURCE_REMOVE
        return GLib.SOURCE_CONTINUE


class Ring(_Animated):
    """A 270° arc gauge with a tick scale, a soft glow and a bright leading dot."""

    START = math.radians(135)
    SWEEP = math.radians(270)
    TICKS = 40

    def __init__(self, size=118, thickness=7):
        super().__init__(size, size)
        self.thickness = thickness

    def do_snapshot(self, snapshot):
        w, h = self.get_width(), self.get_height()
        cx, cy = w / 2, h / 2
        r = min(w, h) / 2 - 13
        if r <= self.thickness:
            return

        groups = {}     # (lit, major) -> tick segments, so each colour is one stroke
        for i in range(self.TICKS + 1):
            a = self.START + self.SWEEP * i / self.TICKS
            major = i % 5 == 0
            lit = self.shown > 0 and i / self.TICKS <= self.shown
            inner, outer = r + 7, r + (12 if major else 10)
            groups.setdefault((lit, major), []).append(
                (cx + inner * math.cos(a), cy + inner * math.sin(a), cx + outer * math.cos(a), cy + outer * math.sin(a)))
        for (lit, major), segments in groups.items():
            b = Gsk.PathBuilder.new()
            for x0, y0, x1, y1 in segments:
                b.move_to(x0, y0)
                b.line_to(x1, y1)
            color = rgba(self.rgb, 0.75 if major else 0.4) if lit else rgba((0.55, 0.65, 0.78), 0.3 if major else 0.12)
            snapshot.append_stroke(b.to_path(), stroke(1), color)

        snapshot.append_stroke(arc(cx, cy, r, self.START, self.START + self.SWEEP),
                               stroke(self.thickness, True), rgba((1, 1, 1), 0.06))
        if self.shown > 0.003:
            end = self.START + self.SWEEP * self.shown
            value_arc = arc(cx, cy, r, self.START, end)
            snapshot.append_stroke(value_arc, stroke(self.thickness + 9, True), rgba(self.rgb, 0.1))
            snapshot.append_stroke(value_arc, stroke(self.thickness, True), rgba(self.rgb))
            ex, ey = cx + r * math.cos(end), cy + r * math.sin(end)
            snapshot.append_fill(circle(ex, ey, self.thickness), Gsk.FillRule.WINDING, rgba(self.rgb, 0.35))
            snapshot.append_fill(circle(ex, ey, self.thickness / 2 - 0.5), Gsk.FillRule.WINDING,
                                 rgba((1, 1, 1), 0.95))
        snapshot.append_stroke(circle(cx, cy, r - self.thickness - 5), stroke(1), rgba(self.rgb, 0.16))


class Segments(_Animated):
    """An LED-style segmented bar that fills the available width."""

    def __init__(self, count=24, height=5, gap=2):
        super().__init__(count * 3, height)
        self.count, self.gap = count, gap
        self.set_hexpand(True)

    def do_snapshot(self, snapshot):
        w, h = self.get_width(), self.get_height()
        seg = (w - self.gap * (self.count - 1)) / self.count
        if seg <= 0:
            return
        lit = self.shown * self.count
        off = rgba((1, 1, 1), 0.07)
        for i in range(self.count):
            fill = max(0.0, min(1.0, lit - i))
            color = rgba(self.rgb, 0.3 + 0.7 * fill) if fill > 0 else off
            snapshot.append_color(color, Graphene.Rect().init(i * (seg + self.gap), 0, seg, h))
