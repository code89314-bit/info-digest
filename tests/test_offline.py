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
CALLS.clear()
got = ai_digest.select_with_llm(CANDS, {**LCFG, "max_per_source": 1})   # 全部 Zenn → 1件まで（スコア最高）
assert [p["link"] for p in got] == ["https://ex.com/2"], got


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

assert podcast_nlm.bitrate_for(20 * 60, 9.5) == 56      # 20分なら 61kbps 相当 → MP3 で使える 56kbps
assert podcast_nlm.bitrate_for(5 * 60, 9.5) == 64       # 短ければ上限64kbps
assert podcast_nlm.bitrate_for(90 * 60, 9.5) == 24      # 長いときは24kbpsから（収まらなければ compress が16kbpsまで下げる）
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

# ---------- onco_news：毎日アーカイブにためる・急ぎの安全性情報だけ即日・曜日テーマの回 ----------
import datetime as dt  # noqa: E402

import onco_news  # noqa: E402

NCFG = {"ai_digest": GCFG["ai_digest"],
        "onco": {"title": "がん", "webhook_env": "DISCORD_WEBHOOK_ONCO", "notice": "原文で確認",
                 "sources": [{"name": "PMDA", "url": "https://ex.com/rss"}],
                 "tags": ["新薬・承認", "治療ストラテジー", "安全性情報", "その他"],
                 "urgent": {"enabled": True, "tags": ["安全性情報"], "min_score": 4, "color": "#D50000"},
                 "avatar_url": "https://raw.githubusercontent.com/x/y/main/assets/onco.png",
                 "tag_style": {"新薬・承認": {"emoji": "💊", "color": "#43A047"}},
                 "schedule": {"mon": {"theme": "新薬", "tags": ["新薬・承認"], "emoji": "💊", "color": "#43A047"},
                              "tue": {"theme": "治療ストラテジー", "tags": ["治療ストラテジー"]},
                              "sat": {"theme": "週間まとめ", "tags": []}},
                 "episode": {"min_items": 2}, "overview": {"enabled": True, "audience": "薬剤師"}}}
ai_digest.load_config = lambda: NCFG
NPOST = []
onco_news.post_discord = lambda env, **k: NPOST.append((env, k))
ai_digest.requests.post = lambda *a, **k: FakeChat(chat_reply(
    [{"i": 0, "title_ja": "新薬Aが承認", "summary_ja": "s", "tag": "新薬・承認", "score": 3},
     {"i": 1, "title_ja": "重大な副作用の追記", "summary_ja": "s", "tag": "安全性情報", "score": 5}]))
common.save_state("onco", {})
common.save_state("onco_archive", {"items": [
    {"id": "old", "title": "Old", "link": "https://ex.com/old", "source": "S", "summary": "", "tag": "新薬・承認",
     "score": 4, "title_ja": "新薬Bが承認", "summary_ja": "s", "date": (onco_news.now_jst().date()
                                                                   - dt.timedelta(days=3)).isoformat()},
    {"id": "stale", "title": "Stale", "link": "https://ex.com/st", "source": "S", "summary": "", "tag": "新薬・承認",
     "score": 5, "title_ja": "古すぎる", "summary_ja": "s", "date": "2000-01-01"}]})
onco_news.collect_into_archive(onco_news.ai_digest.section_config("onco"), "DISCORD_WEBHOOK_ONCO", "がん")
arch = common.load_state("onco_archive")["items"]
assert {a["id"] for a in arch} == {"old", "a", "b"}, arch          # 21日より古いものは消える
assert len(NPOST) == 1 and "安全性情報（速報）" in NPOST[0][1]["content"]   # score 5 の安全性情報だけ即日
assert NPOST[0][1]["embeds"][0]["color"] == 0xD50000   # 速報は赤
assert "重大な副作用" in NPOST[0][1]["embeds"][0]["description"] and "新薬A" not in NPOST[0][1]["embeds"][0]["description"]
ep, its = onco_news.episode_items(onco_news.ai_digest.section_config("onco"), "mon")
assert ep["theme"] == "新薬" and [i["id"] for i in its] == ["old", "a"]   # スコア順、テーマの分類だけ
NPOST.clear()
ai_digest.requests.post = lambda *a, **k: FakeChat("**肺がん**\n- 今週の新薬")
onco_news.make_episode(onco_news.ai_digest.section_config("onco"), "mon", "DISCORD_WEBHOOK_ONCO", "がん")
k = NPOST[0][1]
assert "💊 **がん｜月曜：新薬**" in k["content"] and k["embeds"][0]["title"] == "💊 今週の「新薬」トピックス"
assert k["embeds"][0]["color"] == 0x43A047 and "要約: Strata" in k["embeds"][0]["footer"]["text"]
assert k["embeds"][1]["title"] == "💊 新薬・承認" and k["avatar_url"].endswith("/assets/onco.png")   # 分類の絵文字とアイコン
NPOST.clear()
onco_news.make_episode(onco_news.ai_digest.section_config("onco"), "tue", "DISCORD_WEBHOOK_ONCO", "がん")
onco_news.make_episode(onco_news.ai_digest.section_config("onco"), "sun", "DISCORD_WEBHOOK_ONCO", "がん")
assert NPOST == []   # 火曜は0件で作らない、日曜は休み
print("✅ onco_news")

