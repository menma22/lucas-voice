"""
ルーカス ステータスバー UI — 常時最前面のフローティングピル
==========================================================
Aqua Voice 風の小さなバーを画面下部中央に表示する。
状態（待機/聞いてる/認識中/対話中/送信/発話中）と下書き件数を可視化。

- tkinter のみ（追加依存なし）。角丸は透過色トリックで実現
- ドラッグで移動可。フォーカスは奪わない（クリックしない限り）
- リスナー（別スレッド）は UIState を書くだけ。tk 操作は本スレッドに閉じる

単体デモ: python ui.py demo   （状態が2秒ごとに切り替わる）
"""
from __future__ import annotations

import time
import tkinter as tk

# --- 共有状態（リスナースレッド → UIスレッド） ---------------------------------


class UIState:
    def __init__(self) -> None:
        self.key = "loading"
        self.text: str | None = None      # ラベル上書き（通常は None で既定文言）
        self.pending = 0                  # 下書き件数
        self.session = False              # 「ルーカス」で始まるセッション中か（バー表示の主条件）
        self.flash_text: str | None = None
        self.flash_until = 0.0
        self.history: list[tuple[str, str, str]] = []  # (時刻, モード, テキスト)
        self.cancel_requested = False  # 録音中の×＝録音キャンセル要求（リスナーが消費）
        self.clear_pending_requested = False  # バーの下書きバッジクリック＝下書き全消し要求
        self.level = 0.0               # マイク音声レベル 0..1（リキッドUIの振幅用）

    def set(self, key: str, text: str | None = None) -> None:
        self.key = key
        self.text = text

    def flash(self, text: str, sec: float = 2.5) -> None:
        self.flash_text = text
        self.flash_until = time.time() + sec

    def add_history(self, mode: str, text: str) -> None:
        self.history.append((time.strftime("%H:%M:%S"), mode, text))
        del self.history[:-200]  # 直近200件だけ保持


# --- スタイル（Tokyo Night パレット） -----------------------------------------

PILL_BG = "#16161e"
PILL_BORDER = "#2f3549"
TEXT = "#c0caf5"
TRANSPARENT = "#010101"  # この色は透過される（実在色と被らない黒近傍）
FLASH_ACCENT = "#9ece6a"

STYLES: dict[str, tuple[str, str]] = {
    # key: (アクセント色, 既定ラベル)
    "loading":       ("#e0af68", "モデル読み込み中…"),
    "idle":          ("#565f89", "待機中 —「ルーカス」で呼んで"),
    "recording":     ("#f7768e", "聞いてるよ…"),
    "transcribing":  ("#e0af68", "認識中…"),
    "booting":       ("#7dcfff", "Claude Code 起動中…"),
    "active":        ("#7aa2f7", "対話中 —「これでOK」で送信"),
    "speaking":      ("#bb9af7", "ルーカス発話中…"),
    "dictating":     ("#73daca", "録音中 — 離すと確定"),
    "dictate_proc":  ("#e0af68", "文字にしてる…"),
    "error":         ("#f7768e", "エラー"),
}

PULSING = {"recording", "speaking", "booting", "loading", "transcribing",
           "dictating", "dictate_proc"}

# セッション外でも表示するキー（文字起こしモードはキー起動＝常に見せる）
ALWAYS_SHOW = {"dictating", "dictate_proc", "error"}


def compute_draw(state: "UIState", closed: bool) -> dict:
    """UIState → 描画パラメータ（tk / glass 両エンジン共通の表示ルール）。"""
    flashing = bool(state.flash_text and time.time() < state.flash_until)
    show = (not closed) and (state.session or flashing or state.key in ALWAYS_SHOW)
    if flashing:
        accent, label = FLASH_ACCENT, state.flash_text
    else:
        accent, label = STYLES.get(state.key, STYLES["error"])
        if state.text:
            label = state.text
    return {"show": show, "key": state.key, "accent": accent, "label": label,
            "pending": state.pending, "level": round(float(state.level), 3)}

W, H, R = 360, 46, 22  # バーの幅・高さ・角丸半径


def _blend(c1: str, c2: str, t: float) -> str:
    """c1→c2 を t (0..1) で線形補間した色を返す。"""
    a = [int(c1[i:i + 2], 16) for i in (1, 3, 5)]
    b = [int(c2[i:i + 2], 16) for i in (1, 3, 5)]
    return "#" + "".join(f"{round(x + (y - x) * t):02x}" for x, y in zip(a, b))


def _rounded_rect(cv: tk.Canvas, x1, y1, x2, y2, r, **kw):
    pts = [
        x1 + r, y1, x2 - r, y1, x2, y1, x2, y1 + r, x2, y2 - r, x2, y2,
        x2 - r, y2, x1 + r, y2, x1, y2, x1, y2 - r, x1, y1 + r, x1, y1,
    ]
    return cv.create_polygon(pts, smooth=True, **kw)


