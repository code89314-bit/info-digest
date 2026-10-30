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
import contextvars
import datetime as dt
import html
import json
import os
import random
import re
import time
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
- 相手が画像を貼ったときは、その画像の人物・雰囲気・場面も要約に書き込む（画像は小説を書くときにも一緒に読む）。
- 知らない作品・キャラクターが出てきても、書く前に自動で下調べする（口調・性格・立場）。知ったかぶりで書かない。
"""},
    "prompts": {"tag": "プロンプト", "rule": """
## スキル「プロンプト」（絵を描く準備: 人物の候補5つとシチュエーション約20個を作ってファイルに残す）
- 使うとき: 絵にしたい・イラストのプロンプトがほしいと言われたとき、小説の場面を絵にしたいと言われたとき。
- 使い方: 一言のあと、最後の行に [[プロンプト: お題]] とだけ書く。お題には、描きたいもの・キャラ（作品名も）・雰囲気・
  場面・服装・参考にする小説（「さっきの小説」など）・貼られた画像の特徴を入れる。
- 作ったあと、描画モード（ComfyUI）に入るかは、こちらで相手に確かめる。
"""},
    "draw": {"tag": "描く", "rule": """
## スキル「描く」（描画モードに入る: 人物の候補5枚 → 番号で選ぶ → シチュエーションを描き続ける）
- 使うとき: 作ってあるプロンプトから絵にしてと言われたとき。まだ準備が無いときは、先に「プロンプト」スキルを使う。
- 使い方: 一言のあと、最後の行に [[描く: おまかせ]] とだけ書く。切り替えるかは、こちらで相手に確かめる。
- 描画モードのあいだ（数十分）はおしゃべりできないことを、一言で伝えておく。
"""},
}
BARE_TAG = re.compile(r"\[\[\s*([^\[\]:：]+?)\s*\]\]\s*$")   # 名前の無い [[…]]（最後の行）
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


# ペルソナ専用チャンネル（ルナのチャンネル）では、切り替えの状態に関係なくそのペルソナにする。
# Bot が会話の処理の頭で set する（asyncio.to_thread にも引き継がれる）
PERSONA_OVERRIDE: contextvars.ContextVar[str | None] = contextvars.ContextVar("persona_override", default=None)
# GPU が Gemma のときのルナは、ふだんの会話・記憶・プロンプト作りも novel_llm（Gemma）で（Bot が ensure_gpu で set する）
USE_NOVEL_LLM: contextvars.ContextVar[bool] = contextvars.ContextVar("use_novel_llm", default=False)


def load_persona(cfg: dict) -> dict:
    """いま使っているペルソナ（専用チャンネルならそのペルソナ。ほかは state/strata_chat.json の persona、
    無ければ config の default_persona）。"""
    ps = personas(cfg)
    if not ps:
        raise LLMError(f"ペルソナがありません（{cfg.get('persona_dir') or 'style/personas'}/*.yaml）")
    pid = PERSONA_OVERRIDE.get() or load_state(SECTION).get("persona") or cfg.get("default_persona")
    p = ps.get(pid) or next(iter(ps.values()))
    return {**p, "llm": p["novel_llm"]} if USE_NOVEL_LLM.get() and p.get("novel_llm") else p


def set_persona(pid: str) -> None:
    state = load_state(SECTION)
    state["persona"] = pid
    save_state(SECTION, state)


def llm(cfg: dict, persona: dict | None = None, **over) -> dict:
    """LLM の接続設定。ペルソナに llm があればそれで上書き（ルナは小説のときだけ novel_llm を llm にして渡す）。
    over の extra_body は、ペルソナの extra_body に足す（Strata 用の項目は llama.cpp では無視される）。"""
    plm = (persona or {}).get("llm") or {}
    base = llm_settings({"llm": {**(cfg.get("llm") or {}), **plm}})
    extra = {**(plm.get("extra_body") or {}), **(over.pop("extra_body", None) or {})}
    return {**base, **over, "extra_body": extra}


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


def _talk(system: str, history: list[dict], cfg: dict, persona: dict | None = None) -> str:
    msgs = [{"role": "system", "content": system}] + [{"role": m["role"], "content": m["content"]} for m in history]
    opts = llm(cfg, persona, json_mode=False, temperature=float(cfg.get("temperature", 0.7)),
               max_tokens=int(cfg.get("max_tokens", 3000)), timeout=600,
               extra_body={"reasoning_budget_tokens": int(cfg.get("reasoning_budget_tokens", 512))})
    return chat(msgs, opts)


def ask_json(prompt: str, cfg: dict, persona: dict | None = None, max_tokens: int = 1500, images: list[str] | None = None) -> dict:
    """考えさせずに JSON を1つ返させる。読めなければ {}。images は data URI（画像を読めるモデルのとき）。"""
    content = prompt if not images else [{"type": "text", "text": prompt}] + \
        [{"type": "image_url", "image_url": {"url": u}} for u in images]
    opts = llm(cfg, persona, json_mode=True, temperature=0.2, max_tokens=max_tokens, timeout=600,
               extra_body={"chat_template_kwargs": {"enable_thinking": False}})
    try:
        return json.loads(re.search(r"\{.*\}", chat([{"role": "user", "content": content}], opts), re.S).group(0))
    except (AttributeError, json.JSONDecodeError):
        return {}


def text_of(content) -> str:
    """メッセージの content（文字列か、文字と画像のリスト）から文字だけ。"""
    if isinstance(content, str):
        return content
    return "\n".join(p.get("text", "") for p in content or [] if p.get("type") == "text")


def images_of(history: list[dict], last_n: int = 2) -> list[str]:
    """直近の発言についた画像（data URI）。"""
    out = []
    for m in history[-last_n:]:
        if isinstance(m["content"], list):
            out += [p["image_url"]["url"] for p in m["content"] if p.get("type") == "image_url"]
    return out


def persona_skills(persona: dict) -> list[str]:
    return [s for s in persona.get("skills") or [] if s in SKILLS]


def base_system(persona: dict) -> str:
    """ペルソナの人柄 ＋ 覚えている嗜好 ＋ 小説の作法。"""
    return (persona.get("system") or "").strip() + memory_block(persona) + craft_block(persona)

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
    text = _talk(system, history, cfg, persona)
    m = SKILL_TAG.search(text)
    sid = TAG_TO_SKILL.get(m.group(1)) if m else None
    if not m and "web_search" in skills:
        # Qwen3.6 は「検索:」を落として [[語1 | 語2]] だけ書くことがある（2026-10-30。「調べてみます」で止まった）
        bare = BARE_TAG.search(text)
        if bare:
            return {"text": BARE_TAG.sub("", text).strip() or "ちょっと待ってて", "skill": "web_search",
                    "arg": bare.group(1).strip()}
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
    text = SKILL_TAG.sub("", _talk(system, history, cfg, persona)).strip() or "（うまく答えを作れませんでした。もう一度送ってください）"
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
    """同じ行（4字以上）が times 回目に出たところで切る（繰り返しに入った小説の後ろを捨てる）。
    1行の中で短い文字列が6回以上続く崩れ（「al al al …」「少し歩的て、少し歩的て…」）も、その手前で切る。"""
    # 3〜12字のかたまりが6回以上 / 1〜2字が20回以上（「お゛お゛お゛」「あぁぁぁ」のような演出の短いくり返しは残す）
    m = re.search(r"(.{3,12}?)\1{5,}|(.{1,2}?)\2{19,}", text, re.S)
    if m and (m.group(1) or m.group(2)).strip("…ーっッ！!？?。、 　\n゛"):   # 「…………」「ーーー」も残す
        text = text[:m.start()].rstrip()
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


# ---- 下調べ（知らない作品・キャラクターは、書く前にウェブで調べてファイルに残す） ----
MOTIF_PROMPT = """次の依頼を正確に書くために、ウェブで調べたほうがよい固有名詞を挙げてください。
対象: 実在の作品・その登場キャラクター・実在の人物・実在の場所やブランドなど（設定や口調を間違えると困るもの）。
対象外: 一般名詞、依頼者がその場で作った創作キャラ、下の「調べ済み」にあるもの。
無ければ空のリスト。最大3つ。query は検索エンジン向けの短い語（例「〇〇 キャラクター 性格 口癖」）。

依頼:
{request}

調べ済み: {known}

JSONのみ: {{"motifs": [{{"name": "...", "query": "..."}}]}}"""

NOTE_PROMPT = """次の検索結果から「{name}」について、小説や絵を書くための資料メモを日本語で作ってください。
次の見出しで、この順に（小説ではとくに上の4つを使う）:
## 基本（出典の作品、フルネーム、年齢。年齢は作中の設定どおり、不明なら学年や立場から分かる範囲で）
## 性格
## 口調（一人称・二人称・口癖・語尾。実際のセリフ例があれば1〜2個）
## 立場・関係（出典の作品、所属・役割、主な人間関係）
## 外見（髪・目・服装・特徴を短く。英語の booru タグも併記）
検索結果に書いてあることだけ。わからない項目は「不明」。全体で700字以内。