# ---------- learn：テーマ帳の補充・重みつき選択・扱ったテーマは繰り返さない ----------
import random  # noqa: E402

import learn  # noqa: E402

LCFG2 = {"audience": "薬剤師", "min_pending": 3, "topics_per_seed": 2, "max_refill_calls": 3,
         "categories": [{"name": "適正使用ガイド", "weight": 1, "seeds": ["薬A"]},
                        {"name": "治療の変遷", "weight": 0, "seeds": ["がんB"]},
                        {"name": "種なし", "weight": 5, "seeds": []}],
         "llm": {"provider": "openai_compat", "model": "m", "base_url": "http://llm.local/v1"}}
TOPIC_PROMPTS = []


def topic_post(url, headers=None, json=None, timeout=None):
    TOPIC_PROMPTS.append(json["messages"][0]["content"])
    n = len(TOPIC_PROMPTS)
    return FakeChat(chat_reply.__globals__["json"].dumps({"topics": [
        {"title": f"テーマ{n}-1", "core": "核心", "research": "PMDAの適正使用ガイド"},
        {"title": f"テーマ{n}-2", "core": "核心", "research": "添付文書"},
        {"title": "researchなし"}]}, ensure_ascii=False))


ai_digest.requests.post = topic_post
st = {"catalog": [], "done": []}
added = learn.refill(LCFG2, st, random.Random(1))
assert added == 4 and len(TOPIC_PROMPTS) == 2, (added, TOPIC_PROMPTS)       # 2件ずつ×2回で min_pending を超える
assert all("薬A" in p and "既にあるテーマ" in p for p in TOPIC_PROMPTS)       # 重み0・種なしのカテゴリは使わない
assert "テーマ1-1" in TOPIC_PROMPTS[1]                                        # 2回目は既存テーマを避けるよう伝える
st["done"] = [t["id"] for t in st["catalog"][:3]]
assert learn.choose(LCFG2, st, random.Random(2))["title"] == st["catalog"][3]["title"]
st["done"].append(st["catalog"][3]["id"])
assert learn.choose(LCFG2, st, random.Random(2)) is None
LPOST = []
learn.post_discord = lambda env, **k: LPOST.append(k)
learn.episode({"podcast": {"enabled": True}}, st["catalog"][0], "DISCORD_WEBHOOK_ONCO", "学習")   # DRY_RUN: 表示だけ
assert LPOST == []
print("✅ learn")

assert ai_digest.ja_fix("转移性胰腺癌の随访と监测、推奨等级、患者分层") == "転移性膵癌のフォローアップとモニタリング、推奨等級、患者層別化"
assert ai_digest.ja_fix("据え置き・複数の薬剤") == "据え置き・複数の薬剤"   # 日本語の字はそのまま
print("✅ ja_fix")

# ---------- 区画ごとの要約・分類・動向まとめの書き方（summary_rule / tag_guide / overview.layout） ----------
one = [{"source": "s", "title": "t", "summary": "x"}]
p = ai_digest.build_prompt(one, {"summary_rule": "事実だけを1文", "tag_guide": "海外は国際", "title_rule": "助詞を省かない"}, 200)
assert "summary_ja: 事実だけを1文（120字以内）" in p and "のいずれか（海外は国際）" in p
assert "title_ja: 助詞を省かない（40字以内）" in p
p = ai_digest.build_prompt(one, {}, 200)   # 書かなければ今までどおり
assert "何が新しく、なぜ重要かを1〜2文" in p and "のいずれか\n" in p and "title_ja: 内容が分かる簡潔な見出し（40字以内）" in p
assert ai_digest.overview_layout({"layout": "箇条書きだけ"}) == "箇条書きだけ"
assert ai_digest.overview_layout({"group_by": "がん種"}).startswith("がん種ごとに **太字の見出し**")
print("✅ summary_rule / tag_guide / layout")

