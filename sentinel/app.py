"""Sentinel's window: a HUD-style desktop widget built with GTK 4 and libadwaita.

Live readings refresh every two seconds on a worker thread. The full security and SMART scan runs at start-up,
every ten minutes, and on demand (Ctrl+R), filling in the matrix one check at a time.
"""

import collections
import datetime
import os
import re
import sys
import threading
import time
import traceback

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("Pango", "1.0")
from gi.repository import Adw, Gdk, Gio, GLib, Gtk, Pango  # noqa: E402

from . import APP_ID, APP_NAME, checks, hardware, health, storage  # noqa: E402
from .widgets import Ring, Segments  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
ICON = os.path.join(os.path.dirname(HERE), "assets", "sentinel.svg")

FAST_SECONDS = 2
RESCAN_SECONDS = 600
SCAN_STAGGER = 0.07    # brief pause between checks so the matrix visibly fills in, row by row

RGB = {
    "accent": (0.133, 0.827, 0.933),
    "ok": (0.204, 0.827, 0.600),
    "watch": (0.220, 0.741, 0.973),
    "warn": (0.984, 0.749, 0.141),
    "fail": (0.973, 0.443, 0.443),
}
STATUSES = ("ok", "watch", "warn", "fail", "pending")
GLYPH = {"ok": "✓", "watch": "i", "warn": "!", "fail": "✕", "pending": "·"}

# Temperature sensors: (comfortable below, warm below, scale maximum) in °C.
TEMP_LIMITS = {"cpu": (80, 95, 100), "gpu": (85, 93, 100), "ssd": (60, 70, 85), "wifi": (70, 85, 100)}


# ------------------------------------------------------------------ helpers

def label(text="", *classes, xalign=0.0, **props):
    return Gtk.Label(label=text, xalign=xalign, css_classes=list(classes), **props)


def set_status(widget, status):
    for s in STATUSES:
        widget.remove_css_class(s)
    widget.add_css_class(status)


def usage_rgb(fraction):
    return RGB["accent"] if fraction < 0.75 else RGB["warn"] if fraction < 0.9 else RGB["fail"]


def temp_status(kind, t):
    ok_below, warm_below, _ = TEMP_LIMITS[kind]
    return "ok" if t < ok_below else "watch" if t < warm_below else "warn"


def gib(n):
    return n / 2 ** 30


def uptime_text(seconds):
    days, rest = divmod(int(seconds), 86400)
    hours, minutes = rest // 3600, rest % 3600 // 60
    return f"{days}d {hours}h {minutes:02d}m" if days else f"{hours}h {minutes:02d}m"


def fix_command(detail):
    m = re.search(r"Fix: ((?:sudo|gsettings) [^\n]+)", detail)
    return m.group(1) if m else None


def panel(title, meta=None):
    box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10, css_classes=["panel"])
    head = Gtk.Box(spacing=8)
    head.append(label(f"▸ {title}", "panel-title", hexpand=True))
    if meta is not None:
        head.append(meta)
    box.append(head)
    return box


# ------------------------------------------------------------------ building blocks

class Tile(Gtk.Box):
    """A compact readout: caption, big value with unit, and a thin bar."""

    def __init__(self, caption, unit):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=3, css_classes=["tile", "ok"], hexpand=True)
        self.append(label(caption, "tile-caption", ellipsize=Pango.EllipsizeMode.END))
        row = Gtk.Box(spacing=4)
        self.value = label("—", "tile-value", valign=Gtk.Align.BASELINE_FILL)
        self.unit = label(unit, "tile-unit", valign=Gtk.Align.BASELINE_FILL)
        row.append(self.value)
        row.append(self.unit)
        self.append(row)
        self.bar = Segments(count=12, height=3)
        self.append(self.bar)

    def set(self, value, fraction, status="ok", unit=None, rgb=None):
        self.value.set_label(value)
        if unit is not None:
            self.unit.set_label(unit)
        set_status(self, status)
        self.bar.set_value(fraction, rgb or RGB.get(status, RGB["accent"]))


