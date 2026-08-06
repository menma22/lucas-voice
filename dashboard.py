"""
Lucas Voice ダッシュボード — ローカル管理アプリ（pywebview）
============================================================
リスナーの起動/停止・設定編集・認識ログ・稼働ログを1画面で。
リスナーとはプロセス分離（状態は logs/status.json、制御はプロセス起動/停止）。

起動: Lucas-Dashboard.vbs / バーの ◎ ボタン
"""
from __future__ import annotations

import ctypes
import json
import subprocess
import time
import tomllib
from pathlib import Path

import webview

BASE = Path(__file__).parent
CONFIG = BASE / "config.toml"
STATUS = BASE / "logs" / "status.json"
LOG = BASE / "logs" / "lucas_voice.log"
PYW = BASE / ".venv" / "Scripts" / "pythonw.exe"

# 保存時はこのテンプレートから再生成する（コメントを失わないため）
CONFIG_TEMPLATE = """[stt]
# 実測(2026-07-12, 171秒音声): large-v3-turbo=RTF0.44・一致率95% / kotoba=RTF0.65・81%
# → turbo が速さも精度も勝ち。kotoba は "kotoba" 指定で戻せる
engine = "{stt_engine}"         # "openvino"(NPU/GPU・CPU解放) / "faster-whisper"(CPU・実績)
device = "{stt_device}"         # openvino時のデバイス: NPU / GPU / CPU / AUTO
ov_model_dir = "{stt_ov_dir}"
unload_idle_min = {stt_unload}  # アイドルこの分数でSTT解放（約950MB返却・復帰2〜4秒）。0=常駐
model = "{stt_model}"
beam_size = {stt_beam}          # turbo は beam2 で精度低下なし（実測 95.2%）。kotoba に戻すなら 5 推奨
cpu_threads = {stt_threads}     # 推論スレッド数（このPCは8コア。0=ライブラリ既定の4）

[vad]
threshold_floor = {vad_floor}   # 発話検出のRMS下限（実測: 発話0.01〜0.03 / ノイズ0.0015）
silence_sec = {vad_silence}     # この秒数無音が続いたら発話終了
max_utterance_sec = {vad_max}   # 1発話の最大長（ウェイクワード用VADパス）
min_utterance_sec = {vad_min}   # これ未満の音は無視（物音対策）
min_voiced_sec = {vad_voiced}   # 有声合計がこれ未満なら文字起こししない（誤発火対策）
speech_gate = {vad_gate}        # 人声ゲート（Silero VAD）: 人の声でない録音を捨てる
min_speech_ratio = {vad_ratio}  # 人声比率がこれ未満なら破棄（実測: 人声0.49/雑音0.00）

[wake]
engine = "{wake_engine}"        # "vosk" = 軽量常時ウェイク（即発火・待機中whisper停止） / "whisper" = 旧方式
words = {wake_words}            # vosk用の厳格ウェイク語（トークン完全一致）。緩い変種は誤発火する
model_dir = "{wake_dir}"

[words]
wake = {words_wake}
farewell = {words_farewell}
send = {words_send}
cancel = {words_cancel}
# farewell/send/cancel は発話全体が短い（15文字未満）ときだけ有効

[cc]
window_title = "{cc_title}"
boot_wait_sec = {cc_wait}       # CC起動からテキスト送信可能までの待ち
workdir = "{cc_workdir}"        # CCセッションのワークスペース

[ui]
enabled = {ui_enabled}          # 最前面ステータスバー（false でバーなし運用）
engine = "{ui_engine}"          # "glass" = リキッド/グラスUI（WebView2） / "classic" = 旧tkバー
fluid_color = "{ui_fluid}"      # 旧磁性流体の色（バックアップ用に残置）
# レンズアイで色を持つのは中心のコア球だけ（旧 eye_skin は 2026-07-19 廃止・ここへ一本化）
pupil_color = "{ui_pupil}"      # 16進色 or "rainbow"。青#4fa8f5 / 赤#ff4d4d(HAL) / 金#ffc24d / 紫#b78aff / 翠#4de0b8

[tts]
engine = "{tts_engine}"         # "voicevox" = リッチ男声（ローカル） / "sapi" = 旧Haruka（確実）
fx = "{tts_fx}"                 # "jarvis" = 深み+AI感の加工 / "none" = 素の声
style = {tts_style}             # VOICEVOXスタイルID（voice_samples\\ で聴き比べて選ぶ）
speed = {tts_speed}             # 話速（1.0=標準）
url = "{tts_url}"
engine_dir = "{tts_dir}"        # run.exe の場所（自動起動用）

[dictation]
enabled = {dict_enabled}        # push-to-talk 文字起こし（カーソル位置に貼り付け＋クリップボード保持）
hold_keys = "{dict_hold}"       # このキーを押している間だけ録音、離すと確定
max_recording_sec = {dict_max}  # 録音上限（残り20秒からカウントダウン→到達で自動確定）
chunk_sec = {dict_chunk}        # 録音しながら裏で認識するチャンク長（無音の切れ目で分割）
"""


