@echo off
echo ===================================
echo  Semantic Folder Organizer - Setup
echo ===================================
echo.

:: Check if Python is available
python --version >nul 2>&1
if %errorlevel% neq 0 (
    echo Error: Python is not installed or not in PATH.
    echo Install Python from https://www.python.org/downloads/
    pause
    exit /b 1
)

:: Create virtual environment
if not exist "venv" (
    echo Creating virtual environment...
    python -m venv venv
) else (
    echo Virtual environment already exists.
)

:: Activate and install dependencies
echo Activating virtual environment...
call venv\Scripts\activate.bat

echo Installing dependencies...
pip install -r requirements.txt

echo.
echo ===================================
echo  Setup complete!
echo  To run the organizer:
echo    venv\Scripts\activate.bat
echo    python cli_agent.py
echo ===================================
pause