class GaugeCard(Gtk.Box):
    """Ring gauge on the left; title, status chip, component name and two spec lines on the right.

    With `details`, hovering shows that widget as the tooltip instead of plain text. It is the same widget every
    time, so whatever updates it keeps the open tooltip live.
    """

    def __init__(self, title, details=None):
        super().__init__(spacing=10, css_classes=["gauge-card"], hexpand=True)
        self.details = details
        if details is not None:
            self.set_has_tooltip(True)
            self.connect("query-tooltip", self._show_details)
        self.ring = Ring(size=96, thickness=6)
        overlay = Gtk.Overlay(valign=Gtk.Align.CENTER, child=self.ring)
        center = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, halign=Gtk.Align.CENTER, valign=Gtk.Align.CENTER)
        self.value = label("—", "gauge-value", xalign=0.5)
        self.unit = label("", "gauge-unit", xalign=0.5)
        center.append(self.value)
        center.append(self.unit)
        overlay.add_overlay(center)
        self.append(overlay)

        text = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=3, valign=Gtk.Align.CENTER, hexpand=True)
        head = Gtk.Box(spacing=6)
        head.append(label(title, "card-title", hexpand=True))
        self.chip = label("", "chip", "ok")
        head.append(self.chip)
        text.append(head)
        self.name = label("", "hw-name", ellipsize=Pango.EllipsizeMode.END, margin_top=4)
        text.append(self.name)
        self.lines = [label("", "hw-line", ellipsize=Pango.EllipsizeMode.END) for _ in range(2)]
        for line in self.lines:
            text.append(line)
        self.append(text)

    def set(self, fraction, rgb, value, unit, chip, status, name, lines, tooltip=None):
        self.ring.set_value(fraction, rgb)
        self.value.set_label(value)
        self.unit.set_label(unit)
        self.chip.set_label(chip)
        set_status(self.chip, status)
        self.name.set_label(name)
        for widget, text in zip(self.lines, lines):
            widget.set_label(text)
        if self.details is None:
            self.set_tooltip_text(tooltip)

    def _show_details(self, _widget, _x, _y, _keyboard, tooltip):
        tooltip.set_custom(self.details)
        return True


class CoreDetails(Gtk.Box):
    """Per-core breakdown for the CPU card's tooltip: load, thread loads, clock and temperature of each core."""

    def __init__(self, topology):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=8, css_classes=["core-details"])
        threads = sum(len(cpus) for _, cpus in topology)
        self.append(label(f"PER-CORE  //  {len(topology)} CORES · {threads} THREADS", "core-title"))

        grid = Gtk.Grid(column_spacing=12, row_spacing=5)
        for col, width, caption in ((0, 1, "CORE"), (1, 2, "LOAD"), (3, 1, "THREADS"), (4, 1, "CLOCK"), (5, 1, "TEMP")):
            grid.attach(label(caption, "core-caption", xalign=0.0 if col < 3 else 1.0), col, 0, width, 1)
        self.rows = []
        for i, (core_id, cpus) in enumerate(topology):
            bar = Segments(count=16, height=4)
            bar.set_size_request(104, -1)
            bar.set_valign(Gtk.Align.CENTER)
            cells = {
                "load": label("—", "core-value", xalign=1.0, width_chars=4),
                "threads": label("—", "core-dim", xalign=1.0),
                "clock": label("—", "core-value", xalign=1.0, width_chars=8),
                "temp": label("—", "core-temp", "ok", xalign=1.0, width_chars=6),
            }
            grid.attach(label(f"CORE {i}", "core-name"), 0, i + 1, 1, 1)
            grid.attach(bar, 1, i + 1, 1, 1)
            for col, key in enumerate(("load", "threads", "clock", "temp"), start=2):
                grid.attach(cells[key], col, i + 1, 1, 1)
            self.rows.append((core_id, cpus, bar, cells))
        self.append(grid)

        self.append(Gtk.Separator(css_classes=["core-sep"]))
        self.summary = label("", "core-dim")
        self.append(self.summary)
        self.verdict = label("", "core-verdict", wrap=True, max_width_chars=64)
        self.append(self.verdict)

    def update(self, cpu, verdict):
        per_cpu, clocks, temps = cpu["per_cpu"], cpu["ghz_by_cpu"], cpu["core_temps"]
        for core_id, cpus, bar, cells in self.rows:
            loads = [per_cpu[n] for n in cpus if n in per_cpu]
            load = sum(loads) / len(loads) if loads else 0.0
            bar.set_value(load / 100, usage_rgb(load / 100))
            cells["load"].set_label(f"{load:.0f}%")
            cells["threads"].set_label("  ".join(f"{v:>3.0f}%" for v in loads) if len(loads) > 1 else "")
            ghz = max((clocks[n] for n in cpus if n in clocks), default=None)     # threads share their core's clock
            cells["clock"].set_label(f"{ghz:.2f} GHz" if ghz else "—")
            temp = temps.get(core_id)
            cells["temp"].set_label(f"{temp:.0f} °C" if temp is not None else "—")
            set_status(cells["temp"], temp_status("cpu", temp) if temp is not None else "ok")

        lines = []
        if cpu["pkg"] is not None:
            hottest = f" · hottest core {cpu['hottest_core']:.0f} °C" if cpu["hottest_core"] is not None else ""
            lines.append(f"Package {cpu['pkg']:.0f} °C{hottest} · limit {cpu['crit']:.0f} °C")
        if cpu["governor"]:
            lines.append(f"Governor {cpu['governor']}" + (f" · EPP {cpu['epp']}" if cpu["epp"] else ""))
        one, five, fifteen = cpu["loadavg"]
        lines.append(f"Load average {one:.2f} · {five:.2f} · {fifteen:.2f}  (1 · 5 · 15 min)")
        self.summary.set_label("\n".join(lines))
        self.verdict.set_label(verdict or "")
        self.verdict.set_visible(bool(verdict))


