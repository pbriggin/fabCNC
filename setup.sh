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
          --quiet

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

# ── Systemd auto-start (Linux only) ─────────────────────────────────────────
if [[ "$OSTYPE" == "linux-gnu"* ]] && command -v systemctl &>/dev/null; then
    echo ""
    echo "==> Configuring systemd auto-start..."

    REPO_DIR="$(cd "$(dirname "$0")" && pwd)"
    CURRENT_USER="$(whoami)"
    VENV_ABS="${REPO_DIR}/${VENV_DIR}"
    SERVICE_FILE="/etc/systemd/system/fabcnc.service"

    sudo tee "$SERVICE_FILE" > /dev/null <<EOF
[Unit]
Description=fabCNC Web Controller
After=network.target

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
