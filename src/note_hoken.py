"""note 水曜号「保険と公費のギモン」の下書きを作る（公開はユーザーが手で行う）。

素材: 厚労省などの行政資料の一覧ページ（疑義解釈・通知・事務連絡）と新着 RSS（config の note_hoken.pages / feeds）。
      質問サイト（しろぼんねっと）のよくある質問は、利用規約を確認できたら note_hoken.questions で有効にする。
書き方: style/hoken_kouhi.yaml（1テーマを Q&A で。答え → 根拠 → 条件・例外 → 出典）。
流れ: 新しい行政資料を集める → Strata が薬剤師に関わる資料を選ぶ → NotebookLM に読ませて、資料にある問・項目を列挙
      → Strata がテーマと「現場の疑問」を決める → NotebookLM のウェブ調査で告示・通知の原文を足す
      → NotebookLM が Q&A を書く → 音声（AI音声）→ 機械チェック（確認メモ）→ .md / .html / note の下書き保存 → Discord に通知。

使い方: python src/note_hoken.py                 作って note に下書き保存し、Discord に通知
        python src/note_hoken.py --dry-run       作るだけ（音声は作らない・note にも Discord にも送らない）
        python src/note_hoken.py --dry-run --audio   dry-run でも音声を作る（NotebookLM の音声の1日上限を1つ使う）
"""
import datetime as dt
import html
import json
import os
import re
import sys
import unicodedata
from urllib.parse import urljoin, urlparse

import feedparser

import ai_digest
from ai_digest import CHAT, llm_settings
from common import ROOT, http_get, load_config, load_state, post_discord, post_discord_file, save_state
from note_weekly import (EMOJI, clean, is_primary_host, load_style, now_jst, parse_obj, sentences,
                         split_suggestions)

SECTION = "note_hoken"
LABELS = [("answer", "答え"), ("basis", "根拠"), ("cond", "条件・例外"), ("source", "出典")]

# 行政資料とみなすドメイン（config の primary_domains で置き換えられる）
PRIMARY = ["mhlw.go.jp", "ssk.or.jp", "kokuho.or.jp", "e-gov.go.jp", "go.jp", "lg.jp", "nanbyou.or.jp",
           "shouman.jp", "pmda.go.jp"]
DEFAULT_KEYWORDS = ["診療報酬", "調剤報酬", "疑義解釈", "保険医療", "保険薬局", "算定", "施設基準", "薬価", "中央社会保険医療協議会",
                    "中医協", "医療保険", "公費", "難病", "小児慢性", "生活保護", "医療扶助", "自立支援医療", "高額療養費",
                    "レセプト", "審査支払", "マイナ保険証", "電子処方箋", "後発医薬品", "選定療養", "長期収載品", "医療費助成"]

PICK_DOCS_PROMPT = """保険診療・公費負担医療の行政資料の一覧です。病院・薬局の薬剤師の業務（調剤、薬剤管理指導、外来化学療法、
在宅、公費の請求など）に関わる内容が含まれていそうな資料を、最大{n}件選んでください。
新しい疑義解釈・通知・事務連絡を優先。会議の開催案内・議事録・人事・統計調査は選ばない。
最近の号で使った資料（避ける）: {used}

{docs}

JSONのみを出力: {{"docs": [番号, ...]}}"""

OUTLINE_PROMPT = """資料（行政資料）の中から、病院・薬局の薬剤師や医療事務の業務に関わる問・項目を、最大{n}個挙げてください。
資料に書かれているものだけ。1項目 = 1つの問・規定。

JSONのみを出力:
{{"items": [{{"doc": "資料名（短く）", "no": "問番号や項番（無ければ \\"\\"）", "point": "要旨（40字以内）"}}]}}"""