検索結果:
{results}"""


def research_dir() -> Path:
    d = ROOT / ".local" / "luna" / "research"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _slug(name: str) -> str:
    return re.sub(r"[\\/:*?\"<>|\s]+", "_", name.strip())[:60] or "untitled"


def study_motifs(request: str, cfg: dict, persona: dict) -> tuple[str, list[str]]:
    """依頼に出てくる作品・キャラを調べ、資料メモ（.local/luna/research/<名前>.md）を作るか読み直す。
    返り値: (system に足す資料の文, 調べたものの名前の一覧)。"""
    known = {p.stem: p for p in research_dir().glob("*.md")}
    notes, names = [], []
    for name, path in known.items():   # 調べ済みで依頼に出てくるもの
        if name and name in request:
            notes.append(path.read_text(encoding="utf-8"))
            names.append(name)
    got = ask_json(MOTIF_PROMPT.format(request=request[:2000], known="、".join(known) or "なし"), cfg, persona, 600)
    failed = []
    for m in (got.get("motifs") or [])[:3]:
        name, query = str(m.get("name", "")).strip(), str(m.get("query", "")).strip()
        if not name or not query or _slug(name) in known:
            continue
        try:
            hits = gather([query], {**cfg, "max_results": 5, "fetch_pages": 2})
        except LLMError:   # SearXNG が止まっているなど
            hits = []
        if not hits:
            failed.append(name)
            continue
        opts = llm(cfg, persona, json_mode=False, temperature=0.2, max_tokens=1500, timeout=600,
                   extra_body={"chat_template_kwargs": {"enable_thinking": False}})
        memo = chat([{"role": "user", "content": NOTE_PROMPT.format(name=name, results=results_block(hits))}], opts)
        text = f"# {name}\n（調べた日: {dt.date.today()}、検索語: {query}）\n\n{memo}\n\n## 出典\n" + \
            "\n".join(f"- {h['title']} {h['url']}" for h in hits)
        (research_dir() / f"{_slug(name)}.md").write_text(text, encoding="utf-8")
        notes.append(text)
        names.append(name)
    miss = (f"\n# 下調べできなかったもの: {'、'.join(failed)}\n"
            "（検索が使えず調べられなかった。調べたとは言わず、知っている範囲で書く。自信のない設定は相手に確かめる）\n") if failed else ""
    if not notes:
        return miss, []
    return "\n# 下調べの資料（口調・性格・立場・外見はこれに合わせる）\n" + "\n\n".join(n[:2500] for n in notes) + miss, names


def write_novel(history: list[dict], situation: str, cfg: dict, novel_model: bool = True) -> dict:
    """返り値: {"title", "text", "path", "studied": [...]}。会話に画像があれば一緒に読む（画像を読めるモデルのとき）。
    novel_model: ペルソナの novel_llm（ルナは Gemma）で書く。Bot が先に GPU を切り替えておく。
    False（配信の時間帯などで切り替えられないとき）は、ふだんのモデル（Strata）で書く。"""
    persona = load_persona(cfg)
    if novel_model and persona.get("novel_llm"):
        persona = {**persona, "llm": persona["novel_llm"]}   # 下調べも含めて、この中は全部 novel_llm で
    else:
        persona = {k: v for k, v in persona.items() if k != "novel_sampling"}   # Gemma 向けの設定を Strata に使わない
    ncfg = cfg.get("novel") or {}
    system = base_system(persona) + NOVEL_RULES.format(chars=int(ncfg.get("chars", 2500)))
    notes, studied = study_motifs(situation + "\n" + text_of(history[-1]["content"]), cfg, persona) \
        if "web_search" in persona_skills(persona) or persona.get("study") else ("", [])
    system += notes
    if situation.startswith(("続き:", "続き：")):
        prev = last_novel(persona["id"])
        if prev:
            system += "\n# 前回の話\n" + prev[-int(ncfg.get("prev_chars", 6000)):]
        situation = situation.split(":", 1)[-1].split("：", 1)[-1].strip()
    system += "\n# シチュエーション\n" + situation
    msgs = [{"role": "system", "content": system}]
    msgs += [{"role": m["role"], "content": m["content"]} for m in history[-6:]]
    msgs.append({"role": "user", "content": "では、小説の本文をどうぞ。"})
    # 低ビット量子化のモデル（Strata の Q2_0）は長文で同じ行を繰り返しがちなので、繰り返しの罰則を強めにかける。
    # ペルソナの novel_sampling があればそちら（ルナの Gemma は Google 推奨の temperature 1.0 / top_p 0.95 / top_k 64）
    sampling = dict(persona.get("novel_sampling") or {"presence_penalty": 0.7, "repetition_penalty": 1.05,
                                                       "penalty_last_n": 1024, "top_p": 0.9, **(ncfg.get("sampling") or {})})
    temperature = float(sampling.pop("temperature", ncfg.get("temperature", 0.75)))
    opts = llm(cfg, persona, json_mode=False, temperature=temperature,
               max_tokens=int(ncfg.get("max_tokens", 8000)), timeout=1800,
               extra_body={"reasoning_budget_tokens": int(ncfg.get("reasoning_budget_tokens", 1024)), **sampling})
    text = cut_loop(SKILL_TAG.sub("", chat(msgs, opts)).strip())
    if not text:
        raise LLMError("小説を書けませんでした（モデルの返事が空）")
    first = text.splitlines()[0].strip()
    title = first.strip("『』「」#* ") if len(first) <= 40 else situation[:30]
    path = novel_dir() / f"{dt.datetime.now():%Y%m%d-%H%M%S}-{persona['id']}.txt"
    path.write_text(text, encoding="utf-8")
    return {"title": title, "text": text, "path": path, "studied": studied}


# ---- 執筆モード（ルナ: 小説を頼まれたら Gemma に切り替え、書き直し・続きの注文を受け続ける） ----
# 状態は .local/state/writing_<id>.json（Bot が再起動しても続く）。終わると previous に移し、次の執筆に持ち越す。
WRITING_RULES = """
# いまは「執筆モード」（小説の執筆と編集に集中している）
- 相手の発言は、いま書いている小説への注文（書く・書き直す・続き・別の場面・設定の変更）か、それについての相談・感想。
- 小説を書く・直すとき: 1行目に「【小説】」とだけ書き、次の行に『タイトル』、1行空けて本文。
  書き直しは、直したところだけでなく全文を書く。続きは、前の本文をくり返さずに続きの部分だけを書く。
  前置き・あとがき・解説・作者としてのコメントは書かない。長さは指定が無ければおよそ {chars} 字。
- 小説を書かずに答えるとき（質問・相談・感想への返事）: キャラクターの口調で短く（200字程度）。【小説】は書かない。
- 下の「小説の作法」は必ず守る。書いた小説（会話の中の【小説】）は自分が書いたものなので、内容を覚えていて、それをもとに直す。
- 書き手はあなただが、あなた自身（キャラクター）は、頼まれない限り登場人物にしない。
"""
INTERVIEW_RULES = """
# いまは打ち合わせ中（まだ本文を書いていない）
- いきなり書かずに、登場人物とシチュエーションについて短く質問して、足りないところを埋める。
  1回に2〜3問まで、短い箇条書きで（例: 二人の関係と呼び方、場所と時間、始まりと終わりの場面、雰囲気・温度感、視点、長さ）。
  下調べの資料・覚えている好み・参考画像で分かることは聞かない。
- 参考画像があれば、人物・服装・場所・雰囲気を読み取って設定に使い、読み取ったことを一言伝える。
- 相手が「書いて」「おまかせ」「それでいい」などと言ったら、または十分に分かったら、【小説】で書き始める。
  質問は2往復までにして、それ以上は聞かずに書く。
"""
# 初回（新しい話の1本目）だけ。「続き」は足さない（展開が毎回単調だったので、まず導入を厚くする。2026-10-29）
FIRST_NOVEL_RULES = """
# 1本目の書き方（導入を濃厚に）
- 本題（行為・事件）に急いで入らない。全体の4割ほどを導入に使う。
- 導入では、登場人物の外見・服装・しぐさ・口調、二人の関係とそれまでの経緯、場所の空気（音・匂い・光・温度）、
  それぞれの心の内（期待・不安・ためらい・隠している気持ち）を具体的に書き、本題に向かう緊張を少しずつ積み上げる。
- きっかけになる出来事を1つ置き、そこから本題へ自然に転がす。
"""
# 2本目から（続き・別の場面）。展開が毎回単調だったので（2026-10-29）
NEXT_NOVEL_RULES = """
# 2本目からの書き方
- 続きや別の場面を書くときは、前の本文と違う転換か意外な出来事を1つ入れる（同じ流れのくり返しにしない）。
  ただし書き直しを頼まれたときは、転換は足さずに、頼まれたところを直す。
