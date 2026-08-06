"""
共有TTSエンジン — リスナー(Speaker)と speak MCP の両方がここを通る
====================================================================
まひろ要望（2026-07-14）: 「リッチでかっこいい男声（JARVIS系・吹き替え級）」
→ 既定エンジンを VOICEVOX（ローカル・無料）に。SAPI(Haruka) は永久フォールバック。

設計:
  synth（エンジン別に wav bytes を生成）→ _play（共通再生）
  - _play が speaking.lock を書き、終了後に last_speak_end を touch する
    —— リスナーのエコー抑制と完全に同じ契約（この2ファイルの意味を変えるの禁止）
  - VOICEVOX が死んでいれば自動で SAPI へフォールバック（声が完全に死なない）
  - VOICEVOX エンジン(run.exe)は未起動なら自動起動（初回~20秒・以後は常駐）

設定: config.toml [tts]（engine / style / speed / url / engine_dir）
ログ: logs/tts.log（エンジン選択・フォールバック・自動起動の記録）
"""
from __future__ import annotations

import io
import json
import subprocess
import time
import tomllib
import urllib.parse
import urllib.request
from pathlib import Path

BASE = Path(__file__).parent
LOCK_DIR = Path.home() / ".lucas-voice"
LOCK = LOCK_DIR / "speaking.lock"
SPEAK_END = LOCK_DIR / "last_speak_end"
TTS_LOG = BASE / "logs" / "tts.log"

_engine_checked = 0.0  # ensure の直近成功時刻（毎回のヘルスチェックを省く）
_vv_announced = False  # このプロセスで voicevox 発話に成功したら1回だけログする


def _log(msg: str) -> None:
    try:
        with open(TTS_LOG, "a", encoding="utf-8") as f:
            f.write(f"[{time.strftime('%m-%d %H:%M:%S')}] {msg}\n")
    except Exception:
        pass


def _cfg() -> dict:
    try:
        with open(BASE / "config.toml", "rb") as f:
            return tomllib.load(f).get("tts", {})
    except Exception:
        return {}


# --- VOICEVOX ----------------------------------------------------------------

def _vv_get(url: str, timeout: float) -> bytes:
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return r.read()


def _find_run_exe(engine_dir: str) -> Path | None:
    d = Path(engine_dir)
    if not d.exists():
        return None
    direct = d / "run.exe"
    if direct.exists():
        return direct
    hits = list(d.glob("**/run.exe"))
    return hits[0] if hits else None


def ensure_voicevox(cfg: dict) -> bool:
    """エンジンが応答するか確認し、居なければ自動起動して待つ。"""
    global _engine_checked
    url = cfg.get("url", "http://127.0.0.1:50021")
    if time.time() - _engine_checked < 60:  # 直近1分以内に生存確認済みならスキップ
        return True
    try:
        _vv_get(f"{url}/version", timeout=1.5)
        _engine_checked = time.time()
        return True
    except Exception:
        pass
    run_exe = _find_run_exe(cfg.get("engine_dir", str(BASE / "voicevox_engine")))
    if not run_exe:
        _log("voicevox: run.exe が見つからない（engine_dir 未設置）→ SAPI へ")
        return False
    _log(f"voicevox: エンジン自動起動 {run_exe}")
    try:
        subprocess.Popen(
            [str(run_exe), "--host", "127.0.0.1"],
            cwd=str(run_exe.parent),
            creationflags=0x08000000,  # CREATE_NO_WINDOW（ヘッドレス）
        )
    except Exception as e:
        _log(f"voicevox: 起動失敗 {e!r}")
        return False
    for _ in range(40):  # 最大 ~40秒（初回起動は遅い）
        time.sleep(1.0)
        try:
            _vv_get(f"{url}/version", timeout=1.5)
            _engine_checked = time.time()
            _log("voicevox: エンジン起動完了")
            return True
        except Exception:
            continue
    _log("voicevox: 起動待ちタイムアウト → SAPI へ")
    return False


