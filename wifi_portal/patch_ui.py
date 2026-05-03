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
<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 220 40">
  <circle cx="20" cy="20" r="20" fill="#5b9bd5"/>
  <path fill="#fff" fill-rule="evenodd"
    d="M20 6a14 14 0 1 1 0 28A14 14 0 0 1 20 6zm0 9a5 5 0 1 0 0 10 5 5 0 0 0 0-10z"/>
  <text x="48" y="27" font-family="-apple-system,BlinkMacSystemFont,Segoe UI,sans-serif"
    font-size="18" font-weight="600" fill="#ffffff">fabCNC Setup</text>
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

# 2. Replace "balena" brand text in JS bundles
#    Only replace the display string, not technical identifiers like
#    "balena-os", "BalenaCloud", module names, URLs etc.
js_files = list(ui_dir.glob('static/js/*.js'))
for js_path in js_files:
    original = js_path.read_text(encoding='utf-8', errors='replace')
    # Match quoted "balena" or 'balena' standing alone as a brand label,
    # but not balena- prefixed identifiers or balena inside longer words.
    patched = re.sub(r'(?<![a-zA-Z])["\']balena["\'](?![a-zA-Z-])',
                     lambda m: m.group(0)[0] + 'fabCNC' + m.group(0)[0],
                     original)
    if patched != original:
        js_path.write_text(patched, encoding='utf-8')
        print(f'Patched JS: {js_path.name}')

# 3. Patch <title> and inject overrides in index.html
html = index_path.read_text()
html = re.sub(r'<title>[^<]*</title>', '<title>fabCNC \u2014 WiFi Setup</title>', html)

# CSS: style submit button + darken navbar
css = (
    '<style>'
    'button[type="submit"]{background-color:#5b9bd5!important;border-color:#5b9bd5!important;}'
    'button[type="submit"]:hover{background-color:#4a8ec5!important;border-color:#4a8ec5!important;}'
    'nav,[class*="Navbar"],[class*="navbar"]{background-color:#2a2a2a!important;}'
    '</style>'
)

html = html.replace('</head>', css + '</head>', 1)
# Remove any previously injected DOMContentLoaded scripts
import re as _re
html = _re.sub(r'<script>document\.addEventListener\("DOMContentLoaded".*?</script>', '', html, flags=_re.DOTALL)
index_path.write_text(html)
print(f'Patched title + styles in {index_path}')

