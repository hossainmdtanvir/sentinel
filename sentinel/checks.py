"""Security posture checks.

They mirror the verification block of the Secure Linux Setup Handbook (Vol. 2, Part H) and the hardening in
Vol. 1, Part E, and all of them run without root. A check reports what it actually found: nothing here assumes
a protection is on because the handbook says it was configured once.
"""

import datetime
import glob
import json
import os
import re
from dataclasses import dataclass

from gi.repository import Gio

from .hardware import read, run


@dataclass
class Check:
    status: str   # ok | watch | warn | fail
    value: str    # short readout shown on the row
    detail: str   # tooltip: what was verified and, when not ok, how to fix it


def _systemctl(verb, unit):
    _, out, _ = run(["systemctl", verb, unit])
    return out.strip() or "not-found"


def _gsetting(schema, key):
    source = Gio.SettingsSchemaSource.get_default()
    found = source.lookup(schema, True) if source else None
    if not found or not found.has_key(key):
        return None
    return Gio.Settings.new(schema).get_value(key).unpack()


# ------------------------------------------------------------------ boot & disk

def secure_boot():
    raw = b""
    for f in glob.glob("/sys/firmware/efi/efivars/SecureBoot-*"):
        try:
            with open(f, "rb") as fh:
                raw = fh.read()
        except OSError:
            pass
    if len(raw) >= 5:
        enabled = raw[4] == 1
    else:
        rc, out, _ = run(["mokutil", "--sb-state"])
        if rc != 0 and not out:
            return Check("warn", "unknown", "Could not read the Secure Boot state.")
        enabled = "SecureBoot enabled" in out
    if enabled:
        return Check("ok", "enabled",
                     "UEFI Secure Boot is on: only signed boot loaders, kernels and drivers can start.")
    return Check("fail", "disabled",
                 "Secure Boot is off. BIOS (F2) → F7 Advanced → Security → Secure Boot Control = Enabled.")


def disk_encryption():
    _, src, _ = run(["findmnt", "-no", "SOURCE", "/"])
    rc, out, _ = run(["lsblk", "-s", "-J", "-o", "NAME,TYPE,FSTYPE", src.strip()])
    chain = []
    try:
        node = json.loads(out)["blockdevices"][0] if rc == 0 else None
    except (ValueError, KeyError, IndexError):
        node = None
    while node:
        chain.append(node)
        node = (node.get("children") or [None])[0]
    if any(n.get("type") == "crypt" or n.get("fstype") == "crypto_LUKS" for n in chain):
        path = " ← ".join(f"{n['name']} ({n.get('fstype') or n.get('type')})" for n in chain)
        return Check("ok", "LUKS full-disk",
                     f"The system drive is encrypted with LUKS. Root file system chain:\n{path}")
    return Check("fail", "not encrypted",
                 "The root file system is not on a LUKS-encrypted device.")


# ------------------------------------------------------------------ network

def firewall():
    enabled = re.search(r"^ENABLED\s*=\s*yes", read("/etc/ufw/ufw.conf", ""), re.M | re.I)
    m = re.search(r'^DEFAULT_INPUT_POLICY\s*=\s*"?(\w+)', read("/etc/default/ufw", ""), re.M)
    policy = m.group(1).upper() if m else "UNKNOWN"
    active = _systemctl("is-active", "ufw") == "active"
    if not (enabled and active):
        return Check("fail", "inactive", "UFW is not running. Fix: sudo ufw enable")
    if policy not in ("DROP", "REJECT"):
        return Check("warn", f"incoming {policy.lower()}",
                     "UFW is on but accepts incoming connections. Fix: sudo ufw default deny incoming")
    return Check("ok", "active · deny in",
                 f"UFW is enabled at boot and running. Default incoming policy: {policy}.")


