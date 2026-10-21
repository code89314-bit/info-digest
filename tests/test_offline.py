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
drug_supply.load_config = lambda: {"drug_supply": {"watchlist": None}}
drug_supply.build_embeds(drug_supply.parse_xlsx(buf.getvalue()), "x", "u", {"watchlist": None})
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


MODELS = [{"id": "Qwen/Qwen4-14B-GGUF", "pipeline_tag": "text-generation", "likes": 900, "downloads": 5000,
           "tags": ["gguf", "license:apache-2.0"]},
          {"id": "some/asr-model", "pipeline_tag": "automatic-speech-recognition", "likes": 50}]


def ai_get(url, timeout=30):
    if "daily_papers" in url:
        return Resp(data=PAPERS)
    if "api/models" in url:
        return Resp(data=MODELS)
    return Resp(content=RSS)


ai_digest.http_get = ai_get
os.environ.pop("ANTHROPIC_API_KEY", None)
cfg = {"ai_digest": {"sources": [{"name": "Blog", "url": "https://ex.com/rss"},
                                 {"name": "HF Papers", "type": "hf_papers", "min_upvotes": 30},
                                 {"name": "HF Models", "type": "hf_models", "pipeline_tags": ["text-generation"]}],
                     "focus": "VRAM16GB以下のローカルAI", "tags": ["ローカルLLM", "その他"],
                     "fallback_keywords": ["llm", "agent"], "lookback_hours": 36}}
ai_digest.load_config = lambda: cfg
got = ai_digest.collect(cfg["ai_digest"], set())
assert [g["id"] for g in got] == ["a", "b", "hfpaper:2610.00001", "hfmodel:Qwen/Qwen4-14B-GGUF"], got
ai_digest.main()
seen = common.load_state("ai_digest")["seen"]
assert "a" in seen and "b" in seen

# Claude選定の経路をモックで確認
class FakePost:
    status_code = 200
    def json(self):
        return {"content": [{"type": "text", "text":
            '{"items":[{"i":0,"title_ja":"新しい推論モデルを発表","summary_ja":"エージェント性能が向上。","tag":"ローカルLLM"}]}'}]}

SENT = {}
def fake_post(*a, **k):
    SENT.update(k["json"])
    return FakePost()
ai_digest.requests.post = fake_post
os.environ["ANTHROPIC_API_KEY"] = "dummy"
picked = ai_digest.select_with_claude(got, cfg["ai_digest"])
assert "VRAM16GB以下のローカルAI" in SENT["messages"][0]["content"] and "ローカルLLM／その他" in SENT["messages"][0]["content"]
assert picked[0]["tag"] == "ローカルLLM"
assert picked[0]["title_ja"] == "新しい推論モデルを発表" and picked[0]["link"] == "https://ex.com/a"
for e in ai_digest.build_embeds(picked, ai_digest.get_tags(cfg["ai_digest"])):
    print(e["title"], "|", e["description"])
print("✅ ai_digest")

# ---------- ローカルLLM（openai_compat）経路 ----------
import json  # noqa: E402


class FakeChat:
    def __init__(self, content, status=200):
        self.status_code = status
        self._content = content
        self.text = content if status >= 400 else ""

    def json(self):
        return {"choices": [{"message": {"content": self._content}}]}


def chat_reply(items):
    return json.dumps({"items": items}, ensure_ascii=False)


CANDS = [{"id": f"x{n}", "title": f"記事{n}", "link": f"https://ex.com/{n}", "summary": "本" * 500,
          "source": "Zenn"} for n in range(5)]
LCFG = {"tags": ["ローカルLLM", "その他"], "max_items": 3,
        "llm": {"provider": "openai_compat", "base_url": "http://llm.local/v1/", "model": "m",
                "api_key_env": "LOCAL_LLM_API_KEY", "batch_size": 2, "summary_chars": 200,
                "extra_body": {"reasoning_budget_tokens": 1024}}}