"""
IMAGE_NOTE_PROMPT = """この参考画像を、小説の設定に使うために日本語で書き出してください（300字以内、箇条書き）。
項目: 人物（人数、外見・髪・目・体格、服装、表情・しぐさ）、場所・時間帯、雰囲気。画像に見えることだけ。"""
NOVEL_HEAD = "【小説】"


def writing_path(pid: str) -> Path:
    return Path(os.environ.get("INFO_DIGEST_STATE_DIR") or ROOT / "state") / f"writing_{pid}.json"


def writing_state(pid: str) -> dict:
    p = writing_path(pid)
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {"active": False}


def save_writing(pid: str, st: dict) -> None:
    p = writing_path(pid)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(st, ensure_ascii=False, indent=1), encoding="utf-8")


def novel_persona(cfg: dict) -> dict:
    """執筆モードで使うペルソナ（llm を novel_llm に差し替えたもの）。"""
    persona = load_persona(cfg)
    return {**persona, "llm": persona["novel_llm"]} if persona.get("novel_llm") else persona


def start_writing(request: str, cfg: dict) -> dict:
    """執筆モードに入る（GPU は Bot が先に切り替えておく）。依頼に出てくる作品・キャラを下調べして状態に残す。
    request は入る前の直近の会話（最後の行が依頼の発言）。前の執筆（previous）は、その発言で「続き」を頼まれたときだけ持ち越す。
    返り値: 状態。"""
    persona = novel_persona(cfg)
    old = writing_state(persona["id"])
    notes, studied = study_motifs(request, cfg, persona) \
        if "web_search" in persona_skills(persona) or persona.get("study") else ("", [])
    wants_more = "続き" in (request.strip().splitlines() or [""])[-1]
    st = {"active": True, "started": dt.datetime.now().isoformat(timespec="seconds"), "last": time.time(),
          "request": request[-3000:], "notes": notes, "studied": studied, "turns": [],
          "previous": (_previous_of(old) or old.get("previous")) if wants_more else None}
    save_writing(persona["id"], st)
    return st


def restart_writing(request: str, cfg: dict) -> None:
    """執筆モードのまま、別の小説（キャラ変更）に切り替える。前の小説・下調べ・提案は忘れて、打ち合わせからやり直す
    （下調べは次の writing_turn の打ち合わせで、新しい依頼について行う）。"""
    pid = novel_persona(cfg)["id"]
    save_writing(pid, {"active": True, "started": dt.datetime.now().isoformat(timespec="seconds"), "last": time.time(),
                       "request": request[:3000], "notes": "", "studied": [], "turns": [], "previous": None})


def _previous_of(st: dict) -> dict | None:
    novels = [t["novel"] for t in st.get("turns") or [] if t.get("novel")]
    if not st.get("request") and not novels:
        return None
    return {"request": st.get("request", ""), "novel": novels[-1] if novels else "", "notes": st.get("notes", "")}


def end_writing(pid: str) -> None:
    st = writing_state(pid)
    if st.get("active"):
        save_writing(pid, {"active": False, "previous": _previous_of(st) or st.get("previous")})


OMIT_MARK = re.compile(r"(\s*[（(]以下(略|省略)[）)])+\s*$")   # 本文の末尾に付いてしまった「（以下略）」


def _novel_text(path: str) -> str:
    p = Path(path)
    return OMIT_MARK.sub("", p.read_text(encoding="utf-8")) if p.exists() else ""


# 提案が、相手がすでに頼んだことをなぞるだけで新しさが無かったので、頼まれたこと・出した提案は「もう出たもの」として渡して避けさせる（2026-10-29）
SUGGEST_PROMPT = """いま書いた小説のあとに、相手へ出す「次の展開の提案」を4つ作ってください。
- 登場人物は変えない（いまの小説の人物のまま。新しい人物を主役にしない）。
- 4つとも、まだ話に出ていない新しい要素を1つ入れる。下の「もう出たもの」（依頼・相手の注文・前に出した提案・書いた本文）を
  言い換えただけの案は出さない。相手が自分では思いつかなかった、と感じる案にする。
- 新しい要素の例: 立場や力関係の逆転、第三者の乱入・目撃、隠していた秘密や本心の露見、場所・時間の大きな移動（数日後・別の舞台）、
  道具や状況の変化、取引・賭け・罰ゲーム、視点人物の交代、予想と逆の反応。4つは互いに違う種類にする。
- 「相手の好み」「小説の作法」には合わせる。相手が嫌がりそうなもの（苦手・NG）は出さない。
- 1つ40字以内で、具体的に（登場人物・場所・何が起きるか）。
{memory}
# もう出たもの（なぞらない）
## 依頼
{request}
## 相手の注文
{asked}
## 前に出した提案
{offered}

# いま書いた小説（終わりのほう）
{tail}

JSONのみ: {{"suggestions": ["...", "...", "...", "..."]}}"""
SUGGEST_PICK = re.compile(r"^\s*([1-4１-４])\s*(番|で|でお願い|がいい|にする)?\s*[!！。]*\s*$")


def suggest_next(cfg: dict) -> list[str]:
    """小説を書いたあとの、次の展開の提案4つ（Gemma）。執筆の記録にも残し、番号で選べるようにする。"""
    persona = novel_persona(cfg)
    st = writing_state(persona["id"])
    novels = [t["novel"] for t in st.get("turns") or [] if t.get("novel")]
    if not novels:
        return []
    turns = st.get("turns") or []
    asked = [t["content"] for t in turns if t.get("role") == "user" and t.get("content")][-8:]
    offered = [t["content"].split("\n", 1)[-1] for t in turns
               if str(t.get("content", "")).startswith("（次の展開の提案）")][-3:]
    got = ask_json(SUGGEST_PROMPT.format(memory=memory_block(persona) + craft_block(persona), request=st.get("request", "")[:1500],
                                         asked="\n".join(f"- {a[:200]}" for a in asked) or "（なし）",
                                         offered="\n".join(offered) or "（なし）",
                                         tail=_novel_text(novels[-1])[-2500:]), cfg, persona, 800)
    sugg = [" ".join(str(s).split())[:80] for s in got.get("suggestions") or [] if str(s).strip()][:4]
    if sugg:
        st["suggestions"] = sugg
        st["turns"].append({"role": "assistant", "content": "（次の展開の提案）\n" + "\n".join(f"{i}. {s}" for i, s in enumerate(sugg, 1))})
        save_writing(persona["id"], st)
    return sugg


def looks_broken(text: str) -> bool:
    """崩れた出力か（ハングル・英語のメモの混入、短い文字列のくり返し、日本語の割合が低い）。"""
    if not text.strip():
        return True
    if re.search(r"[가-힣]|\b(Note|Wait):|enough to be", text) or cut_loop(text) != text and len(cut_loop(text)) < len(text) * 0.7:
        return True
    return len(re.findall(r"[ぁ-んァ-ヶ一-龥。、「」！？]", text)) / len(text) < 0.5


# ---- セリフ辞書（ペルソナの line_dict: true のときだけ。いまはルナのみ）。本文の [defeat] などを .local/persona/defeat.txt からランダムな1行に置き換える ----
# 括弧の揺れ（［］【】・大文字）も拾う。「」の外に裸で置かれたタグは「」で囲む。辞書が無い・空ならタグを消す。
LINE_TAG = re.compile(r"(「?)[\[［【]\s*([A-Za-z_]+)\s*[\]］】](」?)")


def fill_lines(body: str) -> str:
    cache: dict[str, list[str]] = {}
    used: dict[str, set[str]] = {}

    def pick(name: str) -> str | None:
        if name not in cache:
            p = ROOT / ".local" / "persona" / f"{name}.txt"
            lines = p.read_text(encoding="utf-8").splitlines() if p.exists() else []
            cache[name] = [s.strip().strip("「」") for s in lines if s.strip() and not s.lstrip().startswith("#")]
            if not cache[name]:
                print(f"[fill_lines] セリフ辞書がありません: {p}", flush=True)
        pool = [s for s in cache[name] if s not in used.setdefault(name, set())] or cache[name]
        if not pool:
            return None
        line = random.choice(pool)
        used[name].add(line)
        return line

    def sub(m: re.Match) -> str:
        o, name, c = m.group(1), m.group(2).lower(), m.group(3)
        line = pick(name)
        if line is None:
            return "" if (o and c) else o + c
        inside = m.string.count("「", 0, m.start()) > m.string.count("」", 0, m.start())
        return o + line + c if (o or c or inside) else f"「{line}」"

    return LINE_TAG.sub(sub, body)


def line_examples(persona: dict) -> str:
    """セリフ辞書から数行ずつ選んで、書き手に「こういう感じ」の例として渡す（毎回選び直す）。"""
    spec = persona.get("line_dict")
    if not isinstance(spec, dict):
        return ""
    n = int(persona.get("line_examples", 3))
    parts = []
    for name, label in spec.items():
        p = ROOT / ".local" / "persona" / f"{name}.txt"
        lines = [s.strip().strip("「」") for s in (p.read_text(encoding="utf-8").splitlines() if p.exists() else [])
                 if s.strip() and not s.lstrip().startswith("#")]
        if lines:
            # 1行を丸ごと渡す（以前は160字で切っていたが、長く崩れていく流れごと見せたい。2026-10-29）
            parts.append(f"## {label}\n" + "\n".join(f"「{s}」" for s in random.sample(lines, min(n, len(lines)))))
    if not parts:
        return ""
    # セリフがほとんど喘ぎだけの回が多かったので、前半は言葉、後半は長い喘ぎ・快楽・悲鳴と配分を決める（2026-10-29）
    return ("\n# セリフの配分\n"
            "- 前半〜中盤は、意味のある言葉のセリフを中心にする: 会話のやりとり、拒絶・抗議・懇願・強がり・挑発、相手への問いかけ、"
            "戸惑いや本音の漏れ。喘ぎは言葉の途中に短く混ぜる程度にし、喘ぎだけのセリフを続けない。相手側のセリフも書く。\n"
            "- 後半（山場から結末）は、長く続いて崩れていく喘ぎ・快楽に溺れた声・悲鳴に移す。言葉は途切れ途切れになり、最後は声だけになってよい。\n"
            "\n# セリフの参考（こういう感じ）\n喘ぎ・快楽・拒絶のセリフは、下の例の崩し方（濁点・途切れ・繰り返し・言い直し）を真似て、"
            "場面と人物に合わせて自分で書く。例をそのまま写さない。タグ（[moan] など）は使わない。"
            "後半では、例のように長く続いて崩れていく喘ぎ・悲鳴を書く（短い一言で済ませない）。\n" + "\n\n".join(parts))


REF_THEME_PROMPT = """次の小説の依頼・打ち合わせに近いテーマを、下の一覧から0〜2個選んでください（近いものが無ければ空）。
# テーマ
{themes}

