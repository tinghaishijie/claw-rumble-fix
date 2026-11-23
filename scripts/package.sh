#!/usr/bin/env bash
# Build the frontend and assemble an installable Decky plugin zip in out/.
set -euo pipefail
cd "$(dirname "$0")/.."

pnpm install
pnpm run build

rm -rf out/claw-rumble-fix out/claw-rumble-fix.zip
mkdir -p out/claw-rumble-fix
mkdir -p out/claw-rumble-fix/dist
cp dist/index.js out/claw-rumble-fix/dist/
cp plugin.json package.json main.py LICENSE README.md out/claw-rumble-fix/
(cd out && zip -qr claw-rumble-fix.zip claw-rumble-fix)
echo "Built out/claw-rumble-fix.zip"
