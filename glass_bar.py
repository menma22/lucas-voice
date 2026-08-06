"""
グラスバー — pywebview 版ステータスバー（リキッドグラスの正方形タイル）
==============================================================================
web/bar.html をフレームレス・最前面・フォーカス不奪のウィンドウでホストする。
表示ルールは ui.compute_draw を共有。正方形タイル（200x200 論理px）を
**画面右下・タスクバーのすぐ上**に置く（2026-07-15 まひろ指示で全面改装）。
中身はオーロラ6色の音反応粒子オーブ＋下部小ラベル（描画は bar.html 側）。

■ 半透明（リキッドグラス）: **自前サンプラ方式（DIYグラス）**。
  Win11 26200 では DWM の透過経路が全滅と実測（SBT明/暗=黒地・accent4=黒地・
  accent2=内容ごと不可視・SetWindowRgn×ExtendFrame 併用=ウィンドウ全消し）。
  そこで _backdrop_sampler がバー背後を定期スクショ→縮小→ガウスぼかし→
  base64 JPEG を bar.html へ push し、タイル最下層に敷く＝本物の背後画素で透ける。
  採取の瞬間だけ WDA_EXCLUDEFROMCAPTURE を立てて自窓を除外（幻影ループ根絶）。
  Pillow 不在などで死んだら poll の glass=False → bar.html が不透明ダークグラスを
  描く（どちらに転んでも不可視にはならない二段構え）。屈折・薄膜は bar.html 側。

■ DPI設計（2026-07-13・まひろ「WebView2のまま堅牢化」選択後の根治。下端スクショで実証）
  結論: 自分では DPI認識を設定せず、バーは **作業領域から水平中央・下端アンカーで配置**、
  サイズ/座標は論理px（312x40 等）で扱い、CSS zoom は 1.0 固定にする。
  なぜこれで直るか（すべて実測で確定）:
    - pywebview/WebView2 は create_window/start で **勝手にプロセスを DPI認識化**し、
      さらに WinForms が窓を **中心を固定したまま DPI倍率で拡大**する。論理 312x40 で
      作った窓を中央(x=960)・下端付近に置くと、物理 468x60・(726,1062) に着地し画面内に
      収まる（＝中心スケールに耐える配置なので、非認識/認識のどちらに転んでも中央下に残る）。
    - 従来の「物理px指定＋CSS zoom」路線は、この中心スケールと二重掛けになって座標が
      2.25倍に膨れ、バーが画面外(y=1593)へ飛び不可視だった（＝まひろの「UIが見えない」の
      真因）。物理px指定をやめ中央+下端アンカーに委ねるのが要点。
    - IsWindowVisible=True も PrintWindow も **画面外の窓で成立してしまう**。可視性の検証は
      必ず「画面キャプチャに写るか」で行うこと（在宅オラクルの落とし穴で一度騙された）。
  ※ lucas_voice.py 側でも SetProcessDpiAwareness を呼ばない（初期化順が変わり別の座標系
    ずれを招く。DPI管理は pywebview に任せる）。

■ 表示切替は pywebview の window.show()/hide() を使わず Win32 ShowWindow を直叩き。
  ── show()/hide() は WebView2 UIスレッドへマーシャリングされ、STT推論でCPUが飽和すると
     飢えて実行されず「出ない」。ShowWindow はOSレベル直呼びで pump 応答性に依存しない。

■ すべての show/hide・座標・hwnd を logs/glass_bar.log に記録＝実機データで次を撃つ。

⚠ WebView2 は透過非対応 → 角丸は SetWindowRgn でクリップ。
⚠ js_api の公開属性に Window 等を置くと再帰公開でブリッジ崩壊（_ 付きで隠す）。

config.toml [ui] engine = "glass"（既定）/ "classic"（tk版に戻す）
"""
from __future__ import annotations

import ctypes
import subprocess
import threading
import time
from ctypes import wintypes
from pathlib import Path

import webview

from ui import UIState, compute_draw

BASE = Path(__file__).parent
LOGFILE = BASE / "logs" / "glass_bar.log"
BAR_W, BAR_H = 116, 116  # 正方形タイル（論理px。200→172→116=2/3。2026-07-17 まひろ「でかすぎ」）
PANEL_H = 308            # 履歴パネル展開分（論理）
GAP = 4                  # タスクバーとの隙間（論理）
MARGIN_R = 10            # 画面右端からの余白（論理）
CORNER_CSS = 32          # 角丸の直径（CSS px = 論理）。bar.html の --radius(16px) と一致

