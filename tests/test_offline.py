"""ネットワークなしで動作確認するテスト（DRY_RUN=1で投稿内容を表示）。

実行: DRY_RUN=1 python tests/test_offline.py
"""
import io
import os
import shutil
import sys
import tempfile
from pathlib import Path

os.environ["DRY_RUN"] = "1"
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

import common  # noqa: E402

TMP = Path(tempfile.mkdtemp())
common.STATE_DIR = TMP  # 本物のstateを汚さない


class Resp:
    def __init__(self, content=b"", data=None):
        self.content = content
        self._data = data

    def json(self):
        return self._data


# ---------- 地震 ----------
import earthquake  # noqa: E402

QUAKES = [
    {"id": "a1", "code": 551, "time": "2026/10/20 21:00:00.000",
     "issue": {"type": "DetailScale", "correct": "None"},
     "earthquake": {"time": "2026/10/20 20:58:00", "maxScale": 45, "domesticTsunami": "None",
                    "hypocenter": {"name": "千葉県北西部", "depth": 60, "magnitude": 5.4}},
     "points": [{"addr": "千葉市中央区", "scale": 45}, {"addr": "船橋市", "scale": 40},
                {"addr": "市川市", "scale": 40}, {"addr": "銚子市", "scale": 30}, {"addr": "館山市", "scale": 20}]},
    {"id": "a2", "code": 551, "time": "2026/10/20 21:05:00.000",
     "issue": {"type": "DetailScale"},
     "earthquake": {"time": "2026/10/20 21:03:00", "maxScale": 10, "domesticTsunami": "None",
                    "hypocenter": {"name": "茨城県南部", "depth": 50, "magnitude": 2.9}}, "points": []},
    {"id": "a3", "code": 552, "time": "2026/10/20 21:10:00.000", "cancelled": False,
     "areas": [{"grade": "Watch", "immediate": False, "name": "千葉県九十九里・外房"},
               {"grade": "Warning", "immediate": True, "name": "伊豆諸島"}]},
]
earthquake.http_get = lambda url, timeout=30: Resp(data=QUAKES[:1])
earthquake.main()  # 初回 → 既読化のみ
earthquake.http_get = lambda url, timeout=30: Resp(data=QUAKES)
earthquake.main()  # 2回目 → a2(震度1)は除外, a3津波は投稿
assert set(common.load_state("earthquake")["seen"]) == {"a1", "a2", "a3"}
print("✅ earthquake\n")

# ---------- 医薬品 ----------
import openpyxl  # noqa: E402
import drug_supply  # noqa: E402

wb = openpyxl.Workbook()
ws = wb.active
ws.append(["医療用医薬品供給状況（令和８年10月16日現在）"])
ws.append([])
ws.append(["No", "薬剤区分", "成分名", "規格単位", "品名", "製造販売業者名", "出荷対応の状況",
           "出荷量の状況", "限定出荷の解除見込み時期", "今回掲載時の\n更新有無"])
ws.append([1, "内用薬", "アモキシシリン水和物", "250mg1カプセル", "アモキシシリンカプセル250mg「A」", "A製薬",
           "④限定出荷（その他）", "B.出荷量減少", "2027年3月", "New"])
ws.append([2, "内用薬", "カルボシステイン", "500mg1錠", "カルボシステイン錠500mg「B」", "B薬品",
           "①通常出荷", "A.出荷量通常", "-", "New"])
ws.append([3, "注射薬", "セファゾリン", "1g1瓶", "セファゾリンNa注1g「C」", "C社",
           "⑤供給停止", "C.出荷停止", "未定", "New"])
ws.append([4, "外用薬", "ヘパリン類似物質", "0.3%1g", "ヘパリン類似物質油性クリーム", "D社",
           "②限定出荷（自社の事情）", "B.出荷量減少", "", ""])
buf = io.BytesIO()
wb.save(buf)

