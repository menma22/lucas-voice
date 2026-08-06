"""Step1 単体検証: SAPI 音声一覧の表示と Haruka での日本語再生。"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import win32com.client  # noqa: E402

from speak_server import _make_voice  # noqa: E402

print("=== インストール済み SAPI 音声 ===")
enum = win32com.client.Dispatch("SAPI.SpVoice")
for token in enum.GetVoices():
    print(" -", token.GetDescription())

print("\n=== _make_voice() で選択された音声で再生 ===")
voice = _make_voice()
try:
    print("選択音声:", voice.Voice.GetDescription())
except Exception as e:
    print("選択音声取得不可:", e)

voice.Speak("ルーカス、起動しました。まひろ、聞こえていますか。")
print("done")
