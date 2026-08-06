"""マイク信号レベル診断: 15秒ベタ録音して1秒ごとのRMSを表示し、WAV保存＋文字起こし。

録音開始時に「ピッ」、終了時に「ピピッ」と鳴る。ビープの間に話すこと。
"""
import time
import winsound

import numpy as np
import sounddevice as sd
import soundfile as sf

SAMPLE_RATE = 16000
SECONDS = 15

winsound.Beep(1200, 300)  # 開始合図（ピッ）
print(f"録音開始（{SECONDS}秒）。この間に2〜3回、普通の声で話しかけてください...")
audio = sd.rec(int(SECONDS * SAMPLE_RATE), samplerate=SAMPLE_RATE, channels=1, dtype="float32")
sd.wait()
winsound.Beep(900, 150); winsound.Beep(900, 150)  # 終了合図（ピピッ）
audio = audio[:, 0]

print("=== 1秒ごとの RMS ===")
for i in range(SECONDS):
    seg = audio[i * SAMPLE_RATE:(i + 1) * SAMPLE_RATE]
    rms = float(np.sqrt(np.mean(seg ** 2)))
    peak = float(np.max(np.abs(seg)))
    bar = "#" * int(min(rms * 2000, 60))
    print(f"{i:2d}s rms={rms:.4f} peak={peak:.3f} {bar}")

path = r"C:\Users\mahim\lucas-voice\logs\level_test.wav"
sf.write(path, audio, SAMPLE_RATE)
print("saved:", path)

print("=== small モデルで文字起こし（閾値と無関係にパイプライン確認）===")
from faster_whisper import WhisperModel  # noqa: E402

t0 = time.time()
model = WhisperModel("small", device="cpu", compute_type="int8")
segments, _ = model.transcribe(audio, language="ja", beam_size=1, vad_filter=True)
text = "".join(s.text for s in segments).strip()
print(f"transcribe {time.time() - t0:.1f}s >>> {text!r}")