THEME_PROMPT = """あなたは、薬剤師・医療事務向けマガジン「保険と公費のギモン」の編集者です。今週のテーマを1つ決めてください。
対象読者: 病院・薬局の薬剤師、医療事務。

行政資料に書かれている問・項目（番号: 資料｜問番号｜要旨）:
{outline}

現場でよく出る質問（質問サイトの質問タイトル。空なら無し）:
{questions}

最近の号のテーマ（同じものは避ける）:
{history}

決め方:
- 上の問・項目のうち、薬剤師の業務に関わり、現場で迷いやすいものを中心に、1つの加算・制度・手続きに絞ったテーマにする。
- 広すぎるテーマ（「令和8年度改定のまとめ」など）は避ける。
- 問・項目が無いときは、公費負担医療（難病・小児慢性・生活保護・自立支援医療・高額療養費など）や算定でよく迷う手続きから選ぶ。
- questions は、読者が実際に抱く疑問の形（「〜は算定できる？」「〜の場合、公費は使える？」）で4〜6個。

JSONのみを出力:
{{"theme": "テーマ（20字以内）", "items": [使う問・項目の番号, ...], "questions": ["...", "..."],
  "research": "告示・通知・事務連絡の原文を探すための調査指示（日本語、1〜2文）", "query": "ウェブ検索語（10語以内）"}}"""

WRITE_PROMPT = """{digest}
{rules}
今回のテーマ: 「{theme}」
読者が抱く疑問（この順に、資料で答えが確かめられるものだけを Q&A にする。資料で答えられない疑問は捨ててよい。
資料にある関連の問を足してもよい。最大{max_qa}問）:
{questions}

資料の一覧（出典の url はここから選ぶ）:
{sources}

書き方の見本（この文体・文末で書く）:
{example}

- q: 小見出し。「Q. 」で始まる問いの形。40字以内。
- answer: 1〜2文。1文40字以内。算定できる／できない、対象／対象外を先に。
- basis: 通知・告示・事務連絡の名前と項番（疑義解釈は問番号）。
- cond: 答えが変わる条件・例外・届出・経過措置（任意、1〜2文）。資料に無ければ ""。
- source: 出典の表記（例「厚生労働省 疑義解釈資料の送付について（その13）」）。url: 上の一覧のURL。
- three_lines: 「今回の3行」。最も大事な答え3つを、それぞれ30字以内の1文で。
- hashtags: 点数・加算名・制度名から4〜7個。「#」は付けない。

JSONのみを出力:
{{"three_lines": ["...", "...", "..."], "hashtags": ["..."],
  "qa": [{{"q": "Q. ...", "answer": "...", "basis": "...", "cond": "...", "source": "...", "url": "..."}}]}}"""

REIWA = re.compile(r"令和\s*(\d+|元)\s*年\s*(\d+)\s*月\s*(\d+)\s*日")


# ---- 素材を集める ----
def reiwa_date(text: str) -> dt.date | None:
    m = REIWA.search(unicodedata.normalize("NFKC", text))
    if not m:
        return None
    y = 2018 + (1 if m.group(1) == "元" else int(m.group(1)))
    try:
        return dt.date(y, int(m.group(2)), int(m.group(3)))
    except ValueError:
        return None


def page_docs(src: dict) -> list[dict]:
    """一覧ページのリンクのうち、文字に「令和X年M月D日」があるもの（疑義解釈・通知・事務連絡の一覧の形）。"""
    r = http_get(src["url"])
    r.encoding = r.apparent_encoding or r.encoding
    out = []
    for href, text in re.findall(r'<a\s[^>]*href="([^"]+)"[^>]*>(.*?)</a>', r.text, re.S):
        title = re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", "", text))).strip()
        day = reiwa_date(title)
        if title and day:
            title = unicodedata.normalize("NFKC", re.sub(r"[［\[][\d.,]+\s*[KM]B[］\]]", "", title)).strip()
            out.append({"title": title, "link": urljoin(src["url"], href), "date": day.isoformat(),
                        "source": src.get("name", "")})
    return out


def feed_docs(src: dict, keywords: list[str]) -> list[dict]:
    feed = feedparser.parse(http_get(src["url"]).content)
    out = []
    for e in feed.entries:
        title = unicodedata.normalize("NFKC", e.get("title", "")).strip()
        if not any(k in title for k in keywords):
            continue
        day = (e.get("updated") or e.get("published") or "")[:10]
        out.append({"title": title, "link": e.get("link", ""), "date": day, "source": src.get("name", "")})
    return out