def _vv_render(text: str, cfg: dict, speed: float, pitch: float, intonation: float) -> bytes:
    """VOICEVOX で1発話を合成。速度/ピッチ/抑揚を明示指定できる共通レンダラ。"""
    url = cfg.get("url", "http://127.0.0.1:50021")
    style = int(cfg.get("style", 13))
    q = urllib.parse.urlencode({"text": text, "speaker": style})
    req = urllib.request.Request(f"{url}/audio_query?{q}", method="POST")
    with urllib.request.urlopen(req, timeout=10) as r:
        query = json.load(r)
    query["speedScale"] = speed
    query["pitchScale"] = pitch
    query["intonationScale"] = intonation
    req2 = urllib.request.Request(
        f"{url}/synthesis?speaker={style}",
        data=json.dumps(query).encode("utf-8"),
        headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req2, timeout=60) as r:
        return r.read()  # wav bytes


def _synth_voicevox(text: str, cfg: dict) -> bytes:
    return _vv_render(text, cfg, float(cfg.get("speed", 1.0)), 0.0, 1.0)


# --- SAPI（フォールバック） -----------------------------------------------------

def _synth_sapi(text: str) -> bytes:
    import tempfile

    try:
        import pythoncom

        pythoncom.CoInitialize()  # どのスレッドから呼ばれてもよいように（冪等）
    except Exception:
        pass
    import win32com.client

    tmp = Path(tempfile.gettempdir()) / f"lucas_sapi_{time.time_ns()}.wav"
    stream = win32com.client.Dispatch("SAPI.SpFileStream")
    fmt = win32com.client.Dispatch("SAPI.SpAudioFormat")
    fmt.Type = 34  # 22kHz 16bit mono
    stream.Format = fmt
    stream.Open(str(tmp), 3)  # 書き込み作成
    voice = win32com.client.Dispatch("SAPI.SpVoice")
    for t in voice.GetVoices():
        if "haruka" in t.GetDescription().lower():
            voice.Voice = t
            break
    voice.AudioOutputStream = stream
    voice.Speak(text)
    stream.Close()
    data = tmp.read_bytes()
    tmp.unlink(missing_ok=True)
    return data


# --- JARVIS風FX（オフライン加工・青山龍星→合成男声。2026-07-15 まひろ承認の設定） ---
# ネット不能でクローンモデルを落とせないため、VOICEVOX出力を numpy 加工して寄せる暫定。
# 採用パラメータは logs/tts_experiment.md（E2-slow）。実験の全変種もそこに記録。
_FX = dict(fac=0.98, tempo=1.14, pitch=-0.02, into=0.9,
           chorus_mix=0.32, comb_ms=6.0, comb_g=0.28, shimmer=0.06,
           lowpass=6800, rev_wet=0.20, rev_decay=0.28)


