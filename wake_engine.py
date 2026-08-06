"""
ウェイクワード検出エンジン — 待機中の耳（軽量・即時・whisper不使用）
====================================================================
まひろ要望（2026-07-14）: 「ルーカス」という音だけに反応する軽量高速なモデルで
瞬時に起動したい。従来は全発話を large-v3-turbo で認識して文字列照合していた
——無音待ち1.5秒＋認識5〜15秒の後にしか起動できず、待機中もCPUを浪費していた。

実装: vosk（Kaldi）+ 日本語小型モデル(48MB) のストリーミング認識。
  - 0.1秒ブロックを逐次 feed し、部分認識結果にウェイク語が現れた瞬間 True
    （発話の終わりを待たない＝語尾から数百ms で発火。実測 0.2〜0.6s）
  - CPU負荷は whisper の数十分の一（実測 RTF 0.02）。待機中はこれだけが回る
    → 部屋の動画音声を延々 whisper する問題（幻聴・CPU張り付き）も構造的に消える
  - 照合は【自由認識＋トークン完全一致】。grammar モード（語彙を絞る方式）は
    候補が少ないと任意の音声を「ルーカス」に吸い込む誤発火が実測で多発したため
    不採用（「おはようございます」まで発火した）。自由認識ならフル言語モデルの
    中で「ルーカス」が全単語と競合するため、本物しか勝てない。

設定: config.toml [wake]（engine="vosk"/"whisper", model_dir, words）
※ウェイク語は厳格リスト（既定「ルーカス」のみ）。whisper 用の緩い変種
  （[words].wake の ルーカ 等）を混ぜると誤発火する——分離が正解。
"""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np

BASE = Path(__file__).parent
DEFAULT_MODEL_DIR = BASE / "models" / "vosk-model-small-ja-0.22"


def _norm(s: str) -> str:
    return s.replace(" ", "").replace("　", "").strip().lower()


class WakeDetector:
    """ストリーミングのウェイク検出器。feed(block) が True を返したら発火。"""

    def __init__(self, wake_words: list[str], model_dir: str | None = None,
                 sample_rate: int = 16000, log=print) -> None:
        import vosk

        vosk.SetLogLevel(-1)  # Kaldi の冗長ログを黙らせる
        self.words = [_norm(w) for w in wake_words if _norm(w)]
        self.rate = sample_rate
        self._log = log
        path = Path(model_dir) if model_dir else DEFAULT_MODEL_DIR
        if not path.exists():
            raise FileNotFoundError(f"vosk model not found: {path}")
        self._model = vosk.Model(str(path))
        self._make_recognizer()

    def _make_recognizer(self) -> None:
        import vosk

        # 自由認識（フル言語モデル）。grammar モードは誤発火多発で不採用（docstring参照）
        self.rec = vosk.KaldiRecognizer(self._model, self.rate)

    def reset(self) -> None:
        """認識状態を破棄（TTS再生後・発火後に呼ぶ）。"""
        try:
            self.rec.Reset()
        except Exception:
            self._make_recognizer()

    def _hit(self, text: str) -> bool:
        # トークン完全一致（voskは分かち書きで返す）。部分文字列にすると
        # 「ルールカード」等の近似音を拾うため不可（実測）
        return any(_norm(tok) in self.words for tok in text.split())

    def feed(self, block: np.ndarray) -> bool:
        """float32 モノラルブロックを1個処理。ウェイク語を聞き取ったら True。"""
        pcm = (np.clip(block[:, 0] if block.ndim > 1 else block, -1, 1)
               * 32767).astype(np.int16).tobytes()
        if self.rec.AcceptWaveform(pcm):  # 発話区切りが確定
            hit = self._hit(json.loads(self.rec.Result()).get("text", ""))
            if hit:
                self.reset()
            return hit
        # 部分結果——ここで拾うから「発話の終わり」を待たずに発火できる
        if self._hit(json.loads(self.rec.PartialResult()).get("partial", "")):
            self.reset()
            return True
        return False


if __name__ == "__main__":  # 簡易マイクデモ: python wake_engine.py
    import sounddevice as sd

    det = WakeDetector(["ルーカス", "ルカス"])
    print("マイク監視中…「ルーカス」と言うと HIT")

    def cb(indata, frames, t, status):  # noqa: ANN001
        if det.feed(indata.copy()):
            print("HIT!")

    with sd.InputStream(samplerate=16000, channels=1, dtype="float32",
                        blocksize=1600, callback=cb):
        import time as _t

        _t.sleep(60)
