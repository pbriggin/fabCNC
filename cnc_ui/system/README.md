# System Configuration Files

This directory contains configuration files for system integration.

## Summary of Changes Made for Kiosk Mode

### Files Created
- `~/start-kiosk.sh` - Script that starts X server with kiosk mode
- `~/kiosk-chromium.sh` - Script that configures and launches Chromium
- `/etc/systemd/system/fabcnc.service` - Systemd service for fabCNC web server
- `/etc/systemd/system/getty@tty1.service.d/autologin.conf` - Auto-login configuration

### Files Modified
- `~/.bashrc` - Added auto-start kiosk on tty1 login
- `/boot/firmware/cmdline.txt` - Disabled splash screen, added boot params
- `/boot/firmware/config.txt` - Set HDMI resolution and overscan settings

### System Settings Changed
- **Boot target**: Changed from `graphical.target` to `multi-user.target` (console mode)
- **User groups**: Added user to `tty` and `video` groups
- **Systemd service**: Enabled `fabcnc.service` to start on boot
- **Auto-login**: Configured getty to auto-login user on tty1

### Old Files Disabled
- `~/.config/autostart/kiosk.desktop.disabled` - Old desktop-based kiosk (was: kiosk.desktop)
- `~/.config/autostart/disable-screensaver.desktop.disabled` - Old screensaver config

### Backup Files Created
- `/boot/firmware/cmdline.txt.backup` - Original cmdline.txt
- `/boot/firmware/config.txt.backup2` - Original config.txt

## How to Completely Undo Kiosk Mode

### Quick Revert to Desktop Mode
```bash
# 1. Switch back to desktop boot
sudo systemctl set-default graphical.target

# 2. Disable fabCNC auto-start
sudo systemctl disable fabcnc.service
sudo systemctl stop fabcnc.service

# 3. Remove kiosk auto-start from .bashrc
sed -i '/Auto-start kiosk mode/,/fi/d' ~/.bashrc

# 4. Reboot
sudo reboot
```

### Full Cleanup (Remove All Kiosk Files)
```bash
# Remove kiosk scripts
rm ~/start-kiosk.sh
rm ~/kiosk-chromium.sh

# Remove systemd service
sudo systemctl disable fabcnc.service
sudo rm /etc/systemd/system/fabcnc.service
sudo systemctl daemon-reload

# Remove auto-login
sudo rm -rf /etc/systemd/system/getty@tty1.service.d

# Restore boot files (if backups exist)
sudo cp /boot/firmware/cmdline.txt.backup /boot/firmware/cmdline.txt
sudo cp /boot/firmware/config.txt.backup2 /boot/firmware/config.txt

# Re-enable old desktop autostart (if you want it)
mv ~/.config/autostart/kiosk.desktop.disabled ~/.config/autostart/kiosk.desktop

# Switch back to desktop
sudo systemctl set-default graphical.target

# Reboot
sudo reboot
```

### Partial Undo - Keep Server, Remove Kiosk
```bash
# Keep fabCNC running but disable kiosk mode
sed -i '/Auto-start kiosk mode/,/fi/d' ~/.bashrc
sudo systemctl set-default graphical.target
sudo reboot
```

### Manual Config File Changes to Undo

**`/boot/firmware/cmdline.txt`** - Remove/change:
- `splash=silent plymouth.enable=0`
- Restore original `splash` behavior

**`/boot/firmware/config.txt`** - Remove added lines:
```ini
# Remove these lines:
overscan_left=0
overscan_right=0
overscan_top=0
overscan_bottom=0
hdmi_ignore_edid=0xa5000080
hdmi_group=2
hdmi_mode=85
```

**`~/.bashrc`** - Remove added section:
```bash
# Remove this block:
# Auto-start kiosk mode on tty1
if [ -z "$DISPLAY" ] && [ "$(tty)" = "/dev/tty1" ]; then
    $HOME/start-kiosk.sh
fi
```

## Direct Boot to Kiosk Mode (NO DESKTOP)

### Quick Setup

Run the automated setup script on your Raspberry Pi:

```bash
cd /home/pi/fabCNC/cnc_ui/system
chmod +x kiosk-setup.sh
./kiosk-setup.sh
```

Then install the systemd service:
```bash
sudo cp fabcnc.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable fabcnc.service
sudo systemctl start fabcnc.service
```

Finally, reboot:
```bash
sudo reboot
```

### What This Does

The setup script configures your Pi to:
1. **Disable boot splash screen** - No RPi logo or boot messages
2. **Boot to console** (not desktop) - Saves resources
3. **Auto-login** to console as `pi` user
4. **Auto-start X server** with Chromium in kiosk mode
5. **Hide mouse cursor** automatically
6. **Disable screen blanking**

### Manual Setup (Alternative)

If you prefer to configure manually:

#### 1. Disable Splash Screen
Edit `/boot/firmware/cmdline.txt` (or `/boot/cmdline.txt` on older systems):
```bash
sudo nano /boot/firmware/cmdline.txt
```
Add to the end of the line: `logo.nologo consoleblank=0 loglevel=1 quiet`

#### 2. Set Boot to Console
```bash
sudo systemctl set-default multi-user.target
```

#### 3. Auto-login to Console
Create `/etc/systemd/system/getty@tty1.service.d/autologin.conf`:
```ini
[Service]
ExecStart=
ExecStart=-/sbin/agetty --autologin pi --noclear %I $TERM
```

#### 4. Install Required Packages
```bash
sudo apt-get install -y chromium-browser xinit xserver-xorg x11-xserver-utils unclutter
```

#### 5. Systemd Service (fabcnc.service)

Copy `fabcnc.service` to `/etc/systemd/system/`:
```bash
sudo cp fabcnc.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable fabcnc.service
sudo systemctl start fabcnc.service
```

#### 6. Create Kiosk Scripts

The setup script creates two files in `/home/pi/`:
- `start-kiosk.sh` - Starts X server
- `kiosk-chromium.sh` - Configures and launches Chromium

And modifies `.bashrc` to auto-start kiosk on tty1.

## Debugging / Stopping Kiosk Mode

### Access the Pi While in Kiosk Mode

**Option 1: Switch to another TTY**
- Press **Ctrl+Alt+F2** (or F3, F4, F5, F6) to switch to another terminal
- Login normally and run commands

**Option 2: SSH from another computer**
```bash
ssh fab@<your-pi-ip>
```

### Stop Kiosk Mode

**Kill the kiosk processes:**
```bash
sudo pkill -f xinit
sudo pkill -f chromium
```

**Stop the fabCNC service:**
```bash
sudo systemctl stop fabcnc.service
```

### Disable Kiosk Mode

**Temporarily (until next reboot):**
```bash
# Comment out auto-start in .bashrc
sed -i '/start-kiosk.sh/s/^/#/' ~/.bashrc
```

**Permanently - Revert to Desktop:**
```bash
sudo systemctl set-default graphical.target
sudo systemctl disable fabcnc.service
sudo reboot
```

**Re-enable old desktop autostart files (if needed):**
```bash
mv ~/.config/autostart/kiosk.desktop.disabled ~/.config/autostart/kiosk.desktop
```

## Old Desktop-Based Kiosk Mode (NOT RECOMMENDED)

This requires the full desktop environment and shows boot graphics:

Edit `/etc/xdg/lxsession/LXDE-pi/autostart`:
```
@chromium-browser --kiosk --disable-restore-session-state --noerrdialogs --disable-infobars --disable-features=TranslateUI http://localhost:8080
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
