@echo off
cd /d "%~dp0"
.venv\Scripts\pyinstaller.exe --noconfirm --onefile --windowed --name DevSpaces devspaces.py