class CheckRow(Gtk.Box):
    def __init__(self, title, on_copy):
        super().__init__(spacing=10, css_classes=["check-row", "pending"])
        self.badge = label(GLYPH["pending"], "badge", xalign=0.5, valign=Gtk.Align.CENTER)
        self.append(self.badge)
        self.append(label(title, "check-title", hexpand=True))
        self.value = label("queued", "check-value", xalign=1.0)
        self.append(self.value)
        self.fix = None
        self.on_copy = on_copy
        click = Gtk.GestureClick()
        click.connect("released", lambda *_: self.fix and self.on_copy(self.fix))
        self.add_controller(click)

    def set_pending(self):
        set_status(self, "pending")
        self.badge.set_label(GLYPH["pending"])
        self.value.set_label("scanning…")

    def set(self, check):
        set_status(self, check.status)
        self.badge.set_label(GLYPH[check.status])
        self.value.set_label(check.value)
        self.fix = fix_command(check.detail)
        self.set_tooltip_text(check.detail + ("\n\nClick to copy the fix command." if self.fix else ""))
        self.set_cursor_from_name("pointer" if self.fix else None)


class Stat(Gtk.Box):
    """A tiny caption-over-value pair used in drive and battery rows."""

    def __init__(self, caption):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=1, hexpand=True)
        self.append(label(caption, "stat-caption"))
        self.value = label("—", "stat-value")
        self.append(self.value)

    def set(self, text, status=None):
        self.value.set_label(text)
        set_status(self.value, status or "pending")


class DriveRow(Gtk.Box):
    def __init__(self, d):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=6, css_classes=["drive"])
        s = d["smart"]
        head = Gtk.Box(spacing=8)
        head.append(label(f"{d['brand']}  {d['model'].split(maxsplit=1)[-1]}", "hw-name",
                          ellipsize=Pango.EllipsizeMode.END))
        head.append(label(f"{d['size'] / 1e9:.0f} GB", "hw-line"))
        head.append(label(d["verdict"], "drive-verdict", d["status"], xalign=1.0, hexpand=True,
                          ellipsize=Pango.EllipsizeMode.END, margin_start=12))
        enc = label("ENCRYPTED" if d["encrypted"] else "PLAIN", "chip", "ok" if d["encrypted"] else "watch")
        enc.set_tooltip_text("LUKS full-disk encryption" if d["encrypted"] else
                             "Not encrypted: keep only non-sensitive media on this drive (handbook Vol. 2, K1).")
        head.append(enc)
        self.chip = label("SMART " + ("PASS" if d["status"] in ("ok", "watch") else "CHECK"), "chip", d["status"])
        head.append(self.chip)
        self.append(head)

        use = Gtk.Box(spacing=10)
        role = "SYSTEM  /" if d["role"] == "SYSTEM" else f"DATA  {d['role']}"
        use.append(label(role, "drive-role"))
        bar = Segments(count=30, height=4)
        use.append(bar)
        if d["total"]:
            frac = d["used"] / d["total"]
            bar.set_value(frac, usage_rgb(frac))
            use.append(label(f"{d['used'] / 1e9:.0f} / {d['total'] / 1e9:.0f} GB", "hw-line"))
        self.append(use)

        stats = Gtk.Box(spacing=6, homogeneous=True)
        wear, spare = s.get("percent_used"), s.get("avail_spare")
        errors = s.get("media_errors")
        for caption, text, status in (
            ("WEAR", f"{wear} %" if wear is not None else "—", "ok" if (wear or 0) < 80 else "warn"),
            ("SPARE", f"{spare} %" if spare is not None else "—", "ok"),
            ("POWER-ON", f"{s['hours']:,} h" if s.get("hours") is not None else "—", "ok"),
            ("WRITTEN", f"{s['total_data_written'] / 1e12:.1f} TB" if s.get("total_data_written") else "—", "ok"),
            ("ERRORS", f"{errors:,}" if errors is not None else "—", "ok" if not errors else d["status"]),
        ):
            stat = Stat(caption)
            stat.set(text, status)
            stats.append(stat)
        self.temp = Stat("TEMP")
        stats.append(self.temp)
        self.append(stats)

    def set_temp(self, t):
        if t is not None:
            self.temp.set(f"{t:.0f} °C", temp_status("ssd", t))


