#!/usr/bin/env bash
# Builds the Docker build context of the app image: solar_visor/ + the hub wheel (with the built web interface) + the
# hashed requirements + BUILD. Used by the workflows; Home Assistant pulls the published images (config.yaml: image).
#   scripts/build-context.sh [output directory, default build/context]
set -euo pipefail
cd "$(dirname "$0")/.."
OUT="$(pwd)/${1:-build/context}"
rm -rf "$OUT" && mkdir -p "$OUT"
cp -R solar_visor/. "$OUT/"
# the web interface, then the renderer of the dashboard card into the same package (served over Ingress)
(cd web && npm ci --no-audit --no-fund --loglevel=error && npx vite build --logLevel error \
    && npx vite build -c vite.card.config.ts --mode renderer --logLevel error)
test -f hub/src/solar_visor_hub/web/index.html && test -f hub/src/solar_visor_hub/web/card/card.js
(cd hub && uv build --wheel -q -o "$OUT" \
    && uv export -q --no-dev --no-emit-project --frozen --format requirements.txt -o "$OUT/requirements.txt")
# licenses: the app's own and those of the web dependencies bundled into the interface (React) and the card (MDI)
mkdir -p "$OUT/licenses"
cp LICENSE NOTICE "$OUT/licenses/"
for pkg in react react-dom scheduler @mdi/js; do
    [ -f "web/node_modules/$pkg/LICENSE" ] && { echo "== $pkg"; cat "web/node_modules/$pkg/LICENSE"; echo; }
done > "$OUT/licenses/THIRD_PARTY_WEB.txt"
VERSION="$(sed -n 's/^version: "\(.*\)"/\1/p' solar_visor/config.yaml)"
echo "$VERSION ($(git rev-parse --short HEAD 2>/dev/null || echo local))" > "$OUT/BUILD"
ls "$OUT"/solar_visor_hub-*.whl >/dev/null
grep -q -- "--hash=" "$OUT/requirements.txt" || { echo "requirements without hashes" >&2; exit 1; }
echo "$OUT ($VERSION)"
