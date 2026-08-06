"""
ルーカス speak MCP サーバー
============================
Claude Code に登録し、ルーカス(Claude)が「声で伝えるべきこと」だけを
読み上げるためのツール `speak` を提供する。

TTS の実体は tts_engine.py（config.toml [tts] で選択）:
  - voicevox … リッチ男声（ローカルエンジン・自動起動・既定）
  - sapi     … Microsoft Haruka（フォールバック・追加インストール不要）
speaking.lock / last_speak_end のエコー抑制契約は tts_engine._play が握る。

登録方法（user スコープ・登録済み）:
  claude mcp add lucas-speak -- "C:\\Users\\mahim\\lucas-voice\\.venv\\Scripts\\python.exe" "C:\\Users\\mahim\\lucas-voice\\speak_server.py"

──────────────────────────────────────────────────────────────────────
2026-07-26 恒久修正: 「声が出ないまま無限に固まる」の根治
──────────────────────────────────────────────────────────────────────
症状: 夜間インタビュー中、最初の speak が 1800 秒無応答のまま停止。
      エラーも stderr も出ず、ルーカスが約40分間まるごと無言になった。
実測(py-spy でライブプロセスのスタックをダンプ)した真因:

    speak_server.speak → tts_engine.speak → _synth_voicevox_fx
      → `import soundfile` → `import numpy`
      → numpy/_core/multiarray の C拡張(.pyd) ロードで**停止**

    しかもこのチェーンは MainThread = asyncio イベントループ上で走っていた
    （AnyIO worker thread は idle のまま＝同期ツールが直接ループを塞いだ）。

構造的な欠陥は2つで、どちらもここで塞ぐ:
  (1) 重い依存(numpy/soundfile/sounddevice)を「最初の speak の中」で
      遅延 import していた → 起動時に前倒しする。固まるなら接続時に
      失敗として**見える**し、通話中の初回呼び出しが脆くなくなる。
  (2) 呼び出しに上限が無く、同期関数がイベントループを直接塞いでいた
      → 別スレッドで実行し、ハード期限を付ける。超えたらエラーを返して
      セッションを続行できるようにする（サーバは落とさない）。

※ tts_engine.py は意図的に無変更。speaking.lock の契約と、常駐リスナーが
   共有している再生経路には触れない（実害の出ていない箇所を触らない）。
"""
from __future__ import annotations

import asyncio
import threading
import time

from mcp.server.fastmcp import FastMCP

import tts_engine

# 1発話の上限。長文＋FX＋再生でも実測60秒台なので、その3倍を打ち切り線に置く。
# ここを超えたら「詰まっている」と断定してよい（無言のまま待ち続けない）。
SPEAK_TIMEOUT_S = 180.0

mcp = FastMCP("lucas-speak")


def _warm() -> None:
    """重い依存を"起動時に"読み込んでおく（上記 (1) の対策）。

    ここで固まった場合は MCP の接続自体が失敗するので、まひろにもルーカスにも
    「声のサーバが上がっていない」と即座に分かる。セッション中に無言化しない。
    """
    t0 = time.monotonic()
    import numpy  # noqa: F401
    import sounddevice  # noqa: F401
    import soundfile  # noqa: F401

    tts_engine._log(f"speak-server: 依存の事前読み込み完了 ({time.monotonic() - t0:.1f}s)")


@mcp.tool()
async def speak(text: str) -> str:
    """ルーカスの声(日本語)でまひろに読み上げる。

    音声対話セッションでは【毎ターン、応答の最後に必ず呼ぶ】こと。
    読み上げるのは要点1〜2文の話し言葉——結論・確認事項・完了報告・質問。
    コード・ログ・長い詳細は読み上げず画面に書く（声は要点だけ）。
    再生は完了までブロックする。
    """
    box: dict[str, str] = {}

    def worker() -> None:
        try:
            box["ok"] = tts_engine.speak(text)
        except Exception as e:  # TTS 失敗はツール結果として返し、サーバは落とさない
            box["err"] = f"{e!r}"

    # 別スレッドで実行＝イベントループを塞がない（上記 (2) の対策）。
    # daemon=True なので、万一この中で固まってもプロセス終了は妨げない。
    th = threading.Thread(target=worker, name="speak", daemon=True)
    t0 = time.monotonic()
    th.start()
    while th.is_alive() and time.monotonic() - t0 < SPEAK_TIMEOUT_S:
        await asyncio.sleep(0.05)

    if th.is_alive():
        tts_engine._log(f"speak: {SPEAK_TIMEOUT_S:.0f}秒で打ち切り（発話は完了していない）")
        return (
            f"error: speak timed out after {SPEAK_TIMEOUT_S:.0f}s "
            "(音声は再生されていない。画面のテキストで伝えること)"
        )
    if "err" in box:
        tts_engine._log(f"speak: 失敗 {box['err']}")
        return f"error: {box['err']}"
    return box.get("ok", "spoken")


if __name__ == "__main__":
    try:
        _warm()
    except Exception as e:  # 事前読み込みに失敗しても SAPI 等で喋れる可能性は残す
        tts_engine._log(f"speak-server: 事前読み込み失敗（続行） {e!r}")
    mcp.run()  # 既定 stdio トランスポート