class Banner(Gtk.Box):
    def __init__(self):
        super().__init__(spacing=18, css_classes=["banner", "pending"])
        self.emblem_ring = Ring(size=62, thickness=4)
        emblem = Gtk.Overlay(child=self.emblem_ring, valign=Gtk.Align.CENTER)
        self.glyph = label("·", "emblem-glyph", xalign=0.5, halign=Gtk.Align.CENTER, valign=Gtk.Align.CENTER)
        emblem.add_overlay(self.glyph)
        self.append(emblem)

        text = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=3, valign=Gtk.Align.CENTER, hexpand=True)
        self.title = label("INITIALISING", "verdict")
        self.sub = label("", "verdict-sub", wrap=True)
        self.meta = label("", "verdict-meta")
        for w in (self.title, self.sub, self.meta):
            text.append(w)
        self.append(text)

        self.scores = {}
        for key, caption in (("security", "SECURITY"), ("hardware", "HARDWARE")):
            box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4, valign=Gtk.Align.CENTER,
                          css_classes=["score-box"])
            box.append(label(caption, "score-caption"))
            value = label("—", "score")
            box.append(value)
            bar = Segments(count=14, height=4)
            bar.set_size_request(120, -1)
            box.append(bar)
            self.scores[key] = (value, bar)
            self.append(box)

    def set(self, status, title, sub, meta, scores):
        set_status(self, status)
        self.emblem_ring.set_value(1.0 if status != "pending" else 0.25, RGB.get(status, RGB["accent"]))
        self.glyph.set_label(GLYPH[status])
        self.title.set_label(title)
        self.sub.set_label(sub)
        self.meta.set_label(meta)
        for key, (passed, total) in scores.items():
            value, bar = self.scores[key]
            value.set_label(f"{passed}/{total}" if total else "—")
            frac = passed / total if total else 0
            bar.set_value(frac, RGB["ok"] if frac == 1 else RGB["warn"])


# ------------------------------------------------------------------ window

