"""note 週刊マガジン「週刊がん薬物療法ニュース」の下書きを作る（公開はユーザーが手で行う）。

素材: 直近7日の週間アーカイブ（state/onco_archive.json、全分類）。
書き方: style/jibun_yakkyoku.yaml（文体・型・構成。コードに文体は書かない）。
流れ: 今週の記事URLを NotebookLM のノートに入れる → テーマごとにトピックを JSON で書かせる（1回に1〜2テーマ）
      → タイトル用の重要トピック2つ・今週の3行・ハッシュタグを書かせる → コードで組み立てる
      → 機械チェック（確認メモとして末尾に付ける。自動では直さない）→ .md と .html に保存 → Discord に通知。

使い方: python src/note_weekly.py            作って Discord に通知
        python src/note_weekly.py --dry-run  作るだけ（Discord には送らない）
        python src/note_weekly.py --end 2026-10-24   その日までの7日分で作る
"""
import datetime as dt
import html
import json
import os
import re
import shutil
import sys
from urllib.parse import urlparse

import yaml

import ai_digest
from ai_digest import CHAT, ja_fix, llm_settings, parse_item_objects, strip_think
from common import ROOT, load_config, load_state, post_discord, post_discord_file

SECTION = "note_weekly"

# 記事の分類（onco の tags）→ 号のテーマ（style の issue.sections.themes.order）。config の theme_map で上書きできる
DEFAULT_THEME_MAP = {"新薬・承認": "新薬・承認", "治療ストラテジー": "治療ストラテジー（臨床試験）",
                     "ガイドライン": "ガイドライン", "治験": "治験", "安全性情報": "安全性・行政", "行政・供給": "安全性・行政"}
LABELS = [("conclusion", "結論"), ("before", "これまで"), ("next", "これから"), ("data", "データ"), ("source", "出典")]

TOPICS_PROMPT = """{digest}
{rules}
資料は今週（{start}〜{end}）のニュース記事です。下の一覧の記事について、テーマ「{themes}」のトピックを書いてください。

- 1トピック = 1つの出来事。同じ出来事を報じた記事はまとめて1トピックにする。
- 各テーマ{per_theme}件まで。重要な順。
- 値はすべて日本語の文字列。資料に無い項目は空文字 "" にする（「記載なし」と書かない）。
- heading: 小見出し（結論そのまま、30字以内）。海外の承認は先頭に【海外】。
- conclusion / before / next: それぞれ1〜2文。1文40字以内。
- data: 試験名・対象・主要評価項目の数値（任意、1行）。
- source: 出典の表記（機関名・論文の書誌）。url: 一次情報のURL（記事に「一次情報」が添えてあればそれを使う。無ければ記事のURL）。
- 事実・数値・日付は、一次情報の資料と記事の資料が食い違うときは一次情報に従う。
- date: 出来事（承認・発表・改訂など）の日付。YYYY-MM-DD。資料に無ければ ""。
- i: もとにした記事の番号（数値。複数なら最も一次情報に近い1つ）。

書き方の見本（この文体・文末で書く）:
{example}

記事の一覧（番号: [分類] 見出し — URL）:
{items}

JSONのみを出力:
{{"topics": [{{"i": 0, "theme": "テーマ名", "date": "YYYY-MM-DD", "heading": "...", "conclusion": "...", "before": "...", "next": "...", "data": "...", "source": "...", "url": "..."}}]}}"""

LEAD_PROMPT = """{digest}
以下は今週の号に載せるトピックの小見出しと結論です。

{topics}

次をJSONのみで出力してください。
- headline_1, headline_2: タイトル用に、今週の重要トピック2つ。それぞれ15字以内の名詞句（例「ペムブロリズマブ適応追加」）。
- three_lines: 「今週の3行」。最重要トピック3つを、それぞれ30字以内の1文で。ファクトのみ。
- hashtags: その号の一般名・がん種・学会名・ガイドライン名から5〜8個。「#」は付けない。

{{"headline_1": "...", "headline_2": "...", "three_lines": ["...", "...", "..."], "hashtags": ["...", "..."]}}"""

QUERY_PROMPT = """以下は今週のがん薬物療法ニュースの記事です。各記事の内容を、一次情報（規制当局・学会・論文・製薬会社の公式発表）で
裏づけるためのウェブ検索語を1つずつ作ってください。
- 国内の出来事（厚労省・PMDA・国内学会・国内承認）: 日本語。薬の一般名（カタカナ）・出来事（承認、適応追加、添付文書改訂など）・「厚生労働省」か「PMDA」を入れる
- 海外の出来事（FDA・EMA・海外学会・海外の試験）: 英語。薬の一般名（英語）・出来事・「FDA」「EMA」や試験名を入れる
- 検索語は20語以内。記事にない固有名詞は足さない

{items}

JSONのみを出力: {{"items": [{{"i": 番号, "query": "検索語"}}]}}"""