PAGE_HTML = ('<p><a href="/content/10800000/261016iyakuhinkyoukyu.xlsx">'
             '医療用医薬品供給状況（令和８年10月16日現在）</a></p>').encode()


def fake_get(url, timeout=30):
    return Resp(content=PAGE_HTML if url.endswith(".html") else buf.getvalue())


drug_supply.http_get = fake_get
drug_supply.load_config = lambda: {"drug_supply": {"watchlist": ["アモキシシリン"]}}
items = drug_supply.parse_xlsx(buf.getvalue())
assert len(items) == 3, items
assert items[0]["maker"] == "A製薬" and "限定出荷" in items[0]["ship"]
drug_supply.main()
drug_supply.main()  # 同じファイル → 更新なし
assert common.load_state("drug_supply")["last_url"].endswith("261016iyakuhinkyoukyu.xlsx")
print("✅ drug_supply\n")

# ---------- AI ----------
import time  # noqa: E402
import ai_digest  # noqa: E402

now = time.strftime("%a, %d %b %Y %H:%M:%S +0000", time.gmtime())
old = "Mon, 01 Jan 2024 00:00:00 +0000"
RSS = f"""<?xml version="1.0"?><rss version="2.0"><channel><title>T</title>
<item><title>Introducing a new reasoning model</title><link>https://ex.com/a</link><guid>a</guid>
<pubDate>{now}</pubDate><description>&lt;p&gt;We release a new LLM with better agent skills.&lt;/p&gt;</description></item>
<item><title>Our holiday party recap</title><link>https://ex.com/b</link><guid>b</guid><pubDate>{now}</pubDate></item>
<item><title>Old LLM post</title><link>https://ex.com/c</link><guid>c</guid><pubDate>{old}</pubDate></item>
</channel></rss>""".encode()
PAPERS = [{"paper": {"id": "2610.00001", "title": "Scaling Agents", "summary": "agents", "upvotes": 80}},
          {"paper": {"id": "2610.00002", "title": "Minor tweak", "summary": "x", "upvotes": 3}}]


def ai_get(url, timeout=30):
    return Resp(data=PAPERS) if "daily_papers" in url else Resp(content=RSS)


ai_digest.http_get = ai_get
os.environ.pop("ANTHROPIC_API_KEY", None)
cfg = {"ai_digest": {"sources": [{"name": "Blog", "url": "https://ex.com/rss"},
                                 {"name": "HF Papers", "type": "hf_papers", "min_upvotes": 30}],
                     "fallback_keywords": ["llm", "agent"], "lookback_hours": 36}}
ai_digest.load_config = lambda: cfg
got = ai_digest.collect(cfg["ai_digest"], set())
assert [g["id"] for g in got] == ["a", "b", "hfpaper:2610.00001"], got
ai_digest.main()
seen = common.load_state("ai_digest")["seen"]
assert "a" in seen and "b" in seen

# Claude選定の経路をモックで確認
class FakePost:
    status_code = 200
    def json(self):
        return {"content": [{"type": "text", "text":
            '{"items":[{"i":0,"title_ja":"新しい推論モデルを発表","summary_ja":"エージェント性能が向上。","tag":"モデル・製品"}]}'}]}

ai_digest.requests.post = lambda *a, **k: FakePost()
os.environ["ANTHROPIC_API_KEY"] = "dummy"
picked = ai_digest.select_with_claude(got, cfg["ai_digest"])
assert picked[0]["title_ja"] == "新しい推論モデルを発表" and picked[0]["link"] == "https://ex.com/a"
for e in ai_digest.build_embeds(picked):
    print(e["title"], "|", e["description"])
print("✅ ai_digest")

# Discordの分割ロジック
chunks = common._split_text("あ" * 50 + "\n" + "い" * 4500, 1900)
assert all(len(c) <= 1900 for c in chunks) and "".join(chunks) == "あ" * 50 + "\n" + "い" * 4500
print("✅ split")
shutil.rmtree(TMP)