def collect_docs(cfg: dict, today: dt.date) -> list[dict]:
    since = (today - dt.timedelta(days=int(cfg.get("lookback_days", 30)))).isoformat()
    keywords = cfg.get("keywords") or DEFAULT_KEYWORDS
    docs, seen = [], set()
    for kind, srcs in (("page", cfg.get("pages") or []), ("feed", cfg.get("feeds") or [])):
        for src in srcs:
            try:
                got = page_docs(src) if kind == "page" else feed_docs(src, keywords)
            except Exception as ex:   # noqa: BLE001  1つの取得元が落ちても続ける
                print(f"{src.get('name')}: 取得に失敗 {ex}")
                continue
            for d in got:
                if d["link"] not in seen and since <= d["date"] <= today.isoformat():
                    seen.add(d["link"])
                    docs.append(d)
    docs.sort(key=lambda d: d["date"], reverse=True)
    return docs


def site_questions(cfg: dict) -> list[str]:
    """質問サイトのよくある質問（タイトルのみ）。利用規約・robots.txt を確認してから questions.enabled を true にする。"""
    q = cfg.get("questions") or {}
    if not q.get("enabled"):
        return []
    try:
        r = http_get(q["url"])
        r.encoding = r.apparent_encoding or r.encoding
        titles = [re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", "", t))).strip()
                  for t in re.findall(q.get("title_pattern") or r"<h\d[^>]*>(.*?)</h\d>", r.text, re.S)]
        return [t for t in titles if t][: int(q.get("max", 30))]
    except Exception as ex:   # noqa: BLE001
        print("質問サイトを読めませんでした:", ex)
        return []


# ---- Strata ----
def strata_obj(prompt: str, cfg: dict) -> dict:
    llm = llm_settings(cfg)
    try:
        return parse_obj(CHAT[llm["provider"]](prompt, llm))
    except Exception as ex:   # noqa: BLE001
        print("Strata に聞けませんでした:", ex)
        return {}


def ints(v) -> list[int]:
    out = []
    for x in v if isinstance(v, list) else []:
        try:
            out.append(int(x))
        except (TypeError, ValueError):
            continue
    return out


def pick_docs(cfg: dict, docs: list[dict], used: set[str]) -> list[dict]:
    fresh = [d for d in docs if d["link"] not in used][:40]
    if not fresh:
        return []
    n = int(cfg.get("max_docs", 3))
    got = strata_obj(PICK_DOCS_PROMPT.format(n=n, used="、".join(sorted(used)[:20]) or "なし", docs="\n".join(
        f"[{i}] {d['date']} {d['title']}（{d['source']}）" for i, d in enumerate(fresh))), cfg)
    idx = [i for i in ints(got.get("docs")) if 0 <= i < len(fresh)][:n]
    return [fresh[i] for i in idx]


def choose_theme(cfg: dict, outline: list[dict], questions: list[str], history: list[dict]) -> dict:
    got = strata_obj(THEME_PROMPT.format(
        outline="\n".join(f"[{i}] {o.get('doc', '')}｜{o.get('no', '')}｜{o.get('point', '')}"
                          for i, o in enumerate(outline)) or "（無し）",
        questions="\n".join(f"- {q}" for q in questions) or "（無し）",
        history="\n".join(f"- {h['date']} {h['theme']}" for h in history[-12:]) or "（無し）"), cfg)
    theme = clean(got.get("theme"))
    if not theme:
        raise RuntimeError("Strata がテーマを決められませんでした")
    return {"theme": theme[:30], "items": [outline[i] for i in ints(got.get("items")) if 0 <= i < len(outline)],
            "questions": [clean(q) for q in got.get("questions") or [] if clean(q)][:8],
            "research": clean(got.get("research")) or f"{theme}に関する厚生労働省の告示・通知・事務連絡・疑義解釈",
            "query": clean(got.get("query")) or f"{theme} 厚生労働省 通知"}


