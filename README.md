# FortiClient VPN Tray

A lightweight **system tray indicator** for Linux that monitors and manages **FortiClient SSL VPN** connections — built for users whose FortiClient GUI doesn't work (a surprisingly common issue on Linux).

![Python](https://img.shields.io/badge/python-3.8%2B-blue)
![License](https://img.shields.io/badge/license-MIT-green)
![Platform](https://img.shields.io/badge/platform-Linux-lightgrey)

---

## Why this exists

FortiClient's built-in GUI/tray often fails on modern Linux desktops (Zorin, Ubuntu, Mint, etc.) while the underlying `forticlient` CLI works fine. This tool gives you:

- A native **AppIndicator tray icon** showing VPN status at a glance
- A **GTK connect dialog** — no terminal needed
- Full support for **2FA tokens** (SMS / e-mail / TOTP) that appear only when the server actually asks
- Per-profile **password caching** (stored locally with `chmod 600`)
- **Zero CPU overhead** — status checked via `/sys/class/net` (no polling of CLI tools)

---

## Features

| Feature | Details |
|---|---|
| 🟢 / ⚪ tray icon | Green = connected, grey = disconnected |
| 🔌 Connect submenu | Lists all profiles from `forticlient vpn list` |
| 🔑 Password dialog | Pre-filled from local cache, masked input |
| 🔐 2FA token dialog | Appears **only if** the server prompts for a token |
| 💾 Password cache | `~/.config/vpn_tray_credentials.json` (owner-readable only) |
| ⛔ Disconnect | One-click `forticlient vpn disconnect` |
| 🔔 Notifications | `notify-send` on connect / disconnect events |
| 🔄 Refresh profiles | Reload profile list without restarting |

---

## How it works

The key insight that makes this work reliably: `forticlient vpn connect` checks whether its stdin is a **real TTY**. When called from a plain subprocess pipe it spawns a second daemon instance (causing "Load VPN profile was failed"). This tool uses Python's `pty` module to give FortiClient a **pseudo-terminal**, making it behave exactly as if run interactively in a terminal — while the credentials flow through GTK dialogs.

```
User clicks profile
       │
       ▼
 ┌─ Password dialog ─┐       pre-filled from cache if available
 │  ●●●●●●●●●●●●     │
 └───────────────────┘
       │ OK
       ▼
 pty.openpty() ──► forticlient vpn connect <profile>
       │
       │  reads "Password:" prompt → sends password
       │
       ├── no token prompt → done
       │
       └── "Token:" / "Code:" / "Two-factor" detected
              │
              ▼
        ┌─ Token dialog ──────────────────┐
        │  [hint from forticlient output]  │
        │  Token: ________________________ │
        └─────────────────────────────────┘
              │ OK → sends token via pty
              ▼
         result notification or error dialog
```

---

## Requirements

- Linux with a system tray supporting **AppIndicator3** (GNOME, KDE, XFCE, Zorin, etc.)
- **FortiClient** installed and the `forticlient` CLI in `$PATH`
- Python 3.8+
- Python GObject bindings:

```bash
sudo apt install python3-gi python3-gi-cairo gir1.2-gtk-3.0 gir1.2-appindicator3-0.1 libnotify-bin
```

---

## Installation

```bash
# Clone the repository
git clone https://github.com/<your-username>/forticlient-vpn-tray.git
cd forticlient-vpn-tray

# Make executable
chmod +x vpn_tray.py

# Run
./vpn_tray.py
```

### Autostart on login

```bash
mkdir -p ~/.config/autostart
cp forticlient-vpn-tray.desktop ~/.config/autostart/
```

Edit the `.desktop` file and set the correct path if you moved the script.

---

## Files

| File | Description |
|---|---|
| `vpn_tray.py` | Main script |
| `forticlient-vpn-tray.desktop` | Autostart / launcher desktop entry |

---

## Password cache

Passwords are stored in `~/.config/vpn_tray_credentials.json` with `chmod 600` permissions (only your user can read it). The file is written atomically. You can delete it at any time — you'll just be prompted for the password again on the next connection.

**The script never passes `--save-password` (`-s`) to `forticlient`**, so credentials are never written into FortiClient's own `config.db`.

---

## Supported token types

The 2FA dialog appears automatically when FortiClient's output contains any of these strings (case-insensitive):

`token:` · `totp:` · `otp:` · `code:` · `two-factor` · `authentication code`

The original prompt text from FortiClient is shown as a hint so you know which method to use (SMS, e-mail, TOTP app, etc.).

---

## License

MIT — see [LICENSE](LICENSE).