# ---------- note_weekly：テーマ別トピック・組み立て・機械チェック ----------
import note_weekly  # noqa: E402

common.save_state("onco_archive", {"items": [
    {"id": "n1", "title": "x", "title_ja": "FDAが新薬Xを承認", "link": "https://ex.com/1", "source": "S", "tag": "新薬・承認",
     "score": 5, "date": "2026-10-20"},
    {"id": "n2", "title": "y", "title_ja": "添付文書改訂", "link": "https://ex.com/2", "source": "PMDA", "tag": "安全性情報",
     "score": 4, "date": "2026-10-19"},
    {"id": "n3", "title": "z", "title_ja": "GL改訂", "link": "https://ex.com/3", "source": "学会", "tag": "ガイドライン",
     "score": 3, "date": "2026-10-18"},
    {"id": "old", "title": "o", "title_ja": "古い", "link": "https://ex.com/o", "source": "S", "tag": "新薬・承認",
     "score": 5, "date": "2026-10-01"}]})
ASKS = []


class FakeNB:
    def ask(self, q):
        ASKS.append(q)
        if "headline_1" in q:
            return json.dumps({"headline_1": "新薬X", "headline_2": "GL改訂", "three_lines": ["一。", "二。", "三。"],
                               "hashtags": ["#新薬X", "乳がん"]}, ensure_ascii=False)
        return ('{"topics": [{"i": 0, "theme": "新薬・承認", "heading": "新薬Xを承認", "conclusion": "FDAが新薬Xを承認しました。", "date": "2026-08-26",'
                ' "before": "記載なし", "next": "", "data": "OS中央値 12.3カ月", "source": "", "url": ""},'
                ' {"i": 2, "theme": "ガイドライン", "heading": "GL改訂", "conclusion": "' + "あ" * 45 + '。画期的です！",'
                ' "before": "", "next": "", "data": "", "source": "学会", "url": "https://ex.com/gl"}]}')

    def close(self):
        ASKS.append("closed")


NWCFG = {"style_file": "style/jibun_yakkyoku.yaml", "split_long_sentences": False, "themes_per_ask": 2,
         "out_dir": str(TMP / "drafts")}
note_weekly.find_audio = lambda *a: None   # 本物の podcast フォルダを見ない
r = note_weekly.make(NWCFG, dt.date(2026, 10, 24), dry_run=True, nb_factory=FakeNB)
assert len(ASKS) == 4 and ASKS[-1] == "closed"                       # テーマ3つ→2回＋見出し1回、最後にノート削除
assert "新薬・承認」「ガイドライン" in ASKS[0] and "古い" not in ASKS[0]   # style の順に2テーマずつ、7日より前は使わない
md = r["md"].read_text(encoding="utf-8")
assert r["md"].name == "2026-W43.md" and md.startswith("# 【週刊がん薬物療法】10/18〜10/24｜新薬X、GL改訂 ほか")
assert md.index("## 新薬・承認") < md.index("## ガイドライン") and "これまで" not in md   # 「記載なし」の行は出さない
assert "[S](https://ex.com/1)" in md                                  # 出典・URLが空なら記事のものを使う
assert md.index("この記事について") < md.index("今週（10/18〜10/24）") and "実験の場" in md   # 冒頭の注意書き
assert "<blockquote>" in r["html"].read_text(encoding="utf-8")
assert "#新薬X" in md and "##新薬X" not in md and "AIは書かない" not in md
memo = "\n".join(r["memo"])
assert "「海外」の表記がありません" in memo and "【海外】がありません" in memo
assert "今週より前の出来事（8/26）" in memo and "出典が報道（ex.com）" in memo
assert "音声版" not in md and "音声" not in r["html"].read_text(encoding="utf-8")   # podcast が無ければ節ごと省く
d2 = note_weekly.build(note_weekly.load_style(NWCFG), {"headline_1": "a", "headline_2": "b", "three_lines": [], "hashtags": []},
                       [], dt.date(2026, 10, 18), dt.date(2026, 10, 24), "2026-W43.mp3")
