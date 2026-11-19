# Sentinel: install on another Ubuntu laptop

Sentinel is a security and hardware-health widget for the GNOME desktop. It needs no root to run and does not start
by itself. You open it from its desktop icon.

**Requirements:** Ubuntu 24.04 LTS or newer with the normal GNOME desktop. Older releases (22.04 and earlier)
have a GTK version that is too old.

---

## 1. Get the code

```bash
git clone https://github.com/hossainmdtanvir/sentinel.git ~/Projects/sentinel
```

**No internet on the target machine?** Copy the folder over on a USB stick instead. On a machine that has it:

```bash
cd ~/Projects
tar czf ~/sentinel.tar.gz --exclude='__pycache__' --exclude='.git' sentinel
```

Then, on the new laptop with the stick plugged in:

```bash
mkdir -p ~/Projects
tar xzf /media/$USER/*/sentinel.tar.gz -C ~/Projects
```

Any location works. The rest of this guide assumes `~/Projects/sentinel`.

## 2. Install the requirements (once)

```bash
sudo apt update
sudo apt install -y python3-gi gir1.2-gtk-4.0 gir1.2-adw-1 python3-psutil
```

Everything else Sentinel reads from (udisks, ufw, systemd, apt, mokutil) is already part of Ubuntu Desktop.

## 3. Add the desktop icon and app-grid entry

```bash
cd ~/Projects/sentinel
./install.sh
```

The script checks the requirements first and tells you if anything is missing. It then creates:

- an entry in the app grid (search for **Sentinel**)
- an icon on the desktop

If double-clicking the desktop icon does nothing, right-click it and choose **Allow Launching** once.
To pin it to the dock: open Sentinel, right-click its dock icon, then **Pin to Dash**.

## 4. First launch

Double-click **Sentinel** on the desktop. The first full scan takes a few seconds:

- **Live readings** (CPU, RAM, GPU, clocks in Hz, temperatures, fans, battery) refresh every 2 seconds.
- **The full security and SSD scan** runs when the app opens, again every 10 minutes, and on **Ctrl+R**.
- **Hover** any row to see what was checked. **Click** a flagged row to copy its fix command, then paste it into
  a terminal with Ctrl+Shift+V.
- **Close** with Esc, Ctrl+W or the ✕ button. A second launch brings back the open window instead of starting
  another copy.

To see error messages, start it from a terminal instead:

```bash
cd ~/Projects/sentinel && python3 -m sentinel
```

---

## What to expect on a different laptop

### Hardware adapts automatically

| Part | What Sentinel shows |
|---|---|
| CPU | Load, clocks and temperature on Intel and AMD processors |
| NVIDIA GPU | Load, clocks, VRAM, temperature and power. Needs the NVIDIA driver (`sudo ubuntu-drivers install`). When the GPU is asleep it shows SLEEP and is not woken, except for one reading when the app opens. |
| Intel integrated GPU | Live clock speed |
| AMD GPU | No live readings: the Graphics card shows "no sensor" |
| Fans | Speeds from the laptop's own fan driver (ASUS, ThinkPad, Dell and others), up to two fans |
| NVMe SSDs | Wear, spare, power-on hours, data written, integrity errors, temperature, encryption |
| SATA SSDs | Listed with their usage, but shown as "SMART unavailable" |
| Wi-Fi | Temperature for Intel and MediaTek cards |
| Battery | Charge, health and charge limit. A desktop PC shows "AC ONLY". |

Tiles for hardware the laptop doesn't have are hidden.

**SSD error trend.** On a new laptop, the first reading of each drive becomes its baseline. If a drive's
integrity-error count later rises above that baseline, its row turns red. The log is kept in
`~/.local/state/sentinel/history.json`.

### Security checks follow the handbooks

The 14 checks verify a hardened Ubuntu setup (the section references below, such as *Vol. 1 E1*, point to the
*Secure Linux Setup Handbook* the checks were written from; it's a personal document and isn't part of this repo). A laptop
that hasn't been set up that way will show items to review. That's expected: the widget reports what it
actually finds. Fix each one, press **Ctrl+R**, and the banner turns green ("ALL SYSTEMS SECURE") once
everything passes.

