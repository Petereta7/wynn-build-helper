#!/bin/bash
# Double-click to open Wynn Build Helper (Mac / Linux).
cd "$(dirname "$0")"
if ! command -v python3 >/dev/null 2>&1; then
  echo "Python is not installed yet. Get it from https://www.python.org/downloads/"
  read -r -p "Press Enter to close."
  exit 1
fi
if [ ! -f .window_ready ]; then
  echo "First start: setting up the app window. This takes about a minute and only happens once..."
  python3 -m pip install --user --quiet pywebview 2>/dev/null \
    || python3 -m pip install --user --quiet --break-system-packages pywebview 2>/dev/null \
    || echo "The app window couldn't be set up, so the app will open in your browser instead."
  touch .window_ready
fi
python3 wynn_app.py
