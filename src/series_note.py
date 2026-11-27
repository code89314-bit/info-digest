"""週の連載を note の「下書き」にする（各回の記事＋音声、週の「まとめて聴く」）。公開はしない（2026-11-23 本人と決めた形の試作）。

- 各回: ep<n>-article.md（出典つきの記事）の前に「この記事について」（AI 音声・学習用のおことわり）と音声を置く。
  note は表を使えないので、表は1行ずつの箇条書きにする。
- まとめて聴く: 週の全回の音声を1ページに順に並べる（ページを移らずに続けて聞ける）。
- 音声: audio/ep<n>-nlm-2p.mp3（2人用の台本で作ったもの）があればそれ、無ければ ep<n>-nlm.mp3。無い回は「音声は後日」。
- 見出し画像: <週>/cover/series_cover.png（シリーズに1枚。作り方は .claude/skills/series-script/scripts/cover_moss.py）。
- 下書き保存は note_publish.save_draft（ブラウザの画面が出る）。マガジンへの追加・公開は本人が note で行う。

usage: python src/series_note.py <週のフォルダ> [--eps 1,2,3] [--summary] [--dry-run]
  --dry-run: .local/series/<週>/note/ に html と json を作るだけ（note に送らない）
"""
import argparse
import html
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from common import ROOT  # noqa: E402

AUDIO_NOTE = "※ここに音声ファイル「{}」を埋め込み（note の「＋」→ 音声）"   # note_publish.AUDIO_MARK を含む
TOC_NOTE = "※ここに note の目次を挿入"
AUTHOR = "NotebookLM（Google）による AI 音声"
COVER = ROOT / "assets" / "onco.png"
NOTICE = ("医療者（薬剤師・がん治療に関わる方）向けの学習用の連載です。台本は一次資料（添付文書・インタビューフォーム・審査報告書・"
          "診療ガイドライン・学会の手引き）をもとに作り、事実は出典を確かめています。音声は台本をもとに NotebookLM（Google）で"
          "生成した AI 音声で、話者は実在の人物ではありません。音声では言い回しが変わることがあるので、数字・推奨は記事と一次資料で"
          "ご確認ください。個別の患者さんへの判断は、最新の添付文書・ガイドライン原文で行ってください。")
FOOTER = ("※医療従事者向けの学習用の記事です。個人・個別の健康相談については、かかりつけ医、薬剤師にお尋ねください。\n"
          "※音声は AI（NotebookLM）で生成しています。")
LINK = re.compile(r"\[([^\]]+)\]\((https?://[^)\s]+)\)")
BOLD = re.compile(r"\*\*([^*]+)\*\*")


def inline(t: str) -> str:
    out, pos = [], 0
    for m in LINK.finditer(t):
        out.append(BOLD.sub(r"<strong>\1</strong>", html.escape(t[pos:m.start()])))
        out.append(f'<a href="{html.escape(m.group(2))}">{html.escape(m.group(1))}</a>')
        pos = m.end()
    out.append(BOLD.sub(r"<strong>\1</strong>", html.escape(t[pos:])))
    return "".join(out)


def cells(line: str) -> list[str]:
    return [c.strip() for c in line.strip().strip("|").split("|")]


def md_to_html(text: str) -> str:
    """## → h2、### と **だけの行** → h3、箇条書き → ul、表 → ul（「**1列目**：残りの列」）、それ以外 → p。"""
    b, ul, head = [], [], None

    def flush():
        if ul:
            b.append("<ul>" + "".join(f"<li>{x}</li>" for x in ul) + "</ul>")
            ul.clear()
    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith("|"):
            c = cells(line)
            if all(re.fullmatch(r":?-{2,}:?", x) for x in c):
                continue
            if head is None:          # 表の見出しの行（読み飛ばす。列名は1列目の太字で分かる）
                head = c
                continue
            ul.append(f"<strong>{inline(c[0])}</strong>：" + "／".join(inline(x) for x in c[1:] if x))
            continue
        head = None
        if not line:
            flush()
            continue
        h2 = re.fullmatch(r"##\s+(.+)", line)
        h3 = re.fullmatch(r"#{3,6}\s+(.+)", line) or re.fullmatch(r"\*\*([^*]+)\*\*[:：]?", line)
        item = re.match(r"^(?:[-*・•]|\d+[.)．])\s+(.+)", line)
        if h2:
            flush()
            b.append(f"<h2>{inline(h2.group(1))}</h2>")
        elif h3:
            flush()
            b.append(f"<h3>{inline(h3.group(1))}</h3>")
        elif item:
            ul.append(inline(item.group(1)))
        elif re.fullmatch(r"#\s+.+", line):
            continue                  # 記事の題（note のタイトルは台本の題から作る）
        else:
            flush()
            b.append(f"<p>{inline(line)}</p>")
    flush()
    return "\n".join(b)


def cover_of(d: Path) -> str | None:
    """週の見出し画像（シリーズに1枚。本人がディレクションして選んだもの）: <週>/cover/series_cover.png"""
    f = d / "cover" / "series_cover.png"
    return str(f) if f.exists() else None


def audio_of(d: Path, n: int) -> Path | None:
    for name in (f"ep{n}-nlm-2p.mp3", f"ep{n}-nlm.mp3"):
        if (d / "audio" / name).exists():
            return d / "audio" / name
    return None


def front(d: Path, n: int) -> dict:
    """台本の頭の series: と title:（記事の題の書き方は回ごとにばらつくので、台本のほうを使う）。"""
    t = (d / f"ep{n}-script.md").read_text(encoding="utf-8").split("---", 2)[1]
    return {k: v.strip() for k, v in re.findall(r"^(\w+):\s*(.+)$", t, re.M)}


