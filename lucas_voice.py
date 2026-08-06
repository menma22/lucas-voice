"""
ルーカス音声常駐AI — 本体
==========================
PC に常駐し、まひろの声を待つ。

  「ルーカス」            → Claude Code（Lucas-CC・lucas-voice ワークスペース）を起動
  （対話中の発話）         → 下書きに溜める（自動送信しない）
  「これでOK / 送信」      → 下書きをまとめて CC に送信
  「キャンセル / クリア」  → 下書きを破棄
  「バイバイ / finish / グッジョブ」→ CC を /exit で閉じて待機に戻る

設計（2026-07-11 実測・E2Eフィードバックに基づく）:
  - 音量ベース VAD（閾値 0.005〜）で発話区間を切り出し
  - 誤発火ゲート: 有声合計 0.3s 未満は文字起こししない（物音で5秒CPUを溶かさない）
  - faster-whisper (kotoba-whisper-v2.0-faster, CPU int8) で文字化
    · ピーク 0.9 正規化 / 内蔵 VAD オフ / ビーム 5
    · 起動時にウォームアップ推論（初回だけ遅い問題への対策）
  - エコー抑制の二重化:
    · ~/.lucas-voice/speaking.lock 存在中はマイク入力を捨てる（ライブ検出）
    · ~/.lucas-voice/last_speak_end のタイムスタンプより古い開始の発話は破棄
      （文字起こし中に CC が喋った場合の取りこぼし対策）
  - 発話は処理中もキューに溜まり続け、捨てない（「認識されない」問題の根治）

起動: start_lucas.bat（コンソール表示・デバッグ用）
     start_lucas_headless.vbs（ウィンドウなし・常用）
停止: stop_lucas.bat（ヘッドレス時）/ Ctrl+C（コンソール時）
ログ: logs\\lucas_voice.log
"""
from __future__ import annotations

import datetime
import queue
import string
import sys
import threading
import time
import tomllib
from pathlib import Path

import numpy as np
import sounddevice as sd

import cc_controller as cc
from ui import UIState

BASE = Path(__file__).parent
LOCK_DIR = Path.home() / ".lucas-voice"
LOCK = LOCK_DIR / "speaking.lock"
SPEAK_END = LOCK_DIR / "last_speak_end"   # 直近のTTS再生終了時刻（mtime で判定）
LOG = BASE / "logs" / "lucas_voice.log"

SAMPLE_RATE = 16000
BLOCK = 1600  # 0.1 秒

MODEL_ALIASES = {"kotoba": "kotoba-tech/kotoba-whisper-v2.0-faster"}

HALLUCINATIONS = {
    "ご視聴ありがとうございました",
    "ありがとうございました",
    "おやすみなさい",
    "ごめん",
    "ごちそう",
    "ごちそうさまでした",
    "シュート",
    "ん",
}

_PUNCT = str.maketrans("", "", string.punctuation + "。、！？!?　 ・…「」『』")


def load_config() -> dict:
    with open(BASE / "config.toml", "rb") as f:
        return tomllib.load(f)


def log(msg: str) -> None:
    line = f"[{datetime.datetime.now():%H:%M:%S}] {msg}"
    if sys.stdout is not None:  # pythonw（ヘッドレス）では stdout が無い
        print(line, flush=True)
    try:
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def normalize(text: str) -> str:
    return text.translate(_PUNCT).strip().lower()


class Speaker:
    """常駐側の発話（起動/終了/送信の短いアック用）。

    実体は tts_engine（config.toml [tts]: voicevox=リッチ男声 / sapi=Haruka）。
    speaking.lock / last_speak_end のエコー抑制契約は tts_engine._play が同一手順で握る。
    """

    def __init__(self) -> None:
        import tts_engine

        self._tts = tts_engine
        self.dirty = False  # 直近の handle 中に喋ったか（呼び側が echo drain の判断に使う）

    def say(self, text: str) -> None:
        try:
            self._tts.speak(text)
        finally:
            self.dirty = True


def _last_speak_end() -> float:
    try:
        return SPEAK_END.stat().st_mtime
    except OSError:
        return 0.0


