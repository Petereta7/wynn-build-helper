@echo off
rem Runs every check at once on all CPU cores. Add --full for the Wynnbuilder comparison and accuracy cases.
cd /d "%~dp0.."
set "PY=python"
where python >nul 2>nul || set "PY=py"
%PY% tests\run_all.py %*
pause
