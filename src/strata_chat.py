"""Discord で Strata と会話する（ペルソナ・スキル・嗜好の記憶）。Bot 本体は src/qa_bot.py（同じ Bot が別チャンネルで受ける）。

流れ: ペルソナ（style/personas/*.yaml）として、ふだんはそのまま会話する（reply）。
      ペルソナの skills にあるスキルが要るとキャラクターが判断したら、短い一言のあと最後の行に [[スキル名: 内容]] を書く
      → Bot はその一言を先に投稿してから、スキルを実行して結果を投稿する。
  検索（web_search）: 自宅の SearXNG で検索し、上位ページの本文を読んで答える（research）→ 出典を添える。
                      検索語は外部の検索エンジンに送られるので、患者・職員を特定しうる語は入れさせない。
  小説（novel）:      指定のシチュエーションで短編を書く（write_novel）。.local/novels/ にも保存。
嗜好の記憶（ペルソナの memory: true）: 会話のたびに、相手の好み・小説の設定などを Strata に抜き出させて
  .local/state/strata_memory_<id>.json に貯め、次の会話・小説で system に入れる（モデル自体は学習しない）。
"""
import datetime as dt
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
from common import ROOT, load_state, save_state

SECTION = "strata_chat"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Safari/537.36"

# スキル: ペルソナの skills に書いた ID だけが使える。tag は会話の最後の行に書かせる [[tag: 内容]] の名前
SKILLS = {
    "web_search": {"tag": "検索", "rule": """
## スキル「検索」（ウェブで調べてから答える）
- 使うとき: 最新の情報・ニュース、薬の用量・相互作用・添付文書の記載、制度や数値など、うろ覚えだと間違えそうな事実を聞かれたとき。
  あいさつ・雑談・気持ちの話・会話の中だけで答えられることでは使わない。
- 使い方: キャラクターの口調で「調べてみる」ことを短く一言伝えてから（答えはまだ書かない）、
  最後の行に [[検索: 検索語1 | 検索語2]] とだけ書いて終える。検索語は1〜2個、検索エンジン向けの短い語の並び。
- 検索語は外部の検索エンジンに送られる。患者・職員の氏名・ID・生年月日・住所・病院名・具体的な日付など、
  人を特定しうる語は絶対に入れない（例「山田さん 80歳 ワルファリン」→「高齢者 ワルファリン 用量」）。
"""},
    "novel": {"tag": "小説", "rule": """
## スキル「小説」（指定のシチュエーションで短編小説を書く）
- 使うとき: 小説・お話・ストーリー・SS を書いてほしいと頼まれたとき、または続きを頼まれたとき。
  書く前に聞きたいことがあっても、まずは書いてよい（足りない設定は嗜好の記憶と会話から補う）。
- 使い方: キャラクターの口調で「書いてみる」ことを短く一言伝えてから（本文はまだ書かない）、
  最後の行に [[小説: シチュエーション・登場人物・雰囲気・長さの希望などの要約]] とだけ書いて終える。
  続きを頼まれたときは要約の頭に「続き:」と書く。
"""},
}
SKILL_TAG = re.compile(r"\[\[\s*(" + "|".join(s["tag"] for s in SKILLS.values()) + r")\s*[:：]\s*(.+?)\]\]", re.S)
TAG_TO_SKILL = {s["tag"]: sid for sid, s in SKILLS.items()}

