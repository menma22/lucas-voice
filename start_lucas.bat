@echo off
rem Start Lucas voice listener HEADLESS (no console window).
rem Uses pythonw.exe (no console) and detaches, so no black window remains.
rem Logs go to logs\lucas_voice.log ; stop with stop_lucas.bat
rem To DEBUG with a visible log window: change pythonw.exe -> python.exe below and add "pause" on a new line.
start "" "C:\Users\mahim\lucas-voice\.venv\Scripts\pythonw.exe" "C:\Users\mahim\lucas-voice\lucas_voice.py"