# ---- NotebookLM ----
def parse_list(text: str, key: str) -> list[dict]:
    data = parse_obj(text)
    v = data.get(key) if isinstance(data, dict) else None
    return [x for x in v if isinstance(x, dict)] if isinstance(v, list) else []


def outline_of(nb, cfg: dict) -> list[dict]:
    items = parse_list(nb.ask(OUTLINE_PROMPT.format(n=int(cfg.get("outline_items", 15)))), "items")
    return [{k: clean(o.get(k)) for k in ("doc", "no", "point")} for o in items if clean(o.get("point"))]


def write_issue(nb, style: dict, plan: dict, sources: list[dict]) -> dict:
    rules = "【守ること】\n" + "".join(f"- {r}\n" for r in ((style.get("rules") or {}).get("must") or []))
    issue = style.get("issue") or {}
    prompt = WRITE_PROMPT.format(
        digest=(style.get("prompt_digest") or "").strip(), rules=rules, theme=plan["theme"],
        max_qa=int(issue.get("max_qa", 7)),
        questions="\n".join(f"- {q}" for q in plan["questions"]) + "".join(
            f"\n- （資料の問）{o['doc']} {o['no']}：{o['point']}" for o in plan["items"]),
        sources="\n".join(f"- {s['title']} — {s['url']}" for s in sources) or "（URL の一覧なし）",
        example=((style.get("qa_block") or {}).get("format_example") or "").strip())
    data = parse_obj(nb.ask(prompt))
    qa = []
    for q in data.get("qa") or []:
        if not isinstance(q, dict) or not clean(q.get("q")) or not clean(q.get("answer")):
            continue
        item = {"q": clean(q.get("q")), **{k: clean(q.get(k)) for k, _ in LABELS}, "url": clean(q.get("url"))}
        if not item["q"].startswith("Q"):
            item["q"] = "Q. " + item["q"]
        qa.append(item)
    if not qa:
        raise RuntimeError("NotebookLM から Q&A を得られませんでした")
    return {"qa": qa[: int(issue.get("max_qa", 7))],
            "three_lines": [clean(x) for x in data.get("three_lines") or [] if clean(x)][:3] or
            [q["answer"] for q in qa[:3]],
            "hashtags": [clean(h).lstrip("#＃") for h in data.get("hashtags") or [] if clean(h)]}


def notebook_sources(nb) -> list[dict]:
    """ノートに入っている資料の [{title, url}]（URL のあるものだけ）。"""
    if not hasattr(nb, "sources"):
        return []
    return [s for s in nb.sources() if s.get("url")]


# ---- 組み立て ----
def fill(text: str, theme: str) -> str:
    return (text or "").replace("{theme}", theme)


def build(style: dict, plan: dict, body: dict, docs: list[dict], audio: str | None) -> dict:
    issue, pod, opening = style.get("issue") or {}, style.get("podcast") or {}, style.get("opening_notice") or {}
    fixed = list((style.get("hashtags") or {}).get("fixed") or [])
    links, seen = [], set()
    for q in body["qa"]:
        if q["url"] and q["url"] not in seen:
            seen.add(q["url"])
            links.append((q["source"] or q["url"], q["url"]))
    for d in docs:
        if d["link"] not in seen:
            seen.add(d["link"])
            links.append((d["title"], d["link"]))
    return {"title": fill(issue.get("title") or "【保険と公費のギモン】{theme}", plan["theme"]),
            "theme": plan["theme"],
            "opening_heading": opening.get("heading") or "", "opening": (opening.get("body") or "").strip(),
            "greeting": fill(issue.get("greeting") or "今回のテーマは「{theme}」です。", plan["theme"]),
            "three_heading": issue.get("three_lines_heading") or "今回の3行", "three_lines": body["three_lines"],
            "audio": audio, "audio_heading": pod.get("heading") or "音声版", "audio_body": (pod.get("body") or "").strip(),
            "qa_heading": issue.get("qa_heading") or "よくある疑問", "qa": body["qa"],
            "links_heading": issue.get("links_heading") or "根拠資料（まとめ）", "links": links,
            "afterword_heading": issue.get("afterword_heading") or "編集後記", "afterword": issue.get("afterword") or "",
            "footer": (style.get("fixed_footer") or "").strip(),
            "hashtags": list(dict.fromkeys(t.replace(" ", "") for t in fixed + body["hashtags"] if t))[:14]}


