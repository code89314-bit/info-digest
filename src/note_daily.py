"""毎朝のがん薬物療法ニュース（onco_news の曜日の回）を、その日のうちに note の記事として公開する。

素材: onco_news.make_episode が保存した「その日の回」（.local/note_daily/YYYY-MM-DD-episode.json）。
      動向まとめ・各記事の見出しと要約（NotebookLM が記事本文を読んで書いたもの）・podcast の mp3。
流れ: 記事を組み立てる → 機械チェック → note に下書き保存 → チェックに通れば公開（config の note_daily.publish）
      → Discord（note 下書きのチャンネル）に URL を知らせる。
      チェックに通らない日（NotebookLM が使えず Strata の要約だった、出典URLが無い など）は公開せず、下書きで止めて知らせる。

使い方: python src/note_daily.py                      今日の回で作る（ふだんは onco_news.py が最後に呼ぶ）
        python src/note_daily.py --date 2026-11-13    その日の回で作り直す
        python src/note_daily.py --draft-only         公開せず下書き保存まで
        python src/note_daily.py --dry-run            ファイルを作るだけ（note にも Discord にも送らない）
"""
import datetime as dt
import html
import json
import os
import re
import sys
from pathlib import Path

import ai_digest
from common import ROOT, post_discord, post_discord_file

SECTION = "note_daily"
JA_DAY = "月火水木金土日"
DEFAULT_NOTICE = ("この記事は、公開情報をもとにAI（NotebookLM 等）が自動で作成し、筆者の確認を経ずに公開しています。\n"
                  "誤りを含むことがあります。内容に当たっては、必ず一次資料（添付文書・ガイドライン・論文原著）をご確認ください。")
DEFAULT_FOOTER = ("※公開情報をもとにAI（NotebookLM 等）が自動で作成した記事です。詳細・判断は必ず一次資料でご確認ください。\n"
                  "※医療従事者向けの情報です。個人・個別の健康相談については、かかりつけ医、薬剤師にお尋ねください。")
AUDIO_NOTE = "※ここに音声ファイル「{}」を埋め込み（note の「＋」→ 音声）"   # note_publish が見つけて差し替える
TOC_NOTE = "※ここに note の目次を挿入"


def out_dir(cfg: dict) -> Path:
    return ROOT / (cfg.get("out_dir") or ".local/note_daily")


def episode_path(cfg: dict, day: dt.date) -> Path:
    return out_dir(cfg) / f"{day.isoformat()}-episode.json"


def save_episode(cfg: dict, day: dt.date, episode: dict) -> Path:
    """onco_news が呼ぶ: その日の回の材料を保存する（作り直し・確認用）。"""
    p = episode_path(cfg, day)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(episode, ensure_ascii=False, indent=1), encoding="utf-8")
    return p


# ---- Discord 向けの動向まとめ（**太字** と箇条書き）を note の HTML に ----
LINK = re.compile(r"\[([^\]]+)\]\((https?://[^)\s]+)\)")
BOLD = re.compile(r"\*\*(.+?)\*\*")


def inline(text: str) -> str:
    out, pos = [], 0
    for m in LINK.finditer(text):
        out.append(BOLD.sub(r"<strong>\1</strong>", html.escape(text[pos:m.start()])))
        out.append(f'<a href="{html.escape(m.group(2))}">{html.escape(m.group(1))}</a>')
        pos = m.end()
    out.append(BOLD.sub(r"<strong>\1</strong>", html.escape(text[pos:])))
    return "".join(out)


def md_to_html(text: str) -> str:
    """見出し（# や **だけの行**）→ h3、箇条書き（- ・ *）→ ul、それ以外 → p。"""
    b, ul = [], []

    def flush():
        if ul:
            b.append("<ul>" + "".join(f"<li>{x}</li>" for x in ul) + "</ul>")
            ul.clear()
    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line:
            flush()
            continue
        head = re.fullmatch(r"#{1,6}\s*(.+)", line) or re.fullmatch(r"\*\*([^*]+)\*\*[:：]?", line)
        item = re.match(r"^(?:[-*・•]|\d+[.)．])\s*(.+)", line)
        if head:
            flush()
            b.append(f"<h3>{inline(head.group(1).strip())}</h3>")
        elif item:
            ul.append(inline(item.group(1)))
        else:
            flush()
            b.append(f"<p>{inline(line)}</p>")
    flush()
    return "\n".join(b)


def title_of(cfg: dict, ep: dict, day: dt.date) -> str:
    tpl = cfg.get("title_format") or "{m}/{d}（{w}）がん薬物療法ニュース｜{theme}"
    return tpl.format(m=day.month, d=day.day, w=JA_DAY[day.weekday()], theme=ep.get("theme") or "今日のニュース")


