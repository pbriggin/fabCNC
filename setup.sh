#!/usr/bin/env bash
# setup.sh — fabCNC project setup
# Creates a virtual environment, installs all dependencies (including packaide),
# verifies the install, and optionally configures systemd auto-start (Linux only).

set -e

VENV_DIR=".venv"
PYTHON_MIN="3.10"
PACKAIDE_REPO="https://github.com/DanielLiamAnderson/Packaide.git"

# ── Python check ────────────────────────────────────────────────────────────
echo "==> Checking Python version..."
PYTHON_BIN=$(command -v python3 || true)
if [ -z "$PYTHON_BIN" ]; then
    echo "ERROR: python3 not found. Install Python ${PYTHON_MIN}+ and try again."
    exit 1
fi

PYTHON_VER=$("$PYTHON_BIN" -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')")
PYTHON_OK=$("$PYTHON_BIN" -c "import sys; print('yes' if sys.version_info >= (3, 10) else 'no')")
if [ "$PYTHON_OK" != "yes" ]; then
    echo "ERROR: Python ${PYTHON_MIN}+ required (found ${PYTHON_VER})."
    exit 1
fi
echo "    Found Python ${PYTHON_VER}"

# ── Virtual environment ──────────────────────────────────────────────────────
echo ""
echo "==> Creating virtual environment at ${VENV_DIR}..."
"$PYTHON_BIN" -m venv "$VENV_DIR"

PIP="${VENV_DIR}/bin/pip"
VENV_PYTHON="${VENV_DIR}/bin/python"
VENV_PREFIX=$("$VENV_PYTHON" -c "import sys; print(sys.prefix)")

# ── Python dependencies ──────────────────────────────────────────────────────
echo ""
echo "==> Installing Python dependencies..."
"$PIP" install --upgrade pip --quiet
"$PIP" install -r requirements.txt

# ── Packaide (build from source) ─────────────────────────────────────────────
echo ""
echo "==> Installing packaide (shape nesting — builds from source)..."

SKIP_PACKAIDE=0

# Install system build dependencies
if [[ "$OSTYPE" == "darwin"* ]]; then
    if ! command -v brew &>/dev/null; then
        echo "    WARNING: Homebrew not found. Cannot auto-install packaide build deps."
        echo "             Install Homebrew, then run: brew install cmake boost cgal"
        SKIP_PACKAIDE=1
    else
        echo "    Installing build deps via Homebrew (cmake, boost, cgal)..."
        brew install cmake boost cgal 2>/dev/null || true
    fi
elif [[ "$OSTYPE" == "linux-gnu"* ]]; then
    echo "    Installing build deps via apt (cmake, libboost-all-dev, libcgal-dev)..."
    sudo apt-get install -y cmake libboost-all-dev libcgal-dev 2>/dev/null || {
        echo "    WARNING: apt install failed. Cannot auto-install packaide build deps."
        SKIP_PACKAIDE=1
    }
else
    echo "    WARNING: Unsupported OS '${OSTYPE}'. Skipping packaide."
    SKIP_PACKAIDE=1
fi

if [ "$SKIP_PACKAIDE" -eq 0 ]; then
    PACKAIDE_TMP=$(mktemp -d)
    trap 'rm -rf "$PACKAIDE_TMP"' EXIT

    echo "    Cloning packaide..."
    git clone --depth=1 "$PACKAIDE_REPO" "$PACKAIDE_TMP/Packaide" --quiet

    echo "    Building packaide..."
    mkdir -p "$PACKAIDE_TMP/Packaide/build"
    cmake -S "$PACKAIDE_TMP/Packaide" \
          -B "$PACKAIDE_TMP/Packaide/build" \
          -DCMAKE_BUILD_TYPE=Release \
          -DCMAKE_INSTALL_PREFIX="$VENV_PREFIX" \
          -DPython3_EXECUTABLE="$VENV_PYTHON" \
          -DPYTHON_EXECUTABLE="$VENV_PYTHON" \
          > /dev/null 2>&1

    CPU_COUNT=$(nproc 2>/dev/null || sysctl -n hw.ncpu 2>/dev/null || echo 2)
    cmake --build "$PACKAIDE_TMP/Packaide/build" --parallel "$CPU_COUNT"

    # Copy the built Python extension directly into venv site-packages
    SITE_PACKAGES=$("$VENV_PYTHON" -c "import site; print(site.getsitepackages()[0])")
    SO_FILE=$(find "$PACKAIDE_TMP/Packaide/build" -name "packaide*.so" | head -n 1)
    if [ -n "$SO_FILE" ]; then
        cp "$SO_FILE" "$SITE_PACKAGES/"
        echo "    packaide installed successfully."
    else
        echo "    WARNING: packaide .so not found after build. Check the build output above."
    fi
fi

# ── Verify ───────────────────────────────────────────────────────────────────
echo ""
echo "==> Verifying install..."
"$VENV_PYTHON" -c "import nicegui, ezdxf, numpy, matplotlib, serial" \
    && echo "    Core dependencies: OK"

"$VENV_PYTHON" -c "import packaide" 2>/dev/null \
    && echo "    packaide: OK" \
    || echo "    packaide: not available (nesting feature will be disabled)"

# ── wifi-connect (Linux only) ────────────────────────────────────────────────
if [[ "$OSTYPE" == "linux-gnu"* ]] && command -v systemctl &>/dev/null; then
    echo ""
    echo "==> Installing wifi-connect (WiFi provisioning)..."

    ARCH=$(uname -m)
    case "$ARCH" in
        aarch64) WC_ARCH="aarch64-unknown-linux-gnu" ;;
        armv7l)  WC_ARCH="armv7-unknown-linux-gnueabihf" ;;
        x86_64)  WC_ARCH="x86_64-unknown-linux-gnu" ;;
        *)       WC_ARCH="" ;;
    esac

    if [ -z "$WC_ARCH" ]; then
        echo "    WARNING: Unsupported arch '$ARCH', skipping wifi-connect."
    else
        WC_VERSION="4.11.84"
        WC_BASE="https://github.com/balena-os/wifi-connect/releases/download/v${WC_VERSION}"
        WC_TMP=$(mktemp -d)

        echo "    Downloading wifi-connect ${WC_VERSION} (${WC_ARCH})..."
        curl -sL "${WC_BASE}/wifi-connect-${WC_ARCH}.tar.gz" -o "$WC_TMP/wifi-connect.tar.gz"
        curl -sL "${WC_BASE}/wifi-connect-ui.tar.gz"          -o "$WC_TMP/wifi-connect-ui.tar.gz"

        tar -xzf "$WC_TMP/wifi-connect.tar.gz" -C "$WC_TMP"
        sudo mv "$WC_TMP/wifi-connect" /usr/local/bin/wifi-connect
        sudo chmod +x /usr/local/bin/wifi-connect

        sudo mkdir -p /usr/local/share/wifi-connect/ui
        tar -xzf "$WC_TMP/wifi-connect-ui.tar.gz" -C "$WC_TMP"
        sudo cp -r "$WC_TMP/build/." /usr/local/share/wifi-connect/ui/

        rm -rf "$WC_TMP"

        # wifi-connect requires NetworkManager
        if ! systemctl is-active --quiet NetworkManager 2>/dev/null; then
            echo "    Enabling NetworkManager (required by wifi-connect)..."
            sudo apt-get install -y network-manager --quiet
            # Disable dhcpcd if present (conflicts with NetworkManager)
            sudo systemctl disable dhcpcd 2>/dev/null || true
            sudo systemctl stop dhcpcd 2>/dev/null || true
            sudo systemctl enable NetworkManager
            sudo systemctl start NetworkManager
        fi

        # Install wifi-provision service (runs before fabcnc, opens AP if offline)
        sudo tee /etc/systemd/system/wifi-provision.service > /dev/null <<'WCSVC'