def _toml_str_list(items: list[str]) -> str:
    return "[" + ", ".join(f'"{i}"' for i in items) + "]"


def _toml_bool(b: bool) -> str:
    return "true" if b else "false"


def load_config() -> dict:
    with open(CONFIG, "rb") as f:
        return tomllib.load(f)


def write_config(cfg: dict) -> None:
    text = CONFIG_TEMPLATE.format(
        stt_engine=cfg["stt"].get("engine", "faster-whisper"),
        stt_device=cfg["stt"].get("device", "NPU"),
        stt_ov_dir=cfg["stt"].get("ov_model_dir", "").replace("\\", "\\\\"),
        stt_unload=cfg["stt"].get("unload_idle_min", 15),
        stt_model=cfg["stt"]["model"], stt_beam=cfg["stt"]["beam_size"],
        stt_threads=cfg["stt"]["cpu_threads"],
        vad_floor=cfg["vad"]["threshold_floor"], vad_silence=cfg["vad"]["silence_sec"],
        vad_max=cfg["vad"]["max_utterance_sec"], vad_min=cfg["vad"]["min_utterance_sec"],
        vad_voiced=cfg["vad"]["min_voiced_sec"],
        vad_gate=_toml_bool(cfg["vad"].get("speech_gate", True)),
        vad_ratio=cfg["vad"].get("min_speech_ratio", 0.1),
        wake_engine=cfg["wake"]["engine"],
        wake_words=_toml_str_list(cfg["wake"]["words"]),
        wake_dir=cfg["wake"]["model_dir"].replace("\\", "\\\\"),
        words_wake=_toml_str_list(cfg["words"]["wake"]),
        words_farewell=_toml_str_list(cfg["words"]["farewell"]),
        words_send=_toml_str_list(cfg["words"]["send"]),
        words_cancel=_toml_str_list(cfg["words"]["cancel"]),
        cc_title=cfg["cc"]["window_title"], cc_wait=cfg["cc"]["boot_wait_sec"],
        cc_workdir=cfg["cc"]["workdir"].replace("\\", "\\\\"),
        ui_enabled=_toml_bool(cfg["ui"]["enabled"]), ui_engine=cfg["ui"]["engine"],
        ui_fluid=cfg["ui"].get("fluid_color", "#0b0d14"),
        ui_pupil=cfg["ui"].get("pupil_color", "#4fa8f5"),
        tts_engine=cfg["tts"]["engine"], tts_fx=cfg["tts"].get("fx", "jarvis"),
        tts_style=cfg["tts"]["style"],
        tts_speed=cfg["tts"]["speed"], tts_url=cfg["tts"]["url"],
        tts_dir=cfg["tts"]["engine_dir"].replace("\\", "\\\\"),
        dict_enabled=_toml_bool(cfg["dictation"]["enabled"]),
        dict_hold=cfg["dictation"]["hold_keys"],
        dict_max=cfg["dictation"]["max_recording_sec"],
        dict_chunk=cfg["dictation"]["chunk_sec"],
    )
    CONFIG.write_text(text, encoding="utf-8")
    tomllib.loads(text)  # 生成物が壊れていたらここで例外 → 保存失敗として報告


def listener_running() -> bool:
    """リスナーの単一インスタンスミューテックスの有無で判定（速い・確実）。"""
    SYNCHRONIZE = 0x00100000
    k32 = ctypes.windll.kernel32
    h = k32.OpenMutexW(SYNCHRONIZE, False, "Lucas-Voice-Listener")
    if h:
        k32.CloseHandle(h)
        return True
    return False