md2 = note_weekly.to_markdown(d2, [])
assert "「2026-W43.mp3」を埋め込み" in md2 and md2.index("音声版") < md2.index("目次を挿入")   # 音声は3行のすぐ下
assert "使わない語「画期的」" in memo and "感嘆符" in memo and "40字超" in memo
assert "<h2>ガイドライン</h2>" in r["html"].read_text(encoding="utf-8")
print("✅ note_weekly")

# 裏どり: Strata の検索語 → discover → 一次情報（当局ドメイン＋Strata が公式発表と判定したもの）をノートへ → 出典を差し替え
class VerifyNB(FakeNB):
    added = []

    def discover(self, q):
        return [{"url": "https://ex.com/news", "title": "報道"}, {"url": "https://www.fda.gov/x", "title": "FDA approves X"},
                {"url": "https://maker.example/pr.pdf", "title": "新薬X 承認取得のお知らせ"}]

    def add_urls(self, urls):
        VerifyNB.added += urls
        return urls


def fake_strata(prompt, cfg):
    if "検索語" in prompt:
        return [{"i": 0, "query": "FDA approves X"}]
    return [{"i": 0, "primary": True}]   # 記事と同じサイト（ex.com）は除くので、残りは maker.example だけ


note_weekly.strata_json = fake_strata
ASKS.clear()
r = note_weekly.make({**NWCFG, "verify": {"max_items": 1, "per_item": 2}}, dt.date(2026, 10, 24), dry_run=True,
                     nb_factory=VerifyNB)
assert VerifyNB.added == ["https://www.fda.gov/x", "https://maker.example/pr.pdf"], VerifyNB.added
assert "一次情報: https://www.fda.gov/x" in ASKS[0]                  # NotebookLM への一覧に一次情報を添える
t0 = r["doc"]["topics"][0]
assert t0["url"] == "https://www.fda.gov/x" and t0["source"] == "FDA" and t0["verified"]   # 報道URLを一次情報に差し替え
memo = "\n".join(r["memo"])
assert "「新薬Xを承認」：出典が報道" not in memo and "裏どり「FDAが新薬Xを承認」：https://www.fda.gov/x" in memo
print("✅ note_weekly verify")

# ---------- note_hoken（水曜号）：資料選び・テーマ・Q&A・チェック ----------
import note_hoken  # noqa: E402

assert note_hoken.reiwa_date("疑義解釈資料の送付について（その１３）（令和８年９月25日保険局医療課事務連絡）") == dt.date(2026, 9, 25)
assert note_hoken.reiwa_date("令和元年5月1日") == dt.date(2019, 5, 1) and note_hoken.reiwa_date("お知らせ") is None
HK_DOCS = [{"title": "疑義解釈資料の送付について（その13）", "link": "https://www.mhlw.go.jp/q13.pdf", "date": "2026-09-25",
            "source": "厚労省"},
           {"title": "第8回検討会の開催について", "link": "https://www.mhlw.go.jp/kaigi.html", "date": "2026-10-21",
            "source": "厚労省"}]
note_hoken.collect_docs = lambda cfg, today: HK_DOCS
STRATA = []


def hk_strata(prompt, cfg):
    STRATA.append(prompt)
    if "最大3件選んで" in prompt:
        return {"docs": [0, 9]}                                        # 範囲外の番号は捨てる
    return {"theme": "外来化学療法の加算", "items": [0], "questions": ["同じ月に2回算定できる？"],
            "research": "外来化学療法の通知", "query": "外来化学療法 通知"}


note_hoken.strata_obj = hk_strata
HK_ASKS = []