def encrypted_dns():
    rc, out, _ = run(["resolvectl", "status"])
    if rc != 0:
        return Check("warn", "unknown", "Could not query systemd-resolved.")
    glob_part = out.split("\nLink ")[0]
    servers = re.search(r"DNS Servers?:\s*(.+)", glob_part)
    server = servers.group(1).split()[0] if servers else ""
    all_tls = all("+DNSOverTLS" in line for line in out.splitlines() if "Protocols:" in line)
    if "+DNSOverTLS" in glob_part and all_tls and server:
        name = "Cloudflare" if "cloudflare" in server else server.split("#")[0]
        return Check("ok", f"{name} · TLS",
                     f"All DNS lookups go over DNS-over-TLS to {server}, which also blocks malware domains.")
    return Check("warn", "plain DNS",
                 "DNS-over-TLS is not active for every link. See handbook Vol. 1, E6 "
                 "(/etc/systemd/resolved.conf.d/dns.conf). It may be temporarily off for a hotel Wi-Fi login.")


def _proc_name(line):
    m = re.search(r'users:\(\("([^"]+)",pid=(\d+)', line)
    if not m:
        return "system"
    try:
        with open(f"/proc/{m.group(2)}/cmdline", "rb") as f:
            argv = [a.decode(errors="replace") for a in f.read().split(b"\0") if a]
    except OSError:
        return m.group(1)
    if len(argv) > 1 and os.path.basename(argv[0]).startswith("python"):
        return os.path.basename(argv[1])
    return m.group(1)


def _is_loopback(host):
    return host.startswith("127.") or host in ("::1", "localhost")


def open_ports():
    rc, out, _ = run(["ss", "-tulnpH"])
    if rc != 0:
        return Check("warn", "unknown", "Could not list listening sockets.")
    tcp, udp, local = [], set(), 0
    for line in out.splitlines():
        parts = line.split()
        if len(parts) < 5:
            continue
        host, _, port = parts[4].rpartition(":")
        if _is_loopback(host.strip("[]").split("%")[0]):
            local += 1
        elif parts[0] == "tcp":
            tcp.append(f"{port} ({_proc_name(line)})")
        else:
            udp.add(_proc_name(line))
    if tcp:
        return Check("warn", f"{len(tcp)} TCP exposed",
                     "Programs accepting network connections from other machines:\n" + "\n".join(tcp)
                     + "\nThe firewall still blocks them, but check each one with: sudo ss -tulpn")
    note = f"{local} services listen on this laptop only (127.0.0.1 / ::1). No TCP port is open to the network."
    if udp:
        note += f"\nUDP discovery sockets: {', '.join(sorted(udp))} (wsdd only searches for Windows shares)."
    return Check("ok", "none exposed", note)


def network_services():
    cups, avahi = _systemctl("is-enabled", "cups-browsed"), _systemctl("is-enabled", "avahi-daemon.service")
    off = ("disabled", "masked", "not-found")
    if cups in off and avahi in ("masked", "not-found"):
        return Check("ok", "avahi masked",
                     f"cups-browsed: {cups}. avahi-daemon: {avahi}, so it cannot be started at all.")
    if cups in off and avahi == "disabled":
        return Check("warn", "avahi not masked",
                     "avahi is disabled but can still be woken by other programs.\n"
                     "Fix: sudo systemctl mask --now avahi-daemon.service avahi-daemon.socket")
    return Check("warn", "running",
                 f"cups-browsed: {cups}, avahi-daemon: {avahi}. Both announce or discover devices on the network.")


# ------------------------------------------------------------------ system protection

def apparmor():
    loaded = read("/sys/module/apparmor/parameters/enabled") == "Y"
    active = _systemctl("is-active", "apparmor") == "active"
    if loaded and active:
        return Check("ok", "enforcing",
                     "The AppArmor kernel module is loaded and its profiles are applied at boot.")
    return Check("fail", "off", "AppArmor is not active. Fix: sudo systemctl enable --now apparmor")


HARDENING = {
    "kernel.kptr_restrict": "2", "kernel.dmesg_restrict": "1", "kernel.yama.ptrace_scope": "1",
    "fs.protected_hardlinks": "1", "fs.protected_symlinks": "1",
    "net.ipv4.conf.all.accept_redirects": "0", "net.ipv4.conf.default.accept_redirects": "0",
    "net.ipv6.conf.all.accept_redirects": "0", "net.ipv4.conf.all.send_redirects": "0",
    "net.ipv4.conf.all.accept_source_route": "0", "net.ipv4.tcp_syncookies": "1",
    "net.ipv4.icmp_echo_ignore_broadcasts": "1",
}