AUDIO_NOTE = "※ここに音声ファイル「{}」を埋め込み（note の「＋」→ 音声）"   # note_publish が見つけて差し替える
TOC_NOTE = "※ここに note の目次を挿入"


def to_markdown(doc: dict, memo: list[str]) -> str:
    md = [f"# {doc['title']}", ""]
    if doc["opening"]:
        md += [f"> **{doc['opening_heading']}**  "] + [f"> {x}  " for x in doc["opening"].splitlines()] + [""]
    md += [doc["greeting"], "", f"**{doc['three_heading']}**", ""] + [f"- {x}" for x in doc["three_lines"]] + [""]
    if doc["audio"]:
        md += [f"## {doc['audio_heading']}", "", doc["audio_body"].replace("\n", "  \n"), "",
               AUDIO_NOTE.format(doc["audio"]), ""]
    md += [TOC_NOTE, "", f"## {doc['qa_heading']}", ""]
    for q in doc["qa"]:
        md.append(f"### {q['q']}")
        for k, label in LABELS:
            if q[k]:
                md.append(f"**{label}**　" + (f"[{q[k]}]({q['url']})" if k == "source" and q["url"] else q[k]) + "  ")
        md.append("")
    md += [f"## {doc['links_heading']}", ""] + [f"- {t}：{u}" for t, u in doc["links"]]
    md += ["", f"## {doc['afterword_heading']}", "", doc["afterword"], "",
           doc["footer"].replace("\n", "  \n"), "", " ".join(f"#{h}" for h in doc["hashtags"])]
    if memo:
        md += ["", "---", "", "## 確認メモ（公開前に削除）", ""] + [f"- {m}" for m in memo]
    return "\n".join(md).rstrip() + "\n"


def body_html(doc: dict) -> str:
    e = html.escape
    b = [(f"<blockquote><p><strong>{e(doc['opening_heading'])}</strong><br>"
          + "<br>".join(e(x) for x in doc["opening"].splitlines()) + "</p></blockquote>" if doc["opening"] else ""),
         f"<p>{e(doc['greeting'])}</p>", f"<p><strong>{e(doc['three_heading'])}</strong></p>",
         "<ul>" + "".join(f"<li>{e(x)}</li>" for x in doc["three_lines"]) + "</ul>"]
    if doc["audio"]:
        b += [f"<h2>{e(doc['audio_heading'])}</h2>",
              "<p>" + "<br>".join(e(x) for x in doc["audio_body"].splitlines()) + "</p>",
              f"<p>{e(AUDIO_NOTE.format(doc['audio']))}</p>"]
    b += [f"<p>{TOC_NOTE}</p>", f"<h2>{e(doc['qa_heading'])}</h2>"]
    for q in doc["qa"]:
        rows = [f"<strong>{label}</strong>　" + (f'<a href="{e(q["url"])}">{e(q[k])}</a>' if k == "source" and q["url"]
                                                else e(q[k])) for k, label in LABELS if q[k]]
        b += [f"<h3>{e(q['q'])}</h3>", "<p>" + "<br>".join(rows) + "</p>"]
    b += [f"<h2>{e(doc['links_heading'])}</h2>",
          "<ul>" + "".join(f'<li>{e(t)}：<a href="{e(u)}">{e(u)}</a></li>' for t, u in doc["links"]) + "</ul>",
          f"<h2>{e(doc['afterword_heading'])}</h2>", f"<p>{e(doc['afterword'])}</p>",
          "<p>" + "<br>".join(e(x) for x in doc["footer"].splitlines()) + "</p>",
          "<p>" + " ".join(e(f"#{h}") for h in doc["hashtags"]) + "</p>"]
    return "\n".join(x for x in b if x)