class SentinelWindow(Adw.ApplicationWindow):
    def __init__(self, app):
        super().__init__(application=app, title=APP_NAME, default_width=1280, default_height=740)
        self.add_css_class("sentinel")
        self.static = hardware.static_info()
        self.sampler = hardware.Sampler()
        self.live = None
        self.drives = []
        self.drive_rows = {}
        self.io_check = None
        self.security = {}
        self.hardware = {}
        self.cpu_temps = collections.deque(maxlen=5)    # ~10 s of readings, so brief spikes don't flip the verdict
        self.last_scan = None
        self._sampling = self._scanning = False

        self._build()
        action = Gio.SimpleAction.new("rescan", None)
        action.connect("activate", lambda *_: self.rescan())
        self.add_action(action)

        self._tick()
        GLib.timeout_add_seconds(FAST_SECONDS, self._tick)
        GLib.timeout_add_seconds(RESCAN_SECONDS, lambda: self.rescan() or GLib.SOURCE_CONTINUE)
        self.rescan()

    # ---------------------------------------------------------- layout

    def _build(self):
        self.toasts = Adw.ToastOverlay()
        root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.toasts.set_child(root)
        self.set_content(self.toasts)

        root.append(self._build_header())
        self.scanbar = Gtk.ProgressBar(css_classes=["scanbar"])
        root.append(self.scanbar)

        body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12, margin_top=12, margin_bottom=12,
                       margin_start=16, margin_end=16)
        root.append(Gtk.ScrolledWindow(child=body, vexpand=True, hscrollbar_policy=Gtk.PolicyType.NEVER))

        self.banner = Banner()
        body.append(self.banner)
        cols = Gtk.Box(spacing=12)
        left = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12, hexpand=True)
        right = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12, hexpand=False)
        right.set_size_request(400, -1)
        cols.append(left)
        cols.append(right)
        body.append(cols)
        left.append(self._build_telemetry())
        left.append(self._build_storage())
        right.append(self._build_security())
        right.append(self._build_power())
        root.append(self._build_footer())

    def _build_header(self):
        bar = Gtk.Box(spacing=12, css_classes=["topbar"])
        logo = Gtk.Image.new_from_file(ICON)
        logo.set_pixel_size(36)
        bar.append(logo)
        titles = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, valign=Gtk.Align.CENTER, hexpand=True)
        titles.append(label("SENTINEL", "brand"))
        titles.append(label(f"SECURITY & HEALTH AGENT  //  {self.static['product'].upper()}", "tagline"))
        bar.append(titles)

        live = Gtk.Box(spacing=7, valign=Gtk.Align.CENTER, css_classes=["live"])
        self.live_dot = Gtk.Box(css_classes=["live-dot", "dim"], valign=Gtk.Align.CENTER)
        live.append(self.live_dot)
        live.append(label("LIVE", "live-label"))
        self.clock = label("", "clock")
        live.append(self.clock)
        bar.append(live)

        for icon, tip, action, extra in (
            ("view-refresh-symbolic", "Rescan everything (Ctrl+R)", "win.rescan", []),
            ("window-minimize-symbolic", "Minimize", "window.minimize", []),
            ("window-close-symbolic", "Close (Esc)", "window.close", ["close"]),
        ):
            bar.append(Gtk.Button(icon_name=icon, tooltip_text=tip, action_name=action, valign=Gtk.Align.CENTER,
                                  css_classes=["hud-button", *extra]))
        return Gtk.WindowHandle(child=bar)

    def _build_telemetry(self):
        self.uptime = label("", "panel-meta")
        box = panel("CORE TELEMETRY", self.uptime)
        cards = Gtk.Box(spacing=10, homogeneous=True)
        self.core_details = CoreDetails(self.static["topology"])
        self.cpu_card = GaugeCard("CPU", details=self.core_details)
        self.ram_card, self.gpu_card = GaugeCard("MEMORY"), GaugeCard("GRAPHICS")
        for card in (self.cpu_card, self.ram_card, self.gpu_card):
            cards.append(card)
        box.append(cards)

        strip = Gtk.Box(spacing=6, homogeneous=True)
        self.clock_tiles = {}
        for key, caption, unit in (("cpu", "CPU CLOCK", "GHz"), ("peak", "CPU PEAK", "GHz"), ("ram", "RAM SPEED", "MT/s"),
                                   ("igpu", "INTEL GPU", "MHz"), ("gpu", "NVIDIA CORE", "MHz"),
                                   ("vram", "NVIDIA VRAM", "MHz"), ("display", "DISPLAY", "Hz")):
            self.clock_tiles[key] = Tile(caption, unit)
            strip.append(self.clock_tiles[key])
        box.append(strip)

        strip = Gtk.Box(spacing=6, homogeneous=True)
        self.temp_tiles = {"cpu": Tile("CPU PKG", "°C"), "gpu": Tile("NVIDIA GPU", "°C")}
        for serial, model in self.sampler.nvme:
            self.temp_tiles[f"ssd:{serial}"] = Tile(f"{storage.brand(model).upper()} SSD", "°C")
        self.temp_tiles["wifi"] = Tile("WI-FI", "°C")
        for tile in self.temp_tiles.values():
            strip.append(tile)
        self.fan_tiles = {}
        for fan in self.sampler.fans():
            self.fan_tiles[fan["label"]] = Tile(f"{fan['label']} FAN", "RPM")
            strip.append(self.fan_tiles[fan["label"]])
        box.append(strip)

        # Hide readouts for hardware this machine doesn't have (another laptop, no NVIDIA card, ...).
        self.clock_tiles["igpu"].set_visible(self.sampler.igpu_card is not None)
        for tile in (self.clock_tiles["gpu"], self.clock_tiles["vram"], self.temp_tiles["gpu"]):
            tile.set_visible(self.sampler.dgpu_dev is not None)
        self.temp_tiles["wifi"].set_visible(self.sampler.wifi_sensor is not None)
        return box

    def _build_storage(self):
        box = panel("STORAGE · SMART", label("via udisks · no root", "panel-meta"))
        self.storage_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        self.storage_box.append(label("Reading SMART data…", "hw-line"))
        box.append(self.storage_box)
        return box

    def _build_security(self):
        self.security_meta = label("", "panel-meta")
        box = panel("SECURITY MATRIX", self.security_meta)
        rows = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=1)
        self.rows = {}
        for title, _ in checks.SECURITY_CHECKS:
            self.rows[title] = CheckRow(title, self._copy_fix)
            rows.append(self.rows[title])
        box.append(rows)
        return box

    def _build_power(self):
        self.power_chip = label("", "chip", "ok")
        box = panel("POWER · BATTERY", self.power_chip)
        row = Gtk.Box(spacing=14)
        self.bat_pct = label("—", "bat-pct", valign=Gtk.Align.CENTER)
        row.append(self.bat_pct)
        lines = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, valign=Gtk.Align.CENTER, spacing=4, hexpand=True)
        self.bat_bar = Segments(count=25, height=5)
        lines.append(self.bat_bar)
        self.bat_state = label("", "hw-line", ellipsize=Pango.EllipsizeMode.END)
        self.bat_limit = label("", "hw-line", ellipsize=Pango.EllipsizeMode.END)
        lines.append(self.bat_state)
        lines.append(self.bat_limit)
        row.append(lines)
        box.append(row)
        return box

    def _build_footer(self):
        bar = Gtk.Box(spacing=12, css_classes=["footer"])
        s = self.static
        bar.append(label(f"{s['os']}  ·  kernel {s['kernel']}  ·  host {s['hostname']}  ·  BIOS {s['bios']}",
                         "footer-text", ellipsize=Pango.EllipsizeMode.END, hexpand=True))
        self.footer_right = label("", "footer-text", xalign=1.0)
        bar.append(self.footer_right)
        return bar

    # ---------------------------------------------------------- live loop

    def _tick(self):
        self.clock.set_label(datetime.datetime.now().strftime("%H:%M"))     # GTK skips identical text: no relayout
        if not self._sampling:
            self._sampling = True
            threading.Thread(target=self._sample_worker, daemon=True).start()
        return GLib.SOURCE_CONTINUE

    def _sample_worker(self):
        try:
            data = self.sampler.sample()
        except Exception:
            traceback.print_exc()
            data = None
        GLib.idle_add(self._apply_sample, data)

    def _apply_sample(self, data):
        self._sampling = False
        if data:
            self.live = data
            try:
                self._render_live()
            except Exception:
                traceback.print_exc()   # one odd reading must not stop the widget; the next tick tries again
            self.live_dot.remove_css_class("dim")
            GLib.timeout_add(450, lambda: self.live_dot.add_css_class("dim") or GLib.SOURCE_REMOVE)
        return GLib.SOURCE_REMOVE

    def _display(self):
        display, surface = self.get_display(), self.get_surface()
        monitor = display.get_monitor_at_surface(surface) if surface else None
        if monitor is None:
            monitors = display.get_monitors()
            monitor = monitors.get_item(0) if monitors.get_n_items() else None
        if monitor is None:
            return None
        geo, scale = monitor.get_geometry(), monitor.get_scale()
        return {"hz": monitor.get_refresh_rate() / 1000, "w": round(geo.width * scale),
                "h": round(geo.height * scale), "scale": scale, "connector": monitor.get_connector() or ""}

    def _render_live(self):
        d, s = self.live, self.static
        cpu, mem, g, ig = d["cpu"], d["mem"], d.get("dgpu"), d.get("igpu")
        if cpu["pkg"] is not None:
            self.cpu_temps.append(cpu["pkg"])
        cpu_temp = sum(self.cpu_temps) / len(self.cpu_temps) if self.cpu_temps else None
        self.hardware = health.hardware_checks(d, cpu_temp, self.drives, self.io_check)
        hw = self.hardware
        self.uptime.set_label(f"UPTIME {uptime_text(d['uptime'])}")

        # CPU
        load = cpu["load"] / 100
        self.cpu_card.set(
            load, usage_rgb(load), f"{cpu['load']:.0f}%" if cpu["load"] >= 10 else f"{cpu['load']:.1f}%", "LOAD",
            f"{cpu['pkg']:.0f} °C" if cpu["pkg"] is not None else "—", hw["CPU thermals"].status, s["cpu_name"],
            [f"{s['cores']} cores · {s['threads']} threads",
             f"boost up to {s['cpu_max_ghz']:.2f} GHz" if s["cpu_max_ghz"] else ""])
        self.core_details.update(cpu, hw["CPU thermals"].detail)

        # Memory
        mods = s["ram"]
        installed = sum(m["size"] for m in mods) / 2 ** 30 if mods else gib(mem["total"])
        kind = mods[0]["type"] if mods else ""
        frac = mem["pct"] / 100
        layout = f"{len(mods)} × {mods[0]['size'] / 2 ** 30:.0f} GB" if mods else ""
        self.ram_card.set(
            frac, usage_rgb(frac), f"{mem['pct']:.0f}%", "IN USE",
            "OK" if hw["Memory"].status == "ok" else "HIGH", hw["Memory"].status,
            f"{installed:.0f} GB {kind}".strip(),
            [f"{gib(mem['used']):.1f} / {gib(mem['total']):.1f} GiB used",
             f"{layout} · swap {gib(mem['swap_used']):.1f}G".strip(" ·")],
            "\n".join(f"{m['size'] / 2 ** 30:.0f} GB {m['type']}-{m['speed']} {m['form']} · {m['maker']}" for m in mods))

        # Graphics
        if g:
            check = hw["NVIDIA GPU"]
            vram = f"{g['vram_total'] / 1024:.0f} GB" if g.get("vram_total") else ""
            short = g.get("name", "NVIDIA GPU").replace("GeForce ", "").removesuffix(" GPU")
            driver = ".".join(g.get("driver", "").split(".")[:2])
            if g.get("awake"):
                util = (g.get("util") or 0) / 100
                watts = f" · {g['watts']:.0f} W" if g.get("watts") is not None else ""
                self.gpu_card.set(
                    util, usage_rgb(util), f"{util * 100:.0f}%", "GPU LOAD", check.value, check.status,
                    short,
                    [f"VRAM {g['vram_used'] / 1024:.1f} / {g['vram_total'] / 1024:.1f} GB"
                     if g.get("vram_used") is not None and g.get("vram_total") else "",
                     f"{g.get('pstate', '')}{watts} · drv {driver}"],
                    check.detail)
            else:
                self.gpu_card.set(
                    0, RGB["watch"], "OFF", "ASLEEP", "SLEEP", "ok", short,
                    [f"{vram} VRAM · drv {driver}".strip(" ·"), "D3cold sleep · on-demand"],
                    check.detail)
        elif ig:
            frac = (ig["mhz"] or 0) / (ig["max_mhz"] or 1)
            self.gpu_card.set(frac, RGB["accent"], f"{ig['mhz'] or 0}", "MHz", "OK", "ok", s["igpu_name"] or "GPU",
                              ["integrated graphics", ""])
        else:
            self.gpu_card.set(0, RGB["accent"], "—", "NO SENSOR", "OK", "ok", s["igpu_name"] or "Graphics",
                              ["no live GPU sensor", "on this machine"])

        # Clocks
        t = self.clock_tiles
        max_ghz = s["cpu_max_ghz"] or 5
        if cpu["ghz_avg"]:
            t["cpu"].set(f"{cpu['ghz_avg']:.2f}", cpu["ghz_avg"] / max_ghz, rgb=RGB["accent"])
            t["peak"].set(f"{cpu['ghz_peak']:.2f}", cpu["ghz_peak"] / max_ghz, rgb=RGB["accent"])
        speed = mods[0]["speed"] if mods else 0
        if speed:
            t["ram"].set(f"{speed}", 1.0, rgb=RGB["accent"])
        if ig and ig["max_mhz"]:
            mhz = ig["mhz"] or 0
            t["igpu"].set(f"{mhz}" if mhz else "idle", mhz / ig["max_mhz"], unit="MHz" if mhz else "RC6",
                          rgb=RGB["accent"])
        if g and g.get("awake") and g.get("core_mhz") is not None:
            t["gpu"].set(f"{g['core_mhz']:.0f}", g["core_mhz"] / (g.get("core_max_mhz") or 2100), unit="MHz",
                         rgb=RGB["accent"])
            if g.get("mem_mhz") is not None:
                t["vram"].set(f"{g['mem_mhz']:.0f}", 1.0, unit="MHz", rgb=RGB["accent"])
        elif g:
            t["gpu"].set("sleep", 0, status="watch", unit="")
            t["vram"].set("sleep", 0, status="watch", unit="")
        disp = self._display()
        if disp:
            t["display"].set(f"{disp['hz']:.0f}", 1.0, rgb=RGB["accent"])
            t["display"].set_tooltip_text(f"{disp['connector']} · {disp['w']}×{disp['h']} · "
                                          f"scale {disp['scale'] * 100:.0f}% · {disp['hz']:.2f} Hz")

        # Thermals and fans
        tt = self.temp_tiles
        if cpu["pkg"] is not None:
            tt["cpu"].set(f"{cpu['pkg']:.0f}", cpu["pkg"] / TEMP_LIMITS["cpu"][2], temp_status("cpu", cpu["pkg"]))
        if g and g.get("awake") and g.get("temp") is not None:
            tt["gpu"].set(f"{g['temp']:.0f}", g["temp"] / TEMP_LIMITS["gpu"][2], temp_status("gpu", g["temp"]),
                          unit="°C")
        elif g:
            tt["gpu"].set("off", 0, "ok", unit="sleep")
        for serial, temp in d["drive_temps"].items():
            tile = tt.get(f"ssd:{serial}")
            if tile and temp is not None:
                tile.set(f"{temp:.0f}", temp / TEMP_LIMITS["ssd"][2], temp_status("ssd", temp))
            if serial in self.drive_rows:
                self.drive_rows[serial].set_temp(temp)
        if d["wifi"] is not None:
            tt["wifi"].set(f"{d['wifi']:.0f}", d["wifi"] / TEMP_LIMITS["wifi"][2], temp_status("wifi", d["wifi"]))
        for fan in d["fans"]:
            tile = self.fan_tiles.get(fan["label"])
            if tile and fan["rpm"] is not None:
                tile.set(f"{fan['rpm']:,}", fan["rpm"] / 6000, "ok", rgb=RGB["accent"])

        self._render_power(d.get("battery"), hw.get("Battery"))
        self._render_banner()

    def _render_power(self, bat, check):
        if not bat:
            self.bat_pct.set_label("AC")
            self.bat_state.set_label("No battery: running on mains power")
            self.power_chip.set_label("AC ONLY")
            set_status(self.power_chip, "watch")
            return
        pct = bat["pct"] or 0
        self.bat_pct.set_label(f"{pct}%")
        self.bat_bar.set_value(pct / 100, RGB["ok"] if pct > 20 else RGB["warn"])
        draw = f" · {bat['watts']:.1f} W" if bat["watts"] else ""
        self.bat_state.set_label(f"{bat['status']} · {'AC power' if bat['ac'] else 'on battery'}{draw}")
        limit = f"limit {bat['limit']} % at boot" if bat.get("limit") else "no charge limit"
        capacity = f" · {bat['full_wh']:.1f} / {bat['design_wh']:.1f} Wh" if bat["full_wh"] and bat["design_wh"] else ""
        self.bat_limit.set_label(limit + capacity)
        if check:
            self.power_chip.set_label(f"HEALTH {check.value.upper()}")
            set_status(self.power_chip, check.status)
            self.power_chip.set_tooltip_text(check.detail)

    def _render_banner(self):
        sec, hw = self.security, self.hardware
        total_sec = len(checks.SECURITY_CHECKS)
        passed = lambda group: sum(c.status in ("ok", "watch") for c in group.values())
        scores = {"security": (passed(sec), total_sec), "hardware": (passed(hw), len(hw))}
        flagged = [(t, c) for t, c in [*sec.items(), *hw.items()] if c.status in ("warn", "fail")]
        names = " · ".join(t for t, _ in flagged)
        if self._scanning and self.last_scan is None:
            status, title = "pending", f"SCANNING  {len(sec)}/{total_sec}"
            sub = "Verifying Secure Boot, encryption, firewall, DNS, kernel hardening and more…"
        elif any(c.status == "fail" for _, c in flagged):
            status, title = "fail", "ACTION REQUIRED"
            sub = f"Problems found: {names}"
        elif flagged:
            status = "warn"
            title = f"PROTECTED  ·  {len(flagged)} TO REVIEW"
            sub = f"All core defences are active. To review: {names}"
        else:
            status, title = "ok", "ALL SYSTEMS SECURE"
            sub = (f"{total_sec} security checks passed · device hardware healthy · "
                   f"Secure Boot, encryption and firewall verified")
        when = self.last_scan.strftime("%H:%M:%S") if self.last_scan else "—"
        nxt = (self.last_scan + datetime.timedelta(seconds=RESCAN_SECONDS)).strftime("%H:%M") if self.last_scan else "—"
        meta = f"LAST FULL SCAN {when}   ·   NEXT {nxt}   ·   LIVE TELEMETRY EVERY {FAST_SECONDS}s"
        self.banner.set(status, title, sub, meta, scores)
        n_ok = scores["security"][0]
        self.security_meta.set_label(f"{n_ok}/{total_sec} PASS" if sec else "")
        self.footer_right.set_label("hover any row for details · click a flagged row to copy its fix")

    # ---------------------------------------------------------- full scan

    def rescan(self):
        if self._scanning:
            return
        self._scanning = True
        for row in self.rows.values():
            row.set_pending()
        self.scanbar.set_opacity(1)
        self.scanbar.set_fraction(0)
        threading.Thread(target=self._scan_worker, daemon=True).start()

    def _scan_worker(self):
        steps = len(checks.SECURITY_CHECKS) + 2
        for i, (title, fn) in enumerate(checks.SECURITY_CHECKS):
            try:
                result = fn()
            except Exception as e:
                traceback.print_exc()
                result = checks.Check("watch", "error", f"This check could not run: {e}")
            GLib.idle_add(self._apply_check, title, result, (i + 1) / steps)
            time.sleep(SCAN_STAGGER)
        try:
            drives = storage.drives()
        except Exception:
            traceback.print_exc()
            drives = []
        GLib.idle_add(self._apply_drives, drives, (steps - 1) / steps)
        try:
            io = checks.drive_io_errors()
        except Exception as e:
            io = checks.Check("watch", "error", f"This check could not run: {e}")
        GLib.idle_add(self._scan_done, io)

    def _apply_check(self, title, result, progress):
        self.security[title] = result
        self.rows[title].set(result)
        self.scanbar.set_fraction(progress)
        self._render_banner()
        return GLib.SOURCE_REMOVE

    def _apply_drives(self, drives, progress):
        self.drives = drives
        self.scanbar.set_fraction(progress)
        child = self.storage_box.get_first_child()
        while child:
            nxt = child.get_next_sibling()
            self.storage_box.remove(child)
            child = nxt
        self.drive_rows = {}
        for d in drives:
            row = DriveRow(d)
            self.drive_rows[d["serial"]] = row
            self.storage_box.append(row)
            if self.live:
                row.set_temp(self.live["drive_temps"].get(d["serial"]))
        if not drives:
            self.storage_box.append(label("No drives found.", "hw-line"))
        return GLib.SOURCE_REMOVE

    def _scan_done(self, io):
        self.io_check = io
        self.last_scan = datetime.datetime.now()
        self._scanning = False
        self.scanbar.set_fraction(1)
        GLib.timeout_add(700, lambda: self.scanbar.set_opacity(0) or GLib.SOURCE_REMOVE)
        if self.live:
            self._render_live()
        else:
            self._render_banner()
        return GLib.SOURCE_REMOVE

    def _copy_fix(self, command):
        self.get_clipboard().set(command)
        self.toasts.add_toast(Adw.Toast(title="Fix command copied. Paste it into a terminal (Ctrl+Shift+V).",
                                        timeout=4))


class SentinelApp(Adw.Application):
    def __init__(self):
        super().__init__(application_id=APP_ID, flags=Gio.ApplicationFlags.DEFAULT_FLAGS)
        self.window = None

    def do_startup(self):
        Adw.Application.do_startup(self)
        Adw.StyleManager.get_default().set_color_scheme(Adw.ColorScheme.FORCE_DARK)
        css = Gtk.CssProvider()
        css.load_from_path(os.path.join(HERE, "style.css"))
        Gtk.StyleContext.add_provider_for_display(Gdk.Display.get_default(), css,
                                                  Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
        self.set_accels_for_action("win.rescan", ["<Control>r", "F5"])
        self.set_accels_for_action("window.close", ["Escape", "<Control>w", "<Control>q"])

    def do_activate(self):
        if self.window is None:
            self.window = SentinelWindow(self)
        else:
            self.window.rescan()
        self.window.present()


def main():
    return SentinelApp().run(sys.argv)
