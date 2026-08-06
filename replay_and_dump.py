"""録音をスピーカーで再生し、VADフィルタなしでセグメント時刻付き全文ダンプ。"""
import time
from pathlib import Path

import numpy as np
import sounddevice as sd
import soundfile as sf
from faster_whisper import WhisperModel

WAV = Path(r"C:\Users\mahim\lucas-voice\logs\level_test.wav")
OUT = Path(r"C:\Users\mahim\lucas-voice\logs\dump_results.txt")

audio, sr = sf.read(WAV, dtype="float32")
peak = float(np.max(np.abs(audio)))
norm = audio / max(peak, 1e-6) * 0.9

print("再生中（15秒・音量注意）...")
sd.play(norm, sr)
sd.wait()
print("再生終了")

lines = []
model = WhisperModel("kotoba-tech/kotoba-whisper-v2.0-faster", device="cpu", compute_type="int8")
for label, kwargs in [
    ("vad_off beam5", dict(vad_filter=False, beam_size=5)),
    ("vad_on  beam5", dict(vad_filter=True, beam_size=5)),
]:
    t0 = time.time()
    segs, info = model.transcribe(norm, language="ja", **kwargs)
    seg_list = list(segs)
    lines.append(f"[{label}] transcribe={time.time()-t0:.1f}s segments={len(seg_list)}")
    for s in seg_list:
        lines.append(f"  {s.start:5.1f}-{s.end:5.1f}s  {s.text}")

OUT.write_text("\n".join(lines), encoding="utf-8")
print("written:", OUT)
