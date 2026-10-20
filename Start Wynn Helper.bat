@echo off
title Wynn Build Helper
cd /d "%~dp0"

set "PY="
set "PYW="
where python >nul 2>nul && (set "PY=python" & set "PYW=pythonw")
if not defined PY where py >nul 2>nul && (set "PY=py -3" & set "PYW=pyw -3")
if not defined PY (
  echo Python is not installed yet.
  echo Get it from https://www.python.org/downloads/ and tick "Add Python to PATH" in the installer.
  pause
  exit /b 1
)

if not exist ".window_ready" (
  echo First start: setting up the app window. This takes about a minute and only happens once...
  %PY% -m pip install --user --quiet pywebview
  echo done> ".window_ready"
)

start "" %PYW% wynn_app.py
exit /b 0