_user32 = ctypes.windll.user32
_gdi32 = ctypes.windll.gdi32

# ⚠ 64bit HWND の切り捨て防止: z-order 走査系は argtypes/restype を明示する
# （既定の c_int だと 2^31 超のハンドルが壊れ、_obscured_by_above が沈黙する。実測）
_user32.GetWindow.restype = wintypes.HWND
_user32.GetWindow.argtypes = [wintypes.HWND, ctypes.c_uint]
_user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
_user32.IsWindowVisible.restype = wintypes.BOOL
_user32.IsWindowVisible.argtypes = [wintypes.HWND]

SWP_NOACTIVATE = 0x0010
SWP_NOSIZE = 0x0001
SWP_NOMOVE = 0x0002
HWND_TOPMOST = -1
HWND_NOTOPMOST = -2
GW_HWNDPREV = 3
SW_HIDE = 0
SW_SHOWNA = 8            # アクティブ化せず表示（フォーカス不奪）
RDW_INVALIDATE = 0x0001
RDW_UPDATENOW = 0x0100
RDW_ALLCHILDREN = 0x0080


def _log(msg: str) -> None:
    """実機で何が起きたかを残す。再現不能を実データに変えるための計測。"""
    try:
        with open(LOGFILE, "a", encoding="utf-8") as f:
            f.write(f"[{time.strftime('%H:%M:%S')}] {msg}\n")
    except Exception:
        pass


def _work_area() -> tuple[int, int, int, int]:
    """作業領域（タスクバー除く・非認識プロセスでは論理px）。"""
    r = wintypes.RECT()
    _user32.SystemParametersInfoW(0x0030, 0, ctypes.byref(r), 0)  # SPI_GETWORKAREA
    return r.left, r.top, r.right, r.bottom


def _geometry(expanded: bool) -> tuple[int, int, int, int, int]:
    """バー窓の論理矩形。プライマリ作業領域の**右下**（タスクバー直上）・必ず画面内。
    返り値: (x, y, width, total_height, bar_height)。"""
    wl, wt, wr, wb = _work_area()
    total_h = BAR_H + (PANEL_H if expanded else 0)
    x = max(wl, wr - BAR_W - MARGIN_R)
    y = max(wt, wb - total_h - GAP)
    return x, y, BAR_W, total_h, BAR_H


def _apply_pill_region(hwnd: int, corner_css: int = CORNER_CSS) -> None:
    """ウィンドウを角丸長方形にクリップ。

    corner_css は CSS px（論理）の角丸直径。実窓は DPI 倍されているため、
    実測幅 / BAR_W からスケールを割り出して物理 px に換算する
    （200論理→300物理なら 48→72。CSS の --radius=24px と視覚的に一致）。"""
    r = wintypes.RECT()
    _user32.GetWindowRect(hwnd, ctypes.byref(r))
    w, h = r.right - r.left, r.bottom - r.top
    corner = max(16, round(corner_css * w / max(BAR_W, 1)))
    rgn = _gdi32.CreateRoundRectRgn(0, 0, w + 1, h + 1, corner, corner)
    _user32.SetWindowRgn(hwnd, rgn, True)


def _set_capture_affinity(hwnd: int, exclude: bool) -> bool:
    """WDA_EXCLUDEFROMCAPTURE の付け外し。

    サンプラが背後を撮る瞬間だけ立てて自窓をキャプチャから除外する
    （常時ONにしないのは、まひろ自身のスクショ/画面録画や検証キャプチャに
    バーが写らなくなるのを避けるため）。"""
    WDA_NONE, WDA_EXCLUDEFROMCAPTURE = 0, 0x11
    return bool(_user32.SetWindowDisplayAffinity(
        hwnd, WDA_EXCLUDEFROMCAPTURE if exclude else WDA_NONE))