def series_name(d: Path, n: int) -> str:
    return re.sub(r"^連載[:：]\s*", "", front(d, n).get("series", d.name))


def short_of(d: Path, n: int) -> str:
    return f"第{n}回 {front(d, n).get('title', '')}".strip()


def title_of(d: Path, n: int) -> str:
    return f"【{series_name(d, n)}】{short_of(d, n)}"


def notice_html() -> str:
    return f"<blockquote><p><strong>この連載について</strong><br>{html.escape(NOTICE)}</p></blockquote>"


def footer_html() -> str:
    return "<p>" + "<br>".join(html.escape(x) for x in FOOTER.splitlines()) + "</p>"


def audio_meta(path: Path, title: str, heading: str) -> dict:
    return {"path": str(path), "cover": str(COVER) if COVER.exists() else None, "heading": heading,
            "title": title[:100], "author": AUTHOR}


YOMI_LINE = re.compile(r"^(.+?)\s*[:：]\s*(.+?)\s*(?:→|->|[|｜])\s*(.+)$")


def yomi_html(d: Path, n: int) -> str:
    """音声の読み間違い（2026-11-26 本人。毎日のニュースと同じ「【今日の読み間違え】」の形）。
    本人が聴いて ep<n>-yomi.txt に1行ずつ「表記：音声の読み → 正しい読み」と書く（# で始まる行は無視）。無ければ何も出さない。"""
    f = d / f"ep{n}-yomi.txt"
    if not f.exists():
        return ""
    rows = []
    for line in f.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        m = YOMI_LINE.match(line)
        rows.append(f"{m.group(1)}：音声では「{m.group(2)}」→ 正しくは「{m.group(3)}」" if m else line)
    if not rows:
        return ""
    return "<p>" + "<br>".join(html.escape(x) for x in ["【今日の読み間違え】", *rows]) + "</p>"


def episode(d: Path, n: int) -> dict:
    title = title_of(d, n)
    a = audio_of(d, n)
    b = [notice_html(), "<h2>音声で聞く（AI 音声・約20分）</h2>"]
    if a and (y := yomi_html(d, n)):
        b.append(y)
    b.append(f"<p>{html.escape(AUDIO_NOTE.format(a.name))}</p>" if a else "<p>この回の音声は後日追加します。</p>")
    b += [f"<p>{html.escape(TOC_NOTE)}</p>", md_to_html((d / f"ep{n}-article.md").read_text(encoding="utf-8")), footer_html()]
    return {"title": title, "body_html": "\n".join(b),
            "audio": [audio_meta(a, title, "音声で聞く")] if a else [], "cover_image": cover_of(d)}


def summary(d: Path, eps: list[int]) -> dict:
    name = series_name(d, eps[0])
    title = f"【{name}】まとめて聴く（第{eps[0]}〜{eps[-1]}回）"
    b = [notice_html(),
         "<p>この週の音声を、1ページに順に並べました。上から順に再生すると、ページを移らずに続けて聞けます"
         "（再生の速さは 0.5〜4倍で変えられます）。出典つきの記事は、各回の記事をご覧ください。</p>"]
    audios = []
    for n in eps:
        t, short = title_of(d, n), short_of(d, n)
        a = audio_of(d, n)
        b.append(f"<h2>{html.escape(short)}</h2>")
        if a:
            b.append(f"<p>{html.escape(AUDIO_NOTE.format(a.name))}</p>")
            audios.append(audio_meta(a, t, short))
        else:
            b.append("<p>この回の音声は後日追加します。</p>")
    b.append(footer_html())
    return {"title": title, "body_html": "\n".join(b), "audio": audios, "cover_image": cover_of(d)}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("week_dir")
    ap.add_argument("--eps", default="")
    ap.add_argument("--summary", action="store_true", help="「まとめて聴く」も作る")
    ap.add_argument("--summary-eps", default="", help="「まとめて聴く」に並べる回（無ければ --eps と同じ。例: 1,2,3,4,5,6,7）")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    d = Path(a.week_dir)
    every = sorted(int(m.group(1)) for p in d.glob("ep*-article.md") if (m := re.match(r"ep(\d+)-article", p.name)))
    eps = [int(x) for x in a.eps.split(",") if x] or ([] if a.summary_eps else every)
    s_eps = [int(x) for x in a.summary_eps.split(",") if x] or eps or every
    docs = [(f"ep{n}", episode(d, n)) for n in eps] + ([("summary", summary(d, s_eps))] if a.summary else [])
    out = d / "note"
    out.mkdir(exist_ok=True)
    for key, doc in docs:
        (out / f"{key}.json").write_text(json.dumps(doc, ensure_ascii=False, indent=1), encoding="utf-8")
        (out / f"{key}.html").write_text(f"<!doctype html><meta charset='utf-8'><h1>{html.escape(doc['title'])}</h1>\n"
                                         + doc["body_html"], encoding="utf-8")
        print(f"{key}: {doc['title']}（音声 {len(doc['audio'])}）", flush=True)
    if a.dry_run:
        return
    import note_publish
    for key, doc in docs:
        url, todo = note_publish.save_draft(doc["title"], doc["body_html"], audio=doc["audio"], cover=doc["cover_image"])
        print(f"{key}: 下書き {url}", *(f"\n  未完了: {t}" for t in todo), flush=True)


if __name__ == "__main__":
    main()
