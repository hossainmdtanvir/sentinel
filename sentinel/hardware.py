"""Live hardware readings. Everything here works without root and never wakes a sleeping GPU on its own."""

import glob
import os
import re
import subprocess
import time

import psutil


def read(path, default=None):
    try:
        with open(path) as f:
            return f.read().strip()
    except OSError:
        return default


def read_int(path, default=None):
    try:
        return int(read(path))
    except (TypeError, ValueError):
        return default


def run(cmd, timeout=8):
    """Run a command and return (returncode, stdout, stderr). Never raises."""
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return p.returncode, p.stdout, p.stderr
    except (OSError, subprocess.TimeoutExpired) as e:
        return -1, "", str(e)


def hwmon_dirs(prefix):
    """hwmon directories whose driver name starts with prefix (e.g. 'coretemp', 'asus', 'iwlwifi')."""
    return [h for h in sorted(glob.glob("/sys/class/hwmon/hwmon*"))
            if (read(f"{h}/name") or "").startswith(prefix)]


def celsius(path):
    v = read_int(path)
    return None if v is None else v / 1000


def _num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


# ------------------------------------------------------------------ static info

def _display_pci(vendor):
    """Display-class PCI devices from a vendor. Reads cached sysfs attributes only, so a sleeping GPU stays asleep."""
    return [d for d in sorted(glob.glob("/sys/bus/pci/devices/*"))
            if read(f"{d}/vendor") == vendor and (read(f"{d}/class") or "").startswith("0x03")]


def _intel_card():
    for card in sorted(glob.glob("/sys/class/drm/card*")):
        if re.search(r"card\d+$", card) and read(f"{card}/device/vendor") == "0x8086" \
                and os.path.exists(f"{card}/gt_act_freq_mhz"):
            return card
    return None


def _fan_inputs():
    """Fan speed sensors from the laptop's platform driver (asus, thinkpad, dell_smm, hp, ...), at most two."""
    found, seen = [], set()
    for h in sorted(glob.glob("/sys/class/hwmon/hwmon*")):
        for f in sorted(glob.glob(f"{h}/fan*_input")):
            raw = read(f.replace("_input", "_label")) or f"{read(f'{h}/name', '')} {os.path.basename(f).split('_')[0]}"
            label = re.sub(r"[_ ]?fan\d*$", "", raw, flags=re.I).strip().upper() or "SYSTEM"
            if label in seen:
                label = f"{label} {len(found) + 1}"
            seen.add(label)
            found.append((label, f))
    return found[:2]


def _wifi_sensor():
    """Temperature input of an Intel (iwlwifi) or MediaTek (mt7xxx) Wi-Fi card, if it has one."""
    for h in hwmon_dirs("iwlwifi") + hwmon_dirs("mt7"):
        if os.path.exists(f"{h}/temp1_input"):
            return f"{h}/temp1_input"
    return None


def _pci_name(dev):
    """Marketing name of a PCI device from lspci, e.g. 'Intel UHD Graphics'."""
    _, out, _ = run(["lspci", "-s", os.path.basename(dev)])
    desc = out.split(": ", 1)[-1].strip()
    bracket = re.search(r"\[([^\]]+)\]", desc)
    vendor = desc.split()[0] if desc else ""
    return f"{vendor} {bracket.group(1)}".strip() if bracket else re.sub(r"\s*\(rev.*\)", "", desc)


def _cpu_name():
    raw = ""
    for line in (read("/proc/cpuinfo") or "").splitlines():
        if line.startswith("model name"):
            raw = line.split(":", 1)[1].strip()
            break
    name = re.sub(r"\(R\)|\(TM\)|\bCPU\b|@.*$|\b\d+(st|nd|rd|th) Gen\b", "", raw)
    return " ".join(name.split()) or "Unknown CPU"


def _cpu_number(path):
    return int(re.search(r"/cpu(\d+)/", path).group(1))


def _cpu_topology():
    """Physical cores with the logical CPUs (hyper-threads) they run: [(core_id, [cpu, ...])], in core order."""
    cores = {}
    for path in glob.glob("/sys/devices/system/cpu/cpu[0-9]*/topology/core_id"):
        core = read_int(path)
        if core is not None:
            cores.setdefault(core, []).append(_cpu_number(path))
    return [(core, sorted(cpus)) for core, cpus in sorted(cores.items())]


def _ram_modules():
    """Installed memory modules from the DMI tables that udev exposes to every user."""
    _, out, _ = run(["udevadm", "info", "--query=property", "/sys/devices/virtual/dmi/id"])
    slots = {}
    for line in out.splitlines():
        m = re.match(r"MEMORY_DEVICE_(\d+)_(\w+)=(.*)", line)
        if m:
            slots.setdefault(int(m.group(1)), {})[m.group(2)] = m.group(3)
    mods = []
    for s in slots.values():
        size = int(s.get("SIZE", 0) or 0)
        if size:
            mods.append({
                "size": size,
                "type": s.get("TYPE", ""),
                "speed": int(s.get("CONFIGURED_SPEED_MTS") or s.get("SPEED_MTS") or 0),
                "maker": s.get("MANUFACTURER", ""),
                "form": s.get("FORM_FACTOR", ""),
            })
    return mods


