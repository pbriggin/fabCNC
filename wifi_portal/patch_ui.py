#!/usr/bin/env python3
"""Patch the wifi-connect React UI with fabCNC branding.

Replaces the balena logo SVG file in static/media/ directly — no script
injection required, works reliably in all browsers including macOS CNA.

Usage: python3 patch_ui.py /path/to/ui/index.html
"""
import sys
import re
from pathlib import Path

FABCNC_SVG = """\
<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100">
  <path fill="#5b9bd5" fill-rule="evenodd"
    d="M50 0a50 50 0 1 1 0 100A50 50 0 0 1 50 0zm0 35a15 15 0 1 0 0 30 15 15 0 0 0 0-30z"/>
</svg>
"""

if len(sys.argv) < 2:
    print(f'Usage: {sys.argv[0]} <index.html path>')
    sys.exit(1)

index_path = Path(sys.argv[1])
ui_dir = index_path.parent

# 1. Replace logo SVG in static/media/
logo_files = list(ui_dir.glob('static/media/logo*.svg'))
if logo_files:
    for f in logo_files:
        f.write_text(FABCNC_SVG)
        print(f'Replaced logo: {f}')
else:
    print('WARNING: no static/media/logo*.svg found — logo not replaced')

# 2. Patch <title> in index.html
html = index_path.read_text()
html = re.sub(r'<title>[^<]*</title>', '<title>fabCNC \u2014 WiFi Setup</title>', html)
index_path.write_text(html)
print(f'Patched title in {index_path}')

