"""Discord で Strata と会話する（検索つき）。Bot 本体は src/qa_bot.py（同じ Bot が別チャンネルで受ける）。

流れ: 会話の流れを見て、検索が要るか・検索語を Strata に決めさせる（考えさせない・JSON）
      → 自宅の SearXNG（config の strata_chat.searxng_url）で検索 → 上位のページ本文を少し読む
      → ペルソナ（style/persona.yaml）として、検索結果をもとに答える → 出典を添える。
検索語は外部の検索エンジンに送られるので、患者・職員を特定しうる語は検索語に入れさせない。
"""
import html
import json
import os
import re
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urlparse

import requests
import yaml

import ai_digest
from ai_digest import LLMError, llm_settings, ja_fix, strip_think
from common import ROOT

SECTION = "strata_chat"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Safari/537.36"

PLAN_PROMPT = """あなたは会話アシスタントの下調べ係です。下の会話の最後の発言に答えるために、ウェブ検索が要るかを決めてください。

- 事実・数値・最新の情報・薬・制度・製品・ニュースなどを聞かれたら search: true。
  あいさつ・雑談・会話の中だけで答えられることなら search: false。
- queries: 検索語を1〜2個（日本語。英語の方が見つかりやすい話題なら英語も可）。短く、検索エンジン向けの語の並びにする。
- 患者・職員の氏名・ID・生年月日・住所・病院名・具体的な日付など、人を特定しうる語は検索語に絶対に入れない。
  そうした情報が発言に含まれていたら、一般的な語に置き換える（例「山田さん 80歳 ワルファリン」→「高齢者 ワルファリン 用量」）。

会話:
{history}

JSONのみを出力: {{"search": true, "queries": ["..."]}}"""

ANSWER_RULES = """
# 検索結果の使い方
- 下の「検索結果」は、いまウェブで調べた内容です。事実・数値・用量はここに書いてあることを優先し、どの資料かが分かるように書く（「PMDA の添付文書では〜」）。
- 検索結果に無いことを、調べたかのように書かない。分からなければ分からないと言う。
- 出典の一覧は最後にこちらで付けるので、本文に URL を並べなくてよい。
- Discord のチャットなので、長くても 600 字程度。見出しは使わず、必要なら短い箇条書き。
"""


class _Text(HTMLParser):
    """HTML から本文らしい文字だけを取り出す（script・style・nav などは捨てる）。"""
    SKIP = {"script", "style", "noscript", "nav", "header", "footer", "aside", "form", "svg"}

    def __init__(self):
        super().__init__()
        self.out, self.depth = [], 0

    def handle_starttag(self, tag, attrs):
        if tag in self.SKIP:
            self.depth += 1

    def handle_endtag(self, tag):
        if tag in self.SKIP and self.depth:
            self.depth -= 1

    def handle_data(self, data):
        if not self.depth and data.strip():
            self.out.append(data.strip())


def section_config() -> dict:
    return ai_digest.section_config(SECTION)


def load_persona(cfg: dict) -> dict:
    path = ROOT / (cfg.get("persona_file") or "style/persona.yaml")
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


def llm(cfg: dict, **over) -> dict:
    return {**llm_settings({"llm": cfg.get("llm")}), **over}


def chat(messages: list[dict], opts: dict) -> str:
    """POST /chat/completions（メッセージ列をそのまま渡す版）。"""
    url = opts["base_url"].rstrip("/") + "/chat/completions"
    headers = {"content-type": "application/json"}
    key = os.environ.get(opts["api_key_env"]) if opts.get("api_key_env") else None
    if key:
        headers["Authorization"] = f"Bearer {key}"
    body = {"model": opts.get("model"), "messages": messages, "temperature": opts["temperature"],
            "max_tokens": opts["max_tokens"], "stream": False, **(opts.get("extra_body") or {})}
    if opts.get("json_mode"):
        body["response_format"] = {"type": "json_object"}
    try:
        r = requests.post(url, headers=headers, json=body, timeout=opts["timeout"])
    except requests.RequestException as ex:
        raise LLMError(f"Strata に接続できません: {ex}") from ex
    if r.status_code >= 400:
        raise LLMError(f"Strata error: {r.status_code} {r.text[:300]}")
    return ja_fix(strip_think(r.json()["choices"][0]["message"].get("content") or "")).strip()


def plan(history_text: str, cfg: dict) -> dict:
    opts = llm(cfg, json_mode=True, temperature=0.1, max_tokens=300, timeout=180,
               extra_body={"chat_template_kwargs": {"enable_thinking": False}})
    try:
        got = json.loads(re.search(r"\{.*\}", chat([{"role": "user", "content": PLAN_PROMPT.format(history=history_text)}],
                                                   opts), re.S).group(0))
    except (AttributeError, json.JSONDecodeError):
        return {"search": False, "queries": []}
    qs = [str(q).strip() for q in got.get("queries") or [] if str(q).strip()][:2]
    return {"search": bool(got.get("search")) and bool(qs), "queries": qs}


