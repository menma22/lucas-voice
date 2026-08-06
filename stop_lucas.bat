@echo off
rem ヘッドレス起動中のルーカス常駐リスナーを停止する
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0stop_lucas.ps1"
pause
