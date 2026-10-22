"""疑問深堀り: Discord の疑問深堀チャンネルに書いた日常診療の疑問を、一次資料で確かめて note の下書きにする。

流れ（1つの疑問ごと）:
  1) Strata（ローカル）が患者を特定しうる情報を確認。疑わしければ外部には何も送らず、言い換え案を返して止める
  2) Strata が疑問を解釈し、確かめる論点・ウェブ検索語・調査指示を作る
  3) NotebookLM のウェブ調査＋検索で、添付文書・ガイドライン・論文・行政資料を資料に入れる
  4) NotebookLM が論点ごとに「結論 → 根拠 → 注意 → 出典」を書く（書き方は style/gimon.yaml）
  5) 機械チェック（確認メモ）→ .md / .html / トップ画像 → note に下書き保存（公開は手で）
  6) 音声: NotebookLM の音声枠に空きがあれば作る。無ければノートを残して待ち、空いたら作って下書きに足す（qa_bot.py）
Discord とのやりとりは src/qa_bot.py。ここは Discord に依存しない（テストしやすいように）。
"""
import datetime as dt
import html
import json
import re
import sys
from urllib.parse import urlparse

import ai_digest
from ai_digest import CHAT, llm_settings
from common import ROOT, load_config
from note_weekly import EMOJI, PRIMARY as NEWS_PRIMARY, clean, is_primary_host, parse_obj, sentences
from note_weekly import load_style as _load_style

SECTION = "qa_bot"
LABELS = [("conclusion", "結論"), ("evidence", "根拠"), ("note", "注意"), ("source", "出典")]
PRIMARY = NEWS_PRIMARY + ["go.jp", "lg.jp", "jstage.jst.go.jp", "pubmed.ncbi.nlm.nih.gov", "cochranelibrary.com",
                          "nccn.org", "esmo.org", "asco.org", "who.int", "bmj.com", "springer.com", "wiley.com",
                          "sciencedirect.com", "jspm.ne.jp", "jshp.or.jp", "jsco-cpg.jp", "japha.or.jp"]

PRIVACY_PROMPT = """次は、医療者が日常診療の疑問として書いた文です。これを外部サービス（Google NotebookLM）と公開記事（note）に
送ってよいかを確認してください。

送ってはいけない情報（患者・職員を特定しうるもの）:
- 氏名・イニシャル・患者ID・カルテ番号・病室・生年月日・住所・勤務先や施設の名前・職員の名前
- 具体的な日付（「10/3に入院」など）と、具体的な年齢・稀な病名・経過などが組み合わさって個人が推測できる記述
送ってよいもの: 一般化した臨床状況（「70代の腎機能低下患者」「透析中の患者」「術後補助療法中」など）、薬剤名、検査値の範囲。

疑問の文:
{text}

JSONのみを出力: {{"ok": true または false, "reasons": ["送ってはいけない理由（ok が false のとき）"],
"rewrite": "個人を特定しうる部分を一般化した疑問文（ok が false のとき）"}}"""

INTERPRET_PROMPT = """あなたは、がん薬物療法に詳しい病院薬剤師の調べものを手伝う編集者です。
次の日常診療の疑問を、一次資料（添付文書・インタビューフォーム・ガイドライン・論文・行政資料）で確かめるための計画を立ててください。

疑問:
{text}

- title: 記事タイトル用の短い名詞句（25字以内）。例「腎機能低下時のカペシタビン減量」
- interpretation: 疑問の解釈（何を知りたいのか、暗黙の前提）。2〜3文。
- points: 確かめる論点。3〜5個。それぞれ1文の問い。
- queries: ウェブ検索語。3〜5個。日本語（添付文書・ガイドライン・厚労省）と英語（論文・海外ガイドライン）を混ぜる。各10語以内。
- research: NotebookLM のウェブ調査への指示（日本語、1〜2文。探す資料の種類を具体的に）。

JSONのみを出力:
{{"title": "...", "interpretation": "...", "points": ["...", "..."], "queries": ["...", "..."], "research": "..."}}"""