[Unit]
Description=WiFi Provisioning (wifi-connect)
Before=fabcnc.service
After=NetworkManager.service
Wants=NetworkManager.service

[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=/bin/bash -c '\
    for i in $(seq 1 15); do \
        if nmcli -t -f STATE g 2>/dev/null | grep -q "^connected"; then \
            echo "Network connected, skipping provisioning."; \
            exit 0; \
        fi; \
        sleep 1; \
    done; \
    echo "No network found, starting WiFi provisioning AP..."; \
    UI_PATH=/usr/local/share/wifi-connect/ui \
    wifi-connect --portal-ssid "fabCNC Setup"'
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
WCSVC

        sudo systemctl daemon-reload
        sudo systemctl enable wifi-provision
        echo "    wifi-provision service enabled."
    fi
fi

# ── Systemd auto-start (Linux only) ─────────────────────────────────────────
if [[ "$OSTYPE" == "linux-gnu"* ]] && command -v systemctl &>/dev/null; then
    echo ""
    echo "==> Configuring systemd auto-start..."

    REPO_DIR="$(cd "$(dirname "$0")" && pwd)"
    CURRENT_USER="$(whoami)"
    VENV_ABS="${REPO_DIR}/${VENV_DIR}"

    sudo tee /etc/systemd/system/fabcnc.service > /dev/null <<EOF
[Unit]
Description=fabCNC Web Controller
After=network.target wifi-provision.service
Wants=wifi-provision.service

[Service]
Type=simple
User=${CURRENT_USER}
WorkingDirectory=${REPO_DIR}/cnc_ui
ExecStart=${VENV_ABS}/bin/python3 ${REPO_DIR}/cnc_ui/main.py
Restart=always
RestartSec=10
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
EOF

    sudo systemctl daemon-reload
    sudo systemctl enable fabcnc
    echo "    fabcnc service enabled — will start on next boot."
    echo "    To start now: sudo systemctl start fabcnc"
fi

# ── Done ─────────────────────────────────────────────────────────────────────
echo ""
echo "---------------------------------------------------------------------"
echo "Setup complete."
echo ""
echo "To activate the environment:"
echo "    source ${VENV_DIR}/bin/activate"
echo ""
echo "To run the app:"
echo "    cd cnc_ui && python main.py"
echo "---------------------------------------------------------------------"
