#!/usr/bin/env bash
# Install Claw Rumble Fix into Decky Loader, bypassing Decky's own ZIP installer.
#
#   ./install.sh                             download the latest release (public repo only)
#   ./install.sh /path/to/claw-rumble-fix.zip    install a local archive
set -euo pipefail

REPO=tinghaishijie/claw-rumble-fix
NAME=claw-rumble-fix
DECKY_HOME=${DECKY_HOME:-"$HOME/homebrew"}
ARCHIVE=${1:-}

if [ "$EUID" -eq 0 ]; then
  echo "Run as your normal desktop user, not root." >&2
  exit 1
fi
if [ ! -d "$DECKY_HOME/plugins" ]; then
  echo "Decky Loader was not found at $DECKY_HOME. Install Decky first." >&2
  exit 1
fi

workdir=$(mktemp -d)
trap 'rm -rf -- "$workdir"' EXIT

if [ -z "$ARCHIVE" ]; then
  ARCHIVE="$workdir/$NAME.zip"
  url="https://github.com/$REPO/releases/latest/download/$NAME.zip"
  echo "Downloading $url"
  if ! curl -fLsS "$url" -o "$ARCHIVE"; then
    echo "Download failed. If the repository is private, copy $NAME.zip to this" >&2
    echo "device and run: ./install.sh /path/to/$NAME.zip" >&2
    exit 1
  fi
fi

# Validate and unpack with Python: SteamOS always has it, unzip may be missing.
python3 - "$ARCHIVE" "$workdir/staged" "$NAME" <<'PY'
import sys
import zipfile
from pathlib import PurePosixPath

archive, dest, name = sys.argv[1:4]
try:
    bundle = zipfile.ZipFile(archive)
except zipfile.BadZipFile:
    raise SystemExit("%s is not a zip file (a failed download saves an error page)" % archive)
with bundle:
    names = bundle.namelist()
    for member in names:
        parts = PurePosixPath(member).parts
        if not parts or parts[0] != name or ".." in parts or member.startswith("/"):
            raise SystemExit("Unexpected archive member: " + member)
    for required in ("main.py", "package.json", "plugin.json", "dist/index.js"):
        if "%s/%s" % (name, required) not in names:
            raise SystemExit("Archive lacks %s. Use %s.zip from the release assets, "
                             "not GitHub's 'Source code' download." % (required, name))
    bundle.extractall(dest)
PY

echo "Installing to $DECKY_HOME/plugins/$NAME (sudo required)"
sudo -v
sudo systemctl stop plugin_loader.service
sudo rm -rf -- "$DECKY_HOME/plugins/$NAME"
sudo mv -- "$workdir/staged/$NAME" "$DECKY_HOME/plugins/$NAME"
sudo chown -R root:root "$DECKY_HOME/plugins/$NAME"
sudo systemctl restart plugin_loader.service
echo "Installed. Open the Decky menu; 'Claw Rumble Fix' should be listed within a few seconds."
echo "If it is missing: journalctl -u plugin_loader -b --no-pager | tail -n 80"
