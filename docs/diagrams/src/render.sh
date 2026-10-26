#!/bin/bash
# Render all diagrams to slide-ready PNGs (requires: npx/mermaid-cli, Chrome)
# Install: PUPPETEER_SKIP_DOWNLOAD=1 npm install @mermaid-js/mermaid-cli
cd "$(dirname "$0")"
for f in *.mmd; do
  name="${f%.mmd}"
  ~/node_modules/.bin/mmdc -i "$f" -o "../${name}.png" \
    -s 3 -b white \
    -p puppeteer-config.json \
    $( [ -f mermaid-config.json ] && echo -c mermaid-config.json ) \
    --quiet && echo "$name OK"
done