def _backdrop_sampler(window, api: "BarApi", hwnd: int) -> None:
    """自前グラス: バー背後をリアルタイム採取して bar.html へ push する常駐スレッド。

    Win11 26200 では DWM の透過バックドロップが本ウィンドウに一切効かない
    （モジュール冒頭の実測記録参照）ため、「本物の背後画素」を自分で採って
    ガラスを合成する。主経路= **dxcam（DXGI Desktop Duplication）**:
    実測1.7ms/フレームでソースの更新レート（〜50fps）まで追随＝体感ライブ
    （2026-07-17 まひろFB「70msより上げられない？」への回答）。
    dxcam 不可時は ImageGrab 45ms（〜22fps）へ自動フォールバック。
    変化時のみ転送＝静止デスクトップでは実質ゼロコスト。
    Pillow まで死んだら glass=False で不透明ダークグラスに切り替わる。"""
    try:
        import base64
        import hashlib
        import io
        import os as _os

        from PIL import Image, ImageGrab
    except Exception as e:
        _log(f"glass(DIY): Pillow 不可 → 不透明フォールバック: {e!r}")
        api._glass = False
        return
    cam = None
    try:
        if _os.environ.get("LB_NO_DXCAM"):  # 検証用ノブ: dxcam中はバーが常時キャプチャ
            raise RuntimeError("LB_NO_DXCAM")  # 除外＝スクショ検証が不能になるため
        import dxcam  # RGB変換はcv2依存のためBGRA生取り→numpyで並べ替える

        cam = dxcam.create(output_idx=0, output_color="BGRA")
        _log("glass: dxcam(DXGI) 有効＝リアルタイム透過（実測~2ms/フレーム）")
    except Exception as e:
        _log(f"glass: dxcam 不可 → ImageGrab 45ms フォールバック: {e!r}")
    last_hash = None
    excluded = False
    while True:
        try:
            if api._sampling:
                r = wintypes.RECT()
                _user32.GetWindowRect(hwnd, ctypes.byref(r))
                w, h = r.right - r.left, r.bottom - r.top
                if w <= 0 or h <= 0:
                    time.sleep(0.05)
                    continue
                t0 = time.time()
                if cam is not None:
                    # DXGI複製は連続稼働＝表示中は自窓を常時キャプチャ除外（幻影ループ防止）。
                    # 副作用: バー表示中はスクショ/録画にバーが写らない（非表示になると解除）
                    if not excluded:
                        excluded = _set_capture_affinity(hwnd, True)
                    frame = cam.grab(region=(r.left, r.top, r.right, r.bottom))
                    if frame is None:  # 新しいデスクトップフレームが無い＝背後は静止
                        time.sleep(0.008)
                        continue
                    img = Image.fromarray(frame[:, :, [2, 1, 0]])  # BGRA -> RGB
                    pace = 0.018  # 上限 ~50fps
                else:
                    ok = _set_capture_affinity(hwnd, True)
                    try:
                        img = ImageGrab.grab(bbox=(r.left, r.top, r.right, r.bottom))
                    finally:
                        if ok:
                            _set_capture_affinity(hwnd, False)
                    # 検証時は LB_PACE=0.5 でWDA除外のデューティ比を下げスクショに写す
                    pace = float(_os.environ.get("LB_PACE", "0.045"))  # 既定 ~22fps
                buf = io.BytesIO()
                img.save(buf, "JPEG", quality=70)
                data = buf.getvalue()
                digest = hashlib.md5(data).hexdigest()
                if digest != last_hash:
                    last_hash = digest
                    b64 = base64.b64encode(data).decode()
                    window.evaluate_js(
                        'window.setBackdrop && window.setBackdrop('
                        f'"data:image/jpeg;base64,{b64}")')
                time.sleep(max(0.004, pace - (time.time() - t0)))
            else:
                if excluded:  # 隠れている間はキャプチャ除外を解除（スクショに写るように）
                    excluded = not _set_capture_affinity(hwnd, False)
                time.sleep(0.25)
        except Exception as e:
            _log(f"glass(DIY) sampler 例外: {e!r}")
            time.sleep(2)


def _apply_noactivate(hwnd: int) -> None:
    """フォーカス絶対不奪（キャレット保護）＋タスクバー非表示。

    ⚠ TOOLWINDOW を足すだけでは駄目: WinForms が WS_EX_APPWINDOW（タスクバー
    強制表示）を立てており、そちらが勝ってボタンが出続ける（2026-07-15 実測。
    まひろ指示「タスクバーに出ないように」の真因）。APPWINDOW を明示的に剥がし、
    FRAMECHANGED でスタイル変更を即反映させる。
    """
    GWL_EXSTYLE = -20
    WS_EX_NOACTIVATE = 0x08000000
    WS_EX_TOOLWINDOW = 0x00000080
    WS_EX_APPWINDOW = 0x00040000
    style = _user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
    _user32.SetWindowLongW(
        hwnd, GWL_EXSTYLE,
        (style | WS_EX_NOACTIVATE | WS_EX_TOOLWINDOW) & ~WS_EX_APPWINDOW)
    SWP_FRAMECHANGED = 0x0020
    _user32.SetWindowPos(hwnd, 0, 0, 0, 0, 0,
                         SWP_FRAMECHANGED | SWP_NOACTIVATE | SWP_NOSIZE
                         | SWP_NOMOVE | 0x0004)  # SWP_NOZORDER


