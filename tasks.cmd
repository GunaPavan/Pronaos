@echo off
REM Pronaos task runner - Windows CMD wrapper.
REM Calls tasks.ps1 with ExecutionPolicy Bypass so users don't need to
REM enable script execution globally. Forwards all args.
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0tasks.ps1" %*