# 依頼・打ち合わせ
{text}

JSONのみ: {{"themes": ["テーマ名", ...]}}"""


def ref_novel(persona: dict, st: dict, text: str, cfg: dict) -> str:
    """参考小説（ペルソナの ref_novels）。依頼・打ち合わせ・この発言からテーマを選び、その1話をランダムに渡す。
    言葉で当たらなければ Gemma に選ばせる（その答えは話ごとに st["ref_themes"] に覚えておく）。"""
    spec = persona.get("ref_novels") or {}
    themes = spec.get("themes") or {}
    if not themes:
        return ""
    said = "\n".join([st.get("request", "")] + [t["content"] for t in st.get("turns") or []
                                                if t.get("role") == "user" and t.get("content")] + [text or ""])
    hit = [k for k, v in themes.items() if v.get("words") and re.search(v["words"], said)]
    if not hit:
        if "ref_themes" not in st:
            got = ask_json(REF_THEME_PROMPT.format(themes="\n".join(f"- {k}: {v.get('about', '')}" for k, v in themes.items()),
                                                   text=said[-3000:]), cfg, persona, 200)
            st["ref_themes"] = [t for t in got.get("themes") or [] if t in themes]
        hit = st["ref_themes"]
    folder = ROOT / ".local" / "persona"
    files = [p for p in folder.glob("*.md") if re.sub(r"_?\d*$", "", p.stem).upper() in hit]
    if not files:
        return ""
    # テーマを先に選んでから話を選ぶ（ファイルの多いテーマばかりにならないように）
    theme = random.choice(sorted({re.sub(r"_?\d*$", "", p.stem).upper() for p in files}))
    stories = []
    for p in [p for p in files if re.sub(r"_?\d*$", "", p.stem).upper() == theme]:
        for part in re.split(r"(?m)^# .*$", p.read_text(encoding="utf-8", errors="replace")):
            if len(part.strip()) > 300:
                stories.append(part.strip())
    if not stories:
        return ""
    story = random.choice(stories)[:int(spec.get("chars", 6000))]
    print(f"[ref_novel] {theme}（{', '.join(hit)} から）{len(story)}字", flush=True)
    return ("\n# 参考小説（展開・セリフ・描写の参考。同じ話は書かない）\n"
            "下は以前に書かれた別の小説。登場人物は今回の人物に置き換えて読み、展開の運び方、セリフの言い回しと崩し方、"
            "描写の細かさ・語彙・擬音を参考にする。筋・場面の流れ・文はそのまま使わず、今回の依頼と打ち合わせに沿った新しい話を書く。\n"
            "<参考小説>\n" + story + "\n</参考小説>\n")


def writing_turn(text: str, images: list[str], cfg: dict) -> dict:
    """執筆モードの1往復（Gemma）。返り値: {"kind": "novel"|"reply", "text", "title", "path"}。"""
    persona = novel_persona(cfg)
    ncfg = cfg.get("novel") or {}
    st = writing_state(persona["id"])
    m = SUGGEST_PICK.match(text or "")
    if m and st.get("suggestions"):   # 「2」「2番」→ 提案の2番目で書く
        n = int(m.group(1).translate(str.maketrans("１２３４", "1234")))
        if n <= len(st["suggestions"]):
            text = f"{n}番の「{st['suggestions'][n - 1]}」で書いて"
    drafting = not any(t.get("novel") for t in st.get("turns") or [])   # まだ1本も書いていない＝打ち合わせ中
    if drafting and (persona.get("study") or "web_search" in persona_skills(persona)):
        # 打ち合わせで新しく出てきた作品・キャラだけ調べる（調べ済みは資料メモを読み直すだけで、検索はしない）
        notes, names = study_motifs(text, cfg, persona)
        new = [n for n in names if n not in (st.get("studied") or [])]
        if new:
            st["studied"] = (st.get("studied") or []) + new
            st["notes"] = (st.get("notes") or "") + "\n" + notes
            st["new_studied"] = new
        elif notes and not names and notes not in (st.get("notes") or ""):   # 「下調べできなかったもの」だけ
            st["notes"] = (st.get("notes") or "") + "\n" + notes
    if images:   # 参考画像は読み取ったことを文字で残す（次の往復からも使えるように）
        note = chat([{"role": "user", "content": [{"type": "text", "text": IMAGE_NOTE_PROMPT}] +
                      [{"type": "image_url", "image_url": {"url": u}} for u in images]}],
                    llm(cfg, persona, json_mode=False, temperature=0.2, max_tokens=800, timeout=600,
                        extra_body={"chat_template_kwargs": {"enable_thinking": False}}))
        st["image_notes"] = ((st.get("image_notes") or []) + [note.strip()])[-4:]
    system = base_system(persona) + WRITING_RULES.format(chars=int(ncfg.get("chars", 2500))) + line_examples(persona)
    try:
        system += ref_novel(persona, st, text, cfg)
    except Exception as ex:   # noqa: BLE001  参考小説が無くても書ける
        print(f"[ref_novel] {ex}", flush=True)
    if drafting:
        system += INTERVIEW_RULES
        if not (st.get("previous") or {}).get("novel"):   # 前回の「続き」として入ったときは、ふだん通り
            system += FIRST_NOVEL_RULES
        else:
            system += NEXT_NOVEL_RULES
    else:
        system += NEXT_NOVEL_RULES
    system += "\n# 執筆モードに入ったときの依頼\n" + st.get("request", "")
    if st.get("notes"):
        system += st["notes"]
    if st.get("image_notes"):
        system += "\n# 参考画像から読み取ったこと\n" + "\n\n".join(st["image_notes"])
    prev = st.get("previous") or {}
    if prev.get("novel") and not any(t.get("novel") for t in st.get("turns") or []):
        system += ("\n# 前回の執筆（続きを頼まれたら、ここから自然につなげる）\n依頼: " + prev.get("request", "")[:800]
                   + "\n本文（終わりのほう）:\n" + _novel_text(prev["novel"])[-int(ncfg.get("prev_chars", 6000)):])
    turns = (st.get("turns") or [])[-int(ncfg.get("writing_turns", 16)):]
    last_novel_i = max((i for i, t in enumerate(turns) if t.get("novel")), default=-1)
    msgs = [{"role": "system", "content": system}]
    for i, t in enumerate(turns):
        if t.get("novel"):   # 自分が書いた小説。最新は全文、それより前は冒頭だけ（長くなりすぎないように）
            body = _novel_text(t["novel"])
            # 前のものは【小説】の形にしない: 「冒頭＋（以下略）」の形で渡していたら、Gemma が本文の終わりに（以下略）と書くようになった（2026-10-29）
            msgs.append({"role": "assistant", "content": NOVEL_HEAD + "\n" + body if i == last_novel_i else
                         f"（前に書いた小説『{t.get('title', '')}』。ここには冒頭だけ載せる）\n{body[:1200]}"})
        else:
            msgs.append({"role": t["role"], "content": t["content"]})
    msgs.append({"role": "user", "content": text if not images else
                 [{"type": "text", "text": text or "（画像）"}] + [{"type": "image_url", "image_url": {"url": u}} for u in images]})
    sampling = dict(persona.get("novel_sampling") or {})
    temperature = float(sampling.pop("temperature", ncfg.get("temperature", 0.75)))
    # 思考は切る: 4bit（IQ4_XS）の Gemma は思考に入ると英語のメモが本文に漏れて崩れることがあった（2026-10-28）
    opts = llm(cfg, persona, json_mode=False, temperature=temperature,
               max_tokens=int(ncfg.get("max_tokens", 8000)), timeout=1800,
               extra_body={**sampling, "chat_template_kwargs": {"enable_thinking": False}})
    refusal = ""
    if refusal:
        out = refusal
    else:
        out = SKILL_TAG.sub("", chat(msgs, opts)).strip()
        if looks_broken(out):   # 崩れていたら、温度を下げて1回だけ書き直す
            out = SKILL_TAG.sub("", chat(msgs, {**opts, "temperature": min(temperature, 0.7)})).strip()
    st["turns"] = (st.get("turns") or []) + [{"role": "user", "content": (text or "（画像）")[:2000]}]
    if NOVEL_HEAD in out:   # Gemma は「書き始めるね！」などの前置きのあとに【小説】を書くことがある（前置きは捨てる）
        body = OMIT_MARK.sub("", cut_loop(out.split(NOVEL_HEAD, 1)[1].strip()))
        if persona.get("line_dict"):   # ルナだけ。タグが残ってしまったときの保険（ふだんは line_examples の例で書かせる）
            body = fill_lines(body)
        first = body.splitlines()[0].strip() if body else ""
        title = first.strip("『』「」#* ") if first and len(first) <= 40 else st.get("request", "")[:30]
        path = novel_dir() / f"{dt.datetime.now():%Y%m%d-%H%M%S}-{persona['id']}.txt"
        path.write_text(body, encoding="utf-8")
        st["turns"].append({"role": "assistant", "novel": str(path), "title": title})
        res = {"kind": "novel", "text": body, "title": title, "path": path}
    else:
        reply_text = cut_loop(out) or "（うまく返事を作れませんでした。もう一度送ってください）"
        st["turns"].append({"role": "assistant", "content": reply_text[:2000]})
        res = {"kind": "reply", "text": reply_text, "title": "", "path": None}
    res["studied"] = st.pop("new_studied", [])
    st["last"] = time.time()
    save_writing(persona["id"], st)
    return res


# ---- 小説の作法（書き方の決まり）。手で読み書きできる Markdown ファイル .local/persona/<id>_craft.md ----
# 「- 」で始まる行が1項目（番号はこの行の通し番号）。それ以外の行（見出し・説明）も、そのまま書き手に渡す。
CRAFT_HEADER = "# {name}の小説の作法\n（自由に書き換えてOK。「- 」で始まる行が1項目。会話で決まった書き方は自動で下に足されます）\n\n"


def craft_path(pid: str) -> Path:
    return ROOT / ".local" / "persona" / f"{pid}_craft.md"


def load_craft(pid: str) -> str:
    p = craft_path(pid)
    return p.read_text(encoding="utf-8") if p.exists() else ""


def save_craft(pid: str, text: str, name: str = "") -> None:
    p = craft_path(pid)
    p.parent.mkdir(parents=True, exist_ok=True)
    if not text.lstrip().startswith("#"):
        text = CRAFT_HEADER.format(name=name or pid) + text.lstrip("\n")
    p.write_text(text.rstrip() + "\n", encoding="utf-8")


def craft_items(pid: str) -> list[str]:
    return [line[2:].strip() for line in load_craft(pid).splitlines() if line.startswith("- ")]


def add_craft(pid: str, text: str, name: str = "") -> None:
    """作法を1項目足す（「!作法 〇〇」・会話から自動で）。同じものは足さない。"""
    text = " ".join(text.split())[:120]
    if not text or text in craft_items(pid):
        return
    save_craft(pid, load_craft(pid).rstrip() + f"\n- {text}", name)


def remove_craft(pid: str, numbers: list[int]) -> list[str]:
    lines, gone, n = load_craft(pid).splitlines(), [], 0
    kept = []
    for line in lines:
        if line.startswith("- "):
            n += 1
            if n in set(numbers):
                gone.append(line[2:].strip())
                continue
        kept.append(line)
    save_craft(pid, "\n".join(kept))
    return gone


def craft_block(persona: dict) -> str:
    text = load_craft(persona["id"]).strip() if persona.get("memory") else ""
    body = "\n".join(line for line in text.splitlines() if not line.startswith("# ")).strip()
    return f"\n\n# 小説の作法（相手と決めた書き方。小説を書くときは必ず守る）\n{body}\n" if craft_items(persona["id"]) or body else ""


# ---- 画像のプロンプト（MOSS-Image の書き方）。メモリではなくファイルに残す ----
# MOSS-Image のガイド（github.com/OpenMOSS/MOSS-Image USERGUIDE）: タグは
#   品質 → スコア → メタ → 年 → キャラ名 → 作品名 → @絵師 → 一般タグ の順。自然文（英語）と混ぜてよい。
#   「描いて」という指示ではなく、できあがった絵の説明として書く。改行したいところは \n。
PROMPTS_PROMPT = """あなたは画像生成AI「MOSS-Image」のプロンプトを作る係です。下の「お題」から、絵のプロンプト案を {n} 個作ってください。