class Listener:
    def __init__(self, cfg: dict, state: UIState | None = None) -> None:
        self.cfg = cfg
        self.q: queue.Queue = queue.Queue()
        self.speaker = Speaker()
        self.pending: list[str] = []  # 下書き（「これでOK」で送信）
        self.state = state or UIState()  # ステータスバーUIとの共有状態
        self._base_key = "idle"
        # push-to-talk（Ctrl+Alt 長押し録音）の状態。_ptt_loop スレッドが書く
        self.ptt_down = False
        self.ptt_press_ts: float | None = None
        self.ptt_release_ts: float | None = None
        self.model = None  # load_model() で遅延ロード（起動応答性のため）
        self.ov_pipe = None  # OpenVINO WhisperPipeline（engine="openvino" 時のみ）
        self.ov_cfg = None
        self.gate = None  # 人声ゲート（Silero VAD）。雑音をwhisperに渡さない関門
        # 遅延マウント（まひろ案 2026-07-15）: アイドルでSTTを解放し、使う瞬間に復帰。
        # 実測: 解放で約950MB返り、キャッシュ済み復帰は2〜4秒（NPU）
        self._stt_lock = threading.Lock()
        self._stt_last_use = time.time()
        self.state.set("loading")

    def _load_openvino(self) -> bool:
        """OpenVINO GenAI（NPU/GPU）でSTTを構える。成功したら True。

        NPU/GPUに載せるとCPUがほぼ空く＝文字起こし中もPCが重くならない
        （まひろ要望 2026-07-14「NPU使おう・リソース活用・メモリ効率」）。
        失敗時は faster-whisper（CPU）へフォールバック——認識が死ぬことはない。
        """
        try:
            import openvino_genai

            d = self.cfg["stt"]
            device = d.get("device", "NPU")
            t0 = time.time()
            self.ov_pipe = openvino_genai.WhisperPipeline(
                d["ov_model_dir"], device,
                CACHE_DIR=str(BASE / "models" / "ov_cache"))  # コンパイル結果を再利用
            self.ov_cfg = self.ov_pipe.get_generation_config()
            self.ov_cfg.language = "<|ja|>"
            self.ov_cfg.task = "transcribe"
            # ⚠ return_timestamps=True を外すな: 長尺のシークがタイムスタンプ依存
            #   （faster-whisper で一致率95.2%→42.9%崩壊を実測した同じ罠）
            self.ov_cfg.return_timestamps = True
            log(f"OpenVINO読み込み完了 ({device}, {time.time() - t0:.1f}s) → ウォームアップ...")
            t0 = time.time()
            warm = (np.random.default_rng(0).standard_normal(SAMPLE_RATE // 2) * 0.01
                    ).astype(np.float32)
            self.ov_pipe.generate(warm.tolist(), self.ov_cfg)
            log(f"ウォームアップ完了 ({time.time() - t0:.1f}s)")
            return True
        except Exception as e:
            log(f"OpenVINO初期化失敗 → faster-whisper(CPU) へフォールバック: {e!r}")
            self.ov_pipe = None
            self.ov_cfg = None
            return False

    def load_model(self) -> None:
        """STTモデルのロード＋ウォームアップ。

        __init__ から分離した理由: マシンが重い時はロードに40秒以上かかることが
        あり（2026-07-12実測）、その間ホットキー登録もUIも死んでいると
        「起動したのに何も反応しない」に見える。ホットキー/UI を先に生かす。
        engine="openvino" 成功時は faster-whisper を一切ロードしない（二重メモリ回避）。
        """
        # 人声ゲート（2MB・常駐。まひろ要望 2026-07-15「雑音を文字起こしするな」）
        if self.gate is None and self.cfg["vad"].get("speech_gate", True):
            try:
                from speech_gate import SpeechGate

                self.gate = SpeechGate(log=log)
            except Exception as e:
                log(f"人声ゲート初期化失敗（ゲート無しで続行）: {e!r}")
        if self.cfg["stt"].get("engine", "faster-whisper") == "openvino":
            if self._load_openvino():
                return
        self._load_fw()

    def _load_fw(self) -> None:
        """faster-whisper（CPU）のロード＋ウォームアップ。"""
        model_id = MODEL_ALIASES.get(self.cfg["stt"]["model"], self.cfg["stt"]["model"])
        log(f"STTモデル読み込み中: {model_id}")
        t0 = time.time()
        from faster_whisper import WhisperModel

        # ⚠必ずローカルキャッシュ優先でロードする。repo ID 指定は毎回 HF Hub へ
        # 更新チェック通信が走り、回線やレート制限次第でロードが 3秒→173秒に
        # 化ける（2026-07-12実測）。キャッシュに無い初回だけダウンロードする。
        threads = int(self.cfg["stt"].get("cpu_threads", 0))  # 0=CT2既定(4)
        try:
            self.model = WhisperModel(model_id, device="cpu", compute_type="int8",
                                      cpu_threads=threads, local_files_only=True)
        except Exception:
            log("ローカルキャッシュに無い → ダウンロード実行（初回のみ）")
            self.model = WhisperModel(model_id, device="cpu", compute_type="int8",
                                      cpu_threads=threads)
        log(f"読み込み完了 ({time.time() - t0:.1f}s) → ウォームアップ推論中...")
        t0 = time.time()
        # ⚠完全無音(zeros)は Whisper がハルシネーションでトークンを延々生成し
        # CPU で40秒以上かかる病的ケース（2026-07-12実測）。微小ノイズ0.5秒で温める。
        warm = (np.random.default_rng(0).standard_normal(SAMPLE_RATE // 2) * 0.01
                ).astype(np.float32)
        list(self.model.transcribe(warm, language="ja", beam_size=1)[0])
        log(f"ウォームアップ完了 ({time.time() - t0:.1f}s)")

    # --- 音声入力 ---------------------------------------------------------
    def _callback(self, indata, frames, t, status) -> None:  # noqa: ANN001
        self.q.put((time.time(), indata.copy()))
        # リキッドUIの振幅用レベル（0..1）。発話rms実測0.01-0.03を1.0近辺へ写像
        rms = float(np.sqrt(np.mean(indata**2)))
        self.state.level = min(1.0, rms * 30)

    def _drain(self) -> None:
        """溜まった入力を捨てて live edge に戻る（自分の声の残響を無視する時だけ使う）。"""
        while True:
            try:
                self.q.get_nowait()
            except queue.Empty:
                return

    def _run_ptt_session(self) -> None:
        """Ctrl+Alt 長押しの録音〜認識〜貼り付けまでを完結させる。

        止まる条件は3つだけ: ①キーを離す ②バーの×（キャンセル） ③上限到達で自動確定。
        クリックや他キーでは止まらない（録音しながら貼り付け先を選べる。まひろ指示）。
        バーには経過タイマーを表示し、上限20秒前からカウントダウンする。

        速度の要: 録音しながら約20秒ごと（無音の切れ目）にチャンクを裏スレッドで
        認識する（SuperWhisper と同じ流儀）。キーを離した時に残っているのは最後の
        断片だけなので、録音がどれだけ長くても待ちは数秒で済む。
        """
        from concurrent.futures import ThreadPoolExecutor

        press = self.ptt_press_ts
        v = self.cfg["vad"]
        d = self.cfg["dictation"]
        max_sec = float(d.get("max_recording_sec", 180))
        chunk_sec = float(d.get("chunk_sec", 20))

        ex = ThreadPoolExecutor(max_workers=1)
        futures = []
        buf: list[np.ndarray] = []
        buf_dur = 0.0
        total_dur = 0.0
        last_shown = -1
        cancelled = False

        def _flush() -> None:
            nonlocal buf, buf_dur
            if not buf:
                return
            chunk = np.concatenate(buf)[:, 0]
            buf = []
            buf_dur = 0.0
            if len(chunk) / SAMPLE_RATE >= 0.3:
                futures.append(ex.submit(self.transcribe, chunk))

        while True:
            if self.state.cancel_requested:  # バーの×＝録音キャンセル
                self.state.cancel_requested = False
                cancelled = True
                break
            rel = self.ptt_release_ts
            elapsed = time.time() - (press or time.time())
            if rel is None and elapsed >= max_sec:  # 上限到達 → 自動確定
                log(f"PTT録音: 上限 {max_sec:.0f}s 到達 → 自動確定")
                self.ptt_release_ts = rel = time.time()
            if rel is None and int(elapsed) != last_shown:  # 経過タイマー表示
                last_shown = int(elapsed)
                remaining = int(max_sec - elapsed)
                if remaining <= 20:
                    self.state.set(
                        "dictating", f"録音中 {last_shown}s — あと{remaining}sで自動確定")
                else:
                    self.state.set("dictating", f"録音中 {last_shown}s — 離すと確定")

            try:
                ts, block = self.q.get(timeout=0.2)
            except queue.Empty:
                if rel is not None:  # 確定後、キューも吸い切った
                    break
                continue
            if ts < press:  # 押す前の音は含めない
                continue
            if rel is not None and ts > rel + 0.1:  # 確定後の音も含めない
                break
            buf.append(block)
            buf_dur += BLOCK / SAMPLE_RATE
            total_dur += BLOCK / SAMPLE_RATE
            # チャンク境界: 目標長を超えたら無音ブロックで切る（文の途中で切らない）
            rms = float(np.sqrt(np.mean(block**2)))
            if buf_dur >= chunk_sec and rms < v["threshold_floor"]:
                _flush()
            elif buf_dur >= chunk_sec + 10:  # 無音が来なくても強制分割
                _flush()

        self.ptt_press_ts = None
        self.ptt_release_ts = None

        if cancelled:
            for f in futures:
                f.cancel()
            ex.shutdown(wait=False)
            log("PTT録音: キャンセル（×ボタン）")
            self.state.set(self._base_key)
            self._drain()
            return

        _flush()  # 最後の断片
        ex.shutdown(wait=False)
        if not futures or total_dur < 0.3:
            self.state.set(self._base_key)
            return

        self.state.set("dictate_proc")
        t0 = time.time()
        text = "".join(f.result() for f in futures).strip()
        log(f"PTT合計 {total_dur:.1f}s → 認識完了 {time.time() - t0:.1f}s待ち "
            f"(チャンク{len(futures)}個): {text!r}")
        if text:
            cc.paste_text(text, keep_clipboard=True)
            self.state.add_history("文字起こし", text)
            self.state.flash("貼り付けたよ（クリップボードにもある）✓")
        else:
            self.state.flash("聞き取れなかった…")

    def _capture_utterance(self, wait_timeout: float | None = None,
                           simple_threshold: bool = False) -> np.ndarray | None:
        """発話 1 回分を返す（セッション用VADパス）。lock 中は聞かない。

        wait_timeout: 話し始めをこの秒数だけ待ち、来なければ None（ウェイク直後の
            「同じ息の続き」捕捉用。無指定=無限に待つ＝従来動作）。
        simple_threshold: ノイズEMA適応を使わず threshold_floor 固定で判定する。
            ウェイク発火直後はまひろが発話中のことがあり、EMAの初期値が発話音量に
            なって「話し始め」を永遠に検出できない（実装時に机上で発見）。
        """
        v = self.cfg["vad"]
        buf: list[np.ndarray] = []
        speech_started = False
        silence_start = None
        utter_start = None
        first_ts = None
        voiced_blocks = 0
        noise_ema = None  # ノイズフロアの指数移動平均（環境に自動追従）
        wait_start = time.time()

        while True:
            if self.ptt_press_ts is not None:  # PTT はウェイク待ちより優先
                return None  # run() が PTT セッションへ振り直す
            if self.state.clear_pending_requested:  # バーの下書きバッジクリック
                self.state.clear_pending_requested = False
                if self.pending:
                    log(f"下書き破棄（バーから・{len(self.pending)}件）")
                    self.pending.clear()
                    self.state.pending = 0
                    self.state.flash("下書きを消したよ")
            if (wait_timeout is not None and not speech_started
                    and time.time() - wait_start > wait_timeout):
                return None  # 続きの発話は無かった
            try:
                ts, block = self.q.get(timeout=1)
            except queue.Empty:
                continue

            if LOCK.exists():  # ルーカス/CC 発話中: 全部捨てて仕切り直し
                buf.clear()
                speech_started = False
                silence_start = None
                voiced_blocks = 0
                self.state.set("speaking")
                self._drain()
                continue

            rms = float(np.sqrt(np.mean(block**2)))

            if not speech_started:
                if self.state.key == "speaking":  # 発話が終わった → 通常表示へ
                    self.state.set(self._base_key)
                if noise_ema is None:
                    noise_ema = rms
                else:
                    noise_ema = noise_ema * 0.95 + rms * 0.05
                threshold = (v["threshold_floor"] if simple_threshold
                             else max(v["threshold_floor"], noise_ema * 3))
                if rms > threshold:
                    speech_started = True
                    utter_start = time.time()
                    first_ts = ts
                    buf.append(block)
                    voiced_blocks = 1
                    self.state.set("recording")
            else:
                buf.append(block)
                threshold = (v["threshold_floor"] if simple_threshold
                             else max(v["threshold_floor"], (noise_ema or 0) * 3))
                if rms > threshold:
                    voiced_blocks += 1
                    silence_start = None
                else:
                    if silence_start is None:
                        silence_start = time.time()
                    elif time.time() - silence_start > v["silence_sec"]:
                        break
                if time.time() - utter_start > v["max_utterance_sec"]:
                    break

        audio = np.concatenate(buf)[:, 0]
        dur = len(audio) / SAMPLE_RATE
        if dur < v["min_utterance_sec"]:
            self.state.set(self._base_key)
            return None
        if voiced_blocks * 0.1 < v["min_voiced_sec"]:  # 物音の誤発火（CPU浪費の主因）
            self.state.set(self._base_key)
            return None
        if first_ts is not None and first_ts < _last_speak_end() + 0.5:  # 遅延エコー
            log("(TTS直後のエコーを破棄)")
            self.state.set(self._base_key)
            return None
        return audio

    # --- 人声ゲート ---------------------------------------------------------
    def _speech_ok(self, audio: np.ndarray, where: str) -> tuple[bool, float]:
        """人声比率が閾値未満の録音を whisper 前に弾く。返り値 (通すか, 比率)。

        実測の分離: クリーン人声≈0.49（音量15%でも不変）/ 雑音（ノイズ・打鍵・
        ハム・低域ゴロゴロ）=0.000。既定閾値 0.1 は雑音側に大きく寄せた安全設定。
        ※動画・TVの「人の声」は音響的に人声なので通る（弾くには話者照合が必要＝次段階）。
        """
        if self.gate is None or not self.cfg["vad"].get("speech_gate", True):
            return True, -1.0
        r = self.gate.speech_ratio(audio)
        if r < float(self.cfg["vad"].get("min_speech_ratio", 0.1)):
            log(f"(人声ゲートで破棄 speech={r:.2f} / {where})")
            return False, r
        return True, r

    # --- STT --------------------------------------------------------------
    def _ensure_stt(self) -> None:
        """遅延マウント: 解放済みなら復帰させる（キャッシュ済みNPUで2〜4秒・実測）。
        ウェイク発火/PTT押下時に別スレッドで先読みされるので、通常は待ちゼロ。"""
        if self.cfg["stt"].get("engine") != "openvino":
            return
        with self._stt_lock:
            if self.ov_pipe is None:
                t0 = time.time()
                if self._load_openvino():
                    log(f"STT復帰（遅延ロード {time.time() - t0:.1f}s）")
                elif self.model is None:  # 復帰失敗の保険。認識を絶対に死なせない
                    log("OpenVINO復帰失敗 → faster-whisper(CPU) をロード")
                    self._load_fw()

    def preload_stt(self) -> None:
        """発火と同時に裏でSTTを起こす（fire-and-forget）。"""
        threading.Thread(target=self._ensure_stt, daemon=True).start()

    def _stt_janitor(self) -> None:
        """アイドルが続いたらSTTを解放してメモリを返す（まひろ案の実装）。
        解放実測: 約950MB。復帰は _ensure_stt（2〜4秒）。0分設定で常駐に戻る。"""
        idle_min = float(self.cfg["stt"].get("unload_idle_min", 15))
        if idle_min <= 0:
            return
        while True:
            time.sleep(30)
            try:
                if (self.ov_pipe is not None
                        and self._base_key == "idle"
                        and self.ptt_press_ts is None
                        and time.time() - self._stt_last_use > idle_min * 60):
                    with self._stt_lock:
                        if self.ov_pipe is not None:
                            self.ov_pipe = None
                            self.ov_cfg = None
                            import gc

                            gc.collect()
                            log(f"STT解放（アイドル{idle_min:.0f}分・メモリ返却。"
                                f"次回使用時に数秒で復帰）")
            except Exception:
                pass

    def transcribe(self, audio: np.ndarray) -> str:
        self._ensure_stt()
        self._stt_last_use = time.time()
        audio = audio / max(float(np.max(np.abs(audio))), 1e-6) * 0.9
        if self.ov_pipe is not None:  # OpenVINO 経路（NPU/GPU。CPUをほぼ使わない）
            text = str(self.ov_pipe.generate(audio.tolist(), self.ov_cfg)).strip()
        else:
            # condition_on_previous_text=False: 長い録音で誤りが後続に伝播して
            # 繰り返し・脱線する Whisper の既知問題を抑える（口述筆記の定石）
            # ⚠ without_timestamps=True を入れてはいけない: 長尺の30秒窓シークが
            # タイムスタンプ頼みのため、一致率が 95.2%→42.9% に崩壊する（2026-07-14実測）。
            segs, _ = self.model.transcribe(
                audio, language="ja",
                beam_size=self.cfg["stt"]["beam_size"], vad_filter=False,
                condition_on_previous_text=False,
            )
            text = "".join(s.text for s in segs).strip()
        if normalize(text) in {normalize(h) for h in HALLUCINATIONS}:
            return ""
        return text

    # --- 文字起こしモード（Ctrl+Alt 長押し = push-to-talk） --------------------
    def ptt_start(self) -> None:
        """_ptt_loop から呼ばれる: キーが押し込まれた＝録音開始。"""
        self.ptt_release_ts = None
        self.ptt_press_ts = time.time()
        self.ptt_down = True
        self.preload_stt()  # 録音中にSTTを裏で起こす（解放中でも待ちが出ない）
        log("PTT録音: 開始（キーを離すと確定）")
        self.state.set("dictating")

    def ptt_stop(self) -> None:
        """キーが離された＝録音確定。（上限到達で既に確定済みなら何もしない）"""
        if self.ptt_press_ts is None:
            return
        self.ptt_release_ts = time.time()
        self.ptt_down = False
        log("PTT録音: 終了 → 認識へ")

    # --- 意図分類 -----------------------------------------------------------
    def _match_word(self, text: str, words: list[str]) -> bool:
        """短い発話（15文字未満）に限定したコマンド語マッチ。"""
        n = normalize(text)
        if not n or len(n) >= 15:
            return False
        return any(normalize(w) in n for w in words)

    def _has_wake(self, text: str) -> bool:
        n = normalize(text)
        return any(normalize(w) in n for w in self.cfg["words"]["wake"])

    def _strip_wake(self, text: str) -> str:
        for w in self.cfg["words"]["wake"]:
            idx = text.find(w)
            if idx >= 0:
                return text[idx + len(w):].lstrip("、。,. ")
        return text

    # --- 発話1件の処理 -------------------------------------------------------
    def handle_text(self, text: str) -> None:
        ccfg = self.cfg["cc"]
        title = ccfg["window_title"]
        w = self.cfg["words"]
        running = cc.is_running(title)

        if running and self._match_word(text, w["farewell"]):
            log("終了ワード検出 → CC を閉じる")
            self.pending.clear()
            self.state.flash("おつかれさま ✓")
            self.speaker.say("おつかれさま。閉じるね")
            cc.stop_cc(title)
        elif running and self._match_word(text, w["cancel"]):
            log(f"下書き破棄（{len(self.pending)}件）")
            self.pending.clear()
            self.state.flash("下書きを消したよ")
            self.speaker.say("下書きを消したよ")
        elif running and self._match_word(text, w["send"]):
            if not self.pending:
                self.speaker.say("送るものがないよ")
                return
            msg = " ".join(self.pending)
            ok = cc.send_text(msg, title)
            log(f"CCへ送信({ok}): {msg!r}")
            self.pending.clear()
            if ok:
                self.state.flash("送ったよ ✓")
                self.speaker.say("送ったよ")
            else:
                self.state.flash("送信できなかった…")
                self.speaker.say("送信できなかった。ウィンドウを確認して")
        elif running:
            self.pending.append(text)
            log(f"下書きに追加（{len(self.pending)}件目）: {text!r}")
        elif self._has_wake(text):
            log("ウェイクワード検出 → CC 起動")
            self.state.session = True  # ここからバー表示開始
            self.state.set("booting")
            self.speaker.say("はい、起動するね")
            cc.start_cc(title, ccfg.get("workdir"))
            rest = self._strip_wake(text)
            if rest and len(normalize(rest)) >= 2:
                self.pending.append(rest)
                log(f"初回指示を下書きに追加: {rest!r}")
            deadline = time.time() + ccfg["boot_wait_sec"] + 30
            while time.time() < deadline and not cc.is_running(title):
                time.sleep(1)
            time.sleep(ccfg["boot_wait_sec"])  # TUI 準備待ち
            self.speaker.say("準備できたよ")
        # else: 待機中の関係ない発話は無視
        self.state.pending = len(self.pending)

    # --- メインループ --------------------------------------------------------
    def _refresh_base(self) -> None:
        """CC の有無からベース表示（idle/active）とセッションフラグを更新する。"""
        running = cc.is_running(self.cfg["cc"]["window_title"])
        self._base_key = "active" if running else "idle"
        self.state.session = running  # セッション外はバー非表示（まひろ指示）
        self.state.set(self._base_key)

    def _wake_wait(self, det) -> str:
        """待機中の耳。軽量ウェイク検出（vosk）だけを回し whisper は一切使わない。

        → 待機中に部屋の動画音声を延々認識してCPUを張り付かせ、幻聴を履歴に
          積む問題（2026-07-14実測）が構造的に消える。
        返り値: "wake"=発火 / "ptt"=PTT開始 / "session"=CCが外部起動された
        """
        det.reset()
        blocks = 0
        while True:
            if self.ptt_press_ts is not None:
                return "ptt"
            try:
                ts, block = self.q.get(timeout=1)
            except queue.Empty:
                continue
            # 自分の声（挨拶や「準備できたよ」に「ルーカス」を含む）での自己発火を防ぐ
            if LOCK.exists() or ts < _last_speak_end() + 0.5:
                det.reset()
                self._drain()
                continue
            if det.feed(block):
                return "wake"
            blocks += 1
            if blocks % 30 == 0 and cc.is_running(self.cfg["cc"]["window_title"]):
                return "session"  # 手動等でCCが開かれた → 対話ループへ

    def _on_wake(self) -> None:
        """vosk 発火からの即時起動（語尾から1秒未満で反応が始まる）。

        順序が命:
          1) バー表示＝視覚アック（即時） 2) CC起動を裏で開始
          3) 「同じ息の続き」（ルーカス、〜して の〜部分）をVADで捕捉
          4) 声のアック——先に喋ると lock が続きの発話を捨ててしまうので必ず捕捉の後
        """
        ccfg = self.cfg["cc"]
        title = ccfg["window_title"]
        log("ウェイクワード検出（vosk・即時）→ CC 起動")
        self.preload_stt()  # 同息指示の認識に間に合わせる（解放中でも裏で復帰）
        self.state.session = True
        self.state.set("booting")
        cc.start_cc(title, ccfg.get("workdir"))  # ターミナル起動は裏で進む
        audio = self._capture_utterance(wait_timeout=2.5, simple_threshold=True)
        rest = ""
        if audio is not None and self._speech_ok(audio, "同息指示")[0]:
            self.state.set("transcribing")
            t0 = time.time()
            text = self.transcribe(audio)
            log(f"同息指示 {len(audio) / SAMPLE_RATE:.1f}s → "
                f"認識 {time.time() - t0:.1f}s: {text!r}")
            rest = self._strip_wake(text).lstrip("、。,. ")
        if rest and len(normalize(rest)) >= 2:
            self.pending.append(rest)
            self.state.add_history("対話", rest)
            log(f"初回指示を下書きに追加: {rest!r}")
        self.state.set("booting")
        self.speaker.say("はい、起動するね")
        deadline = time.time() + ccfg["boot_wait_sec"] + 30
        while time.time() < deadline and not cc.is_running(title):
            time.sleep(1)
        time.sleep(ccfg["boot_wait_sec"])  # TUI 準備待ち
        self.speaker.say("準備できたよ")
        self.state.pending = len(self.pending)
        self._drain()  # 自声の残響を捨てる

    def run(self) -> None:
        log("=== ルーカス待機開始 ===")
        self.speaker.say("ルーカス、待機を開始したよ")
        self._refresh_base()
        threading.Thread(target=self._stt_janitor, daemon=True).start()

        # 軽量ウェイク検出（待機の耳）。初期化に失敗したら従来の whisper 方式で続行
        wake_det = None
        wcfg = self.cfg.get("wake", {})
        if wcfg.get("engine", "vosk") == "vosk":
            try:
                from wake_engine import WakeDetector

                wake_det = WakeDetector(
                    wcfg.get("words", ["ルーカス"]),
                    wcfg.get("model_dir") or None, SAMPLE_RATE, log)
                log("ウェイク検出: vosk 常時監視（待機中の whisper 認識は停止）")
            except Exception as e:
                log(f"vosk ウェイク初期化失敗 → whisper 方式で継続: {e!r}")

        with sd.InputStream(
            samplerate=SAMPLE_RATE, channels=1, dtype="float32",
            blocksize=BLOCK, callback=self._callback,
        ):
            self._drain()
            while True:
                if self.ptt_press_ts is not None:  # 文字起こし（PTT）セッション
                    self._run_ptt_session()
                    self._refresh_base()
                    continue
                if wake_det is not None and self._base_key == "idle":
                    r = self._wake_wait(wake_det)  # ← 待機中はここに常駐（軽量）
                    if r == "wake":
                        self._on_wake()
                    self._refresh_base()
                    continue
                # セッション中（または vosk 無し）: whisper VADパス（従来どおり）
                audio = self._capture_utterance()
                if audio is None:
                    continue
                ok, sp = self._speech_ok(audio, "対話")
                if not ok:
                    self._refresh_base()
                    continue
                self.state.set("transcribing")
                dur = len(audio) / SAMPLE_RATE
                t0 = time.time()
                text = self.transcribe(audio)
                log(f"発話 {dur:.1f}s speech={sp:.2f} → "
                    f"認識 {time.time() - t0:.1f}s: {text!r}")
                if not text:
                    self._refresh_base()
                    continue
                mode = "対話" if self._base_key == "active" else "待機"
                self.state.add_history(mode, text)
                self.speaker.dirty = False
                self.handle_text(text)
                if self.speaker.dirty:
                    self._drain()  # 自分の声の残響だけ捨てる（まひろの発話は捨てない）
                self._refresh_base()


_HOLD_VK = {"ctrl": 0x11, "alt": 0x12, "shift": 0x10}


def _parse_hold_keys(spec: str) -> list[int]:
    """"ctrl+alt" → [0x11, 0x12]（push-to-talk で押し続けるキーの組）。"""
    return [_HOLD_VK[p.strip()] for p in spec.lower().split("+")
            if p.strip() in _HOLD_VK]


def _ptt_loop(listener: Listener, spec: str) -> None:
    """push-to-talk 監視: 指定キーを全部押している間だけ録音する。

    録音を止めるのはキーを離すことだけ。長押し中のクリック・他キーでは
    止めない——録音しながらクリックで貼り付け先を選ぶのが実際の使い方
    （「他キー=キャンセル」の旧仕様は誤爆だらけで撤去。まひろFB 2026-07-12）。
    """
    import ctypes

    u = ctypes.windll.user32
    vks = _parse_hold_keys(spec)
    if not vks:
        log(f"hold_keys を解釈できない: {spec!r}（[dictation] hold_keys を確認）")
        return
    held_prev = False
    log(f"文字起こしトリガー登録: {spec} 長押し（push-to-talk）")
    while True:
        time.sleep(0.01)
        held = all(u.GetAsyncKeyState(vk) & 0x8000 for vk in vks)
        if held and not held_prev:
            listener.ptt_start()
        elif not held and held_prev:
            listener.ptt_stop()
        held_prev = held


def _start_ptt(listener: Listener, cfg: dict) -> None:
    d = cfg.get("dictation", {})
    if not d.get("enabled", True):
        return
    import threading

    threading.Thread(
        target=_ptt_loop, args=(listener, d.get("hold_keys", "ctrl+alt")),
        daemon=True,
    ).start()


def _status_dump_loop(state: UIState, cfg: dict) -> None:
    """ダッシュボード用に状態を logs/status.json へ定期出力する（疎結合IPC）。"""
    import json
    import os

    path = BASE / "logs" / "status.json"
    flag = BASE / "logs" / "flash.req"  # このファイルが置かれたらバーを3秒テスト表示
    started = time.time()
    while True:
        try:
            if flag.exists():
                flag.unlink()
                state.flash("バーはここに出るよ", 3.0)
        except Exception:
            pass
        try:
            payload = {
                "ts": time.time(),
                "pid": os.getpid(),
                "key": state.key,
                "label": state.text,
                "session": state.session,
                "pending": state.pending,
                "model": cfg["stt"]["model"],
                "uptime_sec": int(time.time() - started),
                "history": state.history[-100:],
            }
            path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        except Exception:
            pass
        time.sleep(0.7)


def _acquire_single_instance() -> bool:
    """二重起動ガード。既に別のリスナーが動いていたら False。

    実際に vbs のダブルクリックで2プロセスが同時稼働し、同じマイクを
    二重処理する事故が起きた（2026-07-11）。Windows ミューテックスで防ぐ。

    ⚠ GetLastError は use_last_error=True の WinDLL 経由で ctypes.get_last_error()
    で読むこと。windll.kernel32.GetLastError() 直呼びは ctypes 自身の API 呼び出しで
    値が汚れて 0 が返り、ガードが素通りする（2026-07-12 に実際に二重起動を許した）。
    """
    import ctypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateMutexW(None, False, "Lucas-Voice-Listener")
    return ctypes.get_last_error() != 183  # ERROR_ALREADY_EXISTS


def main() -> int:
    # DPI認識は「呼ばない」こと（重要）。DPI管理は pywebview に任せ、glass バーは論理座標＋
    # 中央下端アンカーで配置する（WinForms の中心固定スケールに耐える）。ここで認識を設定すると
    # 初期化順が変わり、pywebview の二重スケールで座標が膨れてバーが画面外へ飛ぶ（2026-07-13
    # 実測: want y=1062 → actual y=1593 のオフスクリーン）。詳細は glass_bar.py 冒頭。
    if not _acquire_single_instance():
        log("既に別のリスナーが稼働中 → このプロセスは終了")
        try:
            Speaker().say("ルーカスはもう起動してるよ")
        except Exception:
            pass
        return 1

    cfg = load_config()
    state = UIState()

    # トレイアイコン＝「起動できているか」の一次インジケータ（まひろ要望 2026-07-14）。
    # プロセス開始と同時に出す（モデル読み込み中も見える）。プロセスが死ねば消える。
    import tray as _tray

    tray_icon = _tray.start_tray(state, log)

    if not cfg.get("ui", {}).get("enabled", True):
        listener = Listener(cfg, state)
        _start_ptt(listener, cfg)  # モデルロードより先に登録（応答性）
        listener.load_model()
        _tray.set_ready(tray_icon)
        try:
            listener.run()
        except KeyboardInterrupt:
            log("終了（Ctrl+C）")
        return 0

    # UI 有効時: UI はメインスレッド、リスナーはワーカースレッドで動かす
    import threading

    def _worker() -> None:
        import pythoncom

        pythoncom.CoInitialize()  # ワーカースレッドで COM (SAPI) を使うため
        try:
            listener = Listener(cfg, state)
            _start_ptt(listener, cfg)  # モデルロードより先に登録（応答性）
            listener.load_model()
            _tray.set_ready(tray_icon)
            listener.run()
        except Exception as e:  # リスナーが死んでもバーにエラーを出す
            log(f"リスナー異常終了: {e!r}")
            state.set("error", f"エラー: {e}")

    threading.Thread(target=_worker, daemon=True).start()
    threading.Thread(target=_status_dump_loop, args=(state, cfg), daemon=True).start()

    engine = cfg.get("ui", {}).get("engine", "glass")
    try:
        if engine == "glass":
            from glass_bar import run_glass_bar

            run_glass_bar(state)  # webview（閉じられるまでブロック）
            return 0
    except Exception as e:
        log(f"グラスバー起動失敗 → クラシックUIへフォールバック: {e!r}")

    from ui import run_status_bar

    try:
        run_status_bar(state)  # tk mainloop（閉じられるまでブロック）
    except KeyboardInterrupt:
        log("終了（Ctrl+C）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
