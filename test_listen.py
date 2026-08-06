"""Step2 単体検証: マイク録音 → faster-whisper 日本語文字起こし。

発話待ち方式: 実行すると環境ノイズを0.5秒測定して閾値を自動調整し、
声を検出したら録音開始、1.5秒の無音で録音終了 → 文字起こしして表示。
（常駐リスナー lucas_voice.py の VAD ループの原型でもある）

使い方:
  python test_listen.py            # small モデル（疎通確認）
  python test_listen.py kotoba     # kotoba-whisper-v2.0-faster（日本語本命）
"""
from __future__ import annotations

import queue
import sys
import time
import winsound

import numpy as np
import sounddevice as sd
from faster_whisper import WhisperModel

SAMPLE_RATE = 16000
BLOCK = 1600            # 0.1 秒ブロック
SILENCE_SEC = 1.5       # この長さ無音が続いたら発話終了
MAX_UTTER_SEC = 15      # 1発話の最大長
WAIT_TIMEOUT_SEC = 60   # 発話が始まらなければ諦める

MODEL_ALIASES = {"kotoba": "kotoba-tech/kotoba-whisper-v2.0-faster"}

# Whisper が無音・雑音で出しがちな既知ハルシネーション（完全一致で破棄）
HALLUCINATIONS = {
    "ご視聴ありがとうございました",
    "ご視聴ありがとうございました。",
    "おやすみなさい",
    "ありがとうございました。",
}


def main() -> int:
    name = sys.argv[1] if len(sys.argv) > 1 else "small"
    model_id = MODEL_ALIASES.get(name, name)

    print(f"モデル読み込み中: {model_id} (CPU int8)")
    t0 = time.time()
    model = WhisperModel(model_id, device="cpu", compute_type="int8")
    print(f"読み込み完了: {time.time() - t0:.1f}s")

    q: queue.Queue = queue.Queue()

    def _cb(indata, frames, t, status):  # noqa: ANN001
        q.put(indata.copy())

    buf: list[np.ndarray] = []
    speech_started = False
    silence_start = None
    utter_start = None

    with sd.InputStream(
        samplerate=SAMPLE_RATE, channels=1, dtype="float32",
        blocksize=BLOCK, callback=_cb,
    ):
        # --- ノイズフロア測定（0.5秒）→ 閾値自動調整 ---
        # 実測(2026-07-11): 発話 rms 0.01〜0.03 / 環境ノイズ 0.0015 → 0.005 が適正下限
        noise_blocks = [q.get(timeout=5) for _ in range(5)]
        noise_floor = float(np.mean([np.sqrt(np.mean(b ** 2)) for b in noise_blocks]))
        threshold = max(0.005, noise_floor * 3)
        print(f"ノイズフロア: {noise_floor:.4f} / 発話閾値: {threshold:.4f}")
        winsound.Beep(1200, 300)  # 準備完了の合図（ピッ）——鳴ったら話してよい
        print("話しかけてください（60秒でタイムアウト）...")

        deadline = time.time() + WAIT_TIMEOUT_SEC
        while True:
            try:
                block = q.get(timeout=1)
            except queue.Empty:
                if time.time() > deadline:
                    print("タイムアウト: 音声が検出されませんでした")
                    return 1
                continue
            rms = float(np.sqrt(np.mean(block ** 2)))
            if not speech_started:
                if rms > threshold:
                    speech_started = True
                    utter_start = time.time()
                    buf.append(block)
                    print("録音開始...")
                elif time.time() > deadline:
                    print("タイムアウト: 音声が検出されませんでした")
                    return 1
            else:
                buf.append(block)
                if rms < threshold:
                    if silence_start is None:
                        silence_start = time.time()
                    elif time.time() - silence_start > SILENCE_SEC:
                        break
                else:
                    silence_start = None
                if time.time() - utter_start > MAX_UTTER_SEC:
                    break

    winsound.Beep(900, 150)  # 発話終了を検出した合図
    audio = np.concatenate(buf)[:, 0]
    dur = len(audio) / SAMPLE_RATE
    print(f"録音長: {dur:.1f}s → 文字起こし中...")

    # 発話WAVを常に保存（あとで再生・再解析できるように証拠を残す）
    import soundfile as sf

    sf.write(r"C:\Users\mahim\lucas-voice\logs\last_utterance.wav", audio, SAMPLE_RATE)

    # 実測に基づく処方箋: 正規化＋内蔵VADオフ（自前VADで区間確定済み）＋ビーム5
    audio = audio / max(float(np.max(np.abs(audio))), 1e-6) * 0.9
    t0 = time.time()
    segments, info = model.transcribe(audio, language="ja", beam_size=5, vad_filter=False)
    text = "".join(s.text for s in segments).strip()
    elapsed = time.time() - t0

    if text in HALLUCINATIONS:
        print(f"(既知ハルシネーションを破棄: {text!r})")
        text = ""

    print(f"文字起こし時間: {elapsed:.1f}s (実時間比 {elapsed / max(dur, 0.1):.2f}x)")
    print(f">>> {text!r}")
    # コンソールは cp932 で日本語が化けるため、UTF-8 ファイルにも必ず書く
    from pathlib import Path

    Path(r"C:\Users\mahim\lucas-voice\logs\listen_result.txt").write_text(
        f"model={model_id}\ndur={dur:.1f}s transcribe={elapsed:.1f}s rtf={elapsed / max(dur, 0.1):.2f}x\ntext={text}\n",
        encoding="utf-8",
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