os.environ["LOCAL_LLM_API_KEY"] = "local-key"
CALLS = []
REPLIES = [
    # バッチ1（候補0,1）: <think>付き＋範囲外の番号＋非整数の番号
    FakeChat("<think>考え中 {\"items\": []}</think>" + chat_reply(
        [{"i": 1, "title_ja": "記事1の見出し", "summary_ja": "要約1", "tag": "ローカルLLM", "score": 2},
         {"i": 9, "title_ja": "範囲外"}, {"i": "x", "title_ja": "非整数"}])),
    # バッチ2（候補2,3）: 1回目は解釈不能 → 再試行で成功。同じ見出しの重複あり
    FakeChat("すみません、JSONが書けませんでした"),
    FakeChat(chat_reply([{"i": 0, "title_ja": "記事2の見出し", "summary_ja": "要約2", "tag": "未知のタグ", "score": 5},
                         {"i": 1, "title_ja": "記事1の見出し", "summary_ja": "重複", "tag": "ローカルLLM", "score": 4}])),
    # バッチ3（候補4）: response_format 非対応のサーバー → 外して再送
    FakeChat("response_format is not supported", status=400),
    FakeChat(chat_reply([{"i": 0, "title_ja": "記事4の見出し", "summary_ja": "要約4", "tag": "ローカルLLM", "score": 3}])),
]


def fake_chat_post(url, headers=None, json=None, timeout=None):
    CALLS.append({"url": url, "headers": headers, "body": dict(json)})
    return REPLIES[len(CALLS) - 1]


ai_digest.requests.post = fake_chat_post
picked = ai_digest.select_with_llm(CANDS, LCFG)
assert CALLS[0]["url"] == "http://llm.local/v1/chat/completions", CALLS[0]["url"]
assert CALLS[0]["headers"]["Authorization"] == "Bearer local-key"
assert CALLS[0]["body"]["model"] == "m" and CALLS[0]["body"]["response_format"] == {"type": "json_object"}
assert CALLS[0]["body"]["reasoning_budget_tokens"] == 1024
prompt0 = CALLS[0]["body"]["messages"][0]["content"]
assert "[0] (Zenn) 記事0" in prompt0 and "[1] (Zenn) 記事1" in prompt0 and "記事2" not in prompt0  # 2件ずつ
assert "本" * 200 in prompt0 and "本" * 201 not in prompt0  # 本文は200字まで
assert "response_format" in CALLS[3]["body"] and "response_format" not in CALLS[4]["body"]  # 400 → 外して再送
assert len(CALLS) == 5  # バッチ1:1回 + バッチ2:再試行で2回 + バッチ3:再送で2回
# スコア順（5,4,3）、同じ見出しはスコアの高い方だけ残す（候補1は落ちる）、未知のタグは「その他」
assert [p["link"] for p in picked] == ["https://ex.com/2", "https://ex.com/3", "https://ex.com/4"], picked
assert picked[0]["tag"] == "その他" and picked[1]["summary_ja"] == "重複"
LCFG["max_items"] = 2
CALLS.clear()
assert len(ai_digest.select_with_llm(CANDS, LCFG)) == 2  # 件数上限
LCFG["max_items"] = 3


# サーバーが落ちている → None（main() はキーワード一致に落ちる）
def down(*a, **k):
    raise ai_digest.requests.ConnectionError("refused")


ai_digest.requests.post = down
assert ai_digest.select_with_llm(CANDS, LCFG) is None
# 全バッチで解釈できない → None
ai_digest.requests.post = lambda *a, **k: FakeChat("JSONではない返事")
assert ai_digest.select_with_llm(CANDS, LCFG) is None
# provider: none → LLMを使わない
assert ai_digest.select_with_llm(CANDS, {**LCFG, "llm": {"provider": "none"}}) is None
assert ai_digest.strip_think("<think>a</think>{\"items\":[]}") == "{\"items\":[]}"
assert ai_digest.strip_think("途中で切れた思考</think>{}") == "{}"
print("✅ ai_digest (openai_compat)")