def to_html(doc: dict, memo: list[str]) -> str:
    e = html.escape
    b = [f"<p><small>タイトル（note のタイトル欄へ）</small><br><strong>{e(doc['title'])}</strong></p><hr>", body_html(doc)]
    if memo:
        b.append('<hr><div style="background:#fff3cd;padding:8px"><p><strong>確認メモ（公開前に削除）</strong></p><ul>'
                 + "".join(f"<li>{e(m)}</li>" for m in memo) + "</ul></div>")
    return ("<!doctype html><html lang=\"ja\"><head><meta charset=\"utf-8\">"
            f"<title>{e(doc['title'])}</title><style>body{{max-width:720px;margin:2em auto;padding:0 16px;"
            "font-family:sans-serif;line-height:1.8}</style></head><body>\n" + "\n".join(b) + "\n</body></html>\n")


# ---- 機械チェック（自動では直さず、確認メモに列挙する） ----
def check(doc: dict, style: dict, primary: list[str] | None = None) -> tuple[list[str], list[tuple[str, str]]]:
    memo, long = [], []
    max_chars = int((style.get("sentences") or {}).get("max_chars", 40))
    forbidden = (style.get("voice") or {}).get("forbidden_phrases") or []
    texts = [("タイトル", doc["title"])] + [(f"今回の3行{n + 1}", x) for n, x in enumerate(doc["three_lines"])]
    for q in doc["qa"]:
        texts.append((f"「{q['q']}」小見出し", q["q"]))
        texts += [(f"「{q['q']}」{label}", q[k]) for k, label in LABELS if k != "source" and q[k]]
    for where, text in texts:
        for s in sentences(text) if where.endswith(("答え", "条件・例外")) or "3行" in where else []:
            if len(s.rstrip("。")) + 1 > max_chars:
                long.append((where, s))
        if where.endswith("小見出し") and len(text) > max_chars:
            memo.append(f"{where}：{max_chars}字超（{len(text)}字）")
        for w in forbidden:
            if w in text:
                memo.append(f"{where}：使わない語「{w}」")
        if re.search(r"(である|だ|できる|なる)。", text):
            memo.append(f"{where}：だ・である調")
        if EMOJI.search(text) or "→" in text or re.search("[!！]", text):
            memo.append(f"{where}：絵文字・「→」・感嘆符")
    for q in doc["qa"]:
        if not q["basis"]:
            memo.append(f"「{q['q']}」：根拠（通知名・項番）がありません")
        host = urlparse(q["url"]).netloc.lower()
        if not host:
            memo.append(f"「{q['q']}」：出典のURLがありません")
        elif not is_primary_host(host, primary or PRIMARY):
            memo.append(f"「{q['q']}」：出典が行政資料ではありません（{host}）。原文を確認してください")
    for where, s in long:
        memo.append(f"{where}：{max_chars}字超（{len(s)}字）「{s}」")
    return memo, long


# ---- 画像・音声 ----
def make_cover_image(cfg: dict, theme: str, day: dt.date, dest):
    if not cfg.get("cover_image", True):
        return None
    sys.path.insert(0, str(ROOT / "scripts"))
    try:
        import make_cover
        return make_cover.make_cover_hoken(day, dest)
    except Exception as ex:   # noqa: BLE001  画像は「おまけ」
        print("トップ画像を作れませんでした:", ex)
        return None


def notebook_settings(cfg: dict, style: dict, theme: str = "") -> dict:
    """NotebookLM の設定。CLI の場所・認証・音声の台帳は onco.podcast と共通にして、note_hoken.notebook で上書き。"""
    base = (load_config().get("onco") or {}).get("podcast") or {}
    pod = style.get("podcast") or {}
    return {**base, "title": "保険と公費のギモン note下書き", "label": f"保険と公費のギモン：{theme}",
            "instructions": (pod.get("instructions") or "").strip() + (f"\nテーマ: {theme}" if theme else ""),
            **(cfg.get("notebook") or {})}