class HokenNB:
    def __init__(self, items):
        self.items, self.pod = items, {}

    def ask(self, q):
        HK_ASKS.append(q)
        if "問・項目を" in q:
            return '{"items": [{"doc": "その13", "no": "問1", "point": "外来腫瘍化学療法診療料の算定"}]}'
        return json.dumps({"three_lines": ["月1回に限り算定します。"], "hashtags": ["外来腫瘍化学療法診療料"], "qa": [
            {"q": "同じ月に2回算定できる？", "answer": "算定できる。", "basis": "その13 問1", "cond": "",
             "source": "厚労省", "url": "https://www.mhlw.go.jp/q13.pdf"},
            {"q": "Q. 公費は？", "answer": "対象です。", "basis": "", "cond": "記載なし", "source": "ブログ",
             "url": "https://blog.example/x"},
            {"q": "Q. 言い換え", "answer": "月1回です。", "basis": "その13 問1", "source": "厚労省",
             "url": "https://www.mhlw.go.jp/q13.pdf"},
            {"q": "Q. 答えなし", "answer": ""}]}, ensure_ascii=False)

    def research(self, query, mode="fast", fallback=None):
        HK_ASKS.append("research:" + fallback)
        return 3

    def sources(self):
        return [{"title": "その13", "url": "https://www.mhlw.go.jp/q13.pdf"}, {"title": "メモ", "url": ""}]

    def close(self):
        HK_ASKS.append("closed")


HKCFG = {"style_file": "style/hoken_kouhi.yaml", "split_long_sentences": False, "out_dir": str(TMP / "drafts")}
note_hoken.make_cover_image = lambda *a: None
r = note_hoken.make(HKCFG, dt.date(2026, 10, 28), dry_run=True, with_audio=False, nb_factory=HokenNB)
assert r["doc"]["title"] == "【保険と公費のギモン】外来化学療法の加算"
assert "[0] 2026-09-25 疑義解釈資料の送付について（その13）" in STRATA[0]
assert "[0] その13｜問1｜外来腫瘍化学療法診療料の算定" in STRATA[1]            # 資料の問からテーマを決める
assert HK_ASKS[1] == "research:外来化学療法 通知" and HK_ASKS[-1] == "closed"
assert "その13 — https://www.mhlw.go.jp/q13.pdf" in HK_ASKS[2] and "メモ —" not in HK_ASKS[2]
qa = r["doc"]["qa"]
assert [q["q"] for q in qa] == ["Q. 同じ月に2回算定できる？", "Q. 公費は？", "Q. 言い換え"] and qa[1]["cond"] == ""
memo = "\n".join(r["memo"])
assert "答え：だ・である調" in memo and "「Q. 公費は？」：根拠" in memo and "行政資料ではありません（blog.example）" in memo
assert "「Q. 言い換え」：「Q. 同じ月に2回算定できる？」と根拠が同じ" in memo
md = r["md"].read_text(encoding="utf-8")
assert md.index("今回の3行") < md.index("ここに note の目次") < md.index("## よくある疑問") < md.index("## 根拠資料")
assert "審査支払機関" in md and "- 第8回検討会" not in md
assert not common.load_state("note_hoken")                           # dry-run は使った資料を記録しない
print("✅ note_hoken")

# ---------- qa_deep（疑問深堀り）：患者情報の確認・解釈・資料・まとめ・音声の枠 ----------
import qa_deep  # noqa: E402

QA_STRATA = []


def qa_strata(prompt, cfg):
    QA_STRATA.append(prompt)
    if "送ってよいか" in prompt:
        if "山田" in prompt:
            return {"ok": False, "reasons": ["患者の氏名"], "rewrite": "70代の患者で…"}
        return {"ok": "true"}
    return {"title": "腎機能低下時のカペシタビン", "interpretation": "減量基準を知りたい。",
            "points": ["CrCl 30未満は？", "30〜50は？"], "queries": ["カペシタビン 腎機能 添付文書", "capecitabine renal"],
            "research": "カペシタビンの添付文書と腎機能別の用量"}


qa_deep.strata_obj = qa_strata
r = qa_deep.answer("山田さんのカペシタビン", {}, qid="t0", publish=False)
assert r["status"] == "blocked" and r["privacy"]["rewrite"] == "70代の患者で…" and len(QA_STRATA) == 1   # 外部には何も送らない


def broken(prompt, cfg):
    raise RuntimeError("down")


qa_deep.strata_obj = broken
assert qa_deep.privacy_check("x", {})["ok"] is False                 # Strata が答えないときは止める側
qa_deep.strata_obj = qa_strata
QA_LOG = []


