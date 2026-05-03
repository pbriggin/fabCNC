#!/usr/bin/env python3
"""Inject fabCNC branding into the wifi-connect React UI index.html.

Usage: python3 patch_ui.py /path/to/index.html
"""
import sys
import urllib.parse

LOGO_SVG = (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100">'
    '<path fill="#5b9bd5" fill-rule="evenodd" '
    'd="M50 0a50 50 0 1 1 0 100A50 50 0 0 1 50 0zm0 35a15 15 0 1 0 0 30 15 15 0 0 0 0-30z"/>'
    '</svg>'
)
LOGO_DATA = 'data:image/svg+xml,' + urllib.parse.quote(LOGO_SVG)

# Injected before </body>: swap logo src + replace "balena" text nodes with "fabCNC"
PATCH = """<script>
(function() {{
  var logoSrc = '{logo}';
  function patch() {{
    var img = document.querySelector('nav img[alt="logo"]');
    if (!img) return false;
    img.src = logoSrc;
    img.style.height = '30px';
    var walker = document.createTreeWalker(
      document.body, NodeFilter.SHOW_TEXT, null, false
    );
    var n;
    while ((n = walker.nextNode())) {{
      if (/balena/i.test(n.nodeValue))
        n.nodeValue = n.nodeValue.replace(/balena/gi, 'fabCNC');
    }}
    return true;
  }}
  if (!patch()) {{
    var ob = new MutationObserver(function() {{
      if (patch()) ob.disconnect();
    }});
    ob.observe(document.documentElement, {{ childList: true, subtree: true }});
  }}
}})();
</script>
""".format(logo=LOGO_DATA)

# Also patch <title>
TITLE_PATCH = '<title>fabCNC \u2014 WiFi Setup</title>'

if len(sys.argv) < 2:
    print(f'Usage: {sys.argv[0]} <index.html path>')
    sys.exit(1)

path = sys.argv[1]
with open(path) as f:
    html = f.read()

# Replace title
import re
html = re.sub(r'<title>[^<]*</title>', TITLE_PATCH, html)

# Inject branding script
html = html.replace('</body>', PATCH + '</body>', 1)

with open(path, 'w') as f:
    f.write(html)

print(f'Patched {path} with fabCNC branding.')