WRITE_PROMPT = """{digest}
{rules}
今回のギモン:
{question}

疑問の解釈（参考）: {interpretation}
確かめる論点（この順に。資料で答えが確かめられるものだけを書く。最大{max_points}個）:
{points}

資料の一覧（出典の url はここから選ぶ）:
{sources}

書き方の見本（この文体・文末で書く）:
{example}

- heading: 論点の小見出し（30字以内）。
- conclusion / evidence / note: それぞれ1〜2文。1文40字以内。note は資料に無ければ ""。
- source: 出典の表記（添付文書・ガイドライン・論文の書誌）。url: 上の一覧のURL。
- three_lines: 「結論（3行）」。疑問への答えを、資料にあることだけで3つ、それぞれ30字以内の1文で。
- unknown: 資料で答えが出なかった論点（1〜3文。無ければ ""）。作文しない。
- hashtags: 一般名・疾患名・ガイドライン名から4〜7個。「#」は付けない。

JSONのみを出力:
{{"three_lines": ["...", "...", "..."], "unknown": "...", "hashtags": ["..."],
  "points": [{{"heading": "...", "conclusion": "...", "evidence": "...", "note": "...", "source": "...", "url": "..."}}]}}"""


def load_style(cfg: dict) -> dict:
    return _load_style({"style_file": "style/gimon.yaml", **{k: v for k, v in cfg.items() if v}})


def strata_obj(prompt: str, cfg: dict) -> dict:
    llm = llm_settings(cfg)
    return parse_obj(CHAT[llm["provider"]](prompt, llm))


def privacy_check(text: str, cfg: dict) -> dict:
    """{"ok": bool, "reasons": [...], "rewrite": str}。Strata が答えられないときは止める側に倒す。"""
    try:
        got = strata_obj(PRIVACY_PROMPT.format(text=text), cfg)
    except Exception as ex:   # noqa: BLE001
        return {"ok": False, "reasons": [f"Strata で確認できませんでした（{str(ex)[:100]}）"], "rewrite": ""}
    ok = got.get("ok")
    ok = ok is True or str(ok).lower() == "true"
    return {"ok": ok, "reasons": [clean(r) for r in got.get("reasons") or [] if clean(r)],
            "rewrite": clean(got.get("rewrite"))}


def interpret(text: str, cfg: dict) -> dict:
    got = strata_obj(INTERPRET_PROMPT.format(text=text), cfg)
    title = clean(got.get("title")) or text[:25]
    points = [clean(p) for p in got.get("points") or [] if clean(p)][:6]
    queries = [clean(q) for q in got.get("queries") or [] if clean(q)][:5]
    return {"title": title[:30], "interpretation": clean(got.get("interpretation")),
            "points": points or [text], "queries": queries or [title],
            "research": clean(got.get("research")) or f"{title}について、添付文書・ガイドライン・論文などの一次資料"}


def gather(nb, plan: dict, cfg: dict) -> list[str]:
    """ウェブ調査（まとめて取り込み）と、検索語ごとの検索（一次資料のドメインだけ）。ログを返す。"""
    log = []
    try:
        n = nb.research(plan["research"] + "\n（一次資料を優先: 添付文書・インタビューフォーム・ガイドライン・論文・行政資料）",
                        mode=cfg.get("research_mode", "fast"), fallback=plan["queries"][0])
        log.append(f"ウェブ調査: 資料 {n} 件")
    except Exception as ex:   # noqa: BLE001
        log.append(f"ウェブ調査に失敗: {str(ex)[:150]}")
    have = {s["url"] for s in nb.sources()} if hasattr(nb, "sources") else set()
    extra, limit = [], int(cfg.get("max_extra_sources", 8))
    domains = cfg.get("primary_domains") or PRIMARY
    for q in plan["queries"]:
        for c in nb.discover(q):
            u = c["url"]
            if len(extra) < limit and u not in have and u not in extra and \
                    is_primary_host(urlparse(u).netloc.lower(), domains):
                extra.append(u)
    if extra:
        loaded = nb.add_urls(extra)
        log.append(f"検索で一次資料を追加: {len(loaded)}/{len(extra)} 件")
    return log