CHAT_RULES = """
# 会話のしかた
- Discord のチャット。ふだんはそのまま会話する。長くても 400 字程度。見出しは使わない。
- 下の「スキル」が要るときだけ使う。使うのは1回の返事で1つまで。スキルの書式は、使うとき以外は書かない。
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


def persona_skills(persona: dict) -> list[str]:
    return [s for s in persona.get("skills") or [] if s in SKILLS]


def base_system(persona: dict) -> str:
    """ペルソナの人柄 ＋ 覚えている嗜好。"""
    return (persona.get("system") or "").strip() + memory_block(persona)


def reply(history: list[dict], cfg: dict) -> dict:
    """まず会話として返す。スキルが要るとキャラクターが判断したら、その一言とスキルの中身を返す。
    history: [{"role": "user"|"assistant", "content": str}, ...]（古い順。最後が今の発言）。
    返り値: {"text": 返答（スキルを使うときは一言）, "skill": スキルID or None, "arg": str}"""
    persona = load_persona(cfg)
    skills = persona_skills(persona)
    system = base_system(persona) + "\n" + CHAT_RULES
    if skills:
        system += "\n# スキル\n" + "".join(SKILLS[s]["rule"] for s in skills)
    else:
        system += "\n# スキル\n（なし）\n"
    text = _talk(system, history, cfg)
    m = SKILL_TAG.search(text)
    sid = TAG_TO_SKILL.get(m.group(1)) if m else None
    if not m or sid not in skills:
        return {"text": SKILL_TAG.sub("", text).strip() or "（うまく返事を作れませんでした。もう一度送ってください）",
                "skill": None, "arg": ""}
    ack = SKILL_TAG.sub("", text).strip() or (persona.get("skill_ack") or {}).get(sid) or "ちょっと待ってて"
    return {"text": ack, "skill": sid, "arg": m.group(2).strip()}


def search_queries(arg: str) -> list[str]:
    return [q.strip() for q in re.split(r"[|｜]", arg) if q.strip()][:2]


def research(history: list[dict], queries: list[str], cfg: dict) -> dict:
    """検索して、その結果をもとにキャラクターとして答える。返り値: {"text", "sources": [(title, url), ...]}"""
    persona = load_persona(cfg)
    hits = gather(queries, cfg)
    system = base_system(persona) + "\n" + RESEARCH_RULES
    system += ("\n# 検索結果（検索語: " + " / ".join(queries) + "）\n" + results_block(hits)) if hits \
        else "\n# 検索結果\n（何も見つからなかった。見つからなかったと正直に伝える）"
    text = SKILL_TAG.sub("", _talk(system, history, cfg)).strip() or "（うまく答えを作れませんでした。もう一度送ってください）"
    used = [h for h in hits if h.get("text")] + [h for h in hits if not h.get("text")]
    return {"text": text, "sources": [(h["title"] or urlparse(h["url"]).netloc, h["url"]) for h in used[:5]]}


# ---- 小説 ----
NOVEL_RULES = """
# いまの仕事: 短編小説を書く
- 下の「シチュエーション」と、覚えている嗜好（特に小説の設定・好み）に沿って、日本語の短編小説を書く。
  嗜好と矛盾しない範囲で、シチュエーションの指定を優先する。