# .env の読み込み（既存の環境変数は上書きしない）
envf = TMP / ".env"
envf.write_text("# コメント\nINFO_DIGEST_TEST_A=1\nINFO_DIGEST_TEST_B=\"two\"\nLOCAL_LLM_API_KEY=from-file\n",
                encoding="utf-8")
common.load_dotenv(envf)
assert os.environ["INFO_DIGEST_TEST_A"] == "1" and os.environ["INFO_DIGEST_TEST_B"] == "two"
assert os.environ["LOCAL_LLM_API_KEY"] == "local-key"
print("✅ dotenv")

# ---------- 区画（ai_global）：投稿先・既読管理は別、llm は ai_digest のものを使う ----------
GCFG = {"ai_digest": {"llm": {"provider": "openai_compat", "model": "shared-model", "base_url": "http://llm.local/v1"}},
        "ai_global": {"title": "海外AI", "webhook_env": "DISCORD_WEBHOOK_AI_GLOBAL",
                      "sources": [{"name": "Reddit", "url": "https://ex.com/rss"}], "tags": ["ローカルLLM", "その他"]}}
ai_digest.load_config = lambda: GCFG
assert ai_digest.section_config("ai_global")["llm"]["model"] == "shared-model"
POSTED = []
ai_digest.post_discord = lambda env, **k: POSTED.append((env, k["username"]))
ai_digest.requests.post = lambda *a, **k: FakeChat(chat_reply(
    [{"i": 0, "title_ja": "新しい推論モデル", "summary_ja": "要約", "tag": "ローカルLLM", "score": 4}]))
ai_digest.main("ai_global")
assert POSTED == [("DISCORD_WEBHOOK_AI_GLOBAL", "海外AI")], POSTED
assert "a" in common.load_state("ai_global")["seen"]
print("✅ ai_global")

# ---------- 429（レート制限）は待ってやり直す ----------
class R429:
    def __init__(self, code):
        self.status_code = code
        self.headers = {"Retry-After": "1"}

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(self.status_code)


SEQ, SLEPT = [429, 429, 200], []
common.requests.get = lambda *a, **k: R429(SEQ.pop(0))
common.time.sleep = lambda s: SLEPT.append(s)
assert common.http_get("https://www.reddit.com/r/x/.rss").status_code == 200
assert SLEPT == [15, 30], SLEPT
print("✅ http 429 retry")

# ---------- onco：CDATA見出し・既読化だけ（--seed）・動向まとめ・podcast の部品 ----------
assert ai_digest.clean("<![CDATA[HER2+ Maintenance Approvals]]>") == "HER2+ Maintenance Approvals"
OCFG = {"ai_digest": GCFG["ai_digest"],
        "onco": {"title": "がん", "icon": "💊", "webhook_env": "DISCORD_WEBHOOK_ONCO", "notice": "原文で確認",
                 "sources": [{"name": "PMDA", "url": "https://ex.com/rss"}], "tags": ["新薬・承認", "その他"],
                 "adopt": "承認・ガイドライン改訂", "summary_len": 150,
                 "overview": {"enabled": True, "audience": "薬剤師", "group_by": "がん種", "rules": ["海外と日本を区別"]},
                 "podcast": {"enabled": True, "min_items": 1}}}
ai_digest.load_config = lambda: OCFG
POSTED.clear()
ai_digest.post_discord = lambda env, **k: POSTED.append((env, k))
ai_digest.main("onco", seed=True)   # 既読にするだけ
assert POSTED == [] and {"a", "b"} <= set(common.load_state("onco")["seen"])

common.save_state("onco", {})
PROMPTS = []


def onco_post(url, headers=None, json=None, timeout=None):
    PROMPTS.append(json)
    if json.get("response_format"):   # 選別（JSON）
        return FakeChat(chat_reply([{"i": 0, "title_ja": "新薬Xが承認", "summary_ja": "肺がんに適応", "tag": "新薬・承認",
                                     "score": 5}]))
    return FakeChat("**肺がん**\n- 新薬Xが承認（海外）")   # 動向まとめ（文章）


