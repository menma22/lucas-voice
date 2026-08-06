"""
人声ゲート — Silero VAD で「人が喋っている音」だけを whisper に通す
====================================================================
まひろ要望（2026-07-15）: 「雑音を拾いすぎるし雑音も文字起こしされる。
ちゃんと人間が喋ってるときだけ文字起こしして」。

従来のVADは音量（RMS）しか見ない＝キーボード音・物音・環境音も閾値を超えれば
whisper に渡り、ハルシネーション文字列が下書きや履歴を汚していた。
Silero VAD（2.3MB・音声/非音声の判別モデル）を発話キャプチャ後の関門にして、
人声比率が低い録音は whisper を呼ばずに捨てる。

- 実行は onnxruntime（CPU・1発話あたり数ms）。OpenVINO は Silero の動的 If 分岐を
  変換できないため不可（2026-07-15 実測: Conv/ReduceMean の rank 不定で変換失敗）
- 音楽や動画の「人の声」は音響的に人声なので通る（それを弾くには話者照合が必要＝別段階）
- 失敗時はゲート無効で従来動作（フェイルオープン）
"""
from __future__ import annotations

from pathlib import Path

import numpy as np

BASE = Path(__file__).parent
MODEL = BASE / "models" / "silero_vad.onnx"
CHUNK = 512  # 16kHz で 32ms（Silero v5 の要求サイズ）


class SpeechGate:
    def __init__(self, model_path: str | Path = MODEL, log=print) -> None:
        import onnxruntime as ort

        opts = ort.SessionOptions()
        opts.log_severity_level = 3  # 冗長ログ抑制
        opts.intra_op_num_threads = 1  # 1発話数msの軽量処理。whisper/CCと競合させない
        self._s = ort.InferenceSession(str(model_path), opts,
                                       providers=["CPUExecutionProvider"])
        names = [i.name for i in self._s.get_inputs()]
        # v5系: input/state/sr。名前が変わっても型と形で解決できるようにしておく
        self._in_audio = "input" if "input" in names else names[0]
        self._in_state = "state" if "state" in names else names[1]
        self._in_sr = "sr" if "sr" in names else names[-1]
        log("人声ゲート準備完了（Silero VAD / onnxruntime CPU）")

    def speech_ratio(self, audio: np.ndarray, sr: int = 16000) -> float:
        """録音全体のうち「人声」と判定されたチャンクの比率 (0..1)。

        ⚠ 各チャンクの頭に直前チャンク末尾64サンプルの「コンテキスト」を連結して
        512+64=576 サンプルで渡すこと（公式ラッパーと同じ）。これを省くと実声でも
        確率がほぼ0に張り付く（2026-07-15 実測: max 0.037 → 連結で 1.000）。
        """
        audio = np.asarray(audio, dtype=np.float32).flatten()
        n = len(audio) // CHUNK
        if n == 0:
            return 0.0
        state = np.zeros((2, 1, 128), dtype=np.float32)
        ctx = np.zeros(64, dtype=np.float32)
        sr64 = np.array(sr, dtype=np.int64)
        hits = 0
        for i in range(n):
            chunk = audio[i * CHUNK:(i + 1) * CHUNK]
            inp = np.concatenate([ctx, chunk]).reshape(1, CHUNK + 64)
            prob, state = self._s.run(
                None, {self._in_audio: inp, self._in_state: state,
                       self._in_sr: sr64})
            ctx = chunk[-64:]
            if float(np.reshape(prob, -1)[0]) > 0.5:
                hits += 1
        return hits / n


if __name__ == "__main__":  # 簡易テスト: python speech_gate.py
    g = SpeechGate()
    rng = np.random.default_rng(0)
    silence = np.zeros(16000 * 3, dtype=np.float32)
    noise = (rng.standard_normal(16000 * 3) * 0.05).astype(np.float32)
    clicks = np.zeros(16000 * 3, dtype=np.float32)
    clicks[::4000] = 0.8  # 打鍵っぽいインパルス列
    for name, a in [("無音", silence), ("ホワイトノイズ", noise), ("打鍵風", clicks)]:
        print(f"{name}: speech_ratio={g.speech_ratio(a):.3f}")