class QANB:
    def __init__(self):
        self.pod, self.nb, self.srcs = {}, "nb1", [{"title": "添付文書", "url": "https://www.pmda.go.jp/pi.pdf"}]

    def research(self, query, mode="fast", fallback=None):
        QA_LOG.append("research")
        return 1

    def sources(self):
        return self.srcs

    def discover(self, q):
        return [{"url": "https://www.pmda.go.jp/pi.pdf"}, {"url": "https://blog.example/a"},
                {"url": "https://pubmed.ncbi.nlm.nih.gov/123/"}]

    def add_urls(self, urls):
        QA_LOG.append(urls)
        self.srcs += [{"title": "論文", "url": u} for u in urls]
        return urls

    def ask(self, q):
        QA_LOG.append(q)
        return json.dumps({"lead": "腎機能が落ちた患者で用量に迷う場面があります。", "three_lines": ["CrCl 30未満は禁忌です。"],
                           "unknown": "透析時のデータはありません。", "hashtags": ["カペシタビン"],
                           "sections": [
                               {"heading": "CrCl 30未満", "paragraphs": ["添付文書では禁忌です。", "減量の目安が示されている。"],
                                "bullets": ["30〜50は75%量"], "sources": [{"title": "添付文書", "url": "https://www.pmda.go.jp/pi.pdf"}]},
                               {"heading": "ブログ", "paragraphs": ["減量します。"],
                                "sources": [{"url": "https://blog.example/a"}]},
                               {"heading": "空", "paragraphs": []}],
                           "wrapup": {"heading": "結局、減量は必要か", "paragraphs": ["CrCl 30未満では使いません。"]}},
                          ensure_ascii=False)

    def close(self):
        QA_LOG.append("closed")


qa_deep.make_cover_image = lambda *a: None
r = qa_deep.answer("70代の腎機能低下患者のカペシタビン", {"out_dir": str(TMP / "qa")}, qid="t1", nb_factory=QANB, publish=False)
assert r["status"] == "done" and r["doc"]["title"] == "【ギモン深堀り】腎機能低下時のカペシタビン"
assert QA_LOG[1] == ["https://pubmed.ncbi.nlm.nih.gov/123/"]           # 一次資料のサイトだけ・既にある資料は足さない
assert "https://pubmed.ncbi.nlm.nih.gov/123/" in QA_LOG[2] and "closed" not in QA_LOG   # ノートは音声まで残す
memo = "\n".join(r["memo"])
assert "「CrCl 30未満」：だ・である調" in memo and "一次資料のサイトではない出典があります（blog.example）" in memo
assert "参考の資料が 2 件だけです" in memo and len(r["doc"]["sections"]) == 2      # 段落の無い節は落とす
md = r["md"].read_text(encoding="utf-8")
assert md.index("今回のギモン") < md.index("腎機能が落ちた") < md.index("この記事のポイント") <     md.index("ここに音声ファイル「t1.mp3」") < md.index("## CrCl 30未満") < md.index("## 結局、減量は必要か") < md.index("## 参考")
assert "※出典：[添付文書](https://www.pmda.go.jp/pi.pdf)" in md and "- 30〜50は75%量" in md
assert "## 資料で分からなかったこと" in md and "個別の患者さんへの判断を示すものではありません" in md
assert "<h2>CrCl 30未満</h2>" in qa_deep.body_html(r["doc"])
import qa_bot  # noqa: E402
assert qa_bot.summary_embeds(r)[1].title == "参考（2件）"
# 音声の枠: 1日3本、朝7:30までは2本、水曜23:00までは1本を空けておく
QCFG = {"audio_reserve": [{"until": "07:30", "n": 2}, {"weekday": 2, "until": "23:00", "n": 1}]}
assert qa_deep.audio_slots({"daily_limit": 3}, QCFG, dt.datetime(2026, 10, 23, 6, 0), used=0) == 1
assert qa_deep.audio_slots({"daily_limit": 3}, QCFG, dt.datetime(2026, 10, 23, 9, 0), used=2) == 1
assert qa_deep.audio_slots({"daily_limit": 3}, QCFG, dt.datetime(2026, 10, 28, 9, 0), used=2) == 0   # 水曜は水曜号の分
assert qa_deep.audio_slots({"daily_limit": 3}, QCFG, dt.datetime(2026, 10, 28, 23, 30), used=2) == 1
print("✅ qa_deep")

# Discordの分割ロジック
chunks = common._split_text("あ" * 50 + "\n" + "い" * 4500, 1900)
assert all(len(c) <= 1900 for c in chunks) and "".join(chunks) == "あ" * 50 + "\n" + "い" * 4500
print("✅ split")
shutil.rmtree(TMP)
