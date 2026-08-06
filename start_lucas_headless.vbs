' ルーカス音声常駐AI をウィンドウなしで起動（常用）
' ログは logs\lucas_voice.log へ。停止は stop_lucas.bat
Set sh = CreateObject("WScript.Shell")
sh.Run """C:\Users\mahim\lucas-voice\.venv\Scripts\pythonw.exe"" ""C:\Users\mahim\lucas-voice\lucas_voice.py""", 0, False
