@echo off
rem Opens "Report a problem with DugUp": it gathers what helps find a problem, shows it, and you choose how to send it.
rem It is a program of its own, so it works even while another DugUp window is stuck.
rem It uses the Python environment in the .venv folder beside it, and shows no black terminal window.
setlocal
set "HERE=%~dp0"
if exist "%HERE%.venv\Scripts\pythonw.exe" (
    start "" "%HERE%.venv\Scripts\pythonw.exe" -m dugup report
    exit /b 0
)
echo Report a problem could not start: there is no Python environment at "%HERE%.venv".
echo Set it up first (see docs\technical.md, "Source installation"), then try again.
pause
exit /b 1