def kernel_hardening():
    bad = [f"{k} = {read('/proc/sys/' + k.replace('.', '/'))} (want {v})"
           for k, v in HARDENING.items() if read("/proc/sys/" + k.replace(".", "/")) != v]
    n = len(HARDENING)
    if not bad:
        return Check("ok", f"{n}/{n} sysctl",
                     "All hardening values from /etc/sysctl.d/99-hardening.conf are live: hidden kernel pointers, "
                     "restricted dmesg and ptrace, link protection, no ICMP redirects, SYN cookies.")
    return Check("warn", f"{n - len(bad)}/{n} sysctl",
                 "Not applied:\n" + "\n".join(bad) + "\nFix: sudo sysctl --system")


def auto_updates():
    _, out, _ = run(["apt-config", "dump"])
    lists = re.search(r'^APT::Periodic::Update-Package-Lists "(\d+)"', out, re.M)
    upgrade = re.search(r'^APT::Periodic::Unattended-Upgrade "(\d+)"', out, re.M)
    on = lists and upgrade and lists.group(1) != "0" and upgrade.group(1) != "0"
    timer = _systemctl("is-enabled", "apt-daily-upgrade.timer") == "enabled"
    if on and timer:
        return Check("ok", "daily",
                     "unattended-upgrades installs security fixes every day.")
    return Check("fail", "off",
                 "Automatic security updates are off. Fix: sudo dpkg-reconfigure -plow unattended-upgrades")


def pending_updates():
    rc, out, err = run(["/usr/lib/update-notifier/apt-check"], timeout=60)
    m = re.match(r"(\d+);(\d+)", (err or out).strip())
    if not m:
        return Check("watch", "unknown", "Could not count pending updates.")
    total, security = int(m.group(1)), int(m.group(2))
    if security:
        return Check("warn", f"{security} security",
                     f"{security} security updates are waiting (unattended-upgrades normally installs them within "
                     "a day). Install now: sudo apt update && sudo apt full-upgrade -y")
    if total:
        return Check("watch", f"0 security · {total} other",
                     f"No security updates pending. {total} regular updates are ready; install them weekly:\n"
                     "sudo apt update && sudo apt full-upgrade -y")
    return Check("ok", "up to date", "The system is fully up to date.")


def ubuntu_pro():
    rc, out, _ = run(["pro", "status", "--format", "json"], timeout=30)
    try:
        data = json.loads(out)
    except ValueError:
        return Check("watch", "unknown", "Could not read Ubuntu Pro status.")
    svc = {s.get("name"): s.get("status") for s in data.get("services", [])}
    if data.get("attached") and svc.get("esm-infra") == "enabled" and svc.get("esm-apps") == "enabled":
        return Check("ok", "ESM apps + infra",
                     "Ubuntu Pro is attached. Expanded Security Maintenance covers main and universe packages.")
    return Check("warn", "not fully on",
                 f"esm-infra: {svc.get('esm-infra')}, esm-apps: {svc.get('esm-apps')}.\n"
                 "Fix: sudo pro enable esm-apps esm-infra")


def livepatch():
    state = _systemctl("is-active", "snap.canonical-livepatch.canonical-livepatchd.service")
    if state == "active":
        return Check("ok", "running",
                     "Critical kernel fixes are applied while the system runs, without a reboot.")
    if state == "not-found":
        return Check("watch", "not installed", "Fix: sudo pro enable livepatch")
    _, out, _ = run(["snap", "connections", "canonical-livepatch"])
    unplugged = re.search(r"^kernel-module-control\s+\S+\s+-\s", out, re.M)
    fix = ("The snap's kernel-module-control interface is not connected, so the daemon cannot load patches.\n"
           "Fix: sudo snap connect canonical-livepatch:kernel-module-control && "
           "sudo snap restart canonical-livepatch\n"
           "If it still fails: sudo pro disable livepatch && sudo pro enable livepatch") if unplugged else \
          "Fix: sudo snap restart canonical-livepatch, then check: sudo canonical-livepatch status"
    return Check("warn", f"daemon {state}",
                 f"The Livepatch daemon is {state}: kernel fixes still arrive through normal updates but need "
                 f"a reboot.\n{fix}")