class DashApi:
    def __init__(self) -> None:
        self._window: webview.Window | None = None
        self._scale = 1.0

    def get_scale(self) -> float:
        """DPIスケール（ページ側が CSS zoom で合わせる。bar と同じ実測理由）。"""
        return self._scale

    # --- ウィンドウ ---
    def close(self) -> None:
        if self._window:
            self._window.destroy()

    def minimize(self) -> None:
        if self._window:
            self._window.minimize()

    # --- ステータス ---
    def overview(self) -> dict:
        running = listener_running()
        st: dict = {}
        try:
            st = json.loads(STATUS.read_text(encoding="utf-8"))
            if time.time() - st.get("ts", 0) > 5:  # 古い残骸は無視
                st = {} if not running else st
        except Exception:
            st = {}
        cfg = load_config()
        return {
            "running": running,
            "key": st.get("key", "idle"),
            "session": st.get("session", False),
            "pending": st.get("pending", 0),
            "model": st.get("model", cfg["stt"]["model"]),
            "uptime_sec": st.get("uptime_sec"),
            "history_count": len(st.get("history", [])),
            "level": 0.35,  # ダッシュボードのオーブは常時ゆらぎ（実レベルはバー側）
        }

    def start_listener(self) -> bool:
        if listener_running():
            return True
        subprocess.Popen([str(PYW), str(BASE / "lucas_voice.py")], cwd=str(BASE))
        return True

    def stop_listener(self) -> bool:
        subprocess.run(
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
             "-File", str(BASE / "stop_lucas.ps1")],
            capture_output=True, timeout=30,
            # powershell.exe はコンソールアプリなので、窓なしの pythonw から素で
            # 起動すると新規コンソールが割り当てられ黒窓が一瞬出る（停止/再起動時）。
            creationflags=0x08000000,  # CREATE_NO_WINDOW（ヘッドレス）
        )
        return True

    def restart_listener(self) -> bool:
        self.stop_listener()
        for _ in range(20):  # ミューテックス解放待ち
            if not listener_running():
                break
            time.sleep(0.25)
        return self.start_listener()

    # --- 設定 ---
    def get_config(self) -> dict:
        return load_config()

    def save_config(self, incoming: dict) -> dict:
        try:
            cfg = load_config()  # UI に出していない項目は現行値を維持
            cfg["stt"].update(incoming.get("stt", {}))
            cfg["vad"].update(incoming.get("vad", {}))
            cfg["words"].update(incoming.get("words", {}))
            cfg["wake"].update(incoming.get("wake", {}))
            cfg["ui"].update(incoming.get("ui", {}))
            cfg["tts"].update(incoming.get("tts", {}))
            cfg["dictation"].update(incoming.get("dictation", {}))
            write_config(cfg)
            return {"ok": True}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    # --- ログ ---
    def get_history(self) -> list:
        try:
            st = json.loads(STATUS.read_text(encoding="utf-8"))
            return st.get("history", [])
        except Exception:
            return []

    def tail_log(self, lines: int = 300) -> str:
        try:
            content = LOG.read_text(encoding="utf-8", errors="replace").splitlines()
            return "\n".join(content[-int(lines):])
        except Exception:
            return ""

    def copy_text(self, text: str) -> bool:
        import cc_controller as cc

        cc._set_clipboard(text)
        return True


def _post_init(window) -> None:
    """タスクバー / Alt-Tab に Lucas アイコンを付ける（WM_SETICON）。"""
    import time as _t

    hwnd = None
    for _ in range(40):
        try:
            hwnd = int(window.native.Handle.ToInt32())
            if hwnd:
                break
        except Exception:
            hwnd = None
        _t.sleep(0.25)
    if not hwnd:
        return
    u = ctypes.windll.user32
    # IMAGE_ICON=1 / LR_LOADFROMFILE(0x10)|LR_DEFAULTSIZE(0x40)
    hicon = u.LoadImageW(None, str(BASE / "assets" / "lucas.ico"), 1, 0, 0, 0x00000050)
    if hicon:
        u.SendMessageW(hwnd, 0x0080, 0, hicon)  # ICON_SMALL
        u.SendMessageW(hwnd, 0x0080, 1, hicon)  # ICON_BIG


def main() -> None:
    # DPI認識は自分で設定しない（重要）: pywebview が create_window で勝手に認識化し、
    # WinForms が窓を中心固定でスケールする。旧方式（SetProcessDpiAwareness＋物理px＋
    # CSS zoom）はこれと二重掛けになりバーを画面外へ飛ばした前科（glass_bar.py 冒頭参照）。
    # → 論理pxで渡し、CSS zoom は 1.0（DashApi._scale の既定値）のまま。
    try:  # タスクバーで素の pythonw と混ざらないよう独自アプリIDに（グループ/ピン分離）
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(
            "Lucas.Voice.Dashboard")
    except Exception:
        pass

    api = DashApi()
    window = webview.create_window(
        "Lucas Voice", url=str(BASE / "web" / "dashboard.html"), js_api=api,
        width=1040, height=640, min_size=(860, 560),
        frameless=True, easy_drag=False, background_color="#05070f",
    )
    api._window = window
    webview.start(_post_init, window)


if __name__ == "__main__":
    main()