def _os_name():
    for line in (read("/etc/os-release") or "").splitlines():
        if line.startswith("PRETTY_NAME="):
            return line.split("=", 1)[1].strip('"')
    return "Linux"


def static_info():
    product = read("/sys/class/dmi/id/product_name", "Unknown device")
    product = re.sub(r"\b(\w+)_\1\b", r"\1", product)          # "FX506HC_FX506HC" -> "FX506HC"
    igpu = _display_pci("0x8086")
    max_khz = read_int("/sys/devices/system/cpu/cpu0/cpufreq/cpuinfo_max_freq")
    return {
        "product": product,
        "bios": read("/sys/class/dmi/id/bios_version", ""),
        "os": _os_name(),
        "kernel": os.uname().release,
        "hostname": os.uname().nodename,
        "cpu_name": _cpu_name(),
        "cores": psutil.cpu_count(logical=False),
        "threads": psutil.cpu_count(logical=True),
        "topology": _cpu_topology(),
        "cpu_max_ghz": max_khz / 1e6 if max_khz else None,
        "ram": _ram_modules(),
        "igpu_name": _pci_name(igpu[0]) if igpu else None,
    }


# ------------------------------------------------------------------ GPU

NVIDIA_FIELDS = ("name", "temperature.gpu", "utilization.gpu", "memory.used", "memory.total",
                 "clocks.gr", "clocks.max.gr", "clocks.mem", "power.draw", "pstate", "driver_version")


def nvidia_smi():
    rc, out, _ = run(["nvidia-smi", f"--query-gpu={','.join(NVIDIA_FIELDS)}",
                      "--format=csv,noheader,nounits"], timeout=10)
    if rc != 0 or not out.strip():
        return None
    vals = [v.strip() for v in out.splitlines()[0].split(",")]
    if len(vals) != len(NVIDIA_FIELDS):
        return None
    r = dict(zip(NVIDIA_FIELDS, vals))
    return {
        "name": r["name"].replace("NVIDIA ", ""),
        "temp": _num(r["temperature.gpu"]),
        "util": _num(r["utilization.gpu"]),
        "vram_used": _num(r["memory.used"]),
        "vram_total": _num(r["memory.total"]),
        "core_mhz": _num(r["clocks.gr"]),
        "core_max_mhz": _num(r["clocks.max.gr"]),
        "mem_mhz": _num(r["clocks.mem"]),
        "watts": _num(r["power.draw"]),
        "pstate": r["pstate"],
        "driver": r["driver_version"],
    }


# ------------------------------------------------------------------ battery

def battery():
    bat = next((p for p in sorted(glob.glob("/sys/class/power_supply/*"))
                if read(f"{p}/type") == "Battery" and read(f"{p}/present", "1") == "1"), None)
    if not bat:
        return None
    ac = any(read(f"{p}/online") == "1" for p in glob.glob("/sys/class/power_supply/*")
             if read(f"{p}/type") == "Mains")
    vmin = read_int(f"{bat}/voltage_min_design")
    if read_int(f"{bat}/energy_full") is not None:
        full, design = read_int(f"{bat}/energy_full"), read_int(f"{bat}/energy_full_design")
        full_wh = full / 1e6 if full else None
        design_wh = design / 1e6 if design else None
    else:
        full, design = read_int(f"{bat}/charge_full"), read_int(f"{bat}/charge_full_design")
        full_wh = full * vmin / 1e12 if full and vmin else None
        design_wh = design * vmin / 1e12 if design and vmin else None
    if read_int(f"{bat}/power_now") is not None:
        watts = read_int(f"{bat}/power_now") / 1e6
    else:
        cur, volt = read_int(f"{bat}/current_now"), read_int(f"{bat}/voltage_now")
        watts = cur * volt / 1e12 if cur is not None and volt else None
    return {
        "pct": read_int(f"{bat}/capacity"),
        "status": read(f"{bat}/status", "Unknown"),
        "limit": read_int(f"{bat}/charge_control_end_threshold"),
        "health": full / design * 100 if full and design else None,
        "full_wh": full_wh,
        "design_wh": design_wh,
        "watts": watts,
        "ac": ac,
        "cycles": read_int(f"{bat}/cycle_count") or None,
    }


# ------------------------------------------------------------------ live sampler

