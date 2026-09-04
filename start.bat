@echo off
title RemoteVal Clean Agent
cd /d "%~dp0"
echo Starting RemoteVal Clean PC Agent on port 8090...
python clean_agent.py
if errorlevel 1 (
    echo.
    echo Python exited with error code %errorlevel%.
    pause
)