# ------------------------------------------------------------------ the human side

def screen_lock():
    enabled = _gsetting("org.gnome.desktop.screensaver", "lock-enabled")
    idle = _gsetting("org.gnome.desktop.session", "idle-delay")
    if enabled is None or idle is None:
        return Check("watch", "unknown", "GNOME screen-lock settings not found.")
    if not enabled or idle == 0:
        return Check("fail", "never",
                     "The screen never locks by itself. Settings → Privacy → Screen Lock.")
    minutes = idle // 60
    if idle > 300:
        return Check("warn", f"after {minutes} min",
                     f"The screen locks after {minutes} minutes idle; the handbook target is 5.\n"
                     "Fix: gsettings set org.gnome.desktop.session idle-delay 300")
    return Check("ok", f"after {minutes} min",
                 f"The screen locks automatically after {minutes} minutes idle.")


def backups():
    ts = {}
    try:
        with open("/etc/timeshift/timeshift.json") as f:
            ts = json.load(f)
    except (OSError, ValueError):
        pass
    sched = [p for p in ("hourly", "daily", "weekly", "monthly", "boot") if ts.get(f"schedule_{p}") == "true"]
    timeshift = f"Timeshift: {', '.join(sched)} snapshots on the system SSD." if sched else "Timeshift: no schedule."
    last = _gsetting("org.gnome.DejaDup", "last-backup")
    age = None
    if last:
        try:
            when = datetime.datetime.fromisoformat(last.replace("Z", "+00:00"))
            age = (datetime.datetime.now(datetime.timezone.utc) - when).days
        except ValueError:
            pass
    if age is None:
        return Check("warn", "none yet",
                     f"{timeshift}\nDéjà Dup: no backup has been made. Timeshift lives on the same SSD (which has "
                     "logged integrity errors), so it does not protect your files if the drive fails.\n"
                     "Fix: plug in the external HDD → Backups → Back Up Now (encrypted).")
    if age > 30:
        return Check("warn", f"{age} days ago",
                     f"{timeshift}\nDéjà Dup: last backup {age} days ago. Connect the HDD and back up.")
    return Check("ok", f"{age} d ago" if age else "today",
                 f"{timeshift}\nDéjà Dup: last encrypted backup {age} days ago.")


SECURITY_CHECKS = (
    ("Secure Boot", secure_boot),
    ("Disk encryption", disk_encryption),
    ("Firewall (UFW)", firewall),
    ("Encrypted DNS", encrypted_dns),
    ("Open ports", open_ports),
    ("Discovery services", network_services),
    ("AppArmor", apparmor),
    ("Kernel hardening", kernel_hardening),
    ("Auto security updates", auto_updates),
    ("Pending updates", pending_updates),
    ("Ubuntu Pro (ESM)", ubuntu_pro),
    ("Livepatch", livepatch),
    ("Screen auto-lock", screen_lock),
    ("Off-drive backup", backups),
)


# ------------------------------------------------------------------ hardware

def drive_io_errors():
    rc, out, err = run(["journalctl", "-k", "-b", "--no-pager", "-q", "-o", "cat"], timeout=20)
    if rc != 0:
        return Check("watch", "unreadable",
                     "The kernel log could not be read (needs membership of the adm group).")
    hits = [l for l in out.splitlines()
            if re.search(r"nvme.*(error|fail|timeout)|I/O error|critical medium", l, re.I)]
    if hits:
        return Check("fail", f"{len(hits)} errors",
                     "Storage errors in this boot's kernel log:\n" + "\n".join(hits[-5:]))
    return Check("ok", "clean",
                 "No NVMe or I/O errors in the kernel log since this boot.")
