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
import sys

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
- source: 出典の表記（機関名・論文の書誌）。url: 一次情報のURL（無ければ記事のURL）。
- i: もとにした記事の番号（数値。複数なら最も一次情報に近い1つ）。

記事の一覧（番号: [分類] 見出し — URL）:
{items}

JSONのみを出力:
{{"topics": [{{"i": 0, "theme": "テーマ名", "heading": "...", "conclusion": "...", "before": "...", "next": "...", "data": "...", "source": "...", "url": "..."}}]}}"""

LEAD_PROMPT = """{digest}
以下は今週の号に載せるトピックの小見出しと結論です。

{topics}

次をJSONのみで出力してください。
- headline_1, headline_2: タイトル用に、今週の重要トピック2つ。それぞれ15字以内の名詞句（例「ペムブロリズマブ適応追加」）。
- three_lines: 「今週の3行」。最重要トピック3つを、それぞれ30字以内の1文で。ファクトのみ。
- hashtags: その号の一般名・がん種・学会名・ガイドライン名から5〜8個。「#」は付けない。

{{"headline_1": "...", "headline_2": "...", "three_lines": ["...", "...", "..."], "hashtags": ["...", "..."]}}"""

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
        lines = [f"[{i}] [{th}] {items[i]['title_ja']} — {items[i]['link']}" for i, th in theme_of.items()]
        prompt = TOPICS_PROMPT.format(digest=(style.get("prompt_digest") or "").strip(), rules=rules,
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
                     "url": clean(t.get("url")) or art.get("link", "")}
            if not topic["source"] and art:
                topic["source"] = art.get("source", "")
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


def build(style: dict, lead: dict, topics: list[dict], start: dt.date, end: dt.date) -> dict:
    """号の中身（md と html の共通の材料）。"""
    links = section(style, "links")
    after = section(style, "afterword")
    return {"title": title_of(style, lead, start, end), "greeting": greeting(style, start, end),
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
    md = [f"# {doc['title']}", "", doc["greeting"], "", "**今週の3行**", ""]
    md += [f"- {x}" for x in doc["three_lines"]]
    md += ["", "※ここに note の目次を挿入", ""]
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
    md += ["", f"## {doc['afterword_heading']}", "", doc["afterword"], "", doc["footer"].replace("\n", "  \n"), "",
           " ".join(f"#{h}" for h in doc["hashtags"])]
    if memo:
        md += ["", "---", "", "## 確認メモ（公開前に削除）", ""] + [f"- {m}" for m in memo]
    return "\n".join(md).rstrip() + "\n"


def to_html(doc: dict, memo: list[str]) -> str:
    """note に貼る用の簡素なHTML（ブラウザで開いて全選択コピー → note に貼り付け）。大見出し=h2、小見出し=h3。"""
    e = html.escape
    b = [f"<p><small>タイトル（note のタイトル欄へ）</small><br><strong>{e(doc['title'])}</strong></p><hr>",
         f"<p>{e(doc['greeting'])}</p>", "<p><strong>今週の3行</strong></p>",
         "<ul>" + "".join(f"<li>{e(x)}</li>" for x in doc["three_lines"]) + "</ul>",
         "<p>※ここに note の目次を挿入</p>"]
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
    b += [f"<h2>{e(doc['afterword_heading'])}</h2>", f"<p>{e(doc['afterword'])}</p>",
          "<p>" + "<br>".join(e(x) for x in doc["footer"].splitlines()) + "</p>",
          "<p>" + " ".join(e(f"#{h}") for h in doc["hashtags"]) + "</p>"]
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


def check(doc: dict, style: dict) -> tuple[list[str], list[tuple[str, str]]]:
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
    for where, s in long:
        memo.append(f"{where}：{max_chars}字超（{len(s)}字）「{s}」")
    return memo, long


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
        topics = write_topics(nb, cfg, style, items, start, end)
        if not topics:
            raise RuntimeError("NotebookLM からトピックを得られませんでした")
        lead = write_lead(nb, style, topics)
    finally:
        nb.close()
    doc = build(style, lead, topics, start, end)
    memo, long = check(doc, style)
    for i, s in split_suggestions(long, cfg).items():
        memo.append(f"分割案：「{long[i][1]}」→「{s}」")
    out_dir = ROOT / (cfg.get("out_dir") or ".local/note_drafts")
    out_dir.mkdir(parents=True, exist_ok=True)
    y, w, _ = end.isocalendar()
    md_path, html_path = out_dir / f"{y}-W{w:02d}.md", out_dir / f"{y}-W{w:02d}.html"
    md_path.write_text(to_markdown(doc, memo), encoding="utf-8")
    html_path.write_text(to_html(doc, memo), encoding="utf-8")
    print(f"下書きを保存しました: {md_path}\n                    {html_path}\nトピック {len(topics)} 件・確認メモ {len(memo)} 件")
    content = (f"📝 **note 下書き：{doc['title']}**\n**今週の3行**\n" + "\n".join(f"- {x}" for x in doc["three_lines"])
               + f"\n-# トピック {len(topics)}件・確認メモ {len(memo)}件。html をブラウザで開き、全選択コピー → note に貼り付け")
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