def make_audio(nb, cfg: dict, style: dict, theme: str, dest, *, enabled: bool) -> tuple[object, str]:
    """NotebookLM で音声を作る。(ファイル or None, 確認メモに書く理由)。"""
    if not enabled or not (style.get("podcast") or {}).get("enabled"):
        return None, ""
    import podcast_nlm
    nb.pod["instructions"] = notebook_settings(cfg, style, theme)["instructions"]
    limit = int(nb.pod.get("daily_limit", 3))
    if podcast_nlm.used_today(nb.pod) >= limit:
        return None, f"音声は作りませんでした（NotebookLM の音声は1日{limit}本まで。上限に達しています）"
    try:
        print("音声を作成中（NotebookLM、15〜20分かかることがあります）…")
        nb.audio(dest)
        return dest, ""
    except Exception as ex:   # noqa: BLE001  音声は「おまけ」
        return None, f"音声を作れませんでした: {str(ex)[:200]}"


def audio_meta(style: dict, audio, theme: str) -> dict | None:
    if not audio:
        return None
    pod = style.get("podcast") or {}
    cover = ROOT / (pod.get("cover") or "assets/onco.png")
    return {"path": str(audio), "cover": str(cover) if cover.exists() else None,
            "title": fill(pod.get("audio_title") or "保険と公費のギモン：{theme}（AI音声）", theme),
            "author": pod.get("author") or "NotebookLM（AI音声）"}


