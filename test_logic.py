"""handle_text（下書き・送信・キャンセル・終了・起動）の全シナリオをモックで検証。"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import lucas_voice as lv  # noqa: E402


class FakeSpeaker:
    def __init__(self):
        self.dirty = False
        self.said = []

    def say(self, text):
        self.said.append(text)
        self.dirty = True


class FakeCC:
    def __init__(self, running=False):
        self.running = running
        self.sent = []
        self.started = []
        self.stopped = 0

    def is_running(self, title):
        return self.running

    def send_text(self, text, title):
        self.sent.append(text)
        return True

    def start_cc(self, title, workdir=None):
        self.started.append(workdir)
        self.running = True
        return 1

    def stop_cc(self, title):
        self.stopped += 1
        self.running = False
        return True


def make(running):
    cfg = lv.load_config()
    fake_cc = FakeCC(running)
    lv.cc = fake_cc
    lv.time.sleep = lambda s: None  # 起動待ちを飛ばす
    l = lv.Listener.__new__(lv.Listener)
    l.cfg = cfg
    l.speaker = FakeSpeaker()
    l.pending = []
    l.state = lv.UIState()
    l._base_key = "idle"
    # 遅延マウント関連（実物の __init__ と同じ初期状態）
    l.ov_pipe = None
    l.ov_cfg = None
    l.model = None
    l._stt_lock = lv.threading.Lock()
    l._stt_last_use = lv.time.time()
    return l, fake_cc


# 1. 対話中の発話 → 下書きに溜まる（自動送信しない）
l, c = make(True)
l.handle_text("この関数をリファクタリングして")
l.handle_text("テストも追加してほしい")
assert c.sent == [], "自動送信されてはいけない"
assert len(l.pending) == 2
print("1. 下書き蓄積 ok")

# 2. 「これでOK」→ まとめて送信・下書きクリア
l.handle_text("これでOK")
assert c.sent == ["この関数をリファクタリングして テストも追加してほしい"]
assert l.pending == []
assert "送ったよ" in l.speaker.said
print("2. これでOK送信 ok")

# 3. 下書きが空で「送信」→ 送らない
l.handle_text("送信")
assert len(c.sent) == 1, "空下書きで送信してはいけない"
assert "送るものがないよ" in l.speaker.said
print("3. 空送信ガード ok")

# 4. 「キャンセル」→ 下書き破棄
l.handle_text("さっきの話は全部忘れて")
l.handle_text("キャンセル")
assert l.pending == []
print("4. キャンセル ok")

# 5. 長文中の「これでOK」/「グッジョブ」は発動しない
l.handle_text("これでOKかどうかを判定するテストコードを書いてほしいんだけど")
assert len(c.sent) == 1 and len(l.pending) == 1, "長文中のsend語で誤送信してはいけない"
l.handle_text("グッジョブと言いたくなるくらい良いコードにリファクタリングして")
assert c.stopped == 0, "長文中のfarewell語で誤終了してはいけない"
print("5. 長文中コマンド語の誤作動なし ok")

# 6. 「バイバイ」→ 終了・下書きクリア
l.handle_text("バイバイ")
assert c.stopped == 1 and l.pending == []
print("6. バイバイ終了 ok")

# 7. 待機中「ルーカス、TODOアプリ作って」→ workdir付き起動＋残りが下書きへ
l, c = make(False)
l.handle_text("ルーカス、TODOアプリ作って")
assert c.started == [r"C:\Users\mahim\lucas-voice"], f"workdir: {c.started}"
assert l.pending == ["TODOアプリ作って"]
print("7. ウェイク起動＋初回指示下書き ok")

# 8. 待機中の関係ない発話 → 何もしない
l, c = make(False)
l.handle_text("今日は暑いなあ")
assert c.started == [] and c.sent == [] and l.pending == []
print("8. 待機中無視 ok")

# 9. PTT（push-to-talk）状態遷移
l, c = make(False)
l.ptt_start()
assert l.ptt_down and l.ptt_press_ts is not None and l.state.key == "dictating"
l.ptt_stop()
assert not l.ptt_down and l.ptt_release_ts is not None
# 確定済み（press=None）後の stop は no-op（上限自動確定後にキーを離すケース）
l.ptt_press_ts = None
before = l.ptt_release_ts
l.ptt_stop()
assert l.ptt_release_ts == before, "確定済み後のstopは何もしない"
print("9. PTT状態遷移 ok")

# 10. hold_keys 解釈
assert lv._parse_hold_keys("ctrl+alt") == [0x11, 0x12]
assert lv._parse_hold_keys("shift") == [0x10]
assert lv._parse_hold_keys("foo") == []
print("10. hold_keys解釈 ok")

print("ALL LOGIC TESTS PASSED")