| Check | Passes when | How to fix |
|---|---|---|
| Secure Boot | Enabled in the firmware | BIOS setting (Vol. 1 B2, Vol. 2 I4) |
| Disk encryption | The system drive is LUKS-encrypted | Only possible at install time: "Use LVM and encryption" (Vol. 1 C4) |
| Firewall (UFW) | Active and denying incoming connections | `sudo ufw default deny incoming && sudo ufw enable` (Vol. 1 E1) |
| Encrypted DNS | DNS-over-TLS on every connection | Vol. 1 E6 |
| Open ports | No TCP port open to the network | `sudo ss -tulpn` to find the program (Vol. 1 E3) |
| Discovery services | cups-browsed off, avahi masked | `sudo systemctl disable --now cups-browsed`, then `sudo systemctl mask --now avahi-daemon.service avahi-daemon.socket` (Vol. 2 I2) |
| AppArmor | Loaded and active | On by default in Ubuntu (Vol. 1 E4) |
| Kernel hardening | All 12 sysctl values applied | Create `/etc/sysctl.d/99-hardening.conf` (Vol. 1 E5) |
| Auto security updates | unattended-upgrades runs daily | `sudo dpkg-reconfigure -plow unattended-upgrades` (Vol. 1 E2) |
| Pending updates | No security updates waiting | `sudo apt update && sudo apt full-upgrade -y` |
| Ubuntu Pro (ESM) | Attached, with esm-apps and esm-infra on | `sudo pro attach <token>`: free for up to 5 machines at ubuntu.com/pro (Vol. 2 J) |
| Livepatch | The Livepatch service is running (if Livepatch isn't installed, this is only a note) | `sudo pro enable livepatch` |
| Screen auto-lock | Locks after 5 minutes or less | `gsettings set org.gnome.desktop.session idle-delay 300` (Vol. 2 I5) |
| Off-drive backup | A Déjà Dup backup in the last 30 days | Backups app → Back Up Now to an external drive (Vol. 2 L3). Install with `sudo apt install deja-dup` if missing. |

The BIOS password and rootkit or Lynis scans need root, so Sentinel doesn't check them.

---

## Updating, moving, uninstalling

- **New version:** `git pull` in the folder (or copy the new files over it). Nothing else is needed.
- **Moved the folder:** run `./install.sh` again from its new location. The icons point to the folder's path.
- **Uninstall:** run `./install.sh --uninstall`, then delete the folder. To also delete the SSD log:
  `rm -rf ~/.local/state/sentinel`

## Troubleshooting

| Problem | Fix |
|---|---|
| `install.sh` says requirements are missing | Run the `apt install` command from step 2 |
| `Namespace Adw not available` or `No module named 'psutil'` | Same: step 2 |
| `AttributeError` mentioning `PathBuilder` or `get_scale` | The Ubuntu release is too old; 24.04 or newer is required |
| Desktop icon does nothing | Right-click it → Allow Launching, or start it from a terminal to see the error |
| "Drive I/O log: unreadable" | Your account isn't in the `adm` group: `sudo usermod -aG adm $USER`, then log out and back in |
| NVIDIA tiles are missing | No NVIDIA driver, so `nvidia-smi` isn't available: `sudo ubuntu-drivers install` |
| Window is taller than the screen | Resize it; the content scrolls |

## What's in this folder

```
sentinel/
├── README.md         overview
├── INSTALL.md        this guide
├── install.sh        creates (or removes) the desktop icon and app-grid entry
├── assets/
│   └── sentinel.svg  app icon
├── docs/
│   └── screenshot.png
└── sentinel/         the app (Python + GTK 4)
    ├── app.py        window and layout
    ├── checks.py     the 14 security checks
    ├── hardware.py   live CPU / RAM / GPU / fan / temperature / battery readings
    ├── storage.py    SSD layout, encryption and SMART health
    ├── health.py     hardware health verdicts
    ├── history.py    SSD error-trend log
    ├── widgets.py    gauge and bar drawing
    └── style.css     the look
```