def write(nb, style: dict, question: str, plan: dict) -> dict:
    rules = "【守ること】\n" + "".join(f"- {r}\n" for r in ((style.get("rules") or {}).get("must") or []))
    issue = style.get("issue") or {}
    max_points = int(issue.get("max_points", 6))
    srcs = [s for s in (nb.sources() if hasattr(nb, "sources") else []) if s.get("url")]
    data = parse_obj(nb.ask(WRITE_PROMPT.format(
        digest=(style.get("prompt_digest") or "").strip(), rules=rules, question=question,
        interpretation=plan["interpretation"] or "（なし）", max_points=max_points,
        points="\n".join(f"- {p}" for p in plan["points"]),
        sources="\n".join(f"- {s['title']} — {s['url']}" for s in srcs) or "（URL の一覧なし）",
        example=((style.get("point_block") or {}).get("format_example") or "").strip())))
    points = []
    for p in data.get("points") or []:
        if isinstance(p, dict) and clean(p.get("heading")) and clean(p.get("conclusion")):
            points.append({"heading": clean(p.get("heading")), **{k: clean(p.get(k)) for k, _ in LABELS},
                           "url": clean(p.get("url"))})
    if not points:
        raise RuntimeError("NotebookLM から答えを得られませんでした（資料が見つからなかった可能性）")
    return {"points": points[:max_points], "unknown": clean(data.get("unknown")),
            "three_lines": [clean(x) for x in data.get("three_lines") or [] if clean(x)][:3] or
            [p["conclusion"] for p in points[:3]],
            "hashtags": [clean(h).lstrip("#＃") for h in data.get("hashtags") or [] if clean(h)]}


# ---- 組み立て ----
AUDIO_NOTE = "※ここに音声ファイル「{}」を埋め込み（note の「＋」→ 音声）"   # note_publish が見つけて差し替える
TOC_NOTE = "※ここに note の目次を挿入"


def build(style: dict, question: str, plan: dict, body: dict, audio_name: str) -> dict:
    issue, pod, opening = style.get("issue") or {}, style.get("podcast") or {}, style.get("opening_notice") or {}
    links, seen = [], set()
    for p in body["points"]:
        if p["url"] and p["url"] not in seen:
            seen.add(p["url"])
            links.append((p["source"] or p["url"], p["url"]))
    fixed = list((style.get("hashtags") or {}).get("fixed") or [])
    return {"title": (issue.get("title") or "【ギモン深堀り】{title}").replace("{title}", plan["title"]),
            "opening_heading": opening.get("heading") or "", "opening": (opening.get("body") or "").strip(),
            "question_heading": issue.get("question_heading") or "今回のギモン", "question": question,
            "three_heading": issue.get("three_lines_heading") or "結論（3行）", "three_lines": body["three_lines"],
            "audio": audio_name if pod.get("enabled", True) else None,
            "audio_heading": pod.get("heading") or "音声版", "audio_body": (pod.get("body") or "").strip(),
            "points_heading": issue.get("points_heading") or "深堀り", "points": body["points"],
            "unknown_heading": issue.get("unknown_heading") or "資料で分からなかったこと", "unknown": body["unknown"],
            "links_heading": issue.get("links_heading") or "根拠資料（まとめ）", "links": links,
            "afterword_heading": issue.get("afterword_heading") or "編集後記", "afterword": issue.get("afterword") or "",
            "footer": (style.get("fixed_footer") or "").strip(),
            "hashtags": list(dict.fromkeys(t.replace(" ", "") for t in fixed + body["hashtags"] if t))[:12]}


