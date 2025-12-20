#!/bin/bash
# fabCNC Kiosk Mode Setup Script
# This script configures Raspberry Pi to boot directly to kiosk mode

# Get the actual user (not root if run with sudo)
ACTUAL_USER="${SUDO_USER:-$USER}"
USER_HOME=$(eval echo ~$ACTUAL_USER)

echo "=== fabCNC Kiosk Mode Setup ==="
echo "Configuring for user: $ACTUAL_USER"
echo "Home directory: $USER_HOME"
echo ""

# 1. Disable splash screen and boot messages
echo "Disabling splash screen and boot messages...
sudo sed -i 's/splash/splash=silent/' /boot/firmware/cmdline.txt 2>/dev/null || \
sudo sed -i 's/splash/splash=silent/' /boot/cmdline.txt
sudo sed -i 's/$/ plymouth.enable=0/' /boot/firmware/cmdline.txt 2>/dev/null || \
sudo sed -i 's/$/ plymouth.enable=0/' /boot/cmdline.txt

# 2. Install required packages if not present
echo "Checking required packages..."
sudo apt-get update
sudo apt-get install -y chromium xinit xserver-xorg x11-xserver-utils unclutter

# 3. Set to boot to console (not desktop)
echo "Setting boot target to console..."
sudo systemctl set-default multi-user.target

# 3b. Add user to required groups for X server
echo "Adding user to tty and video groups..."
sudo usermod -a -G tty $ACTUAL_USER
sudo usermod -a -G video $ACTUAL_USER

# 4. Enable auto-login to console
echo "Configuring auto-login..."
sudo mkdir -p /etc/systemd/system/getty@tty1.service.d
sudo tee /etc/systemd/system/getty@tty1.service.d/autologin.conf > /dev/null <<EOF
[Service]
ExecStart=
ExecStart=-/sbin/agetty --autologin $ACTUAL_USER --noclear %I \$TERM
EOF

# 5. Create kiosk startup script
echo "Creating kiosk startup script..."
cat > $USER_HOME/start-kiosk.sh <<'EOF'
#!/bin/bash

# Wait for network
while ! ping -c 1 -W 1 localhost &> /dev/null; do
    sleep 1
done

# Start X server with kiosk mode
startx $HOME/kiosk-chromium.sh -- vt1
EOF

chmod +x $USER_HOME/start-kiosk.sh
chown $ACTUAL_USER:$ACTUAL_USER $USER_HOME/start-kiosk.sh

# 6. Create Chromium kiosk script
echo "Creating Chromium kiosk script..."
cat > $USER_HOME/kiosk-chromium.sh <<'EOF'
#!/bin/bash

# Set to 1280x720 resolution
xrandr --output HDMI-1 --mode 1280x720 2>/dev/null || \
xrandr --output HDMI-1 --auto

# Disable screen blanking
xset s off
xset -dpms
xset s noblank

# Hide mouse cursor
unclutter -idle 0.1 &

# Wait for fabCNC server to be ready
while ! curl -s http://localhost:8080 > /dev/null; do
    sleep 1
done

# Get actual screen resolution
RESOLUTION=$(xrandr | grep '\*' | awk '{print $1}' | head -1)
WIDTH=$(echo $RESOLUTION | cut -d'x' -f1)
HEIGHT=$(echo $RESOLUTION | cut -d'x' -f2)

# Launch Chromium in kiosk mode with explicit window size
chromium --kiosk \
  --window-size=$WIDTH,$HEIGHT \
  --window-position=0,0 \
  --disable-restore-session-state \
  --noerrdialogs \
  --disable-infobars \
  --disable-features=TranslateUI \
  --disable-session-crashed-bubble \
  --disable-pinch \
  --overscroll-history-navigation=0 \
  --start-fullscreen \
  --force-device-scale-factor=1 \
  http://localhost:8080
EOF

chmod +x $USER_HOME/kiosk-chromium.sh
chown $ACTUAL_USER:$ACTUAL_USER $USER_HOME/kiosk-chromium.sh

# 7. Add kiosk startup to .bashrc
echo "Configuring automatic kiosk startup..."
if ! grep -q "start-kiosk.sh" $USER_HOME/.bashrc; then
    cat >> $USER_HOME/.bashrc <<'EOF'

# Auto-start kiosk mode on tty1
if [ -z "$DISPLAY" ] && [ "$(tty)" = "/dev/tty1" ]; then
    $HOME/start-kiosk.sh
fi
EOF
fi

# 8. Ensure fabCNC service is enabled
if [ -f /etc/systemd/system/fabcnc.service ]; then
    echo "Enabling fabCNC service..."
    sudo systemctl enable fabcnc.service
else
    echo "WARNING: fabcnc.service not found. Create it first!"
fi

echo ""
echo "=== Setup Complete! ==="
echo ""
echo "To complete the setup, you need to:"
echo "1. Ensure fabcnc.service is created and enabled"
echo "2. Reboot your Raspberry Pi: sudo reboot"
echo ""
echo "After reboot, your Pi will boot directly to kiosk mode."
echo ""
