# System Configuration Files

This directory contains configuration files for system integration.

## Auto-Start Configuration

### Systemd Service (fabcnc.service)

Create `/etc/systemd/system/fabcnc.service`:

```ini
[Unit]
Description=fabCNC Web Controller
After=network.target

[Service]
Type=simple
User=pi
WorkingDirectory=/home/pi/fabCNC/cnc_ui
ExecStart=/usr/bin/python3 /home/pi/fabCNC/cnc_ui/main.py
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
```

Enable and start:
```bash
sudo systemctl enable fabcnc.service
sudo systemctl start fabcnc.service
```

### Chromium Kiosk Mode (Full Screen)

Edit `/etc/xdg/lxsession/LXDE-pi/autostart` or create `~/.config/lxsession/LXDE-pi/autostart`:

```
@lxpanel --profile LXDE-pi
@pcmanfm --desktop --profile LXDE-pi
@xscreensaver -no-splash
@chromium-browser --kiosk --disable-restore-session-state --noerrdialogs --disable-infobars --disable-features=TranslateUI --disable-session-crashed-bubble --window-position=0,0 http://localhost:8080
```

Or create a desktop entry at `~/.config/autostart/fabcnc.desktop`:

```ini
[Desktop Entry]
Type=Application
Name=fabCNC Kiosk
Exec=chromium-browser --kiosk --disable-restore-session-state --noerrdialogs --disable-infobars --disable-features=TranslateUI --disable-session-crashed-bubble --window-position=0,0 http://localhost:8080
X-GNOME-Autostart-enabled=true
```

**Chromium Kiosk Flags Explained:**
- `--kiosk`: Full-screen mode with no browser UI
- `--disable-restore-session-state`: Don't show "Restore pages?" dialog
- `--noerrdialogs`: Suppress error dialogs
- `--disable-infobars`: Remove info bars at top
- `--disable-features=TranslateUI`: No translation popups
- `--disable-session-crashed-bubble`: No crash notification
- `--window-position=0,0`: Ensure window starts at top-left corner

**For specific resolution (e.g., 1280x720):**
```bash
chromium-browser --kiosk --window-size=1280,720 --window-position=0,0 --disable-restore-session-state --noerrdialogs --disable-infobars http://localhost:8080
```

**To disable screen blanking and screensaver:**
```bash
# Add to /etc/xdg/lxsession/LXDE-pi/autostart
@xset s off
@xset -dpms
@xset s noblank
```

## Network Configuration

To find your Raspberry Pi's IP address:
```bash
hostname -I
```

Access from other devices at `http://<pi-ip>:8080`