def _show(hwnd: int) -> None:
    """Win32 直叩きで表示。pywebview のUIスレッドマーシャリングを回避。"""
    _user32.ShowWindow(hwnd, SW_SHOWNA)
    _raise_topmost(hwnd)
    _user32.RedrawWindow(hwnd, None, None,
                         RDW_INVALIDATE | RDW_UPDATENOW | RDW_ALLCHILDREN)


def _raise_topmost(hwnd: int) -> None:
    """topmost帯の最上位へ再挿入する。

    HWND_TOPMOST の再指定だけでは「既にtopmostな他窓」の上には行けない
    （帯内の順序は変わらない）ため、NOTOPMOST→TOPMOST のトグルで帯の
    先頭に入り直す——最前面オーバーレイの定石。
    """
    f = SWP_NOACTIVATE | SWP_NOSIZE | SWP_NOMOVE
    _user32.SetWindowPos(hwnd, HWND_NOTOPMOST, 0, 0, 0, 0, f)
    _user32.SetWindowPos(hwnd, HWND_TOPMOST, 0, 0, 0, 0, f)
    _user32.SetWindowPos(hwnd, 0, 0, 0, 0, 0, f)  # HWND_TOP: topmost帯内の先頭へ


def _obscured_by_above(hwnd: int) -> bool:
    """自分より上のZ順に、自分の矩形へ重なる可視ウィンドウが居るか。

    まひろ指示（2026-07-15）「認識中のUIは全ウィンドウの最前面に固定」——
    他アプリの常時最前面窓（動画プレイヤー等）が後から上に乗るのを検知して
    再主張するための判定。DWMクローク窓（見えないUWP残骸）は除外。
    """
    r = wintypes.RECT()
    _user32.GetWindowRect(hwnd, ctypes.byref(r))
    h = _user32.GetWindow(hwnd, GW_HWNDPREV)
    n = 0
    while h and n < 256:
        n += 1
        if _user32.IsWindowVisible(h):
            hr = wintypes.RECT()
            _user32.GetWindowRect(h, ctypes.byref(hr))
            overlap = not (hr.right <= r.left or hr.left >= r.right
                           or hr.bottom <= r.top or hr.top >= r.bottom)
            if overlap and hr.right - hr.left > 0 and hr.bottom - hr.top > 0:
                cloaked = ctypes.c_int(0)
                try:
                    ctypes.windll.dwmapi.DwmGetWindowAttribute(
                        h, 14, ctypes.byref(cloaked), 4)  # DWMWA_CLOAKED
                except Exception:
                    pass
                if not cloaked.value:
                    return True
        h = _user32.GetWindow(h, GW_HWNDPREV)
    return False


def _hide(hwnd: int) -> None:
    _user32.ShowWindow(hwnd, SW_HIDE)


_ENUMPROC = ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)


def _get_hwnd(window: "webview.Window") -> int | None:
    try:
        h = int(window.native.Handle.ToInt32())  # EdgeChromium/WinForms
        if h:
            return h
    except Exception:
        pass
    # フォールバック: 自プロセスが持つ "Lucas-Bar" だけを対象にする。
    # タイトル一致だけだと、別プロセス（クラッシュ残骸・診断・二重起動）の同名窓を
    # 誤って掴む（実測: 診断が本番リスナーの窓を掴んだ）。pid で必ず自窓に限定する。
    import os

    mypid = os.getpid()
    found: list[int] = []

    def _cb(h, _):
        pid = wintypes.DWORD()
        _user32.GetWindowThreadProcessId(h, ctypes.byref(pid))
        if pid.value == mypid:
            buf = ctypes.create_unicode_buffer(32)
            _user32.GetWindowTextW(h, buf, 32)
            if buf.value == "Lucas-Bar":
                found.append(h)
        return True

    try:
        _user32.EnumWindows(_ENUMPROC(_cb), 0)
    except Exception:
        return None
    return found[0] if found else None


