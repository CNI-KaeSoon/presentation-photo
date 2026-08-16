@echo off
chcp 65001 >nul
REM Package-root launcher. Keep this file ASCII-only.
for /d %%D in ("%~dp000_*") do for %%F in ("%%~fD\*.bat") do call "%%~fF"