def to_markdown(doc: dict, memo: list[str]) -> str:
    md = [f"# {doc['title']}", ""]
    if doc["opening"]:
        md += [f"> **{doc['opening_heading']}**  "] + [f"> {x}  " for x in doc["opening"].splitlines()] + [""]
    md += [f"**{doc['question_heading']}**", ""] + [f"> {x}  " for x in doc["question"].splitlines()] + [""]
    md += [f"**{doc['three_heading']}**", ""] + [f"- {x}" for x in doc["three_lines"]] + [""]
    if doc["audio"]:
        md += [f"## {doc['audio_heading']}", "", doc["audio_body"].replace("\n", "  \n"), "",
               AUDIO_NOTE.format(doc["audio"]), ""]
    md += [TOC_NOTE, "", f"## {doc['points_heading']}", ""]
    for p in doc["points"]:
        md.append(f"### {p['heading']}")
        for k, label in LABELS:
            if p[k]:
                md.append(f"**{label}**　" + (f"[{p[k]}]({p['url']})" if k == "source" and p["url"] else p[k]) + "  ")
        md.append("")
    if doc["unknown"]:
        md += [f"## {doc['unknown_heading']}", "", doc["unknown"], ""]
    md += [f"## {doc['links_heading']}", ""] + [f"- {t}：{u}" for t, u in doc["links"]]
    md += ["", f"## {doc['afterword_heading']}", "", doc["afterword"], "",
           doc["footer"].replace("\n", "  \n"), "", " ".join(f"#{h}" for h in doc["hashtags"])]
    if memo:
        md += ["", "---", "", "## 確認メモ（公開前に削除）", ""] + [f"- {m}" for m in memo]
    return "\n".join(md).rstrip() + "\n"


def body_html(doc: dict) -> str:
    e = html.escape
    br = lambda t: "<br>".join(e(x) for x in t.splitlines())   # noqa: E731
    b = [(f"<blockquote><p><strong>{e(doc['opening_heading'])}</strong><br>{br(doc['opening'])}</p></blockquote>"
          if doc["opening"] else ""),
         f"<p><strong>{e(doc['question_heading'])}</strong></p>", f"<p>{br(doc['question'])}</p>",
         f"<p><strong>{e(doc['three_heading'])}</strong></p>",
         "<ul>" + "".join(f"<li>{e(x)}</li>" for x in doc["three_lines"]) + "</ul>"]
    if doc["audio"]:
        b += [f"<h2>{e(doc['audio_heading'])}</h2>", f"<p>{br(doc['audio_body'])}</p>",
              f"<p>{e(AUDIO_NOTE.format(doc['audio']))}</p>"]
    b += [f"<p>{TOC_NOTE}</p>", f"<h2>{e(doc['points_heading'])}</h2>"]
    for p in doc["points"]:
        rows = [f"<strong>{label}</strong>　" + (f'<a href="{e(p["url"])}">{e(p[k])}</a>' if k == "source" and p["url"]
                                                else e(p[k])) for k, label in LABELS if p[k]]
        b += [f"<h3>{e(p['heading'])}</h3>", "<p>" + "<br>".join(rows) + "</p>"]
    if doc["unknown"]:
        b += [f"<h2>{e(doc['unknown_heading'])}</h2>", f"<p>{e(doc['unknown'])}</p>"]
    b += [f"<h2>{e(doc['links_heading'])}</h2>",
          "<ul>" + "".join(f'<li>{e(t)}：<a href="{e(u)}">{e(u)}</a></li>' for t, u in doc["links"]) + "</ul>",
          f"<h2>{e(doc['afterword_heading'])}</h2>", f"<p>{e(doc['afterword'])}</p>",
          f"<p>{br(doc['footer'])}</p>", "<p>" + " ".join(e(f"#{h}") for h in doc["hashtags"]) + "</p>"]
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


