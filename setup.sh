#!/bin/bash
# Create a venv with OpenCV, reusing the system python-evdev (which needs
# kernel headers to build from source, so inheriting it is the easy path).
set -euo pipefail
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
venv="$here/.venv"

echo "==> creating $venv"
python3 -m venv --system-site-packages "$venv"

echo "==> installing OpenCV"
"$venv/bin/pip" install -q --upgrade pip
"$venv/bin/pip" install -q opencv-python

echo "==> checking python-evdev (needed only to send key presses)"
if "$venv/bin/python" -c "import evdev" 2>/dev/null; then
    echo "    ok"
else
    echo "    MISSING. Install your distro's package, e.g."
    echo "      Fedora/Nobara : sudo dnf install python3-evdev"
    echo "      Debian/Ubuntu : sudo apt install python3-evdev"
    echo "      Arch          : sudo pacman -S python-evdev"
    echo "    (pip install evdev also works if you have kernel headers)"
fi

echo "==> checking /dev/uinput access"
if "$venv/bin/python" - <<'PY'
import os, sys
sys.exit(0 if os.access("/dev/uinput", os.W_OK) else 1)
PY
then
    echo "    ok - no root needed"
else
    echo "    NOT WRITABLE. Grant access once with:"
    echo "      echo 'KERNEL==\"uinput\", GROUP=\"input\", MODE=\"0660\", OPTIONS+=\"static_node=uinput\"' | sudo tee /etc/udev/rules.d/99-uinput.rules"
    echo "      sudo usermod -aG input \"$USER\" && sudo udevadm control --reload"
    echo "    then log out and back in."
fi

echo
echo "done.  Try:  ./taikocam --selftest --no-keys --headless"
