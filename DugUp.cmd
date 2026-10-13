@echo off
rem Opens the DugUp window. Double-click this file, or make a shortcut to it (right-click, Send to, Desktop).
rem It uses the Python environment in the .venv folder beside it, and shows no black terminal window.
setlocal
set "HERE=%~dp0"
if exist "%HERE%.venv\Scripts\pythonw.exe" (
    start "" "%HERE%.venv\Scripts\pythonw.exe" -c "from dugup.window import main; main()"
    exit /b 0
)
echo DugUp could not start: there is no Python environment at "%HERE%.venv".
echo Set it up first (see README.md, "Set up"), then try again.
pause
exit /b 1
