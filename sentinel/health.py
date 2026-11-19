"""Hardware health verdicts, built from the live readings plus the SMART data of the last full scan.

Thresholds follow the Device Health Report: CPU cores are comfortable under 80 °C in sustained load (limit 100 °C),
a battery is "worn" below 80 % of its design capacity, and SSD integrity errors matter only when they rise.
"""

from .checks import Check


def cpu(temp):
    if temp is None:
        return Check("watch", "no sensor", "No CPU temperature sensor found.")
    if temp < 80:
        return Check("ok", f"{temp:.0f} °C", "CPU temperature is in the comfortable range (under 80 °C).")
    if temp < 95:
        return Check("watch", f"{temp:.0f} °C", "CPU is warm, which is normal under sustained load. Limit: 100 °C.")
    return Check("warn", f"{temp:.0f} °C", "CPU is close to its 100 °C limit and will throttle. Check the fans "
                 "and vents.")


def gpu(dgpu):
    if not dgpu.get("awake"):
        return Check("ok", "asleep", "The NVIDIA GPU is powered down (on-demand mode): no heat, no battery drain.")
    temp = dgpu.get("temp")
    if temp is None or temp < 85:
        return Check("ok", f"{temp:.0f} °C" if temp is not None else "active", "GPU temperature is normal.")
    if temp < 93:
        return Check("watch", f"{temp:.0f} °C", "GPU is hot but within its operating range.")
    return Check("warn", f"{temp:.0f} °C", "GPU is near its thermal limit.")


def memory(mem):
    pct = mem["pct"]
    if pct < 85:
        return Check("ok", f"{pct:.0f} %", "Plenty of free memory.")
    if pct < 95:
        return Check("watch", f"{pct:.0f} %", "Memory is getting full; the system may start swapping.")
    return Check("warn", f"{pct:.0f} %", "Memory is almost exhausted. Close heavy apps or AI models.")


def drive(d, temp):
    if temp is not None and temp >= 70:
        return Check("warn", f"{temp:.0f} °C", f"{d['model']} is running hot ({temp:.0f} °C).")
    return Check(d["status"], d["verdict"], d["verdict"])


def battery(bat):
    health = bat.get("health")
    limit = f" Charge limit: {bat['limit']} %." if bat.get("limit") else ""
    if health is None:
        return Check("watch", "unknown", "Battery capacity not reported." + limit)
    if health >= 80:
        return Check("ok", f"{health:.0f} %", f"Battery holds {health:.0f} % of its design capacity.{limit}")
    if health >= 50:
        return Check("watch", f"{health:.0f} % · worn",
                     f"Battery holds {health:.0f} % of its design capacity: normal wear, shorter runtime.{limit}\n"
                     "Replace it at once if the touchpad lifts or the case bulges.")
    return Check("warn", f"{health:.0f} %", f"Battery holds only {health:.0f} % of its design capacity; plan a "
                 "replacement." + limit)


def cooling(fans, cpu_temp):
    if not fans:
        return Check("watch", "no sensor", "Fan speeds are not reported.")
    spinning = [f for f in fans if (f["rpm"] or 0) > 0]
    if spinning:
        return Check("ok", "spinning", " · ".join(f"{f['label']} fan {f['rpm']} RPM" for f in fans))
    if (cpu_temp or 0) > 70:
        return Check("warn", "fans stopped", "The fans report 0 RPM while the CPU is above 70 °C.")
    return Check("ok", "idle", "Fans are stopped because the laptop is cool.")


def hardware_checks(live, cpu_temp, drives, io_check):
    """Ordered {title: Check} for everything the HARDWARE score counts."""
    out = {"CPU thermals": cpu(cpu_temp)}
    if live.get("dgpu"):
        out["NVIDIA GPU"] = gpu(live["dgpu"])
    out["Memory"] = memory(live["mem"])
    out["Cooling"] = cooling(live["fans"], cpu_temp)
    for d in drives:
        out[f"{d['brand']} SSD"] = drive(d, live["drive_temps"].get(d["serial"]))
    if io_check:
        out["Drive I/O log"] = io_check
    if live.get("battery"):
        out["Battery"] = battery(live["battery"])
    return out