class StatusBar:
    def __init__(self, state: UIState) -> None:
        self.state = state
        self.root = tk.Tk()
        self.root.overrideredirect(True)          # 枠なし
        self.root.attributes("-topmost", True)    # 常時最前面
        self.root.attributes("-transparentcolor", TRANSPARENT)
        sw, sh = self.root.winfo_screenwidth(), self.root.winfo_screenheight()
        self.root.geometry(f"{W}x{H}+{(sw - W) // 2}+{sh - H - 70}")  # 下部中央

        self.cv = tk.Canvas(self.root, width=W, height=H,
                            bg=TRANSPARENT, highlightthickness=0)
        self.cv.pack()

        # ドラッグ移動・ホバー（×ボタン表示）
        self.cv.bind("<Button-1>", self._drag_start)
        self.cv.bind("<B1-Motion>", self._drag_move)
        self.cv.bind("<Enter>", lambda e: self._set_hover(True))
        self.cv.bind("<Leave>", lambda e: self._set_hover(False))
        self._off = (0, 0)
        self.hover = False
        self.hidden = False   # 待機中の自動非表示
        self.closed = False   # ×で閉じられた（以後このセッションでは表示しない）

        self._phase = 0.0
        self._last_draw = ()
        self.hist_win: tk.Toplevel | None = None
        self.root.update_idletasks()
        self._apply_noactivate()
        self._tick()

    def _apply_noactivate(self) -> None:
        """バーが絶対にフォーカスを奪わないようにする。

        tk の deiconify はウィンドウをアクティブ化し、まひろが今書いている
        テキスト欄からキャレットを奪う（文字起こし先が消える実害。2026-07-12）。
        WS_EX_NOACTIVATE でクリックしても活性化しないウィンドウにする。
        """
        try:
            import ctypes

            GWL_EXSTYLE = -20
            WS_EX_NOACTIVATE = 0x08000000
            WS_EX_TOOLWINDOW = 0x00000080
            GA_ROOT = 2
            u = ctypes.windll.user32
            # GetParent は中間ウィンドウを返し属性が効かない（実測）。GA_ROOT で最上位へ
            hwnd = u.GetAncestor(self.cv.winfo_id(), GA_ROOT)
            style = u.GetWindowLongW(hwnd, GWL_EXSTYLE)
            u.SetWindowLongW(hwnd, GWL_EXSTYLE,
                             style | WS_EX_NOACTIVATE | WS_EX_TOOLWINDOW)
        except Exception:
            pass

    def _set_hover(self, v: bool) -> None:
        self.hover = v

    # --- drag / close / history ---
    def _drag_start(self, e) -> None:  # noqa: ANN001
        cy = H // 2
        if self.hover and (e.x - (W - 20)) ** 2 + (e.y - cy) ** 2 <= 11 ** 2:
            if self.state.key == "dictating":
                # 録音中の× = 録音キャンセル（バーは閉じない）
                self.state.cancel_requested = True
                return
            # 通常の× = バーを閉じる
            self.closed = True
            self.root.withdraw()
            self.hidden = True
            return
        if self.hover and (e.x - (W - 44)) ** 2 + (e.y - cy) ** 2 <= 11 ** 2:
            # ≡ボタン: 認識ログウィンドウをトグル
            self._toggle_history()
            return
        self._off = (e.x, e.y)

    # --- 認識ログウィンドウ ---
    def _toggle_history(self) -> None:
        if self.hist_win is not None and self.hist_win.winfo_exists():
            self.hist_win.destroy()
            self.hist_win = None
            return
        win = tk.Toplevel(self.root)
        win.title("ルーカス 認識ログ")
        win.attributes("-topmost", True)
        win.configure(bg=PILL_BG)
        bx = self.root.winfo_x()
        by = self.root.winfo_y()
        win.geometry(f"480x320+{max(bx - 60, 20)}+{max(by - 350, 20)}")
        txt = tk.Text(win, bg=PILL_BG, fg=TEXT, insertbackground=TEXT,
                      font=("Yu Gothic UI", 10), relief="flat", wrap="word",
                      padx=10, pady=8)
        sb = tk.Scrollbar(win, command=txt.yview)
        txt.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        txt.pack(side="left", fill="both", expand=True)
        txt.tag_configure("meta", foreground="#565f89")
        txt.tag_configure("dict", foreground="#73daca")
        txt.tag_configure("talk", foreground="#7aa2f7")
        self.hist_win = win

        def _render() -> None:
            if not (self.hist_win and self.hist_win.winfo_exists()):
                return
            txt.configure(state="normal")
            txt.delete("1.0", "end")
            if not self.state.history:
                txt.insert("end", "まだ認識ログはないよ。\n", "meta")
            for ts, mode, body in reversed(self.state.history):  # 新しい順
                tag = "dict" if "文字起こし" in mode else "talk"
                txt.insert("end", f"{ts} ", "meta")
                txt.insert("end", f"[{mode}] ", tag)
                txt.insert("end", body + "\n")
            txt.configure(state="disabled")

        last_len = [-1]

        def _loop() -> None:
            if not (self.hist_win and self.hist_win.winfo_exists()):
                return
            if len(self.state.history) != last_len[0]:
                last_len[0] = len(self.state.history)
                _render()
            win.after(500, _loop)

        _loop()

    def _drag_move(self, e) -> None:  # noqa: ANN001
        self.root.geometry(
            f"+{e.x_root - self._off[0]}+{e.y_root - self._off[1]}")

    # --- 描画ループ ---
    def _tick(self) -> None:
        s = self.state
        flashing = s.flash_text and time.time() < s.flash_until

        # 表示要否: 「ルーカス」と呼んだ後のセッション中だけ表示（まひろ指示）。
        # 待機中は録音・認識が走ってもバーを出さない（周囲の音での点滅防止）。
        # ×で閉じたら以後表示しない。文字起こしモード・エラー・フラッシュは見せる。
        show = (not self.closed) and (
            s.session or bool(flashing) or s.key in ALWAYS_SHOW)
        if show and self.hidden:
            self.root.deiconify()
            self.root.overrideredirect(True)
            self.root.attributes("-topmost", True)
            self._apply_noactivate()  # 再表示でフォーカスを奪わない（キャレット保護）
            self.hidden = False
        elif not show and not self.hidden:
            self.root.withdraw()
            self.hidden = True
        if self.hidden:
            self.root.after(150, self._tick)
            return

        key = s.key if not flashing else "flash"
        accent, label = (FLASH_ACCENT, s.flash_text) if flashing \
            else STYLES.get(s.key, STYLES["error"])
        if s.text and not flashing:
            label = s.text

        pulse = key in PULSING or (s.key in PULSING and not flashing)
        if pulse:
            self._phase = (self._phase + 0.12) % 2.0
            t = abs(1.0 - self._phase)          # 0→1→0 の三角波
            dot = _blend(accent, PILL_BG, 0.55 * t)
        else:
            dot = accent

        sig = (key, label, s.pending, dot, self.hover)
        if sig != self._last_draw:
            self._last_draw = sig
            self._draw(accent, dot, label, s.pending)
        self.root.after(90, self._tick)

    def _draw(self, accent: str, dot: str, label: str, pending: int) -> None:
        cv = self.cv
        cv.delete("all")
        _rounded_rect(cv, 1, 1, W - 1, H - 1, R,
                      fill=PILL_BG, outline=PILL_BORDER, width=1)
        # ステータスドット（外輪＋本体）
        cx, cy = 24, H // 2
        cv.create_oval(cx - 8, cy - 8, cx + 8, cy + 8,
                       fill=_blend(dot, PILL_BG, 0.7), outline="")
        cv.create_oval(cx - 5, cy - 5, cx + 5, cy + 5, fill=dot, outline="")
        # ラベル
        cv.create_text(42, cy, text=label, anchor="w",
                       font=("Yu Gothic UI", 10), fill=TEXT)
        # 下書きバッジ（右端。ホバー中は ≡・× ボタンの分だけ左へ）
        right = W - 12 if not self.hover else W - 62
        if pending > 0:
            badge = f"下書き {pending}"
            bw = 14 + 9 * len(badge)
            _rounded_rect(cv, right - bw, cy - 11, right, cy + 11, 11,
                          fill=_blend(accent, PILL_BG, 0.75), outline=accent)
            cv.create_text(right - bw / 2, cy, text=badge,
                           font=("Yu Gothic UI", 9), fill=TEXT)
        # ≡（認識ログ）と ×（閉じる）ボタン（ホバー中のみ表示）
        if self.hover:
            for bx, label, color in ((W - 44, "≡", "#7aa2f7"), (W - 20, "✕", "#f7768e")):
                cv.create_oval(bx - 10, cy - 10, bx + 10, cy + 10,
                               fill="#2f3549", outline=PILL_BORDER)
                cv.create_text(bx, cy, text=label,
                               font=("Yu Gothic UI", 10), fill=color)

    def run(self) -> None:
        self.root.mainloop()


