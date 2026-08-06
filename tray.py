"""
タスクトレイ常駐アイコン — 「起動できてるかどうか」を一目で分かるようにする
==============================================================================
リスナー（lucas_voice.py）起動と同時に通知領域へアイコンを出す。
アイコンが有る＝ルーカス稼働中。プロセスが死ねばアイコンも消える。

右クリックメニュー:
  - ダッシュボードを開く（ダブルクリックでも同じ）
  - バーをテスト表示（3秒）— グラスバーが画面に出るかの自己診断
  - ルーカスを終了

※ Windows 11 は新しいトレイアイコンを「^」のオーバーフロー内に隠す。
  常時見せたい場合は、アイコンを掴んで時計の横へドラッグすれば固定される。
※ pystray はフェイルオープン——起動に失敗しても本体機能には影響させない。
"""
from __future__ import annotations

import os
import subprocess
import threading
from pathlib import Path

BASE = Path(__file__).parent
ICO = BASE / "assets" / "lucas.ico"


def start_tray(state, log=print):
    """トレイアイコンを別スレッドで表示する。戻り値は pystray.Icon（失敗時 None）。"""
    try:
        import pystray
        from PIL import Image

        img = Image.open(ICO)

        def _dash(icon, item):
            pyw = BASE / ".venv" / "Scripts" / "pythonw.exe"
            subprocess.Popen([str(pyw), str(BASE / "dashboard.py")], cwd=str(BASE))

        def _flash(icon, item):
            state.flash("バーはここに出るよ", 3.0)

        def _quit(icon, item):
            log("トレイから終了指示 → リスナー停止")
            try:
                icon.visible = False
                icon.stop()
            except Exception:
                pass
            os._exit(0)

        menu = pystray.Menu(
            pystray.MenuItem("ダッシュボードを開く", _dash, default=True),
            pystray.MenuItem("バーをテスト表示（3秒）", _flash),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("ルーカスを終了", _quit),
        )
        icon = pystray.Icon("lucas-voice", img, "Lucas Voice — 起動中…", menu)
        threading.Thread(target=icon.run, daemon=True).start()
        log("トレイアイコン表示（通知領域。^ の中に居る場合はドラッグで常時表示化できる）")
        return icon
    except Exception as e:
        log(f"トレイアイコン起動失敗（本体機能には影響なし）: {e!r}")
        return None


def set_ready(icon) -> None:
    """モデル読み込み完了後に呼ぶ——ツールチップを「稼働中」へ。"""
    if icon is None:
        return
    try:
        icon.title = "Lucas Voice — 稼働中（右クリックでメニュー）"
    except Exception:
        pass