class Sampler:
    """One snapshot of fast-changing readings. sample() is called from a worker thread every couple of seconds."""

    GPU_BUSY_INTERVAL = 2     # seconds between nvidia-smi calls while the GPU is working
    GPU_IDLE_INTERVAL = 20    # long enough for the driver to put an idle GPU back to sleep
    DRIVE_INTERVAL = 10       # reading an NVMe temperature wakes the drive's controller, so not every tick

    def __init__(self):
        psutil.cpu_percent(None)
        psutil.cpu_percent(None, percpu=True)
        self.online_cpus = sorted(cpu for _, cpus in _cpu_topology() for cpu in cpus)
        self.dgpu_dev = next(iter(_display_pci("0x10de")), None)
        self.igpu_card = _intel_card()
        self.nvme = sorted((read(f"{c}/serial", ""), read(f"{c}/model", "")) for c in glob.glob("/sys/class/nvme/nvme*"))
        self.fan_inputs = _fan_inputs()
        self.wifi_sensor = _wifi_sensor()
        self.gpu = None
        self._gpu_due = 0.0
        self._drive_cache = {}
        self._drives_due = 0.0

    def sample(self):
        return {
            "cpu": self._cpu(),
            "mem": self._mem(),
            "igpu": self._igpu(),
            "dgpu": self._dgpu(),
            "fans": self.fans(),
            "drive_temps": self._drive_temps(),
            "wifi": self._wifi(),
            "battery": battery(),
            "uptime": time.time() - psutil.boot_time(),
        }

    def _cpu(self):
        khz = {_cpu_number(p): read_int(p) for p in glob.glob("/sys/devices/system/cpu/cpu[0-9]*/cpufreq/scaling_cur_freq")}
        ghz_by_cpu = {n: k / 1e6 for n, k in khz.items() if k}
        ghz = list(ghz_by_cpu.values())
        pkg, cores, core_temps, crit = None, [], {}, 100.0
        for h in hwmon_dirs("coretemp"):
            for label_path in glob.glob(f"{h}/temp*_label"):
                label = read(label_path, "")
                value = celsius(label_path.replace("_label", "_input"))
                if value is None:
                    continue
                if label.startswith("Package"):
                    pkg = value
                    crit = celsius(label_path.replace("_label", "_crit")) or crit
                elif label.startswith("Core"):
                    cores.append(value)
                    core_id = re.search(r"\d+", label)       # "Core 3" is the core with topology core_id 3
                    if core_id:
                        core_temps[int(core_id.group())] = value
        if pkg is None and not cores:
            amd = {}
            for h in hwmon_dirs("k10temp") + hwmon_dirs("zenpower"):
                for label_path in glob.glob(f"{h}/temp*_label"):
                    amd[read(label_path, "")] = celsius(label_path.replace("_label", "_input"))
            pkg = amd.get("Tdie") or amd.get("Tctl")
            cores = [v for k, v in amd.items() if k.startswith("Tccd") and v is not None]
        return {
            "load": psutil.cpu_percent(None),
            "ghz_avg": sum(ghz) / len(ghz) if ghz else None,
            "ghz_peak": max(ghz) if ghz else None,
            "pkg": pkg if pkg is not None else (max(cores) if cores else None),
            "hottest_core": max(cores) if cores else None,
            "crit": crit,
            # psutil lists online CPUs in /proc/stat order, which is ascending CPU number.
            "per_cpu": dict(zip(self.online_cpus, psutil.cpu_percent(None, percpu=True))),
            "ghz_by_cpu": ghz_by_cpu,
            "core_temps": core_temps,
            "governor": read("/sys/devices/system/cpu/cpu0/cpufreq/scaling_governor"),
            "epp": read("/sys/devices/system/cpu/cpu0/cpufreq/energy_performance_preference"),
            "loadavg": os.getloadavg(),
        }

    def _mem(self):
        vm, sw = psutil.virtual_memory(), psutil.swap_memory()
        return {"used": vm.total - vm.available, "total": vm.total, "pct": vm.percent,
                "swap_used": sw.used, "swap_total": sw.total}

    def _igpu(self):
        if not self.igpu_card:
            return None
        return {"mhz": read_int(f"{self.igpu_card}/gt_act_freq_mhz"),
                "max_mhz": read_int(f"{self.igpu_card}/gt_max_freq_mhz")}

    def _dgpu(self):
        if not self.dgpu_dev:
            return None
        status_path = f"{self.dgpu_dev}/power/runtime_status"
        awake = read(status_path) != "suspended"
        now = time.monotonic()
        # nvidia-smi wakes a sleeping GPU. Read it once at start-up so the card's details are known, then only
        # while it is already awake, backing off when idle so the driver can suspend it again.
        if self.gpu is None or (awake and now >= self._gpu_due):
            reading = nvidia_smi()
            self.gpu = reading or self.gpu or {}
            busy = bool(reading) and (reading["util"] or 0) >= 5
            self._gpu_due = now + (self.GPU_BUSY_INTERVAL if busy else self.GPU_IDLE_INTERVAL)
            awake = read(status_path) != "suspended"
        return {"awake": awake, **self.gpu}

    def fans(self):
        return [{"label": label, "rpm": read_int(path)} for label, path in self.fan_inputs]

    def _drive_temps(self):
        if time.monotonic() < self._drives_due:
            return self._drive_cache
        self._drives_due = time.monotonic() + self.DRIVE_INTERVAL
        temps = self._drive_cache = {}
        for ctrl in glob.glob("/sys/class/nvme/nvme*"):
            serial = read(f"{ctrl}/serial")
            sensor = next(iter(sorted(glob.glob(f"{ctrl}/hwmon*/temp1_input"))), None)
            if serial and sensor:
                temps[serial] = celsius(sensor)
        return temps

    def _wifi(self):
        return celsius(self.wifi_sensor) if self.wifi_sensor else None