def check(doc: dict, style: dict, primary: list[str] | None = None) -> list[str]:
    memo = []
    max_chars = int((style.get("sentences") or {}).get("max_chars", 40))
    forbidden = (style.get("voice") or {}).get("forbidden_phrases") or []
    texts = [(f"結論（3行）{n + 1}", x) for n, x in enumerate(doc["three_lines"])]
    for p in doc["points"]:
        texts.append((f"「{p['heading']}」小見出し", p["heading"]))
        texts += [(f"「{p['heading']}」{label}", p[k]) for k, label in LABELS if k != "source" and p[k]]
    for where, text in texts:
        if not where.endswith("小見出し"):
            for s in sentences(text):
                if len(s.rstrip("。")) + 1 > max_chars:
                    memo.append(f"{where}：{max_chars}字超（{len(s)}字）「{s}」")
        for w in forbidden:
            if w in text:
                memo.append(f"{where}：使わない語「{w}」")
        if re.search(r"(である|だ|いる|する|ある)。", text):
            memo.append(f"{where}：だ・である調")
        if EMOJI.search(text) or "→" in text or re.search("[!！]", text):
            memo.append(f"{where}：絵文字・「→」・感嘆符")
    for p in doc["points"]:
        host = urlparse(p["url"]).netloc.lower()
        if not host:
            memo.append(f"「{p['heading']}」：出典のURLがありません")
        elif not is_primary_host(host, primary or PRIMARY):
            memo.append(f"「{p['heading']}」：出典が一次資料のサイトではありません（{host}）。原文を確認してください")
    return memo


def make_cover_image(day: dt.date, dest):
    sys.path.insert(0, str(ROOT / "scripts"))
    try:
        import make_cover
        return make_cover.make_cover_gimon(day, dest)
    except Exception as ex:   # noqa: BLE001  画像は「おまけ」
        print("トップ画像を作れませんでした:", ex)
        return None


def notebook_settings(cfg: dict, style: dict, title: str = "") -> dict:
    base = (load_config().get("onco") or {}).get("podcast") or {}
    pod = style.get("podcast") or {}
    return {**base, "title": f"ギモン深堀り {title}"[:60], "label": f"ギモン深堀り：{title}",
            "instructions": (pod.get("instructions") or "").strip() + (f"\nテーマ: {title}" if title else ""),
            **(cfg.get("notebook") or {})}


def audio_meta(style: dict, path, title: str) -> dict:
    pod = style.get("podcast") or {}
    cover = ROOT / (pod.get("cover") or "assets/onco.png")
    return {"path": str(path), "cover": str(cover) if cover.exists() else None,
            "title": (pod.get("audio_title") or "ギモン深堀り：{title}（AI音声）").replace("{title}", title),
            "author": pod.get("author") or "NotebookLM（AI音声）", "heading": pod.get("heading") or "音声版"}


# ---- 音声の枠 ----
def audio_slots(pod: dict, cfg: dict, now: dt.datetime | None = None, used: int | None = None) -> int:
    """今使ってよい音声の枠の数。ほかの番組のために空けておく枠（config の audio_reserve）を差し引く。"""
    now = now or dt.datetime.now()
    if used is None:
        import podcast_nlm
        used = podcast_nlm.used_today(pod)
    reserve = 0
    for r in cfg.get("audio_reserve") or []:
        if "weekday" in r and int(r["weekday"]) != now.weekday():
            continue
        if now.strftime("%H:%M") < str(r.get("until", "00:00")):
            reserve += int(r.get("n", 1))
    return int(pod.get("daily_limit", 3)) - used - reserve