各案の項目:
- character: キャラクター名の booru タグ（英語、小文字、例 "hatsune miku"）。創作キャラなら [] にして、外見は general に書く。
- series: 作品名の booru タグ（例 "vocaloid"）。無ければ []。
- artist: 絵柄を指定したいときだけ絵師名（"@" は付けない）。ふつうは []。
- general: 一般の booru タグ（人数 "1girl"、髪・目・服装・表情・ポーズ・構図・背景・光・画風。英語、10〜25個）。
- caption: 絵の説明の英語の文章（1〜3文）。指示形（draw/make）ではなく、できあがった絵の描写として書く。
- size: "portrait" / "landscape" / "square" のどれか。
ルール:
- 同じ構図・同じ場面ばかりにしない。場面・アングル・表情・時間帯・衣装を変えて幅を出す。
- 下の「これまでの案」と似たものは作らない。
- 下調べの資料がある実在キャラは、資料の外見に合わせる。
- 覚えている嗜好（好きな絵柄・NG）に合わせる。

お題:
{theme}
{notes}{memory}
これまでの案（似せない）:
{previous}

JSONのみ: {{"prompts": [{{"character": [], "series": [], "artist": [], "general": [], "caption": "...", "size": "portrait"}}]}}"""

SIZES = {"portrait": (896, 1152), "landscape": (1152, 896), "square": (1024, 1024)}   # 約1MP、16の倍数


def prompts_dir() -> Path:
    d = ROOT / ".local" / "luna" / "prompts"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _tags(xs) -> list[str]:
    return [str(x).strip().replace("_", " ") for x in (xs or []) if str(x).strip()]


def assemble(p: dict, mcfg: dict) -> str:
    """MOSS-Image のタグ順で1本のプロンプトにする（最後の行に自然文の説明）。"""
    tags = list(mcfg.get("quality") or ["best quality", "highres"]) + _tags(p.get("character")) + \
        _tags(p.get("series")) + ["@" + a.lstrip("@") for a in _tags(p.get("artist"))] + _tags(p.get("general"))
    seen, uniq = set(), []
    for t in tags:
        if t.lower() not in seen:
            seen.add(t.lower())
            uniq.append(t)
    caption = str(p.get("caption") or "").strip()
    return ", ".join(uniq) + (f"\n{caption}" if caption else "")


def make_prompts(history: list[dict], theme: str, cfg: dict, n: int | None = None) -> dict:
    """お題（会話・小説・参照画像）からプロンプト案を n 個（既定 100）作り、JSON ファイルに保存する。
    返り値: {"path", "title", "count", "samples": [...], "studied": [...]}"""
    persona = load_persona(cfg)
    mcfg = cfg.get("moss") or {}
    n = int(n or mcfg.get("prompt_count", 100))
    batch = int(mcfg.get("batch", 20))
    notes, studied = study_motifs(theme, cfg, persona) if "web_search" in persona_skills(persona) or persona.get("study") else ("", [])
    if theme.startswith(("小説", "さっきの小説", "この小説")) or "小説" in theme[:20]:
        novel = last_novel(persona["id"])
        if novel:
            theme += "\n\n# もとにする小説\n" + novel[:6000]
    images = images_of(history)
    memory = memory_block(persona)
    items: list[dict] = []
    for _ in range(max(1, (n + batch - 1) // batch) + 2):   # 足りなければ2回まで追加で頼む
        if len(items) >= n:
            break
        prev = "\n".join(f"- {i['caption'][:90]}" for i in items[-40:]) or "（まだ無い）"
        got = ask_json(PROMPTS_PROMPT.format(n=min(batch, n - len(items)), theme=theme[:8000], notes=notes,
                                             memory=memory, previous=prev), cfg, persona, max_tokens=6000, images=images)
        for p in got.get("prompts") or []:
            if not isinstance(p, dict) or not (p.get("general") or p.get("caption")):
                continue
            size = p.get("size") if p.get("size") in SIZES else "portrait"
            item = {"id": len(items) + 1, "character": _tags(p.get("character")), "series": _tags(p.get("series")),
                    "artist": _tags(p.get("artist")), "general": _tags(p.get("general")),
                    "caption": str(p.get("caption") or "").strip(), "size": size}
            item["prompt"] = assemble(item, mcfg)
            items.append(item)
    if not items:
        raise LLMError("プロンプトを作れませんでした（モデルの返事を読めませんでした）")
    title = re.sub(r"\s+", " ", theme.splitlines()[0])[:40]
    doc = {"title": title, "created": dt.datetime.now().isoformat(timespec="seconds"), "persona": persona["id"],
           "theme": theme[:3000], "studied": studied, "with_images": len(images),
           "negative": mcfg.get("negative") or "nsfw, worst quality, bad anatomy, watermark, jpeg artifact",
           "prompts": items[:n]}
    path = prompts_dir() / f"{dt.datetime.now():%Y%m%d-%H%M%S}-{_slug(title)[:30]}.json"
    path.write_text(json.dumps(doc, ensure_ascii=False, indent=1), encoding="utf-8")
    return {"path": path, "title": title, "count": len(doc["prompts"]), "samples": doc["prompts"][:3], "studied": studied}


def latest_prompts() -> Path | None:
    files = sorted(prompts_dir().glob("*.json"))
    return files[-1] if files else None


def pick_prompts(path: Path, arg: str, k: int) -> list[dict]:
    """番号の指定（「3 15 42」）があればそれ、無ければ全体から k 個を等間隔に選ぶ。"""
    items = json.loads(path.read_text(encoding="utf-8"))["prompts"]
    nums = [int(x) for x in re.findall(r"\d+", arg or "")]
    if nums and not re.search(r"(\d+)\s*(枚|個|つ)", arg):
        chosen = [i for i in items if i["id"] in set(nums)]
        if chosen:
            return chosen[:12]
    m = re.search(r"(\d+)\s*(枚|個|つ)", arg or "")
    k = min(int(m.group(1)), 12) if m else k
    if len(items) <= k:
        return items
    step = len(items) / k
    return [items[int(i * step)] for i in range(k)]


# ---- 描画セット（ComfyUI に入ったら出ずに描き続けるための、人物の候補5つ＋シチュエーション20個） ----
# 流れ: Strata で作る（make_draw_set）→ ComfyUI で人物だけの候補5枚（同じシード）→ 番号で1つ選ぶ →
#       その人物・絵柄を固定してシチュエーション20枚 → 数字でさらに追加、0 で終わり（Bot の描画モード）。
# キャラ名は Danbooru のタグ（日本語名 → wiki の other_names → タグ）で引く。「kurumi \(lycoris recoil\)」の形が一番再現しやすい。
DANBOORU = "https://danbooru.donmai.us"
CHARA_PROMPT = """次のお題に出てくる登場人物を挙げてください（主役から順に最大2人）。
各人物: name_ja（日本語の名前）、series_ja（出典の作品。創作キャラなら ""）、name_en（英語・ローマ字の名前の推測）、
series_en（作品の英語名の推測）、appearance（外見の booru タグ。英語・小文字で10個前後: 人数 "1girl"、髪の色と型、目の色、体格、いつもの服装、特徴的な小物）。
下調べの資料があれば、その外見と booru タグに合わせる。