CLASSIFY_PROMPT = """次のウェブページの候補を、ニュースの一次情報として使えるかで分けてください。
一次情報 = 規制当局・行政（PMDA、厚労省、FDA、EMA など）、学会・ガイドライン、論文、製薬会社の公式発表（プレスリリース・IR）。
一次情報でない = 報道・業界ニュース・まとめ・ブログ・患者向け解説・病院の案内。

{items}

JSONのみを出力: {{"items": [{{"i": 番号, "primary": true}}]}}  （一次情報の候補だけを列挙する）"""

SPLIT_PROMPT = """次の各文は40字を超えています。意味・事実・数値を変えずに、それぞれ40字以内の2〜3文に分けてください。
言葉を足したり、解釈を加えたりしないこと。です・ます体か体言止め。

{items}

JSONのみを出力: {{"items": [{{"i": 番号, "text": "分けた文（文と文の間は「。」で区切る）"}}]}}"""


def now_jst() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc).replace(tzinfo=None) + dt.timedelta(hours=9)


def load_style(cfg: dict) -> dict:
    with open(ROOT / (cfg.get("style_file") or "style/jibun_yakkyoku.yaml"), encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def md_day(d: dt.date, fmt: str) -> str:
    """strftime の %-m / %-d（ゼロ埋めなし）を Windows でも使えるようにする。"""
    return d.strftime(fmt.replace("%-m", str(d.month)).replace("%-d", str(d.day)))


def fill_dates(text: str, start: dt.date, end: dt.date) -> str:
    return re.sub(r"\{(start|end):([^}]*)\}", lambda m: md_day(start if m.group(1) == "start" else end, m.group(2)),
                  text)


def section(style: dict, sid: str) -> dict:
    for s in ((style.get("issue") or {}).get("sections") or []):
        if s.get("id") == sid:
            return s
    return {}


def week_items(cfg: dict, end: dt.date) -> tuple[dt.date, list[dict]]:
    start = end - dt.timedelta(days=int(cfg.get("lookback_days", 7)) - 1)
    items = [a for a in load_state("onco_archive").get("items", [])
             if start.isoformat() <= a.get("date", "") <= end.isoformat()]
    items.sort(key=lambda a: -float(a.get("score") or 0))
    return start, items[: int(cfg.get("max_items", 30))]


def themes_of(cfg: dict, style: dict, items: list[dict]) -> list[tuple[str, list[int]]]:
    """号のテーマ順に、そのテーマに入る記事の番号。記事の無いテーマは省く。"""
    tmap = {**DEFAULT_THEME_MAP, **(cfg.get("theme_map") or {})}
    order = section(style, "themes").get("order") or list(dict.fromkeys(tmap.values()))
    out = []
    for th in order:
        idx = [i for i, a in enumerate(items) if tmap.get(a.get("tag")) == th]
        if idx:
            out.append((th, idx))
    return out


def parse_topics(text: str) -> list[dict]:
    """{"topics": [...]} を読む。途中で切れていても、読める {...} を1件ずつ取り出す。"""
    text = strip_think(text)
    m = re.search(r"\{.*\}", text, re.S)
    if m:
        try:
            data = json.loads(m.group(0))
            if isinstance(data, dict) and isinstance(data.get("topics"), list):
                return [t for t in data["topics"] if isinstance(t, dict) and t.get("heading")]
        except json.JSONDecodeError:
            pass
    out = []
    for m in re.finditer(r"\{[^{}]*\"heading\"[^{}]*\}", text):
        try:
            out.append(json.loads(m.group(0)))
        except json.JSONDecodeError:
            continue
    return out


def parse_obj(text: str) -> dict:
    m = re.search(r"\{.*\}", strip_think(text), re.S)
    try:
        return json.loads(m.group(0)) if m else {}
    except json.JSONDecodeError:
        return {}


def clean(v) -> str:
    v = ja_fix(str(v or "").strip())
    return "" if v in ("記載なし", "不明", "なし", "-", "―") else v


def write_topics(nb, cfg: dict, style: dict, items: list[dict], start: dt.date, end: dt.date) -> list[dict]:
    """テーマごとのトピック（1回の質問に1〜2テーマ）。トピックは theme の順に並べて返す。"""
    groups = themes_of(cfg, style, items)
    per_ask = max(1, int(cfg.get("themes_per_ask", 2)))
    rules = "【守ること】\n" + "".join(f"- {r}\n" for r in ((style.get("rules") or {}).get("must") or []))
    topics = []
    for k in range(0, len(groups), per_ask):
        batch = groups[k:k + per_ask]
        theme_of = {i: th for th, ids in batch for i in ids}
        lines = [f"[{i}] [{th}] {items[i]['title_ja']} — {items[i]['link']}"
                 + "".join(f"\n    一次情報: {p['url']}" for p in items[i].get("primary") or [])
                 for i, th in theme_of.items()]
        prompt = TOPICS_PROMPT.format(digest=(style.get("prompt_digest") or "").strip(), rules=rules,
                                      example=((style.get("topic_block") or {}).get("format_example") or "").strip(),
                                      start=md_day(start, "%-m/%-d"), end=md_day(end, "%-m/%-d"),
                                      themes="」「".join(th for th, _ in batch),
                                      per_theme=cfg.get("max_topics_per_theme", 4), items="\n".join(lines))
        got = parse_topics(nb.ask(prompt))
        if not got:
            print(f"トピックを読めませんでした（{'・'.join(th for th, _ in batch)}）")
        names = [th for th, _ in batch]
        for t in got:
            th = t.get("theme") if t.get("theme") in names else None
            try:
                i = int(t.get("i"))
            except (TypeError, ValueError):
                i = None
            if th is None:   # テーマ名が崩れていたら、記事の分類から決める
                th = next((n for n, ids in batch if i in ids), names[0])
            art = items[i] if i is not None and 0 <= i < len(items) else {}
            topic = {"theme": th, **{k: clean(t.get(k)) for k, _ in LABELS}, "heading": clean(t.get("heading")),
                     "url": clean(t.get("url")) or art.get("link", ""), "date": clean(t.get("date"))}
            if not topic["source"] and art:
                topic["source"] = art.get("source", "")
            prim = art.get("primary") or []
            if prim and topic["url"] not in {p["url"] for p in prim}:
                # 一次情報が見つかっているのに報道のURLを選んだときは、一次情報に差し替える
                topic["url"], topic["source"] = prim[0]["url"], prim[0]["label"]
            topic["verified"] = bool(prim) and topic["url"] in {p["url"] for p in prim}
            topics.append(topic)
    order = [th for th, _ in groups]
    topics.sort(key=lambda t: order.index(t["theme"]) if t["theme"] in order else len(order))
    return topics[: int((style.get("issue") or {}).get("max_topics", 12))]


def write_lead(nb, style: dict, topics: list[dict]) -> dict:
    lines = "\n".join(f"- [{t['theme']}] {t['heading']}：{t['conclusion']}" for t in topics)
    data = parse_obj(nb.ask(LEAD_PROMPT.format(digest=(style.get("prompt_digest") or "").strip(), topics=lines)))
    three = [clean(x) for x in (data.get("three_lines") or []) if clean(x)][:3]
    return {"headline_1": clean(data.get("headline_1")) or (topics[0]["heading"] if topics else ""),
            "headline_2": clean(data.get("headline_2")) or (topics[1]["heading"] if len(topics) > 1 else ""),
            "three_lines": three or [t["heading"] for t in topics[:3]],
            "hashtags": [clean(h).lstrip("#＃") for h in (data.get("hashtags") or []) if clean(h)]}


# ---- 組み立て ----
def greeting(style: dict, start: dt.date, end: dt.date) -> str:
    m = re.search(r"固定文：「(.+?)」", section(style, "lead").get("body") or "")
    return fill_dates(m.group(1) if m else "今週（{start:%-m/%-d}〜{end:%-m/%-d}）のがん薬物療法の主な動きです。",
                      start, end)


def title_of(style: dict, lead: dict, start: dt.date, end: dt.date) -> str:
    t = fill_dates((style.get("issue") or {}).get("title") or "{headline_1}、{headline_2}", start, end)
    return t.replace("{headline_1}", lead["headline_1"]).replace("{headline_2}", lead["headline_2"])


def hashtags(style: dict, lead: dict) -> list[str]:
    tags = list((style.get("hashtags") or {}).get("fixed") or []) + lead["hashtags"]
    return list(dict.fromkeys(t.replace(" ", "") for t in tags if t))[:15]


def build(style: dict, lead: dict, topics: list[dict], start: dt.date, end: dt.date,
          audio: str | None = None) -> dict:
    """号の中身（md と html の共通の材料）。audio は貼る音声のファイル名（無ければ音声の節を出さない）。"""
    links = section(style, "links")
    after = section(style, "afterword")
    pod = style.get("podcast") or {}
    opening = style.get("opening_notice") or {}
    return {"opening_heading": opening.get("heading") or "", "opening": (opening.get("body") or "").strip(),
            "audio": audio, "audio_heading": pod.get("heading") or "音声版",
            "audio_body": (pod.get("body") or "").strip(),"title": title_of(style, lead, start, end), "greeting": greeting(style, start, end),
            "three_lines": lead["three_lines"], "topics": topics,
            "links_heading": links.get("heading") or "一次資料リンク（まとめ）",
            "afterword_heading": after.get("heading") or "編集後記",
            # 本文の【…】だけが下書きに入れる文（後ろの「AIは書かない。」は作り手への指示）
            "afterword": (re.findall(r"【[^】]*】", after.get("body") or "") or [""])[0],
            "footer": (style.get("fixed_footer") or "").strip(), "hashtags": hashtags(style, lead)}


def themes_in(doc: dict) -> list[tuple[str, list[dict]]]:
    out = []
    for t in doc["topics"]:
        if not out or out[-1][0] != t["theme"]:
            out.append((t["theme"], []))
        out[-1][1].append(t)
    return out


def to_markdown(doc: dict, memo: list[str]) -> str:
    md = [f"# {doc['title']}", ""]
    if doc["opening"]:
        md += [f"> **{doc['opening_heading']}**  "] + [f"> {x}  " for x in doc["opening"].splitlines()] + [""]
    md += [doc["greeting"], "", "**今週の3行**", ""]
    md += [f"- {x}" for x in doc["three_lines"]]
    md.append("")
    if doc["audio"]:   # 音声版は「今週の3行」（結論）のすぐ下
        md += [f"## {doc['audio_heading']}", "", doc["audio_body"].replace("\n", "  \n"), "",
               f"※ここに音声ファイル「{doc['audio']}」を埋め込み（note の「＋」→ 音声）", ""]
    md += ["※ここに note の目次を挿入", ""]
    for theme, ts in themes_in(doc):
        md += [f"## {theme}", ""]
        for t in ts:
            md.append(f"### {t['heading']}")
            for k, label in LABELS:
                if not t[k]:
                    continue
                val = f"[{t[k]}]({t['url']})" if k == "source" and t["url"] else t[k]
                md.append(f"**{label}**　{val}  ")
            md.append("")
    md += [f"## {doc['links_heading']}", ""]
    md += [f"- {t['heading']}：{t['url']}" for t in doc["topics"] if t["url"]]
    md += ["", f"## {doc['afterword_heading']}", "", doc["afterword"], ""]
    md += [doc["footer"].replace("\n", "  \n"), "",
           " ".join(f"#{h}" for h in doc["hashtags"])]
    if memo:
        md += ["", "---", "", "## 確認メモ（公開前に削除）", ""] + [f"- {m}" for m in memo]
    return "\n".join(md).rstrip() + "\n"


def body_html(doc: dict) -> str:
    """note の本文（タイトル・確認メモを除く）。大見出し=h2、小見出し=h3。note への自動の下書き保存でもこれを貼る。"""
    e = html.escape
    b = [(f"<blockquote><p><strong>{e(doc['opening_heading'])}</strong><br>"
          + "<br>".join(e(x) for x in doc["opening"].splitlines()) + "</p></blockquote>" if doc["opening"] else ""),
         f"<p>{e(doc['greeting'])}</p>", "<p><strong>今週の3行</strong></p>",
         "<ul>" + "".join(f"<li>{e(x)}</li>" for x in doc["three_lines"]) + "</ul>"]
    if doc["audio"]:   # 音声版は「今週の3行」（結論）のすぐ下
        b += [f"<h2>{e(doc['audio_heading'])}</h2>",
              "<p>" + "<br>".join(e(x) for x in doc["audio_body"].splitlines()) + "</p>",
              f"<p>※ここに音声ファイル「{e(doc['audio'])}」を埋め込み（note の「＋」→ 音声）</p>"]
    b.append("<p>※ここに note の目次を挿入</p>")
    for theme, ts in themes_in(doc):
        b.append(f"<h2>{e(theme)}</h2>")
        for t in ts:
            b.append(f"<h3>{e(t['heading'])}</h3>")
            rows = []
            for k, label in LABELS:
                if not t[k]:
                    continue
                val = f'<a href="{e(t["url"])}">{e(t[k])}</a>' if k == "source" and t["url"] else e(t[k])
                rows.append(f"<strong>{label}</strong>　{val}")
            b.append("<p>" + "<br>".join(rows) + "</p>")
    b.append(f"<h2>{e(doc['links_heading'])}</h2>")
    b.append("<ul>" + "".join(f'<li>{e(t["heading"])}：<a href="{e(t["url"])}">{e(t["url"])}</a></li>'
                              for t in doc["topics"] if t["url"]) + "</ul>")
    b += [f"<h2>{e(doc['afterword_heading'])}</h2>", f"<p>{e(doc['afterword'])}</p>"]
    b += ["<p>" + "<br>".join(e(x) for x in doc["footer"].splitlines()) + "</p>",
          "<p>" + " ".join(e(f"#{h}") for h in doc["hashtags"]) + "</p>"]
    return "\n".join(x for x in b if x)


def to_html(doc: dict, memo: list[str]) -> str:
    """確認用・手で貼る用のHTML（ブラウザで開いて全選択コピー → note に貼り付け）。"""
    e = html.escape
    b = [f"<p><small>タイトル（note のタイトル欄へ）</small><br><strong>{e(doc['title'])}</strong></p><hr>",
         body_html(doc)]
    if memo:
        b += ['<hr><div style="background:#fff3cd;padding:8px"><p><strong>確認メモ（公開前に削除）</strong></p><ul>'
              + "".join(f"<li>{e(m)}</li>" for m in memo) + "</ul></div>"]
    return ("<!doctype html><html lang=\"ja\"><head><meta charset=\"utf-8\">"
            f"<title>{e(doc['title'])}</title><style>body{{max-width:720px;margin:2em auto;padding:0 16px;"
            "font-family:sans-serif;line-height:1.8}</style></head><body>\n" + "\n".join(b) + "\n</body></html>\n")


# ---- 機械チェック（自動では直さず、確認メモに列挙する） ----
EMOJI = re.compile("[\U0001F300-\U0001FAFF☀-➿⭐⭕]")


def sentences(text: str) -> list[str]:
    return [s + "。" for s in re.split(r"。", text) if s.strip()]


def mentions_date(text: str, d: dt.date) -> bool:
    return any(x in text for x in (f"{d.month}/{d.day}", f"{d.month}月{d.day}日"))


# 一次情報とみなすドメイン（config の primary_domains で置き換えられる）。これ以外だけが出典なら「報道のみ」として確認メモに出す
PRIMARY = ["pmda.go.jp", "mhlw.go.jp", "fda.gov", "ema.europa.eu", "cancer.gov", "nih.gov", "doi.org", "jsco.or.jp",
           "jsmo.or.jp", "minds.jcqhc.or.jp", "nejm.org", "thelancet.com", "ascopubs.org", "annalsofoncology.org",
           "jamanetwork.com", "nature.com", "clinicaltrials.gov", "jrct.niph.go.jp"]


def check(doc: dict, style: dict, start: dt.date | None = None,
          primary: list[str] | None = None) -> tuple[list[str], list[tuple[str, str]]]:
    """確認メモと、40字を超える文の一覧 [(場所, 文)]。"""
    memo, long = [], []
    max_chars = int((style.get("sentences") or {}).get("max_chars", 40))
    forbidden = (style.get("voice") or {}).get("forbidden_phrases") or []
    texts = [("タイトル", doc["title"]), ("あいさつ", doc["greeting"])] + \
        [(f"今週の3行{n + 1}", x) for n, x in enumerate(doc["three_lines"])]
    for t in doc["topics"]:
        texts.append((f"「{t['heading']}」小見出し", t["heading"]))
        texts += [(f"「{t['heading']}」{label}", t[k]) for k, label in LABELS if k != "source" and t[k]]
    for where, text in texts:
        for s in sentences(text) if where.endswith(("結論", "これまで", "これから")) or "3行" in where else [text]:
            if len(s.rstrip("。")) + 1 > max_chars and not where.endswith(("小見出し", "タイトル", "データ")):
                long.append((where, s))
        for w in forbidden:
            if w in text:
                memo.append(f"{where}：使わない語「{w}」")
        if re.search(r"(である|だ)。", text):
            memo.append(f"{where}：だ・である調")
        if EMOJI.search(text) or "→" in text or re.search("[!！]", text):
            memo.append(f"{where}：絵文字・「→」・感嘆符")
    for t in doc["topics"]:
        body = " ".join(t[k] for k, _ in LABELS if k != "source")
        if not t["source"]:
            memo.append(f"「{t['heading']}」：出典がありません" + ("（数値あり）" if re.search(r"\d", body) else ""))
        if re.search(r"FDA|EMA", body + t["heading"]) and "海外" not in body + t["heading"]:
            memo.append(f"「{t['heading']}」：FDA/EMA の承認に「海外」の表記がありません")
        if re.search(r"FDA|EMA", t["heading"] + t["conclusion"]) and not t["heading"].startswith("【海外】"):
            memo.append(f"「{t['heading']}」：海外の承認なのに小見出しに【海外】がありません")
        try:
            day = dt.date.fromisoformat(t.get("date") or "")
        except ValueError:
            day = None
        if start and day and day < start and not mentions_date(t["heading"] + t["conclusion"], day):
            memo.append(f"「{t['heading']}」：今週より前の出来事（{day.month}/{day.day}）なのに、結論に日付がありません")
        host = urlparse(t["url"]).netloc.lower()
        is_primary = any(host == d or host.endswith("." + d) for d in (primary or PRIMARY))
        if host and not is_primary and not t.get("verified") and "（報道）" not in t["conclusion"]:
            memo.append(f"「{t['heading']}」：出典が報道（{host}）だけです。一次情報を確認し、無ければ結論の文末に「（報道）」")
    for where, s in long:
        memo.append(f"{where}：{max_chars}字超（{len(s)}字）「{s}」")
    return memo, long


LABEL_OF = {"pmda.go.jp": "PMDA", "mhlw.go.jp": "厚生労働省", "fda.gov": "FDA", "ema.europa.eu": "EMA",
            "cancer.gov": "NCI", "clinicaltrials.gov": "ClinicalTrials.gov", "jrct.niph.go.jp": "jRCT"}


def is_primary_host(host: str, domains: list[str]) -> bool:
    return any(host == d or host.endswith("." + d) for d in domains)


def strata_json(prompt: str, cfg: dict) -> list[dict]:
    llm = llm_settings(cfg)
    try:
        return parse_item_objects(CHAT[llm["provider"]](prompt, llm))
    except Exception as ex:   # noqa: BLE001
        print("Strata に聞けませんでした:", ex)
        return []


def verify_sources(nb, items: list[dict], cfg: dict) -> list[str]:
    """記事ごとに一次情報を探してノートの資料に足し、items[i]["primary"] に記録する。裏どりの記録（確認メモ用）を返す。

    Strata: 検索語づくりと「一次情報か」の判定。検索: NotebookLM のウェブ検索（discover）。
    """
    opt = cfg.get("verify") or {}
    if not opt.get("enabled", True) or not hasattr(nb, "discover"):
        return []
    domains = cfg.get("primary_domains") or PRIMARY
    target = [i for i in range(len(items))][: int(opt.get("max_items", 15))]
    got = strata_json(QUERY_PROMPT.format(items="\n".join(
        f"[{i}] {items[i]['title_ja']}（{items[i]['source']}）{items[i].get('summary_ja') or ''}"[:300] for i in target)), cfg)
    queries = {}
    for g in got:
        try:
            if int(g["i"]) in target and str(g.get("query") or "").strip():
                queries[int(g["i"])] = ja_fix(str(g["query"]).strip())
        except (KeyError, ValueError, TypeError):
            continue
    log, per = [], int(opt.get("per_item", 2))
    for i in target:
        q = queries.get(i)
        if not q:
            continue
        cands = [c for c in nb.discover(q) if urlparse(c["url"]).netloc.lower() not in
                 {urlparse(items[i]["link"]).netloc.lower()}][:10]
        official = [c for c in cands if is_primary_host(urlparse(c["url"]).netloc.lower(), domains)]
        others = [c for c in cands if c not in official]
        if others:   # 規制当局・論文のドメイン以外は、製薬会社の公式発表かどうかを Strata に判定させる
            ok = {int(g["i"]) for g in strata_json(CLASSIFY_PROMPT.format(items="\n".join(
                f"[{k}] {c.get('title', '')} — {c['url']}" for k, c in enumerate(others))), cfg)
                if str(g.get("primary")).lower() == "true" and str(g.get("i", "")).isdigit()}
            official += [c for k, c in enumerate(others) if k in ok]
        if re.search(r"[ぁ-んァ-ヶ一-龠]", q):   # 国内の出来事（日本語の検索語）に海外当局の資料は当てない
            official = [c for c in official if not is_primary_host(urlparse(c["url"]).netloc.lower(),
                                                                   ["fda.gov", "ema.europa.eu"])]
        # 規制当局・行政を先に、次に論文・学会、最後に製薬会社の発表
        official.sort(key=lambda c: 0 if is_primary_host(urlparse(c["url"]).netloc.lower(), list(LABEL_OF))
                      else 1 if is_primary_host(urlparse(c["url"]).netloc.lower(), domains) else 2)
        picked = official[:per]
        loaded = set(nb.add_urls([c["url"] for c in picked])) if picked else set()
        items[i]["primary"] = [{"url": c["url"], "title": c.get("title", ""),
                                "label": LABEL_OF.get(next((d for d in LABEL_OF if is_primary_host(
                                    urlparse(c["url"]).netloc.lower(), [d])), ""), "") or c.get("title", "")[:40]}
                               for c in picked if c["url"] in loaded]
        found = items[i]["primary"]
        log.append(f"裏どり「{items[i]['title_ja']}」：" + ("、".join(p["url"] for p in found) if found
                                                     else f"一次情報は見つかりませんでした（検索語: {q}）"))
        print(log[-1])
    return log


def split_suggestions(long: list[tuple[str, str]], cfg: dict) -> dict[int, str]:
    """40字を超える文を、Strata に意味を変えずに分けさせた案（確認メモに併記するだけ）。"""
    if not long or not cfg.get("split_long_sentences", True):
        return {}
    llm = llm_settings(cfg)
    try:
        text = CHAT[llm["provider"]](SPLIT_PROMPT.format(items="\n".join(f"[{i}] {s}" for i, (_, s) in
                                                                         enumerate(long))), llm)
    except Exception as ex:   # noqa: BLE001  案は「おまけ」
        print("分割案を作れませんでした:", ex)
        return {}
    out = {}
    for g in parse_item_objects(text):
        try:
            out[int(g["i"])] = ja_fix(str(g["text"]))
        except (KeyError, ValueError, TypeError):
            continue
    return out


def find_audio(cfg: dict, style: dict, start: dt.date, end: dt.date, dest):
    """今週の onco の podcast（圧縮前の mp3。土曜の週間まとめがあればそれ）を下書きの横にコピーする。"""
    if not (style.get("podcast") or {}).get("enabled"):
        return None
    pod = (load_config().get("onco") or {}).get("podcast") or {}
    folder = ROOT / (pod.get("out_dir") or ".local/podcasts")
    found = []
    for f in folder.glob(f"{pod.get('file_prefix', 'onco')}-*.mp3"):
        m = re.match(r".*-(\d{8})-\d{4}\.mp3$", f.name)     # -discord.mp3（圧縮版）は除く
        if m:
            day = dt.datetime.strptime(m.group(1), "%Y%m%d").date()
            if start <= day <= end:
                found.append((day.weekday() == 5, f.name, f))    # 土曜の回を優先、次に新しいもの
    if not found:
        return None
    src = max(found)[2]
    if src.stat().st_size > float(cfg.get("audio_max_mb", 100)) * 1e6:   # note の音声埋め込みは100MBまで
        print(f"音声が note の上限を超えています: {src.name}")
        return None
    shutil.copyfile(src, dest)
    return dest


def make_cover_image(cfg: dict, start: dt.date, end: dt.date, dest):
    """トップ画像（scripts/make_cover.py。過去の投稿のテイストの手描き風、背景色は週ごとに変わる）。"""
    if not cfg.get("cover_image", True):
        return None
    sys.path.insert(0, str(ROOT / "scripts"))
    try:
        import make_cover
        return make_cover.make_cover(start, end, dest)
    except Exception as ex:   # noqa: BLE001  画像は「おまけ」
        print("トップ画像を作れませんでした:", ex)
        return None


def audio_meta(cfg: dict, style: dict, audio, start: dt.date, end: dt.date) -> dict | None:
    """note の音声埋め込みに入れる情報（ファイル・カバー画像・タイトル・作者）。"""
    if not audio:
        return None
    pod = style.get("podcast") or {}
    cover = ROOT / (pod.get("cover") or "assets/onco.png")
    return {"path": str(audio), "cover": str(cover) if cover.exists() else None,
            "title": fill_dates(pod.get("audio_title") or "週刊がん薬物療法 {start:%-m/%-d}〜{end:%-m/%-d}（AI音声）",
                                start, end),
            "author": pod.get("author") or "NotebookLM（AI音声）"}


def notebook_settings(cfg: dict) -> dict:
    """NotebookLM の設定。CLI の場所・認証は onco.podcast と共通にして、note_weekly.notebook で上書き。"""
    base = (load_config().get("onco") or {}).get("podcast") or {}
    return {**base, "title": "週刊がん薬物療法 note下書き", **(cfg.get("notebook") or {})}


def make(cfg: dict, end: dt.date, *, dry_run: bool, nb_factory=None) -> dict | None:
    style = load_style(cfg)
    start, items = week_items(cfg, end)
    print(f"{start}〜{end}: アーカイブの記事 {len(items)} 件")
    if len(items) < int(cfg.get("min_items", 3)):
        print("記事が少ないため作りません")
        return None
    if nb_factory is None:
        import podcast_nlm
        nb_factory = lambda: podcast_nlm.Notebook(notebook_settings(cfg), items)   # noqa: E731
    nb = nb_factory()
    try:
        try:
            verify_log = verify_sources(nb, items, cfg)
        except Exception as ex:   # noqa: BLE001  裏どりができなくても下書きは作る
            print("裏どりに失敗:", ex)
            verify_log = [f"裏どりに失敗しました: {str(ex)[:200]}"]
        topics = write_topics(nb, cfg, style, items, start, end)
        if not topics:
            raise RuntimeError("NotebookLM からトピックを得られませんでした")
        lead = write_lead(nb, style, topics)
    finally:
        nb.close()
    out_dir = ROOT / (cfg.get("out_dir") or ".local/note_drafts")
    out_dir.mkdir(parents=True, exist_ok=True)
    y, w, _ = end.isocalendar()
    audio = find_audio(cfg, style, start, end, out_dir / f"{y}-W{w:02d}.mp3")
    doc = build(style, lead, topics, start, end, audio.name if audio else None)
    memo, long = check(doc, style, start, cfg.get("primary_domains"))
    for i, s in split_suggestions(long, cfg).items():
        memo.append(f"分割案：「{long[i][1]}」→「{s}」")
    memo += verify_log
    md_path, html_path = out_dir / f"{y}-W{w:02d}.md", out_dir / f"{y}-W{w:02d}.html"
    md_path.write_text(to_markdown(doc, memo), encoding="utf-8")
    html_path.write_text(to_html(doc, memo), encoding="utf-8")
    json_path = out_dir / f"{y}-W{w:02d}.json"   # note への下書き保存（やり直しは note_publish.py --draft）に使う
    audio_info = audio_meta(cfg, style, audio, start, end)
    cover = make_cover_image(cfg, start, end, out_dir / f"{y}-W{w:02d}-cover.png")
    json_path.write_text(json.dumps({"title": doc["title"], "body_html": body_html(doc), "audio": audio_info,
                                     "cover_image": str(cover) if cover else None}, ensure_ascii=False),
                         encoding="utf-8")
    print(f"下書きを保存しました: {md_path}\n                    {html_path}\nトピック {len(topics)} 件・確認メモ {len(memo)} 件")
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
        except Exception as ex:   # noqa: BLE001  下書き保存は「おまけ」。通知とファイルは送る
            note_line = f"note に下書き保存できませんでした（手で貼ってください）: {str(ex)[:300]}"
        print(note_line)
    content = (f"📝 **note 下書き：{doc['title']}**\n**今週の3行**\n" + "\n".join(f"- {x}" for x in doc["three_lines"])
               + f"\n{note_line}\n-# トピック {len(topics)}件・確認メモ {len(memo)}件（確認メモは添付の md・html の末尾）"
               + (f"\n-# 音声: {audio}（note の音声埋め込みで貼る）" if audio else
                  "\n-# 今週の podcast は見つかりませんでした（音声の節は省きました）"))
    if dry_run:
        print("---- dry-run（Discord には送りません）----\n" + content)
    else:
        webhook = cfg.get("webhook_env") or "DISCORD_WEBHOOK_NOTE"
        post_discord_file(webhook, md_path, content=content, username=cfg.get("title") or "note 下書き",
                          avatar_url=cfg.get("avatar_url"))
        post_discord_file(webhook, html_path, username=cfg.get("title") or "note 下書き",
                          avatar_url=cfg.get("avatar_url"))
    return {"doc": doc, "memo": memo, "md": md_path, "html": html_path}


def main(argv: list[str]) -> None:
    cfg = ai_digest.section_config(SECTION)
    if not cfg:
        raise SystemExit(f"config.yaml に {SECTION}: がありません")
    if not cfg.get("enabled", True) and "--dry-run" not in argv:
        print("note_weekly は無効です（config.yaml の note_weekly.enabled）")
        return
    end = dt.date.fromisoformat(argv[argv.index("--end") + 1]) if "--end" in argv else now_jst().date()
    try:
        make(cfg, end, dry_run="--dry-run" in argv)
    except Exception as ex:   # noqa: BLE001
        print("note 下書きの作成に失敗:", ex)
        if "--dry-run" not in argv and os.environ.get("DRY_RUN") != "1":
            post_discord(cfg.get("webhook_env") or "DISCORD_WEBHOOK_NOTE", username=cfg.get("title") or "note 下書き",
                         content=f"📝 今週の note 下書きは作れませんでした: {str(ex)[:300]}")
        raise


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