def search(query: str, cfg: dict) -> list[dict]:
    r = requests.get(cfg.get("searxng_url", "http://127.0.0.1:8888").rstrip("/") + "/search",
                     params={"q": query, "format": "json", "language": cfg.get("language", "ja")}, timeout=30)
    r.raise_for_status()
    return [{"title": html.unescape(x.get("title") or ""), "url": x["url"], "snippet": html.unescape(x.get("content") or "")}
            for x in r.json().get("results") or [] if x.get("url")]


def page_text(url: str, limit: int) -> str:
    """ページ本文の冒頭（HTML のみ。PDF などは検索結果の抜粋だけを使う）。読めなければ ""。"""
    try:
        r = requests.get(url, headers={"User-Agent": UA}, timeout=10)
    except requests.RequestException:
        return ""
    if r.status_code >= 400 or "html" not in r.headers.get("content-type", ""):
        return ""
    r.encoding = r.apparent_encoding if r.encoding in (None, "ISO-8859-1") else r.encoding
    p = _Text()
    try:
        p.feed(r.text)
    except Exception:   # noqa: BLE001
        return ""
    return re.sub(r"\s+", " ", " ".join(p.out))[:limit]


def gather(queries: list[str], cfg: dict) -> list[dict]:
    """検索語ごとに検索し、URL の重複を除いて上位 max_results 件。上位 fetch_pages 件は本文も読む。"""
    seen, hits = set(), []
    for q in queries:
        try:
            got = search(q, cfg)
        except requests.RequestException as ex:
            raise LLMError(f"SearXNG（{cfg.get('searxng_url')}）で検索できません: {ex}") from ex
        for h in got:
            key = h["url"].split("#")[0].rstrip("/")
            if key not in seen:
                seen.add(key)
                hits.append(h)
    hits = hits[:int(cfg.get("max_results", 6))]
    for h in hits[:int(cfg.get("fetch_pages", 3))]:
        h["text"] = page_text(h["url"], int(cfg.get("page_chars", 2500)))
    return hits


def results_block(hits: list[dict]) -> str:
    out = []
    for i, h in enumerate(hits, 1):
        body = h.get("text") or h["snippet"]
        out.append(f"[{i}] {h['title']}（{urlparse(h['url']).netloc}）\n{body}")
    return "\n\n".join(out)


def system_prompt(persona: dict) -> str:
    return (persona.get("system") or "").strip() + "\n" + ANSWER_RULES


def answer(history: list[dict], cfg: dict) -> dict:
    """history: [{"role": "user"|"assistant", "name": str, "content": str}, ...]（古い順。最後が今の発言）。
    返り値: {"text": 返答, "queries": [...], "sources": [(title, url), ...]}"""
    persona = load_persona(cfg)
    hist_text = "\n".join(f"{m['name']}: {m['content']}" for m in history[-6:])
    p = plan(hist_text, cfg) if cfg.get("search", True) else {"search": False, "queries": []}
    hits = gather(p["queries"], cfg) if p["search"] else []
    sys = system_prompt(persona)
    if hits:
        sys += "\n# 検索結果（検索語: " + " / ".join(p["queries"]) + "）\n" + results_block(hits)
    msgs = [{"role": "system", "content": sys}]
    msgs += [{"role": m["role"], "content": m["content"]} for m in history]
    opts = llm(cfg, json_mode=False, temperature=float(cfg.get("temperature", 0.5)),
               max_tokens=int(cfg.get("max_tokens", 3000)), timeout=600,
               extra_body={"reasoning_budget_tokens": int(cfg.get("reasoning_budget_tokens", 512))})
    text = chat(msgs, opts) or "（うまく答えを作れませんでした。もう一度送ってください）"
    used = [h for h in hits if h.get("text")] + [h for h in hits if not h.get("text")]
    return {"text": text, "queries": p["queries"] if p["search"] else [],
            "sources": [(h["title"] or urlparse(h["url"]).netloc, h["url"]) for h in used[:5]]}


if __name__ == "__main__":   # 試し: python src/strata_chat.py "質問"
    import sys
    sys.stdout.reconfigure(encoding="utf-8")
    q = " ".join(sys.argv[1:]) or "ワルファリンとビタミンKの関係を教えて"
    res = answer([{"role": "user", "name": "利用者", "content": q}], section_config())
    print("検索語:", res["queries"])
    print(res["text"])
    for t, u in res["sources"]:
        print("-", t, u)