# ---- 1つの疑問を処理（Discord から呼ぶ） ----
def answer(question: str, cfg: dict, *, qid: str, nb_factory=None, publish: bool = True) -> dict:
    """戻り値: {"status": "blocked", privacy} か {"status": "done", doc, memo, md, html, json, plan, note_url, todo, nb}。
    nb（NotebookLM のノート）は閉じずに返す（音声を作るまで残す。使い終わったら呼び出し側で close）。"""
    privacy = privacy_check(question, cfg)
    if not privacy["ok"]:
        return {"status": "blocked", "privacy": privacy}
    style = load_style(cfg)
    plan = interpret(question, cfg)
    print(f"[{qid}] 解釈: {plan['title']} / 論点 {len(plan['points'])} / 検索語 {plan['queries']}")
    if nb_factory is None:
        import podcast_nlm
        nb_factory = lambda: podcast_nlm.Notebook(notebook_settings(cfg, style, plan["title"]), [])   # noqa: E731
    nb = nb_factory()
    try:
        log = gather(nb, plan, cfg)
        print(f"[{qid}] " + " / ".join(log))
        body = write(nb, style, question, plan)
    except BaseException:
        nb.close()
        raise
    audio_name = f"{qid}.mp3"
    doc = build(style, question, plan, body, audio_name)
    memo = check(doc, style, cfg.get("primary_domains")) + log
    out_dir = ROOT / (cfg.get("out_dir") or ".local/qa")
    out_dir.mkdir(parents=True, exist_ok=True)
    md_path, html_path, json_path = out_dir / f"{qid}.md", out_dir / f"{qid}.html", out_dir / f"{qid}.json"
    md_path.write_text(to_markdown(doc, memo), encoding="utf-8")
    html_path.write_text(to_html(doc, memo), encoding="utf-8")
    cover = make_cover_image(dt.date.today(), out_dir / f"{qid}-cover.png") if cfg.get("cover_image", True) else None
    json_path.write_text(json.dumps({"title": doc["title"], "body_html": body_html(doc), "audio": None,
                                     "cover_image": str(cover) if cover else None}, ensure_ascii=False),
                         encoding="utf-8")
    note_url, todo = "", []
    post = cfg.get("note_post") or {}
    if publish and post.get("enabled", True):
        import note_publish
        try:   # 音声はあとで足す（この時点では位置メモのまま）
            note_url, todo = note_publish.save_draft(doc["title"], body_html(doc), cover=cover,
                                                     headless=post.get("headless", False))
        except Exception as ex:   # noqa: BLE001
            todo = [f"note に下書き保存できませんでした（手で貼ってください）: {str(ex)[:200]}"]
    return {"status": "done", "doc": doc, "memo": memo, "md": md_path, "html": html_path, "json": json_path,
            "plan": plan, "note_url": note_url, "todo": todo, "nb": nb, "style": style}


def add_audio(nb, style: dict, title: str, qid: str, note_url: str, cfg: dict) -> dict:
    """音声を作り、note の下書きに埋め込む。{"raw", "small", "secs", "todo"}。"""
    import shutil

    import podcast_nlm
    out_dir = ROOT / (cfg.get("out_dir") or ".local/qa")
    raw, small = out_dir / f"{qid}.mp3", out_dir / f"{qid}-discord.mp3"
    nb.audio(raw)
    ffmpeg = nb.pod.get("ffmpeg") or shutil.which("ffmpeg") or "ffmpeg"
    secs = podcast_nlm.compress(raw, small, float(nb.pod.get("max_mb", 9.5)), ffmpeg)
    todo = []
    post = cfg.get("note_post") or {}
    if note_url and post.get("enabled", True):
        import note_publish
        try:
            _, todo = note_publish.save_draft("", "", audio=audio_meta(style, raw, title), edit_url=note_url,
                                              headless=post.get("headless", False))
        except Exception as ex:   # noqa: BLE001
            todo = [f"音声を note に埋め込めませんでした（手で: {raw}）: {str(ex)[:150]}"]
    return {"raw": raw, "small": small, "secs": secs, "todo": todo}


def section_config() -> dict:
    return ai_digest.section_config(SECTION) or {}
