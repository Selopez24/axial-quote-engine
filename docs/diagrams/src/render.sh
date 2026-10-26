#!/bin/bash
# Render all diagrams to slide-ready PNGs (requires: npx/mermaid-cli, Chrome)
# Install: PUPPETEER_SKIP_DOWNLOAD=1 npm install @mermaid-js/mermaid-cli
cd "$(dirname "$0")"
for f in *.mmd; do
  name="${f%.mmd}"
  # 04-at-scale gets a wider wrapping config so its boxes don't collapse
  # into tall narrow columns
  cfg="mermaid-config.json"
  if [ "$name" = "04-at-scale" ]; then cfg="mermaid-config-wide.json"; fi
  ~/node_modules/.bin/mmdc -i "$f" -o "../${name}.png" \
    -s 3 -b white \
    -p puppeteer-config.json \
    -c "$cfg" \
    --quiet && echo "$name OK"
done
