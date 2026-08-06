"""
CC(素の claude TUI) 制御 — 起動 / テキスト送信 / 終了
========================================================
Windows 専用。新しいコンソールウィンドウで素の `claude` を起動し、
そのウィンドウをフォアグラウンド化して **クリップボード貼り付け(Ctrl+V)** で
テキストを投入する。

なぜクリップボード経由か:
  SendKeys で日本語を直接打つと IME で化ける。Set-Clipboard → Ctrl+V なら
  日本語がそのまま入る（OpenWhispr も同手法）。

依存: pywin32 (win32gui / win32com / win32clipboard / win32con)

単体テスト（依存インストール後）:
  python cc_controller.py start           # 新コンソールで claude 起動
  python cc_controller.py send "こんにちは"  # CC にテキスト投入して Enter
  python cc_controller.py stop            # /exit 送信で終了
"""
from __future__ import annotations

import subprocess
import time

WINDOW_TITLE = "Lucas-CC"   # 起動する CC コンソールのウィンドウタイトル（探索キー）
CREATE_NEW_CONSOLE = 0x00000010


def start_cc(title: str = WINDOW_TITLE, workdir: str | None = None) -> int:
    """Windows Terminal の新ウィンドウで claude を起動し、ランチャー pid を返す。

    重要: CC は起動するとターミナルタイトルを自分の状態表示で上書きする。
    そのままだとタイトルでウィンドウを特定できない（しかも他の CC セッションと
    誤爆する危険がある）ため、wt.exe の --suppressApplicationTitle で
    タイトルを "Lucas-CC" に固定する。identity はこの固定タイトルで担保する。

    workdir を渡すとそのフォルダを CC のワークスペースとして起動する。
    """
    import os
    from pathlib import Path

    # workdir の効かせ方の変遷（全て実測）:
    #   wt -d          → 効かない（cwd プローブで home のままと確認。2026-07-11）
    #   -Command "…; claude" → wt が「;」を自身の区切りと解釈し分割事故（0x80070002）
    #   → 結論: セミコロンを launch_cc.ps1 に封じ込め、-File で渡す（現行）
    launcher = str(Path(__file__).parent / "launch_cc.ps1")
    cmd = [
        "wt.exe", "-w", "_new", "new-tab",
        "--title", title, "--suppressApplicationTitle",
        "powershell", "-NoLogo", "-ExecutionPolicy", "Bypass", "-File", launcher,
    ]
    if workdir:
        cmd += ["-WorkDir", workdir]
    # 環境変数スクラブ: 呼び出し元が Claude 系プロセス（デスクトップアプリ等）だと
    # CLAUDE_CODE_CHILD_SESSION 等を子が継承し、「アプリの子セッション」として
    # 起動されてしまう（履歴・トランスクリプトが通常の場所に残らない。2026-07-11実証）。
    # どこから呼ばれても素の claude TUI になるよう CLAUDE系/ANTHROPIC系を落とす。
    env = {k: v for k, v in os.environ.items()
           if not (k.startswith("CLAUDE") or k.startswith("ANTHROPIC"))}
    # -NoExit を付けない: claude 終了 = シェル終了 = タブが閉じる（/exit で綺麗に消える）
    proc = subprocess.Popen(cmd, env=env)
    return proc.pid


def _find_hwnd_by_title(title: str):
    """可視トップレベルウィンドウからタイトル完全一致で hwnd を返す（無ければ None）。

    部分一致だと他ウィンドウ（エディタのタブ名等）と衝突し得るため完全一致に限定。
    suppressApplicationTitle 起動なのでタイトルは固定文字列のまま。
    """
    import win32gui

    found = []

    def _cb(hwnd, _):
        if win32gui.IsWindowVisible(hwnd):
            if win32gui.GetWindowText(hwnd).strip().lower() == title.strip().lower():
                found.append(hwnd)

    win32gui.EnumWindows(_cb, None)
    return found[0] if found else None


def _set_clipboard(text: str) -> None:
    import win32clipboard

    win32clipboard.OpenClipboard()
    try:
        win32clipboard.EmptyClipboard()
        win32clipboard.SetClipboardData(win32clipboard.CF_UNICODETEXT, text)
    finally:
        win32clipboard.CloseClipboard()


def _get_clipboard_text():
    """現在のクリップボードのテキストを返す（テキスト以外/失敗時は None）。"""
    import win32clipboard

    try:
        win32clipboard.OpenClipboard()
        try:
            if win32clipboard.IsClipboardFormatAvailable(win32clipboard.CF_UNICODETEXT):
                return win32clipboard.GetClipboardData(win32clipboard.CF_UNICODETEXT)
            return None
        finally:
            win32clipboard.CloseClipboard()
    except Exception:
        return None