def build(cfg: dict, ep: dict, day: dt.date, audio_name: str | None) -> dict:
    tags = (ai_digest.section_config("onco") or {}).get("tag_style") or {}
    items = [{"heading": (tags.get(i.get("tag")) or {}).get("emoji", "") + " " + (i.get("title_ja") or i.get("title") or ""),
              "summary": i.get("summary_ja") or "", "source": i.get("source") or "", "url": i.get("link") or "",
              "tag": i.get("tag") or ""} for i in ep.get("items") or []]
    for i in items:
        i["heading"] = i["heading"].strip()
    span = "今日の新着" if ep.get("daily") else "直近1週間"
    return {"title": title_of(cfg, ep, day), "intro": (cfg.get("intro") or "").strip(), "notice": (cfg.get("opening_notice") or DEFAULT_NOTICE).strip(),
            "lead": f"{day.month}/{day.day}（{JA_DAY[day.weekday()]}）のテーマは「{ep.get('theme')}」。"
                    f"{span}の記事 {len(items)} 件から、主なトピックをまとめました。",
            "overview": (ep.get("overview") or "").strip(), "items": items, "audio": audio_name,
            "audio_heading": cfg.get("audio_heading") or "音声版（AI音声）",
            "audio_body": (cfg.get("audio_body") or "NotebookLM（Google）で自動生成したAI音声です。話者は実在の人物ではありません。"
                           "内容に誤りを含むことがあります。").strip(),
            "footer": (cfg.get("footer") or DEFAULT_FOOTER).strip(), "hashtags": list(cfg.get("hashtags") or []),
            "by": ep.get("by") or ""}


def body_html(doc: dict) -> str:
    e = html.escape
    b = ["<p>" + "<br>".join(e(x) for x in doc["intro"].splitlines()) + "</p>" if doc.get("intro") else "",
         "<blockquote><p><strong>この記事について</strong><br>"
         + "<br>".join(e(x) for x in doc["notice"].splitlines()) + "</p></blockquote>",
         f"<p>{e(doc['lead'])}</p>"]
    if doc["audio"]:
        b += [f"<h2>{e(doc['audio_heading'])}</h2>", f"<p>{e(doc['audio_body'])}</p>",
              f"<p>{e(AUDIO_NOTE.format(doc['audio']))}</p>"]
    b.append(f"<p>{e(TOC_NOTE)}</p>")
    if doc["overview"]:
        b += ["<h2>今日のトピックス</h2>", md_to_html(doc["overview"])]
    b.append("<h2>記事ごとの要約</h2>")
    for i in doc["items"]:
        b.append(f"<h3>{e(i['heading'])}</h3>")
        src = f'<a href="{e(i["url"])}">{e(i["source"] or i["url"])}</a>' if i["url"] else e(i["source"])
        b.append(f"<p>{e(i['summary'])}<br><strong>出典</strong>　{src}</p>")
    b.append("<p>" + "<br>".join(e(x) for x in doc["footer"].splitlines()) + "</p>")
    return "\n".join(x for x in b if x)


def to_html(doc: dict, problems: list[str]) -> str:
    e = html.escape
    memo = ("" if not problems else '<hr><div style="background:#fff3cd;padding:8px"><p><strong>公開を止めた理由</strong></p><ul>'
            + "".join(f"<li>{e(p)}</li>" for p in problems) + "</ul></div>")
    return ("<!doctype html><html lang=\"ja\"><head><meta charset=\"utf-8\">"
            f"<title>{e(doc['title'])}</title><style>body{{max-width:720px;margin:2em auto;padding:0 16px;"
            "font-family:sans-serif;line-height:1.8}</style></head><body>\n"
            f"<p><small>タイトル</small><br><strong>{e(doc['title'])}</strong></p><hr>\n{body_html(doc)}\n{memo}\n</body></html>\n")


def check(cfg: dict, doc: dict) -> list[str]:
    """公開してよいかの機械チェック。問題があれば理由を返す（あれば下書きで止める）。"""
    out = []
    if len(doc["items"]) < int(cfg.get("min_items", 1)):
        out.append(f"記事が {len(doc['items'])} 件しかありません")
    if cfg.get("require_notebooklm", True) and doc["by"] != "NotebookLM":
        out.append("NotebookLM が使えず、ローカルのモデル（Strata）の要約になっています（薬の名前を取り違えることがあるため）")
    if not doc["overview"]:
        out.append("今日のトピックス（動向まとめ）がありません")
    no_url = [i["heading"] for i in doc["items"] if not i["url"].startswith("http")]
    if no_url:
        out.append("出典のURLが無い記事があります: " + "、".join(no_url[:3]))
    empty = [i["heading"] for i in doc["items"] if not i["summary"].strip() or not i["heading"].strip()]
    if empty:
        out.append("見出しか要約が空の記事があります")
    return out


def make_cover(cfg: dict, day: dt.date, dest: Path) -> Path | None:
    if not cfg.get("cover_image", True):
        return None
    sys.path.insert(0, str(ROOT / "scripts"))
    try:
        import make_cover as mc
        return mc.make_cover(day, day, dest, seed=day.toordinal(), head="毎朝")
    except Exception as ex:   # noqa: BLE001  画像は「おまけ」
        print("トップ画像を作れませんでした:", ex)
        return None