ai_digest.requests.post = onco_post
PODS = []
sys.modules["podcast_nlm"] = type(sys)("podcast_nlm")
sys.modules["podcast_nlm"].run = lambda picked, pod, **k: PODS.append((len(picked), k["webhook"], k["notebook"]))
ai_digest.main("onco")
env, k = POSTED[0]
assert env == "DISCORD_WEBHOOK_ONCO" and k["content"].startswith("💊 **がん**") and "原文で確認" in k["content"]
assert k["embeds"][0]["title"] == "📋 今日の動向まとめ" and "新薬X" in k["embeds"][0]["description"]
assert "承認・ガイドライン改訂" in PROMPTS[0]["messages"][0]["content"] and "150字以内" in PROMPTS[0]["messages"][0]["content"]
assert "response_format" not in PROMPTS[1] and "海外と日本を区別" in PROMPTS[1]["messages"][0]["content"]
assert PODS == [(1, "DISCORD_WEBHOOK_ONCO", None)], PODS


# podcast.summarize: 要約と動向まとめは NotebookLM（ノートに ask）が書き、同じノートで音声を作る
class FakeNotebook:
    def __init__(self, pod, picked):
        self.asked, self.closed = [], False

    def ask(self, q):
        self.asked.append(q)
        if len(self.asked) == 1:
            return '{"items": [{"i": 0, "title_ja": "NLMの見出し", "summary_ja": "NLMの要約"}, {"i": 5, "title_ja": "x"}]}'
        return "**肺がん**\n- NLMのまとめ"

    def close(self):
        self.closed = True


NBS = []
sys.modules["podcast_nlm"].Notebook = lambda pod, picked: NBS.append(FakeNotebook(pod, picked)) or NBS[-1]
OCFG["onco"]["podcast"]["summarize"] = True
common.save_state("onco", {})
POSTED.clear()
PODS.clear()
os.environ.pop("DRY_RUN")
ai_digest.main("onco")
os.environ["DRY_RUN"] = "1"
k = POSTED[0][1]
assert "NLMのまとめ" in k["embeds"][0]["description"] and "NLMの見出し" in k["embeds"][1]["description"]
assert "NLMの要約" in k["embeds"][1]["description"]
assert "https://ex.com/a" in NBS[0].asked[0] and "海外と日本を区別" in NBS[0].asked[1]
assert PODS[0][2] is NBS[0] and len(PROMPTS) == 3   # Strata は選別だけ（動向まとめは書かない）
OCFG["onco"]["podcast"]["summarize"] = False
del sys.modules["podcast_nlm"]
print("✅ onco digest")

import podcast_nlm  # noqa: E402

assert podcast_nlm.bitrate_for(20 * 60, 9.5) == 61      # 20分なら 61kbps で 9.5MB に収まる
assert podcast_nlm.bitrate_for(5 * 60, 9.5) == 64       # 短ければ上限64kbps
assert podcast_nlm.bitrate_for(90 * 60, 9.5) == 24      # 長すぎても24kbpsより下げない（その場合は上限超えで失敗）
ledger = TMP / "audio_usage.json"
pod = {"ledger": str(ledger)}
assert podcast_nlm.used_today(pod) == 0
podcast_nlm.record_use(pod)
podcast_nlm.record_use(pod)
assert podcast_nlm.used_today(pod) == 2
txt = podcast_nlm.source_text([{"title_ja": "新薬X", "tag": "新薬・承認", "source": "PMDA", "link": "https://ex.com/x",
                                 "summary_ja": "要約", "title": "X approved", "summary": "body"}], "がん 2026")
assert "https://ex.com/x" in txt and "X approved" in txt and "新薬X" not in txt   # LLMの日本語は入れない
assert podcast_nlm.strip_citations("承認された [1]。適応は肺がん [2, 3]、[4-6]。") == "承認された。適応は肺がん、。"
assert podcast_nlm._id({"notebook": {"id": "nb1"}}, "notebook", "id") == "nb1"
assert podcast_nlm._id({"source_id": "s1"}, "source", "source_id", "id") == "s1"
print("✅ podcast parts")

