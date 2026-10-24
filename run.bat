@echo off
set "PYW=%APPDATA%\uv\python\cpython-3.11-windows-x86_64-none\pythonw.exe"
if not exist "%PYW%" set "PYW=pythonw"
start "" "%PYW%" "%~dp0claude_office.pyw"