def _apply_fx(x, sr: int):
    import numpy as np
    # resample: factor<1 でピッチ+フォルマントを下げ深く大きい声に（tempoは合成側で相殺済み）
    fac = _FX["fac"]
    n = int(len(x) / fac)
    x = np.interp(np.linspace(0, len(x) - 1, n), np.arange(len(x)), x).astype(np.float32)
    # chorus: 揺れる短ディレイの重ね=合成的な厚み/艶
    t = np.arange(len(x))
    lfo = (18.0 + 3.0 * np.sin(2 * np.pi * 0.28 * t / sr)) * sr / 1000.0
    wet = np.interp(np.clip(t - lfo, 0, len(x) - 1), t, x).astype(np.float32)
    x = (1 - _FX["chorus_mix"]) * x + _FX["chorus_mix"] * wet
    # comb: 金属的な共鳴=AIっぽさ
    d = int(sr * _FX["comb_ms"] / 1000.0)
    y = x.copy()
    y[d:] += _FX["comb_g"] * x[:-d]
    x = y
    # ringshimmer: 高域の艶
    m = _FX["shimmer"]
    x = (1 - m) * x + m * (x * np.sin(2 * np.pi * 2200.0 * np.arange(len(x)) / sr)).astype(np.float32)
    # lowpass(1次IIR): 耳障りな高域を丸めて温かみ
    a = (1.0 / sr) / (1.0 / (2 * np.pi * _FX["lowpass"]) + 1.0 / sr)
    out = np.empty_like(x); acc = 0.0
    for i in range(len(x)):
        acc += a * (x[i] - acc); out[i] = acc
    x = out
    # reverb(FFT畳み込み・長文でも高速): 映画的残響
    N = int(sr * _FX["rev_decay"])
    ir = (np.random.randn(N) * np.exp(-np.linspace(0, 6, N))).astype(np.float32); ir[0] = 1.0
    nfft = 1 << ((len(x) + N - 1).bit_length())
    w = np.fft.irfft(np.fft.rfft(x, nfft) * np.fft.rfft(ir, nfft), nfft)[: len(x)].astype(np.float32)
    w /= (np.max(np.abs(w)) + 1e-9)
    x = (1 - _FX["rev_wet"]) * x + _FX["rev_wet"] * w
    return (x / (np.max(np.abs(x)) + 1e-9) * 0.95).astype(np.float32)


def _synth_voicevox_fx(text: str, cfg: dict) -> bytes:
    """JARVIS風FXを通したwavを返す。合成側で tempo/fac を先取りして体感速度を保つ。"""
    import soundfile as sf
    speed = float(cfg.get("speed", 1.0)) * _FX["tempo"] / _FX["fac"]
    wav = _vv_render(text, cfg, speed, _FX["pitch"], _FX["into"])
    data, sr = sf.read(io.BytesIO(wav), dtype="float32")
    if data.ndim > 1:
        data = data.mean(axis=1)
    y = _apply_fx(data, sr)
    buf = io.BytesIO()
    sf.write(buf, y, sr, format="WAV")
    return buf.getvalue()


# --- 共通再生（speaking.lock 契約はここだけが握る） ------------------------------

def _play(wav: bytes) -> None:
    import sounddevice as sd
    import soundfile as sf

    data, sr = sf.read(io.BytesIO(wav), dtype="float32")
    LOCK_DIR.mkdir(parents=True, exist_ok=True)
    try:
        LOCK.write_text("tts", encoding="utf-8")   # 再生中フラグ ON（STTミュート）
        sd.play(data, sr)
        sd.wait()  # 完了までブロック（speak の契約）
    finally:
        LOCK.unlink(missing_ok=True)
        try:
            SPEAK_END.write_text("", encoding="utf-8")  # mtime = 再生終了時刻
        except Exception:
            pass


def speak(text: str) -> str:
    """設定エンジンで読み上げる。VOICEVOX 不調時は SAPI に自動フォールバック。"""
    text = (text or "").strip()
    if not text:
        return "empty"
    cfg = _cfg()
    engine = cfg.get("engine", "sapi")
    if engine == "voicevox":
        try:
            if ensure_voicevox(cfg):
                if cfg.get("fx") == "jarvis":
                    wav = _synth_voicevox_fx(text, cfg)
                else:
                    wav = _synth_voicevox(text, cfg)
                global _vv_announced
                if not _vv_announced:
                    _vv_announced = True
                    _log(f"voicevox で発話（style={cfg.get('style', 13)}・このプロセス初回）")
                _play(wav)
                return "spoken"
        except Exception as e:
            global _engine_checked
            _engine_checked = 0.0  # 次回はヘルスチェックからやり直す
            _log(f"voicevox 失敗 → SAPI フォールバック: {e!r}")
    _play(_synth_sapi(text))
    return "spoken"


if __name__ == "__main__":  # 手動テスト: python tts_engine.py こんにちは
    import sys

    print(speak(" ".join(sys.argv[1:]) or "テストだよ"))