class BarApi:
    """JS ⇔ Python ブリッジ。

    ⚠ 公開属性に pywebview の Window 等の複雑なオブジェクトを置かないこと。
    js_api は公開属性を再帰的に辿って公開するため、WinForms のオブジェクト
    グラフに入り込み無限再帰＋COMスレッド違反でブリッジごと壊れる（実測）。
    """

    def __init__(self, state: UIState) -> None:
        self.state = state
        self.closed = False
        self.expanded = False
        self._hwnd: int | None = None
        self._scale = 1.0  # 非認識運用なので CSS zoom は常に 1.0
        self._glass = True   # DIYグラス（背後サンプラ）が生きているか。死んだら solid 描画
        self._sampling = False  # バー表示中のみ True（monitor が更新・サンプラが従う）
        self._fluid = "#0b0d14"  # 旧磁性流体の色（config [ui] fluid_color。互換のため残置）
        self._eye = "skyblue"    # レンズアイのスキン（config [ui] eye_skin）
        self._pupil = "#4fa8f5"  # 瞳孔の色（config [ui] pupil_color。白へのグラデ発光核）

    # --- JS から ---
    def get_scale(self) -> float:
        return self._scale

    def poll(self) -> dict:
        d = compute_draw(self.state, self.closed)
        d["scale"] = self._scale
        d["glass"] = self._glass
        d["fluid"] = self._fluid
        d["eye"] = self._eye
        d["pupil"] = self._pupil
        return d

    def history(self) -> list:
        return [list(x) for x in self.state.history[-200:]]

    def copy_text(self, text: str) -> bool:
        import cc_controller as cc

        cc._set_clipboard(text)
        return True

    def close_clicked(self) -> bool:
        if self.state.key == "dictating":
            self.state.cancel_requested = True  # 録音中の×＝録音キャンセル
        else:
            self.closed = True  # 監視スレッドが隠す
        return True

    def clear_pending(self) -> bool:
        """下書きバッジのクリック＝下書きを全部消す（まひろ要望 2026-07-15）。"""
        self.state.clear_pending_requested = True
        return True

    def set_expanded(self, open_: bool) -> bool:
        """履歴パネルを上方向に展開/収納。実ジオメトリは _monitor が追従する。"""
        self.expanded = bool(open_)
        return True

    def open_dashboard(self) -> bool:
        import os

        pyw = BASE / ".venv" / "Scripts" / "pythonw.exe"
        # バー用の WebView2 透過設定を子プロセスに継がせない（ダッシュボードは不透明前提）
        env = {k: v for k, v in os.environ.items()
               if k != "WEBVIEW2_DEFAULT_BACKGROUND_COLOR"}
        subprocess.Popen([str(pyw), str(BASE / "dashboard.py")], env=env)
        return True