- 長さはおよそ {chars} 字（指定があればそれに従う）。地の文と会話をまぜ、場面が目に浮かぶように書く。
- 書き手はあなただが、あなた自身（キャラクター）は、頼まれない限り登場人物にしない。
- 同じ短い台詞のくり返し（「はい」「はい」）で行数を稼がない。場面を進める。
- 1行目に『タイトル』だけを書き、1行空けて本文。前置き・あとがき・解説・作者としてのコメントは書かない。
- 続きを頼まれたときは、直前までの話（下の「前回の話」）から自然につなげる。
"""


def cut_loop(text: str, times: int = 3) -> str:
    """同じ行（4字以上）が times 回目に出たところで切る（繰り返しに入った小説の後ろを捨てる）。"""
    seen: dict[str, int] = {}
    lines = text.splitlines()
    for i, line in enumerate(lines):
        key = line.strip()
        if len(key) < 4:
            continue
        seen[key] = seen.get(key, 0) + 1
        if seen[key] >= times:
            kept = lines[:i]
            while kept and kept[-1].strip() in (key, ""):   # 繰り返しの1回目・2回目も落とす
                kept.pop()
            return "\n".join(kept).rstrip()
    return text


def novel_dir() -> Path:
    d = ROOT / ".local" / "novels"
    d.mkdir(parents=True, exist_ok=True)
    return d


def last_novel(pid: str) -> str:
    files = sorted(novel_dir().glob(f"*-{pid}.txt"))
    return files[-1].read_text(encoding="utf-8") if files else ""


def write_novel(history: list[dict], situation: str, cfg: dict) -> dict:
    """返り値: {"title", "text", "path"}。"""
    persona = load_persona(cfg)
    ncfg = cfg.get("novel") or {}
    system = base_system(persona) + NOVEL_RULES.format(chars=int(ncfg.get("chars", 2500)))
    if situation.startswith(("続き:", "続き：")):
        prev = last_novel(persona["id"])
        if prev:
            system += "\n# 前回の話\n" + prev[-int(ncfg.get("prev_chars", 6000)):]
        situation = situation.split(":", 1)[-1].split("：", 1)[-1].strip()
    system += "\n# シチュエーション\n" + situation
    msgs = [{"role": "system", "content": system}]
    msgs += [{"role": m["role"], "content": m["content"]} for m in history[-6:]]
    msgs.append({"role": "user", "content": "では、小説の本文をどうぞ。"})
    # 低ビット量子化のモデルは長文で同じ行を繰り返しがちなので、繰り返しの罰則を強めにかける（Strata が対応する項目）
    sampling = {"presence_penalty": 0.7, "repetition_penalty": 1.05, "penalty_last_n": 1024, "top_p": 0.9,
                **(ncfg.get("sampling") or {})}
    opts = llm(cfg, json_mode=False, temperature=float(ncfg.get("temperature", 0.75)),
               max_tokens=int(ncfg.get("max_tokens", 8000)), timeout=1800,
               extra_body={"reasoning_budget_tokens": int(ncfg.get("reasoning_budget_tokens", 1024)), **sampling})
    text = cut_loop(SKILL_TAG.sub("", chat(msgs, opts)).strip())
    if not text:
        raise LLMError("小説を書けませんでした（Strata の返事が空）")
    first = text.splitlines()[0].strip()
    title = first.strip("『』「」#* ") if len(first) <= 40 else situation[:30]
    path = novel_dir() / f"{dt.datetime.now():%Y%m%d-%H%M%S}-{persona['id']}.txt"
    path.write_text(text, encoding="utf-8")
    return {"title": title, "text": text, "path": path}


# ---- 嗜好の記憶 ----
MEMORY_PROMPT = """あなたは、会話から「相手（利用者）の嗜好」を記録する係です。
下の「いまの記憶」と「直近の会話」を読み、記憶を更新してください。

記録するもの:
- like: 相手の好きなもの・趣味・話し方の好み・うれしかったこと
- dislike: 苦手なもの・嫌がったこと・NG
- novel: 小説の好み・追加設定（登場人物の名前や性格、関係性、世界観、文体、視点、長さ、甘さの度合い、
         「次はこうして」という注文、書いた小説への感想）
- profile: 相手自身のこと（呼ばれたい名前、仕事の様子、生活リズムなど。本人が話したことだけ）
ルール:
- 会話にはっきり出たことだけ。推測で足さない。1件は短い1文（40字以内）。
- 一度きりの依頼の中身（「〜の話を書いて」そのもの）は記録しない。次からも使える好み・設定の形にして記録する
  （例: ×「屋上で缶コーヒーの話」→ ○「後輩キャラの名前は湊くん」「甘めの話が好き」）。
- 前の記憶と食い違う新しい発言があれば、古い方を消して新しい方を残す（remove に古い番号）。
- 患者さん・同僚など、相手以外の実在の人を特定できる情報は記録しない。
- 新しく記録することが無ければ add も remove も空にする。

いまの記憶（番号: 種類: 内容）:
{memory}

直近の会話:
{talk}

