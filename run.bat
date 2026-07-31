@echo off
cd /d "%~dp0"

if not exist "venv\Scripts\activate.bat" (
    echo Creating virtual environment...
    python -m venv venv
    if errorlevel 1 (
        echo Failed to create venv. Is Python installed?
        exit /b 1
    )
    echo Installing dependencies...
    call venv\Scripts\activate.bat
    pip install -r requirements.txt
    if errorlevel 1 (
        echo Failed to install dependencies.
        exit /b 1
    )
) else (
    call venv\Scripts\activate.bat
)

echo Starting Website Monitor...
python "Agent Website.py"
