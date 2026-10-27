"""Discord で Strata と会話する（検索つき）。Bot 本体は src/qa_bot.py（同じ Bot が別チャンネルで受ける）。

流れ: ペルソナ（style/persona.yaml）として、ふだんは調べずに会話する（reply）。
      調べた方がよい話題のときだけ、キャラクターが「調べてみる」と言って、最後の行に [[検索: 語1 | 語2]] を書く
      → Bot はその一言を先に投稿 → 自宅の SearXNG（config の strata_chat.searxng_url）で検索し、
      上位のページ本文を少し読んで、もう一度キャラクターとして答える（research）→ 出典を添える。
検索語は外部の検索エンジンに送られるので、患者・職員を特定しうる語は検索語に入れさせない。
"""
import html
import os
import re
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urlparse

import requests
import yaml

import ai_digest
from ai_digest import LLMError, llm_settings, ja_fix, strip_think
from common import ROOT, load_state, save_state

SECTION = "strata_chat"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Safari/537.36"

SEARCH_TAG = re.compile(r"\[\[\s*検索\s*[:：]\s*(.+?)\]\]", re.S)

CHAT_RULES = """
# 会話のしかた
- Discord のチャット。ふだんは調べずに、そのまま会話する。長くても 400 字程度。見出しは使わない。
- 次のようなときだけ、ウェブで調べてから答える:
  最新の情報・ニュース、薬の用量・相互作用・添付文書の記載、制度や数値など、うろ覚えだと間違えそうな事実を聞かれたとき。
  あいさつ・雑談・気持ちの話・会話の中だけで答えられることでは調べない。
- 調べるときは、キャラクターの口調で「調べてみる」ことを短く一言伝えてから（答えはまだ書かない）、
  最後の行に [[検索: 検索語1 | 検索語2]] とだけ書いて終える。検索語は1〜2個、検索エンジン向けの短い語の並び。
- 検索語は外部の検索エンジンに送られる。患者・職員の氏名・ID・生年月日・住所・病院名・具体的な日付など、
  人を特定しうる語は絶対に入れない（例「山田さん 80歳 ワルファリン」→「高齢者 ワルファリン 用量」）。
"""

RESEARCH_RULES = """
# いま調べた結果
- 下の「検索結果」は、直前の発言について、いまウェブで調べた内容。これをもとに、キャラクターの口調のまま答える。
- 事実・数値・用量は検索結果に書いてあることを優先し、どの資料かが分かるように言う（「PMDA の添付文書だと〜」）。
- 検索結果に無いことを、調べたかのように言わない。分からなければ分からないと言う。
- もう「調べてみる」とは言わない。[[検索: …]] も書かない。出典の一覧はこちらで付けるので URL は並べない。
- 長くても 600 字程度。必要なら短い箇条書き。
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


def personas(cfg: dict) -> dict[str, dict]:
    """style/personas/*.yaml（ファイル名がID）。{id: ペルソナ}。"""
    out = {}
    for path in sorted((ROOT / (cfg.get("persona_dir") or "style/personas")).glob("*.yaml")):
        p = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        out[path.stem] = {**p, "id": path.stem, "name": p.get("name") or path.stem}
    return out


def find_persona(cfg: dict, word: str) -> dict | None:
    """ID・名前・別名（aliases）のどれかに合うペルソナ。大文字小文字は区別しない。"""
    w = word.strip().lower()
    for p in personas(cfg).values():
        if w in {p["id"].lower(), str(p["name"]).lower(), *(str(a).lower() for a in p.get("aliases") or [])}:
            return p
    return None


def load_persona(cfg: dict) -> dict:
    """いま使っているペルソナ（state/strata_chat.json の persona。無ければ config の default_persona）。"""
    ps = personas(cfg)
    if not ps:
        raise LLMError(f"ペルソナがありません（{cfg.get('persona_dir') or 'style/personas'}/*.yaml）")
    pid = load_state(SECTION).get("persona") or cfg.get("default_persona")
    return ps.get(pid) or next(iter(ps.values()))


def set_persona(pid: str) -> None:
    state = load_state(SECTION)
    state["persona"] = pid
    save_state(SECTION, state)


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
    text = ja_fix(strip_think(r.json()["choices"][0]["message"].get("content") or ""))
    return text.replace("，", "、").replace("．", "。").strip()   # 中国語の句読点が混ざることがある


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


def _talk(system: str, history: list[dict], cfg: dict) -> str:
    msgs = [{"role": "system", "content": system}] + [{"role": m["role"], "content": m["content"]} for m in history]
    opts = llm(cfg, json_mode=False, temperature=float(cfg.get("temperature", 0.7)),
               max_tokens=int(cfg.get("max_tokens", 3000)), timeout=600,
               extra_body={"reasoning_budget_tokens": int(cfg.get("reasoning_budget_tokens", 512))})
    return chat(msgs, opts)


def reply(history: list[dict], cfg: dict) -> dict:
    """まず会話として返す。調べた方がよいとキャラクターが判断したら、「調べてみる」の一言と検索語を返す。
    history: [{"role": "user"|"assistant", "content": str}, ...]（古い順。最後が今の発言）。
    返り値: {"text": 返答（調べるときは「調べてみる」の一言）, "queries": [...]（調べないときは []）}"""
    persona = load_persona(cfg)
    rules = CHAT_RULES if cfg.get("search", True) else ""
    text = _talk((persona.get("system") or "").strip() + "\n" + rules, history, cfg)
    m = SEARCH_TAG.search(text)
    if not m:
        return {"text": text or "（うまく返事を作れませんでした。もう一度送ってください）", "queries": []}
    queries = [q.strip() for q in re.split(r"[|｜]", m.group(1)) if q.strip()][:2]
    ack = SEARCH_TAG.sub("", text).strip() or (persona.get("search_ack") or "ちょっと調べてみるね🔎")
    return {"text": ack, "queries": queries}


def research(history: list[dict], queries: list[str], cfg: dict) -> dict:
    """検索して、その結果をもとにキャラクターとして答える。返り値: {"text", "sources": [(title, url), ...]}"""
    persona = load_persona(cfg)
    hits = gather(queries, cfg)
    system = (persona.get("system") or "").strip() + "\n" + RESEARCH_RULES
    system += ("\n# 検索結果（検索語: " + " / ".join(queries) + "）\n" + results_block(hits)) if hits \
        else "\n# 検索結果\n（何も見つからなかった。見つからなかったと正直に伝える）"
    text = SEARCH_TAG.sub("", _talk(system, history, cfg)).strip() or "（うまく答えを作れませんでした。もう一度送ってください）"
    used = [h for h in hits if h.get("text")] + [h for h in hits if not h.get("text")]
    return {"text": text, "sources": [(h["title"] or urlparse(h["url"]).netloc, h["url"]) for h in used[:5]]}


if __name__ == "__main__":   # 試し: python src/strata_chat.py "発言"
    import sys
    sys.stdout.reconfigure(encoding="utf-8")
    cfg = section_config()
    hist = [{"role": "user", "content": " ".join(sys.argv[1:]) or "ワルファリンとビタミンKの関係を教えて"}]
    r1 = reply(hist, cfg)
    print("▶", r1["text"], "\n検索語:", r1["queries"])
    if r1["queries"]:
        r2 = research(hist + [{"role": "assistant", "content": r1["text"]}], r1["queries"], cfg)
        print("▶", r2["text"])
        for t, u in r2["sources"]:
            print("-", t, u)