def audio_meta(cfg: dict, doc: dict, audio: Path | None, day: dt.date) -> dict | None:
    if not audio:
        return None
    cover = ROOT / (cfg.get("audio_cover") or "assets/onco.png")
    return {"path": str(audio), "cover": str(cover) if cover.exists() else None, "heading": doc["audio_heading"],
            "title": f"がん薬物療法ニュース {day.month}/{day.day}（AI音声）",
            "author": cfg.get("audio_author") or "じぶんの薬局（NotebookLM による AI音声）"}


def make(cfg: dict, day: dt.date, *, dry_run: bool = False, draft_only: bool = False) -> dict | None:
    p = episode_path(cfg, day)
    if not p.exists():
        print(f"{day} の回がありません: {p}")
        return None
    ep = json.loads(p.read_text(encoding="utf-8"))
    audio = Path(ep["audio"]) if ep.get("audio") and Path(ep["audio"]).exists() else None
    if audio and audio.stat().st_size > float(cfg.get("audio_max_mb", 100)) * 1e6:   # note の音声は100MBまで
        print(f"音声が note の上限を超えています: {audio.name}")
        audio = None
    doc = build(cfg, ep, day, audio.name if audio else None)
    problems = check(cfg, doc)
    base = out_dir(cfg) / day.isoformat()
    base.with_suffix(".html").write_text(to_html(doc, problems), encoding="utf-8")
    cover = make_cover(cfg, day, base.with_name(f"{day.isoformat()}-cover.png"))
    info = {"title": doc["title"], "body_html": body_html(doc), "audio": audio_meta(cfg, doc, audio, day),
            "cover_image": str(cover) if cover else None, "hashtags": doc["hashtags"]}
    base.with_suffix(".json").write_text(json.dumps(info, ensure_ascii=False), encoding="utf-8")
    publish = cfg.get("publish", False) and not draft_only and not problems
    print(f"{doc['title']}: 記事 {len(doc['items'])} 件・音声 {'あり' if audio else 'なし'}・"
          f"{'公開する' if publish else '下書きまで'}" + "".join(f"\n  止めた理由: {x}" for x in problems))
    if dry_run or os.environ.get("DRY_RUN") == "1":
        return {"doc": doc, "problems": problems, "publish": publish}

    import note_publish
    webhook = cfg.get("webhook_env") or "DISCORD_WEBHOOK_NOTE"
    name = cfg.get("title") or "note 毎日のニュース"
    try:
        url, todo, published = note_publish.save_and_publish(
            doc["title"], info["body_html"], audio=info["audio"], cover=info["cover_image"],
            headless=cfg.get("headless", False), publish_tags=doc["hashtags"] if publish else None)
    except Exception as ex:   # noqa: BLE001
        post_discord(webhook, username=name, avatar_url=cfg.get("avatar_url"),
                     content=f"📰 今日の note（{doc['title']}）を作れませんでした: {str(ex)[:300]}")
        raise
    if published:
        line = f"📰 **note に公開しました：{doc['title']}**\n{url}"
    else:
        why = problems + todo + ([] if publish or problems else ["公開しない設定です（--draft-only か config の note_daily.publish）"])
        line = (f"📝 **note に下書き保存しました（公開はしていません）：{doc['title']}**\n{url}"
                + "".join(f"\n-# 理由: {w}" for w in why) + "\n-# 確かめて、よければ note で「公開に進む」→「投稿する」")
    post_discord(webhook, username=name, avatar_url=cfg.get("avatar_url"), content=line)
    if not published:
        post_discord_file(webhook, base.with_suffix(".html"), username=name, avatar_url=cfg.get("avatar_url"))
    return {"doc": doc, "problems": problems, "url": url, "published": published}


def run_for_episode(day: dt.date) -> None:
    """onco_news から呼ぶ。失敗しても onco_news 本体は止めない。"""
    cfg = ai_digest.section_config(SECTION)
    if not cfg or not cfg.get("enabled", False):
        return
    if day.weekday() in [JA_DAY.index(d) for d in (cfg.get("skip_days") or [])]:
        print(f"{JA_DAY[day.weekday()]}曜は note の毎日の記事を作りません（note_daily.skip_days）")
        return
    try:
        make(cfg, day)
    except Exception as ex:   # noqa: BLE001
        print("note の毎日の記事に失敗:", ex)


def main(argv: list[str]) -> None:
    cfg = ai_digest.section_config(SECTION)
    if not cfg:
        raise SystemExit(f"config.yaml に {SECTION}: がありません")
    day = dt.date.fromisoformat(argv[argv.index("--date") + 1]) if "--date" in argv else \
        dt.datetime.utcfromtimestamp(dt.datetime.now().timestamp() + 9 * 3600).date()
    make(cfg, day, dry_run="--dry-run" in argv, draft_only="--draft-only" in argv)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