def run_status_bar(state: UIState) -> None:
    StatusBar(state).run()


# --- 単体デモ -----------------------------------------------------------------
if __name__ == "__main__":
    import sys
    import threading

    st = UIState()
    if len(sys.argv) > 1 and sys.argv[1] == "demo":
        st.add_history("文字起こし", "これはpush-to-talkで入力したテキストの例だよ")
        st.add_history("対話", "ルーカス、このファイルをリファクタリングして")
        st.add_history("文字起こし", "認識ログはこのウィンドウで確認できる")

        def _cycle() -> None:
            # (key, pending, session) — session=False の録音/認識は非表示のはず
            seq = [
                ("loading", 0, False), ("idle", 0, False),
                ("recording", 0, False), ("transcribing", 0, False),
                ("booting", 0, True), ("active", 2, True),
                ("speaking", 2, True), ("dictating", 0, False),
            ]
            i = 0
            while True:
                key, pend, sess = seq[i % len(seq)]
                st.set(key)
                st.pending = pend
                st.session = sess
                if key == "active" and i % len(seq) == 5:
                    time.sleep(1.0)
                    st.flash("送ったよ ✓")
                time.sleep(2.0)
                i += 1

        threading.Thread(target=_cycle, daemon=True).start()
        bar = StatusBar(st)
        bar.root.after(2500, bar._toggle_history)  # 履歴ウィンドウの見た目確認用
        bar.run()
    else:
        run_status_bar(st)
