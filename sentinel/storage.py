"""Physical drives: layout and encryption from lsblk, SMART health from udisks (no root needed)."""

import datetime
import json

import psutil
from gi.repository import Gio, GLib

from . import history
from .hardware import run

UDISKS = "org.freedesktop.UDisks2"
NVME = "org.freedesktop.UDisks2.NVMe.Controller"
KELVIN = 273.15


def _smart_by_serial():
    try:
        bus = Gio.bus_get_sync(Gio.BusType.SYSTEM, None)
        objects = bus.call_sync(UDISKS, "/org/freedesktop/UDisks2", "org.freedesktop.DBus.ObjectManager",
                                "GetManagedObjects", None, None, Gio.DBusCallFlags.NONE, 5000, None).unpack()[0]
    except GLib.Error:
        return {}
    smart = {}
    for path, ifaces in objects.items():
        drive, nvme = ifaces.get("org.freedesktop.UDisks2.Drive"), ifaces.get(NVME)
        if not drive or not nvme:
            continue
        try:
            attrs = bus.call_sync(UDISKS, path, NVME, "SmartGetAttributes", GLib.Variant("(a{sv})", ({},)),
                                  None, Gio.DBusCallFlags.NONE, 5000, None).unpack()[0]
        except GLib.Error:
            attrs = {}
        temp_k = nvme.get("SmartTemperature") or 0
        smart[drive.get("Serial", "")] = {
            "critical": list(nvme.get("SmartCriticalWarning") or []),
            "hours": nvme.get("SmartPowerOnHours"),
            "temp": temp_k - KELVIN if temp_k else None,
            **attrs,
        }
    return smart


def _walk(node):
    yield node
    for child in node.get("children") or []:
        yield from _walk(child)


def _verdict(serial, s):
    """Health status of one drive: (status, short text)."""
    if not s:
        return "watch", "SMART unavailable"
    errors = s.get("media_errors") or 0
    base = history.record(serial, errors, s.get("percent_used"))
    if s["critical"]:
        return "fail", "SMART warning: " + ", ".join(s["critical"])
    if s.get("avail_spare") is not None and s.get("spare_thresh") is not None \
            and s["avail_spare"] < s["spare_thresh"]:
        return "fail", "spare blocks below threshold"
    if (s.get("percent_used") or 0) >= 100:
        return "warn", "past rated write life"
    if errors > base["media_errors"]:
        return "fail", f"integrity errors rising: +{errors - base['media_errors']} since {_short_date(base['date'])}"
    if errors:
        return "watch", f"{errors} legacy errors, stable since {_short_date(base['date'])}"
    return "ok", "SMART healthy"


def brand(model):
    """'SAMSUNG MZVLQ512HBLU-00B00' -> 'Samsung', 'ADATA LEGEND 710' -> 'ADATA'."""
    word = model.split()[0] if model else "Drive"
    return word.capitalize() if len(word) > 5 else word.upper()


def _short_date(iso):
    try:
        return datetime.date.fromisoformat(iso).strftime("%-d %b")
    except ValueError:
        return iso


def drives():
    rc, out, _ = run(["lsblk", "-J", "-b", "-o", "NAME,TYPE,FSTYPE,MOUNTPOINTS,SERIAL,MODEL,SIZE"])
    try:
        tree = json.loads(out)["blockdevices"] if rc == 0 else []
    except (ValueError, KeyError):
        tree = []
    smart = _smart_by_serial()
    result = []
    for disk in tree:
        if disk.get("type") != "disk":
            continue
        nodes = list(_walk(disk))
        mounts = [m for n in nodes for m in (n.get("mountpoints") or []) if m and m != "[SWAP]"]
        main = "/" if "/" in mounts else next((m for m in mounts if not m.startswith("/boot")), None)
        usage = psutil.disk_usage(main) if main else None
        serial = (disk.get("serial") or "").strip()
        model = (disk.get("model") or "Unknown drive").strip()
        s = smart.get(serial, {})
        status, verdict = _verdict(serial, s)
        result.append({
            "name": disk["name"],
            "model": model,
            "brand": brand(model),
            "serial": serial,
            "size": disk.get("size") or 0,
            "encrypted": any(n.get("fstype") == "crypto_LUKS" or n.get("type") == "crypt" for n in nodes),
            "mount": main,
            "role": "SYSTEM" if main == "/" else (main or "unmounted"),
            "used": usage.used if usage else None,
            "total": usage.total if usage else None,
            "smart": s,
            "status": status,
            "verdict": verdict,
        })
    return result