def run_glass_bar(state: UIState) -> None:
    """メインスレッドで呼ぶ（webview.start がブロックする。tk mainloop と同じ位置）。"""
    import os

    try:
        if LOGFILE.exists() and LOGFILE.stat().st_size > 300_000:
            LOGFILE.write_text("", encoding="utf-8")
    except Exception:
        pass
    # WebView2 の既定背景を完全透過に（ブラウザプロセス生成前に必須。
    # DWMアクリルが失敗しても bar.html 側が不透明背景を描くので害はない）
    os.environ["WEBVIEW2_DEFAULT_BACKGROUND_COLOR"] = "00000000"
    x0, y0, w0, h0, _ = _geometry(False)
    _log(f"=== start === logical rect=({x0},{y0},{w0}x{h0})")

    api = BarApi(state)
    try:  # 見た目設定（config [ui]）: eye_skin=スキン / pupil_color=瞳孔 / fluid_color=旧磁性流体（互換）
        import tomllib
        with open(BASE / "config.toml", "rb") as f:
            _ui = tomllib.load(f).get("ui", {})
        api._fluid = str(_ui.get("fluid_color", api._fluid))
        api._eye = str(_ui.get("eye_skin", api._eye))
        api._pupil = str(_ui.get("pupil_color", api._pupil))
    except Exception as e:
        _log(f"config読込(ui)失敗→既定値: {e!r}")
    # hidden=False で作る: WebView2 は hidden=True だとネイティブ Handle 生成を遅延し、
    # _get_hwnd がフォールバックする。表示で作って確実に初期化 → _monitor が即隠す。
    window = webview.create_window(
        "Lucas-Bar", url=str(BASE / "web" / "bar.html"), js_api=api,
        width=w0, height=h0, x=x0, y=y0, min_size=(BAR_W, BAR_H),
        frameless=True, easy_drag=False, on_top=True,
        focus=False, shadow=False,
        background_color="#0a0c16",
    )

    def _monitor() -> None:
        hwnd = None
        for _ in range(60):  # hwnd を取れるまでリトライ（最大 ~15s）
            hwnd = _get_hwnd(window)
            if hwnd:
                break
            time.sleep(0.25)
        if not hwnd:
            _log("FATAL: hwnd を取得できず、バーを配置できない")
            return
        api._hwnd = hwnd
        _apply_noactivate(hwnd)
        threading.Thread(target=_backdrop_sampler, args=(window, api, hwnd),
                         daemon=True).start()
        _hide(hwnd)  # 起動直後は隠して待機（待機中は出さない）
        _log(f"hwnd={hwnd} 取得・初期化完了 glass=DIYサンプラ起動")

        shown: bool | None = None
        expanded_applied: bool | None = None
        beat = 0
        last_raise = 0.0
        while True:
            d = compute_draw(state, api.closed)
            want = d["show"] or api.expanded
            api._sampling = bool(want)  # 表示中だけ背後サンプラを回す
            x, y, w, h, bar_h = _geometry(api.expanded)
            try:
                if want:
                    _user32.SetWindowPos(hwnd, HWND_TOPMOST, x, y, w, h, SWP_NOACTIVATE)
                    # 他の常時最前面窓が上に乗ったら topmost帯の先頭へ入り直す
                    # （まひろ指示: 認識中のUIは全ウィンドウの最前面に固定）
                    if (time.time() - last_raise > 0.5
                            and _obscured_by_above(hwnd)):
                        _raise_topmost(hwnd)
                        last_raise = time.time()
                        _log("最前面を再主張（上に他のtopmost窓を検知）")
                    if shown is not True or expanded_applied != api.expanded:
                        _apply_pill_region(hwnd)
                        _apply_noactivate(hwnd)
                        expanded_applied = api.expanded
                    if shown is not True:
                        _show(hwnd)
                        ar = wintypes.RECT()
                        _user32.GetWindowRect(hwnd, ctypes.byref(ar))
                        _log(f"SHOW key={state.key} set=({x},{y},{w}x{h}) "
                             f"actual=({ar.left},{ar.top},{ar.right - ar.left}x{ar.bottom - ar.top})")
                        shown = True
                else:
                    if shown is not False:
                        _hide(hwnd)
                        _log(f"HIDE key={state.key}")
                        shown = False
            except Exception as e:
                _log(f"monitor loop 例外: {e!r}")
            beat += 1
            if beat % 250 == 0:  # ~30秒ごと: 生存＋実測矩形（画面内かを追跡）
                ar = wintypes.RECT()
                _user32.GetWindowRect(hwnd, ctypes.byref(ar))
                _log(f"heartbeat key={state.key} shown={shown} "
                     f"actual=({ar.left},{ar.top},{ar.right - ar.left}x{ar.bottom - ar.top})")
            time.sleep(0.12)

    threading.Thread(target=_monitor, daemon=True).start()
    webview.start()


# --- 単体デモ: python glass_bar.py demo（30秒で自動終了＝残骸防止） ---
if __name__ == "__main__":
    import math
    import os
    import sys

    st = UIState()
    if len(sys.argv) > 1 and sys.argv[1] == "demo":
        threading.Timer(45, os._exit, args=(0,)).start()  # 消し忘れ保険
        st.add_history("文字起こし", "これはpush-to-talkで入力したテキストの例だよ")
        st.add_history("対話", "ルーカス、このファイルをリファクタリングして")

        def _cycle() -> None:
            seq = [("active", 2, True), ("dictating", 0, False),
                   ("speaking", 2, True), ("dictate_proc", 0, False),
                   ("booting", 0, True), ("recording", 0, False), ("idle", 0, False)]
            i, t0 = 0, time.time()
            while True:
                key, pend, sess = seq[i % len(seq)]
                st.set(key)
                st.pending = pend
                st.session = sess
                for _ in range(20):
                    st.level = abs(math.sin((time.time() - t0) * 3.1)) * 0.8
                    time.sleep(0.1)
                i += 1

        threading.Thread(target=_cycle, daemon=True).start()
    run_glass_bar(st)