# ---- 本体 ----
def make(cfg: dict, today: dt.date, *, dry_run: bool, with_audio: bool, nb_factory=None) -> dict | None:
    style = load_style(cfg)
    state = load_state(SECTION)
    history = state.get("history") or []
    avoid = (today - dt.timedelta(weeks=int(cfg.get("avoid_weeks", 26)))).isoformat()
    used = {u for h in history if h.get("date", "") >= avoid for u in h.get("docs") or []}

    docs = collect_docs(cfg, today)
    print(f"行政資料の候補: {len(docs)} 件（直近{cfg.get('lookback_days', 30)}日）")
    picked = pick_docs(cfg, docs, used)
    for d in picked:
        print(f"  使う資料: {d['date']} {d['title']}")
    questions = site_questions(cfg)

    if nb_factory is None:
        import podcast_nlm
        nb_factory = lambda items: podcast_nlm.Notebook(notebook_settings(cfg, style), items)   # noqa: E731
    nb = nb_factory([{**d, "summary": d["title"]} for d in picked])
    memo_extra = []
    try:
        outline = outline_of(nb, cfg) if picked else []
        print(f"資料の問・項目: {len(outline)} 件")
        plan = choose_theme(cfg, outline, questions, history)
        print(f"テーマ: {plan['theme']}\n疑問: " + " / ".join(plan["questions"]))
        if cfg.get("research", True):
            try:
                n = nb.research(plan["research"] + "\n（日本の厚生労働省・地方厚生局・審査支払機関・自治体の一次資料に限る）",
                                mode=cfg.get("research_mode", "fast"), fallback=plan["query"])
                print(f"ウェブ調査のあと、資料 {n} 件")
            except Exception as ex:   # noqa: BLE001  調査ができなくても、選んだ資料で書く
                memo_extra.append(f"告示・通知の原文の追加に失敗しました: {str(ex)[:200]}")
                if not picked:
                    raise
        sources = notebook_sources(nb) or [{"title": d["title"], "url": d["link"]} for d in picked]
        body = write_issue(nb, style, plan, sources)
        out_dir = ROOT / (cfg.get("out_dir") or ".local/note_drafts")
        out_dir.mkdir(parents=True, exist_ok=True)
        stem = f"hoken-{today:%Y%m%d}"
        audio, why = make_audio(nb, cfg, style, plan["theme"], out_dir / f"{stem}.mp3",
                                enabled=with_audio and cfg.get("audio", True))
        if why:
            memo_extra.append(why)
    finally:
        nb.close()

    doc = build(style, plan, body, picked, audio.name if audio else None)
    memo, long = check(doc, style, cfg.get("primary_domains"))
    for i, s in split_suggestions(long, cfg).items():
        memo.append(f"分割案：「{long[i][1]}」→「{s}」")
    memo += memo_extra
    md_path, html_path = out_dir / f"{stem}.md", out_dir / f"{stem}.html"
    md_path.write_text(to_markdown(doc, memo), encoding="utf-8")
    html_path.write_text(to_html(doc, memo), encoding="utf-8")
    audio_info = audio_meta(style, audio, plan["theme"])
    cover = make_cover_image(cfg, plan["theme"], today, out_dir / f"{stem}-cover.png")
    (out_dir / f"{stem}.json").write_text(json.dumps({"title": doc["title"], "body_html": body_html(doc),
                                                      "audio": audio_info, "cover_image": str(cover) if cover else None},
                                                     ensure_ascii=False), encoding="utf-8")
    print(f"下書きを保存しました: {md_path}\n                    {html_path}\nQ&A {len(doc['qa'])} 問・確認メモ {len(memo)} 件")

    note_line = "html をブラウザで開き、全選択コピー → note に貼り付け"
    post = cfg.get("note_post") or {}
    if post.get("enabled") and not dry_run:
        import note_publish
        try:
            url, todo = note_publish.save_draft(doc["title"], body_html(doc), audio=audio_info, cover=cover,
                                                headless=post.get("headless", False))
            note_line = f"note に下書き保存しました（非公開）: {url}" + "".join(f"\n-# 未完了: {t}" for t in todo)
        except note_publish.NeedLogin as ex:
            note_line = f"note に下書き保存できませんでした: {ex}"
        except Exception as ex:   # noqa: BLE001
            note_line = f"note に下書き保存できませんでした（手で貼ってください）: {str(ex)[:300]}"
        print(note_line)
    content = (f"📝 **note 下書き（水曜）：{doc['title']}**\n**{doc['three_heading']}**\n"
               + "\n".join(f"- {x}" for x in doc["three_lines"])
               + f"\n{note_line}\n-# Q&A {len(doc['qa'])}問・確認メモ {len(memo)}件（確認メモは添付の md・html の末尾）"
               + (f"\n-# 音声: {audio.name}" if audio else ""))
    if dry_run:
        print("---- dry-run（Discord には送りません・使った資料も記録しません）----\n" + content)
    else:
        history.append({"date": today.isoformat(), "theme": plan["theme"], "docs": [d["link"] for d in picked]})
        state["history"] = history[-60:]
        save_state(SECTION, state)
        webhook = cfg.get("webhook_env") or "DISCORD_WEBHOOK_NOTE"
        name = cfg.get("title") or "note 下書き"
        post_discord_file(webhook, md_path, content=content, username=name, avatar_url=cfg.get("avatar_url"))
        post_discord_file(webhook, html_path, username=name, avatar_url=cfg.get("avatar_url"))
    return {"doc": doc, "memo": memo, "md": md_path, "html": html_path, "plan": plan}


def main(argv: list[str]) -> None:
    cfg = ai_digest.section_config(SECTION)
    if not cfg:
        raise SystemExit(f"config.yaml に {SECTION}: がありません")
    dry = "--dry-run" in argv
    if not cfg.get("enabled", True) and not dry:
        print("note_hoken は無効です（config.yaml の note_hoken.enabled）")
        return
    try:
        make(cfg, now_jst().date(), dry_run=dry, with_audio=(not dry) or "--audio" in argv)
    except Exception as ex:   # noqa: BLE001
        print("note 下書き（水曜）の作成に失敗:", ex)
        if not dry and os.environ.get("DRY_RUN") != "1":
            post_discord(cfg.get("webhook_env") or "DISCORD_WEBHOOK_NOTE", username=cfg.get("title") or "note 下書き",
                         content=f"📝 今週の水曜号（保険と公費のギモン）は作れませんでした: {str(ex)[:300]}")
        raise


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
