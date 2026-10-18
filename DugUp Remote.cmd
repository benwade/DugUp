@echo off
rem Opens DugUp Remote: start or quit DugUp in the tray, bring up the search box, free the graphics card.
rem Double-click this file, or make a shortcut to it (right-click, Send to, Desktop).
rem It uses the Python environment in the .venv folder beside it, and shows no black terminal window.
setlocal
set "HERE=%~dp0"
if exist "%HERE%.venv\Scripts\pythonw.exe" (
    start "" "%HERE%.venv\Scripts\pythonw.exe" -m dugup remote
    exit /b 0
)
echo DugUp Remote could not start: there is no Python environment at "%HERE%.venv".
echo Set it up first (see README.md, "Set up from the source"), then try again.
pause
exit /b 1