お題:
{theme}
{notes}
JSONのみ: {{"characters": [{{"name_ja": "...", "series_ja": "...", "name_en": "...", "series_en": "...", "appearance": ["..."]}}]}}"""
SITUATION_PROMPT = """画像生成AI「MOSS-Image」で、ある登場人物の絵を {n} 枚描きます。人物の名前・髪・目などの「その人らしさ」は別に指定するので、
ここでは「シチュエーション」だけを {n} 個作ってください。

各案の項目:
- general: booru タグ（英語・小文字、10〜20個）。服装、ポーズ・しぐさ、表情、場所・背景、時間帯・光、構図・アングル、起きていること。
  人物の名前・髪の色・目の色は書かない。人数（1girl など）は書く。
- caption: 場面の英語の説明（1〜2文。人物は "the girl" などと呼ぶ。できあがった絵の描写として書く）。
- size: "portrait" / "landscape" / "square"。
ルール:
- お題・小説の場面を中心に、服装・場所・アングル・表情に幅を出す（同じ場面ばかりにしない）。
- 覚えている嗜好（好きな絵柄・NG）に合わせる。

お題:
{theme}
{memory}
JSONのみ: {{"situations": [{{"general": [], "caption": "...", "size": "portrait"}}]}}"""
CANDIDATE_BASE = ["solo", "simple background", "white background", "standing", "cowboy shot", "looking at viewer"]


def _danbooru(path: str, params: dict) -> list:
    try:
        r = requests.get(f"{DANBOORU}/{path}.json", params=params, headers={"User-Agent": "info-digest/1.0"}, timeout=20)
        return r.json() if r.ok else []
    except (requests.RequestException, ValueError):
        return []


def _tag_counts(names: list[str]) -> dict[str, tuple[int, int]]:
    rows = _danbooru("tags", {"search[name_comma]": ",".join(names), "limit": 20}) if names else []
    return {t["name"]: (t["category"], t["post_count"]) for t in rows}


def danbooru_character(c: dict) -> dict:
    """人物の Danbooru タグを探す。返り値: {"tag", "posts", "series", "alts": [衣装違いなどのタグ]}（見つからなければ tag ""）。"""
    def slug(s: str) -> str:
        return re.sub(r"[^a-z0-9]+", "_", (s or "").lower()).strip("_")
    if not (c.get("series_ja") or c.get("series_en")):   # 創作キャラ（ルナ・小説のオリジナル）は引かない（同名の別人を拾うので）
        return {"tag": "", "posts": 0, "series": "", "alts": []}
    wiki = {w["title"]: (w.get("body") or "").lower()
            for w in _danbooru("wiki_pages", {"search[other_names_match]": c.get("name_ja", ""), "limit": 8}) if c.get("name_ja")}
    cands = list(wiki)
    if c.get("name_en"):
        cands += [t["name"] for t in _danbooru("tags", {"search[name_matches]": f"*{slug(c['name_en'])}*", "search[category]": 4,
                                                      "search[order]": "count", "limit": 8})]
    counts = _tag_counts(list(dict.fromkeys(cands)))
    ser = slug(c.get("series_en"))
    series_tags = [t["name"] for t in _danbooru("tags", {"search[name_matches]": f"*{ser}*", "search[category]": 3,
                                                         "search[order]": "count", "limit": 3})] if ser else []
    series = series_tags[0] if series_tags else ""
    marks = [m for m in {ser, series} if m]

    def score(n: str) -> int:
        """同名の別キャラ（「クルミ」→ tokisaki_kurumi / kurumi_(blue_archive) …）より、作品の合うものを上に。"""
        if any(m in n for m in marks):
            return 3   # タグ名に作品名（kurumi_(lycoris_recoil)）
        if n in wiki and any(m in wiki[n] or m.replace("_", " ") in wiki[n] for m in marks):
            return 2   # 日本語名の wiki で、本文に作品名
        return 1 if n in wiki else 0
    chars = sorted([(n, p) for n, (cat, p) in counts.items() if cat == 4], key=lambda x: (-score(x[0]), -x[1]))
    if not chars or (marks and score(chars[0][0]) == 0):   # 作品の合う人物が見つからない（英語名が同じだけの別人を使わない）
        return {"tag": "", "posts": 0, "series": series, "alts": []}
    tag, posts = chars[0]
    base = re.sub(r"_\(.+\)$", "", tag)
    alts = [t["name"] for t in _danbooru("tags", {"search[name_matches]": f"{base}_*", "search[category]": 4,
                                                  "search[order]": "count", "limit": 6})
            if t["name"] != tag and t["post_count"] > 0 and (not series or series in t["name"])]
    return {"tag": tag, "posts": posts, "series": series, "alts": alts[:2]}


def esc_tag(tag: str, underscores: bool = False) -> str:
    """Danbooru タグ → プロンプトの書き方（括弧は ComfyUI の強調記号にならないよう \\( \\) に）。"""
    t = tag if underscores else tag.replace("_", " ")
    return t.replace("(", r"\(").replace(")", r"\)")


def character_candidates(c: dict, db: dict) -> list[dict]:
    """主役の人物指定を5パターン。各: {"label", "character": [タグ], "series": [タグ], "caption": 頭に付ける英文}。"""
    look = _tags(c.get("appearance"))
    if db.get("tag"):
        tag, series = db["tag"], ([esc_tag(db["series"])] if db.get("series") else [])
        name = re.sub(r"_\(.+\)$", "", tag).replace("_", " ").title()
        fifth = ({"label": f"衣装違いの公式タグ（{db['alts'][0]}）", "character": [esc_tag(db["alts"][0])], "series": series, "caption": ""}
                 if db.get("alts") else {"label": "名前なし・外見のタグだけ", "character": look, "series": series, "caption": ""})
        return [
            {"label": f"公式タグ（{esc_tag(tag)}、{db.get('posts', 0)}件）", "character": [esc_tag(tag)], "series": series, "caption": ""},
            {"label": "公式タグ＋外見のタグ", "character": [esc_tag(tag)] + look, "series": series, "caption": ""},
            {"label": "アンダーバーの形", "character": [esc_tag(tag, True)], "series": [esc_tag(db["series"], True)] if db.get("series") else [],
             "caption": ""},
            {"label": "自然文＋外見のタグ", "character": look, "series": [],
             "caption": f"{name} from {c.get('series_en') or db.get('series', '').replace('_', ' ').title()}."},
            fifth,
        ]
    name, series = (c.get("name_en") or "").strip(), (c.get("series_en") or "").strip().lower()   # Danbooru に無い（創作キャラなど）
    if series:   # 作品はあるが Danbooru で見つからなかった
        named = [esc_tag(f"{name.lower()} ({series})")] if name else []
        return [
            {"label": "推測した名前タグ", "character": named, "series": [series], "caption": ""},
            {"label": "推測した名前タグ＋外見のタグ", "character": named + look, "series": [series], "caption": ""},
            {"label": "外見のタグ＋作品名", "character": look, "series": [series], "caption": ""},
            {"label": "自然文＋外見のタグ", "character": look, "series": [], "caption": f"{name or 'A girl'} from {c['series_en']}."},
            {"label": "外見のタグだけ", "character": look, "series": [], "caption": ""},
        ]
    who = f"A girl named {name}." if name else ""   # 創作キャラ: 外見の書き方と絵柄で幅を出す
    return [
        {"label": "外見のタグだけ", "character": look, "series": [], "caption": ""},
        {"label": "外見のタグ＋original", "character": look + ["original"], "series": [], "caption": ""},
        {"label": "自然文＋外見のタグ", "character": look, "series": [], "caption": who},
        {"label": "外見のタグ＋アニメ塗り", "character": look + ["anime coloring", "cel shading"], "series": [], "caption": ""},
        {"label": "外見のタグ＋厚塗り", "character": look + ["thick painting", "detailed shading"], "series": [], "caption": ""},
    ]


def make_draw_set(history: list[dict], theme: str, cfg: dict) -> dict:
    """描画セットを作って .local/luna/prompts/*-drawset.json に残す（Strata）。返り値: {"path", "title", "doc"}。"""
    persona = load_persona(cfg)
    mcfg = cfg.get("moss") or {}
    n = int(mcfg.get("situation_count", 20))
    notes, studied = study_motifs(theme, cfg, persona) if "web_search" in persona_skills(persona) or persona.get("study") else ("", [])
    if theme.startswith(("小説", "さっきの小説", "この小説")) or "小説" in theme[:20]:
        novel = last_novel(persona["id"])
        if novel:
            theme += "\n\n# もとにする小説\n" + novel[:6000]
    chars = (ask_json(CHARA_PROMPT.format(theme=theme[:6000], notes=notes), cfg, persona, 1200).get("characters") or [])[:2]
    found = [{**c, "danbooru": danbooru_character(c)} for c in chars if isinstance(c, dict)]
    main = found[0] if found else {"appearance": ["1girl"], "danbooru": {}}
    others = [esc_tag(c["danbooru"]["tag"]) if c["danbooru"].get("tag") else ", ".join(_tags(c.get("appearance"))) for c in found[1:]]
    sits = []
    for _ in range(3):   # 足りなければ追加で頼む
        if len(sits) >= n:
            break
        got = ask_json(SITUATION_PROMPT.format(n=n - len(sits), theme=theme[:8000], memory=memory_block(persona)),
                       cfg, persona, max_tokens=6000, images=images_of(history))
        for s in got.get("situations") or []:
            if isinstance(s, dict) and s.get("general"):
                sits.append({"id": len(sits) + 1, "general": _tags(s["general"]), "caption": str(s.get("caption") or "").strip(),
                             "size": s.get("size") if s.get("size") in SIZES else "portrait"})
    if not sits:
        raise LLMError("シチュエーションを作れませんでした（モデルの返事を読めませんでした）")
    title = re.sub(r"\s+", " ", theme.splitlines()[0])[:40]
    doc = {"kind": "drawset", "title": title, "created": dt.datetime.now().isoformat(timespec="seconds"), "persona": persona["id"],
           "theme": theme[:3000], "studied": studied, "characters": found, "others": others,
           "candidates": character_candidates(main, main.get("danbooru") or {}), "situations": sits[:n],
           "negative": mcfg.get("negative") or "nsfw, worst quality, bad anatomy, watermark, jpeg artifact"}
    path = prompts_dir() / f"{dt.datetime.now():%Y%m%d-%H%M%S}-{_slug(title)[:30]}-drawset.json"
    path.write_text(json.dumps(doc, ensure_ascii=False, indent=1), encoding="utf-8")
    return {"path": path, "title": title, "doc": doc}


def draw_prompt(doc: dict, cand: int, situation: dict | None, mcfg: dict) -> tuple[str, str]:
    """描画セットから1枚分のプロンプト（MOSS-Image の順: 品質 → キャラ → 作品 → 一般 → 自然文）とサイズ。
    situation が None なら、人物だけの候補（背景なし）。"""
    c = doc["candidates"][cand]
    general = (situation or {}).get("general") or CANDIDATE_BASE
    tags = list(mcfg.get("quality") or ["best quality", "highres"]) + c["character"] + doc.get("others", []) + c["series"] + general
    seen, uniq = set(), []
    for t in tags:
        if t and t.lower() not in seen:
            seen.add(t.lower())
            uniq.append(t)
    caption = " ".join(x for x in [c.get("caption", ""), (situation or {}).get("caption", "")] if x).strip()
    return ", ".join(uniq) + (f"\n{caption}" if caption else ""), (situation or {}).get("size", "portrait")


def latest_draw_set() -> Path | None:
    files = sorted(prompts_dir().glob("*-drawset.json"))
    return files[-1] if files else None


def draw_session_path(pid: str) -> Path:
    return Path(os.environ.get("INFO_DIGEST_STATE_DIR") or ROOT / "state") / f"draw_{pid}.json"


def draw_session(pid: str) -> dict:
    p = draw_session_path(pid)
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {"active": False}


def save_draw_session(pid: str, st: dict) -> None:
    p = draw_session_path(pid)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(st, ensure_ascii=False, indent=1), encoding="utf-8")


# ---- 嗜好の記憶 ----
MEMORY_PROMPT = """あなたは、会話から「相手（利用者）の嗜好」を記録する係です。
下の「いまの記憶」と「直近の会話」を読み、記憶を更新してください。

記録するもの:
- like: 相手の好きなもの・趣味・話し方の好み・うれしかったこと
- dislike: 苦手なもの・嫌がったこと・NG
- novel: 小説のシチュエーションの好み（どんな状況・展開・関係性・場面・描写を好むか、書いた小説への感想から分かる好み）。
         キャラクター名・その話だけの設定（誰が誰の妹か、名前の綴りなど）は嗜好ではないので記録しない
- craft: 小説の作法＝書き方の決まり（文体、視点・人称、地の文と台詞の割合、描写の細かさ、擬音・表現の使い方、
         避ける言い回し、改行・段落の作り方、長さなど。「次からはこう書いて」「〜はやめて」という書き方への注文）
- profile: 相手自身のこと（呼ばれたい名前、仕事の様子、生活リズムなど。本人が話したことだけ）
ルール:
- あなた（キャラクター）自身の名前・設定は記録しない（profile は相手のことだけ）。
- 会話にはっきり出たことだけ。推測で足さない。1件は短い1文（40字以内）。
- 一度きりの依頼の中身（「〜の話を書いて」そのもの）は記録しない。次からも使える、シチュエーションの好みの形にして記録する
  （例: ×「屋上で缶コーヒーの話」×「後輩キャラの名前は湊くん」→ ○「放課後の屋上で二人きりになる場面が好き」「甘めの話が好き」）。
- 前の記憶と食い違う新しい発言があれば、古い方を消して新しい方を残す（remove に古い番号）。
- 患者さん・同僚など、相手以外の実在の人を特定できる情報は記録しない。
- 新しく記録することが無ければ add も remove も空にする。

いまの記憶（番号: 種類: 内容）:
{memory}

直近の会話:
{talk}

JSONのみを出力: {{"add": [{{"kind": "like|dislike|novel|craft|profile", "text": "..."}}], "remove": [番号]}}"""

KIND_LABEL = {"profile": "相手のこと", "like": "好きなもの", "dislike": "苦手・NG", "novel": "小説の好み（シチュエーション）"}
OLD_KIND_LABEL = {"小説の好み・設定": "novel"}   # 以前の見出し（ファイルに残っていても読めるように）
# craft（小説の作法）は記憶ではなく作法ファイル（craft_path）に足す


# 記憶は手で編集できる Markdown（作法ファイルと同じ場所）。「## 種類」の下に「- 内容（日付）」を1行1項目。
# 以前の state/strata_memory_<id>.json は、最初に読んだときにこちらへ移す（2026-10-29）
MEMORY_HEAD = ("# {name}の記憶\n"
               "（このファイルは直接編集してよい。「## 種類」の下に「- 内容」を1行1項目。末尾の（日付）は無くてもよい。\n"
               "  種類は「相手のこと／好きなもの／苦手・NG／小説の好み（シチュエーション）」。ほかの見出しを作ってもそのまま渡す。\n"
               "  多すぎると日付の古いものから忘れる。会話から覚えたことは Bot が書き足す）\n")
MEMORY_LINE = re.compile(r"^\s*[-*・]\s*(.+?)\s*(?:[（(](\d{4}-\d{2}-\d{2})[）)])?\s*$")


def memory_path(pid: str) -> Path:
    return ROOT / ".local" / "persona" / f"{pid}_memory.md"


def _old_memory_path(pid: str) -> Path:
    return Path(os.environ.get("INFO_DIGEST_STATE_DIR") or ROOT / "state") / f"strata_memory_{pid}.json"


def load_memory(pid: str) -> list[dict]:
    p = memory_path(pid)
    if not p.exists():
        old = _old_memory_path(pid)
        if not old.exists():
            return []
        items = json.loads(old.read_text(encoding="utf-8")).get("items", [])
        save_memory(pid, items)
        old.rename(old.with_suffix(".json.migrated"))
        return items
    kinds = {**OLD_KIND_LABEL, **{label: kind for kind, label in KIND_LABEL.items()}}
    items, kind = [], None
    for line in p.read_text(encoding="utf-8").splitlines():
        if line.startswith("## "):
            label = line[3:].strip()
            kind = kinds.get(label, label)   # 自分で作った見出しは、見出しの名前をそのまま種類に
            continue
        m = MEMORY_LINE.match(line)
        if kind and m:
            items.append({"kind": kind, "text": m.group(1), "date": m.group(2) or ""})
    return items


def save_memory(pid: str, items: list[dict]) -> None:
    p = memory_path(pid)
    p.parent.mkdir(parents=True, exist_ok=True)
    name = next((x["name"] for x in personas(section_config()).values() if x["id"] == pid), pid)
    out = [MEMORY_HEAD.format(name=name)]
    for kind in list(KIND_LABEL) + [k for k in dict.fromkeys(i["kind"] for i in items) if k not in KIND_LABEL]:
        xs = [i for i in items if i.get("kind") == kind]
        if xs:
            out.append(f"## {KIND_LABEL.get(kind, kind)}\n" +
                       "\n".join(f"- {i['text']}" + (f"（{i['date']}）" if i.get("date") else "") for i in xs) + "\n")
    text = "\n".join(out)
    if not p.exists() or p.read_text(encoding="utf-8") != text:   # 変わらなければ書かない（手で開いているファイルを上書きしない）
        p.write_text(text, encoding="utf-8")


def memory_block(persona: dict) -> str:
    if not persona.get("memory"):
        return ""
    items = load_memory(persona["id"])
    if not items:
        return ""
    out = ["\n\n# 覚えている相手の嗜好（会話で自然に活かす。いちいち「覚えてるよ」と言わなくてよい）"]
    for kind in list(KIND_LABEL) + [k for k in dict.fromkeys(i["kind"] for i in items) if k not in KIND_LABEL]:
        xs = [i["text"] for i in items if i.get("kind") == kind]
        if xs:
            out.append(f"## {KIND_LABEL.get(kind, kind)}\n" + "\n".join(f"- {x}" for x in xs))
    return "\n".join(out) + "\n"


def update_memory(persona: dict, history: list[dict], cfg: dict) -> list[str]:
    """直近の会話から嗜好を抜き出して記憶を更新する。新しく覚えたことの一覧を返す。"""
    if not persona.get("memory"):
        return []
    items = load_memory(persona["id"])
    mem = "\n".join(f"{i}: {m['kind']}: {m['text']}" for i, m in enumerate(items)) or "（まだ無い）"
    talk = "\n".join(f"{'相手' if m['role'] == 'user' else persona['name']}: {text_of(m['content'])[:600]}" for m in history[-4:])
    try:
        got = ask_json(MEMORY_PROMPT.format(memory=mem, talk=talk), cfg, persona, max_tokens=800)
    except LLMError:
        return []
    drop = {int(x) for x in got.get("remove") or [] if str(x).isdigit()}
    items = [m for i, m in enumerate(items) if i not in drop]
    have = {m["text"] for m in items}
    added = []
    for a in got.get("add") or []:
        kind, text = str(a.get("kind", "")).strip(), str(a.get("text", "")).strip()
        if kind == "craft" and text:   # 作法は手で編集できるファイルへ
            if text not in craft_items(persona["id"]):
                add_craft(persona["id"], text, persona.get("name", ""))
                added.append("作法: " + text)
        elif kind in KIND_LABEL and text and text not in have:
            items.append({"kind": kind, "text": text[:80], "date": dt.date.today().isoformat()})
            added.append(text)
    mcfg = cfg.get("memory") or {}
    limit = int(mcfg.get("max_items", 80))
    if len(items) > limit:   # 多すぎたら日付の古いものから忘れる（手で書いた日付なしの項目は忘れない）
        keep = {id(i) for i in sorted(items, key=lambda i: i.get("date") or "9999")[-limit:]}
        items = [i for i in items if id(i) in keep]
    save_memory(persona["id"], items)
    if added and len(items) > int(mcfg.get("compact_at", 50)):   # 増えすぎたら、似たものをまとめて減らす
        try:
            before, after = compact_memory(persona, cfg)
            if after < before:
                added.append(f"（記憶が多くなったので {before}→{after} 件にまとめた）")
        except Exception as ex:   # noqa: BLE001
            print(f"[compact_memory] {ex}", flush=True)
    return added


COMPACT_PROMPT = """次は、相手（利用者）の嗜好の記憶です。似たもの・重なるものを1つの文にまとめて、全体を {target} 件以内に圧縮してください。
- 残すのは嗜好（どんなシチュエーション・展開・場面・描写・関係性を好むか、苦手・NG）。好みの具体的な中身（どんな場面・描写か）は
  まとめた文の中に残す。
- キャラクター名や、その話だけの設定（誰が誰の妹か、名前・家族構成など）は嗜好ではないので捨てる。
  ただし設定から分かる好みは、名前を外した一般的な形で残す（例: 「まひろの妹に覗かれる」→「身内に覗かれる羞恥」）。
- 食い違うものは日付の新しい方を残す。新しい情報や推測は足さない。
- kind（種類）は元のまま変えない。違う kind 同士はまとめない。1件は60字以内。

# いまの記憶（番号: kind: 内容（日付））
{memory}

JSONのみ: {{"items": [{{"kind": "...", "text": "..."}}]}}"""


def compact_memory(persona: dict, cfg: dict, target: int | None = None) -> tuple[int, int]:
    """記憶の似た項目をまとめて減らす（前のファイルは .local/persona/backup/ に残す）。返り値: (前の件数, 後の件数)。"""
    pid = persona["id"]
    items = load_memory(pid)
    target = target or int((cfg.get("memory") or {}).get("compact_to", 30))
    if len(items) <= target:
        return len(items), len(items)
    mem = "\n".join(f"{i}: {m['kind']}: {m['text']}" + (f"（{m['date']}）" if m.get("date") else "") for i, m in enumerate(items, 1))
    got = ask_json(COMPACT_PROMPT.format(target=target, memory=mem), cfg, persona, max_tokens=4000)
    kinds = {m["kind"] for m in items}
    new = [{"kind": str(a.get("kind")), "text": " ".join(str(a.get("text", "")).split())[:80], "date": dt.date.today().isoformat()}
           for a in got.get("items") or [] if str(a.get("kind")) in kinds and str(a.get("text", "")).strip()]
    # 壊れた答え（空・少なすぎ・減っていない）なら、何も変えない
    if len(new) < min(5, target) or len(new) >= len(items):
        raise LLMError(f"まとめた結果が使えませんでした（{len(items)}→{len(new)} 件）")
    backup = ROOT / ".local" / "persona" / "backup"
    backup.mkdir(parents=True, exist_ok=True)
    src = memory_path(pid)
    (backup / f"{pid}_memory-{dt.datetime.now():%Y%m%d-%H%M%S}.md").write_text(src.read_text(encoding="utf-8"), encoding="utf-8")
    save_memory(pid, new)
    return len(items), len(new)


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
