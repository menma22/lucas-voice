"""保存済みWAVを small / kotoba の両モデル×正規化あり/なしで文字起こしして比較。

結果はコンソール文字化けを避けるため UTF-8 ファイルに書く:
  logs/transcribe_results.txt
"""
import time
from pathlib import Path

import numpy as np
import soundfile as sf
from faster_whisper import WhisperModel

WAV = Path(r"C:\Users\mahim\lucas-voice\logs\level_test.wav")
OUT = Path(r"C:\Users\mahim\lucas-voice\logs\transcribe_results.txt")

audio, sr = sf.read(WAV, dtype="float32")
peak = float(np.max(np.abs(audio)))
norm = audio / max(peak, 1e-6) * 0.9

lines = [f"wav: {WAV} peak={peak:.4f} dur={len(audio)/sr:.1f}s"]

for label, model_id in [
    ("small", "small"),
    ("kotoba", "kotoba-tech/kotoba-whisper-v2.0-faster"),
]:
    t0 = time.time()
    model = WhisperModel(model_id, device="cpu", compute_type="int8")
    load_s = time.time() - t0
    for aud_label, aud in [("raw", audio), ("normalized", norm)]:
        t0 = time.time()
        segs, _ = model.transcribe(aud, language="ja", beam_size=1, vad_filter=True)
        text = "".join(s.text for s in segs).strip()
        el = time.time() - t0
        lines.append(f"[{label} / {aud_label}] load={load_s:.1f}s transcribe={el:.1f}s")
        lines.append(f"  >>> {text}")

OUT.write_text("\n".join(lines), encoding="utf-8")
print("written:", OUT)