JSONのみを出力: {{"add": [{{"kind": "like|dislike|novel|profile", "text": "..."}}], "remove": [番号]}}"""

KIND_LABEL = {"profile": "相手のこと", "like": "好きなもの", "dislike": "苦手・NG", "novel": "小説の好み・設定"}


def memory_path(pid: str) -> Path:
    return Path(os.environ.get("INFO_DIGEST_STATE_DIR") or ROOT / "state") / f"strata_memory_{pid}.json"


def load_memory(pid: str) -> list[dict]:
    p = memory_path(pid)
    return json.loads(p.read_text(encoding="utf-8")).get("items", []) if p.exists() else []


def save_memory(pid: str, items: list[dict]) -> None:
    p = memory_path(pid)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"items": items}, ensure_ascii=False, indent=1), encoding="utf-8")


def memory_block(persona: dict) -> str:
    if not persona.get("memory"):
        return ""
    items = load_memory(persona["id"])
    if not items:
        return ""
    out = ["\n\n# 覚えている相手の嗜好（会話で自然に活かす。いちいち「覚えてるよ」と言わなくてよい）"]
    for kind, label in KIND_LABEL.items():
        xs = [i["text"] for i in items if i.get("kind") == kind]
        if xs:
            out.append(f"## {label}\n" + "\n".join(f"- {x}" for x in xs))
    return "\n".join(out) + "\n"


def update_memory(persona: dict, history: list[dict], cfg: dict) -> list[str]:
    """直近の会話から嗜好を抜き出して記憶を更新する。新しく覚えたことの一覧を返す。"""
    if not persona.get("memory"):
        return []
    items = load_memory(persona["id"])
    mem = "\n".join(f"{i}: {m['kind']}: {m['text']}" for i, m in enumerate(items)) or "（まだ無い）"
    talk = "\n".join(f"{'相手' if m['role'] == 'user' else persona['name']}: {m['content'][:600]}" for m in history[-4:])
    opts = llm(cfg, json_mode=True, temperature=0.1, max_tokens=800, timeout=300,
               extra_body={"chat_template_kwargs": {"enable_thinking": False}})
    try:
        got = json.loads(re.search(r"\{.*\}", chat([{"role": "user", "content": MEMORY_PROMPT.format(memory=mem, talk=talk)}],
                                                   opts), re.S).group(0))
    except (AttributeError, json.JSONDecodeError, LLMError):
        return []
    drop = {int(x) for x in got.get("remove") or [] if str(x).isdigit()}
    items = [m for i, m in enumerate(items) if i not in drop]
    have = {m["text"] for m in items}
    added = []
    for a in got.get("add") or []:
        kind, text = str(a.get("kind", "")).strip(), str(a.get("text", "")).strip()
        if kind in KIND_LABEL and text and text not in have:
            items.append({"kind": kind, "text": text[:80], "date": dt.date.today().isoformat()})
            added.append(text)
    limit = int((cfg.get("memory") or {}).get("max_items", 80))
    save_memory(persona["id"], items[-limit:])   # 多すぎたら古いものから忘れる
    return added


def forget(pid: str, numbers: list[int]) -> list[str]:
    items = load_memory(pid)
    gone = [items[n - 1]["text"] for n in numbers if 1 <= n <= len(items)]
    save_memory(pid, [m for i, m in enumerate(items, 1) if i not in set(numbers)])
    return gone


if __name__ == "__main__":   # 試し: python src/strata_chat.py "発言"
    import sys
    sys.stdout.reconfigure(encoding="utf-8")
    cfg = section_config()
    hist = [{"role": "user", "content": " ".join(sys.argv[1:]) or "ワルファリンとビタミンKの関係を教えて"}]
    r1 = reply(hist, cfg)
    print("▶", r1["text"], "\nスキル:", r1["skill"], r1["arg"])
    hist.append({"role": "assistant", "content": r1["text"]})
    if r1["skill"] == "web_search":
        r2 = research(hist, search_queries(r1["arg"]), cfg)
        print("▶", r2["text"])
        for t, u in r2["sources"]:
            print("-", t, u)
    elif r1["skill"] == "novel":
        r2 = write_novel(hist, r1["arg"], cfg)
        print("▶", r2["title"], r2["path"])
        print(r2["text"][:1500])
    print("記憶:", update_memory(load_persona(cfg), hist, cfg))
