@echo off
chcp 65001 >nul
title BT-AudioSink

cd /d "%~dp0"

python src\gui.py

if %ERRORLEVEL% NEQ 0 (
    echo.
    echo  ERROR: Could not start the GUI.
    echo   - Is Python installed and on PATH?
    echo   - Run setup\install.ps1 to install dependencies
    echo.
    pause
)