# ---------- 出典違いの同じ出来事を LLM でまとめる（llm.dedupe） ----------
DCFG = {**LCFG, "max_items": 5, "llm": {**LCFG["llm"], "batch_size": 10, "dedupe": True}}
DEDUPE_SENT = []


def dedupe_post(url, headers=None, json=None, timeout=None):
    content = json["messages"][0]["content"]
    if "同じ出来事" in content:
        DEDUPE_SENT.append(content)
        return FakeChat('{"drop": [1, 7]}')   # 7 は範囲外 → 無視
    return FakeChat(chat_reply([{"i": n, "title_ja": f"見出し{n}", "summary_ja": "s", "tag": "その他", "score": 5 - n}
                                for n in range(3)]))


ai_digest.requests.post = dedupe_post
got = ai_digest.select_with_llm(CANDS, DCFG)
assert [p["title_ja"] for p in got] == ["見出し0", "見出し2"], got
assert "[1] 見出し1" in DEDUPE_SENT[0]
ai_digest.requests.post = lambda *a, **k: FakeChat(chat_reply([{"i": 0, "title_ja": "a", "summary_ja": "s"},
                                                               {"i": 1, "title_ja": "b", "summary_ja": "s"}]))
assert len(ai_digest.select_with_llm(CANDS, DCFG)) == 2   # 重複チェックが解釈できなければそのまま
print("✅ llm dedupe")

# ---------- Discord: 1メッセージの embed 合計6000字・10個までに分ける ----------
SENT_PAYLOADS = []
common._send = lambda url, p: SENT_PAYLOADS.append(p)
os.environ.pop("DRY_RUN")
os.environ["DISCORD_TEST"] = "https://example.invalid/hook"
big = [{"title": f"t{n}", "description": "あ" * 2500} for n in range(5)] + [{"title": "s", "description": "x"}] * 12
common.post_discord("DISCORD_TEST", content="見出し", embeds=big)
os.environ["DRY_RUN"] = "1"
assert all(len(p["embeds"]) <= 10 for p in SENT_PAYLOADS)
assert all(sum(len(e["title"]) + len(e["description"]) for e in p["embeds"]) <= 5800 for p in SENT_PAYLOADS)
assert sum(len(p["embeds"]) for p in SENT_PAYLOADS) == 17 and SENT_PAYLOADS[0]["content"] == "見出し"
print("✅ discord embed chunks")

# ---------- NotebookLM の回答が途中で切れても読める分を使い、残りだけ聞き直す ----------
cut = '{"items": [{"i": 0, "title_ja": "見0", "summary_ja": "要0"}, {"i": 1, "title_ja": "見1", "summary_ja": "要1"}, {"i": 2, "titl'
assert [g["i"] for g in ai_digest.parse_item_objects(cut)] == [0, 1]


class CutNotebook:
    def __init__(self):
        self.asked = []

    def ask(self, q):
        self.asked.append(q)
        return cut if len(self.asked) == 1 else '{"items": [{"i": 2, "title_ja": "見2", "summary_ja": "要2"}]}'


cn = CutNotebook()
three = [{"title": f"T{n}", "link": f"https://ex.com/{n}", "source": "S", "tag": "その他", "title_ja": "s", "summary_ja": "s"}
         for n in range(3)]
res, _ = ai_digest.summarize_with_notebooklm(cn, three, {"overview": {"enabled": False}})
assert [r["title_ja"] for r in res] == ["見0", "見1", "見2"]
assert "[0] T0" in cn.asked[0] and "[0] T0" not in cn.asked[1] and "[2] T2" in cn.asked[1]   # 2回目は残りだけ
print("✅ notebooklm partial answer")

# Discordの分割ロジック
chunks = common._split_text("あ" * 50 + "\n" + "い" * 4500, 1900)
assert all(len(c) <= 1900 for c in chunks) and "".join(chunks) == "あ" * 50 + "\n" + "い" * 4500
print("✅ split")
shutil.rmtree(TMP)
