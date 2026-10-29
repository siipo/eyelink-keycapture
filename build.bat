@echo off
rem Builds dist\EyeLinkKeyCapture\ - copy that whole folder to the Display PC.
rem Needs Python 3.12 (pylink has no wheels for newer Python yet).
setlocal
cd /d "%~dp0"

if not exist .venv (
    py -V:3.12 -m venv .venv || goto :error
    .venv\Scripts\python -m pip install mss pynput pygame pyinstaller || goto :error
    .venv\Scripts\python -m pip install --index-url=https://pypi.sr-research.com sr-research-pylink || goto :error
)

.venv\Scripts\pyinstaller --noconfirm --clean --onedir --console ^
    --name EyeLinkKeyCapture ^
    --collect-all pylink ^
    eyelink_keycapture.py || goto :error

copy /y config.ini dist\EyeLinkKeyCapture\ >nul
copy /y README.md dist\EyeLinkKeyCapture\ >nul
echo.
echo Built: dist\EyeLinkKeyCapture\EyeLinkKeyCapture.exe
exit /b 0

:error
echo Build failed.
exit /b 1