def _foreground(hwnd) -> None:
    """hwnd を前面化する。フォアグラウンドロック対策に入力スレッドをアタッチする。"""
    import win32con
    import win32gui
    import win32process

    try:
        fg = win32gui.GetForegroundWindow()
        target_thread, _ = win32process.GetWindowThreadProcessId(hwnd)
        cur_thread = win32api_GetCurrentThreadId()
        fg_thread, _ = win32process.GetWindowThreadProcessId(fg) if fg else (0, 0)
        for t in {fg_thread, cur_thread}:
            if t and t != target_thread:
                try:
                    win32process.AttachThreadInput(t, target_thread, True)
                except Exception:
                    pass
        win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
        win32gui.SetForegroundWindow(hwnd)
    except Exception:
        # 前面化に失敗しても送信は試みる（既に前面のことが多い）
        pass


def win32api_GetCurrentThreadId() -> int:
    import win32api

    return win32api.GetCurrentThreadId()


def _paste_and_enter(text: str) -> None:
    """クリップボード保存→結果コピー→Ctrl+V＋Enter→クリップボード復元。

    SuperWhisper / Aqua Voice と同じ注入パターン。まひろの元のクリップボード
    内容を壊さない。
    """
    import win32com.client

    prev = _get_clipboard_text()
    _set_clipboard(text)
    time.sleep(0.05)
    shell = win32com.client.Dispatch("WScript.Shell")
    shell.SendKeys("^v")
    time.sleep(0.1)
    shell.SendKeys("{ENTER}")
    if prev is not None:
        time.sleep(0.5)  # ペーストがクリップボードを読み終えるのを待ってから復元
        try:
            _set_clipboard(prev)
        except Exception:
            pass


def paste_text(text: str, keep_clipboard: bool = False) -> bool:
    """今フォアグラウンドにあるウィンドウのカーソル位置に貼り付ける（Enter なし）。

    文字起こしモード用。フォーカスは一切動かさない——ユーザーが今書いている
    場所こそが貼り付け先。keep_clipboard=True なら結果をクリップボードに残す
    （カーソルがどこにも無い場合でも、あとから手動 Ctrl+V で取り出せる）。
    """
    import win32com.client

    prev = _get_clipboard_text()
    _set_clipboard(text)
    time.sleep(0.05)
    shell = win32com.client.Dispatch("WScript.Shell")
    shell.SendKeys("^v")
    if not keep_clipboard and prev is not None:
        time.sleep(0.5)
        try:
            _set_clipboard(prev)
        except Exception:
            pass
    return True


def send_text(text: str, title: str = WINDOW_TITLE) -> bool:
    """CC ウィンドウにテキストを投入して Enter。成功なら True。

    安全装置: 前面化の後、実際に対象がフォアグラウンドになったことを確認
    してからキーを送る。SendKeys は「今前面にあるウィンドウ」に届くため、
    前面化が失敗したまま送ると無関係なウィンドウ（まひろの作業中の画面）に
    文字を打ち込む事故になる。確認できなければ送らず False。
    """
    import win32gui

    hwnd = _find_hwnd_by_title(title)
    if not hwnd:
        return False
    _foreground(hwnd)
    time.sleep(0.15)
    if win32gui.GetForegroundWindow() != hwnd:  # 前面化できていない → 送らない
        return False
    _paste_and_enter(text)
    return True


def stop_cc(title: str = WINDOW_TITLE) -> bool:
    """CC に /exit を送って終了させる。10秒待ってもウィンドウが残っていれば
    WM_CLOSE でウィンドウごと閉じる（フォールバック）。成功なら True。"""
    import win32con
    import win32gui

    hwnd = _find_hwnd_by_title(title)
    if not hwnd:
        return False
    _foreground(hwnd)
    time.sleep(0.15)
    if win32gui.GetForegroundWindow() != hwnd:  # 前面化失敗 → 誤爆防止で送らない
        return False
    _paste_and_enter("/exit")
    for _ in range(10):  # 最大10秒、自然終了を待つ
        time.sleep(1)
        if not _find_hwnd_by_title(title):
            return True
    hwnd = _find_hwnd_by_title(title)
    if hwnd:
        win32gui.PostMessage(hwnd, win32con.WM_CLOSE, 0, 0)
    return True


def is_running(title: str = WINDOW_TITLE) -> bool:
    """CC ウィンドウが存在するか。"""
    return _find_hwnd_by_title(title) is not None


if __name__ == "__main__":
    import sys

    action = sys.argv[1] if len(sys.argv) > 1 else "status"
    if action == "start":
        wd = sys.argv[2] if len(sys.argv) > 2 else None
        print("pid:", start_cc(workdir=wd))
    elif action == "send":
        text = sys.argv[2] if len(sys.argv) > 2 else "テスト送信"
        print("sent:", send_text(text))
    elif action == "stop":
        print("stopped:", stop_cc())
    else:
        print("running:", is_running())
