"""音声の読み間違いを、Discord の返信から記事と読みの辞書に足す（2026-11-26 本人）。

本人が Bot のいるサーバーで、記事・音声の投稿に「表記:音声の読み|正しい読み」（例: HER2:ヘルツー|ハーツー）と返信する
→ qa_bot が受けて apply() を呼ぶ:
  1. 読みの辞書 style/nlm_yomi.yaml に「表記: 正しい読み」を足す（もう有れば触らない）。
  2. 返信先の投稿から記事を決めて「【今日の読み間違え】」の欄に1行足す:
     - note の URL がある投稿（毎日のニュースの「note に公開しました」など）→ その記事を note の編集画面で直して「更新する」
     - 週の連載の音声の投稿（「🎧 週の連載 第n回 …」）→ 週のフォルダの ep<n>-yomi.txt に足す（series_note.py が記事に入れる）
     - どちらでもない（朝のニュースの音声の投稿など）→ 投稿した日の「がん薬物療法ニュース」の note 記事
"""
import datetime as dt
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from common import ROOT  # noqa: E402

YOMI_FILE = ROOT / "style" / "nlm_yomi.yaml"
SERIES = ROOT / ".local" / "series"
CREATOR = "code89314"
REPLY = re.compile(r"^\s*(.+?)\s*[:：]\s*(.+?)\s*[|｜]\s*(.+?)\s*$")
NOTE_KEY = re.compile(r"note\.com/(?:[\w-]+/n|notes)/(n[0-9a-f]{12})")
SERIES_POST = re.compile(r"週の連載 第(\d+)回\s*(.*?)（約")
JST = dt.timezone(dt.timedelta(hours=9))


def parse(text: str) -> tuple[str, str, str] | None:
    """「表記:音声の読み|正しい読み」を（表記, 音声の読み, 正しい読み）に。1行だけ・形が違えば None。"""
    if "\n" in text.strip():
        return None
    m = REPLY.match(text)
    return (m.group(1), m.group(2), m.group(3)) if m else None


def errata_line(word: str, wrong: str, right: str) -> str:
    return f"{word}：音声では「{wrong}」→ 正しくは「{right}」"


def add_dict(word: str, wrong: str, right: str, path: Path = YOMI_FILE, today: dt.date | None = None) -> str:
    """読みの辞書に足す。戻り値は報告の一言。"""
    import yaml
    text = path.read_text(encoding="utf-8") if path.exists() else ""
    have = yaml.safe_load(text) or {}
    if word in have:
        return f"辞書にはもう「{word}: {have[word]}」があります（音声が守らなかった分）"
    key = json.dumps(word, ensure_ascii=False) if re.search(r"[:#\[\]{},&*!|>'\"%@`]", word) else word
    day = (today or dt.date.today()).isoformat()
    add = f"# 「{wrong}」と読んだ（{day} 本人・Discord から）\n{key}: {right}\n"
    path.write_text(text + ("" if not text or text.endswith("\n") else "\n") + add, encoding="utf-8")
    return f"辞書に「{word}: {right}」を足しました（次の音声から）"


def series_target(n: int, title: str) -> Path | None:
    """週の連載の第n回の週のフォルダ。題が合う週、無ければ第n回の台本がある新しい週。"""
    cands = sorted((p.parent for p in SERIES.glob(f"*/ep{n}-script.md")), key=lambda p: p.name, reverse=True)
    for d in cands:
        m = re.search(r"^title:\s*(.+)$", (d / f"ep{n}-script.md").read_text(encoding="utf-8"), re.M)
        if title and m and m.group(1).strip() == title.strip():
            return d
    return cands[0] if cands else None


def add_series(d: Path, n: int, word: str, wrong: str, right: str) -> str:
    f = d / f"ep{n}-yomi.txt"
    old = f.read_text(encoding="utf-8") if f.exists() else "# 第%d回の音声の読み間違い（表記：音声の読み → 正しい読み）\n" % n
    if re.search(rf"^{re.escape(word)}\s*[:：]", old, re.M):
        return f"{d.name} 第{n}回: 「{word}」はもう書いてあります"
    f.write_text(old + ("" if old.endswith("\n") else "\n") + f"{word}：{wrong} → {right}\n", encoding="utf-8")
    return f"{d.name} 第{n}回の ep{n}-yomi.txt に足しました（note の記事を作るときに入ります）"


def daily_key(day: dt.date) -> str | None:
    """その日の「がん薬物療法ニュース」の note 記事のキー（公開一覧から題で探す）。"""
    import requests
    head = f"{day.month}/{day.day}（"
    for page in (1, 2):
        r = requests.get(f"https://note.com/api/v2/creators/{CREATOR}/contents",
                         params={"kind": "note", "page": page}, timeout=30)
        for c in (r.json().get("data") or {}).get("contents") or []:
            if c.get("name", "").startswith(head) and "がん薬物療法ニュース" in c.get("name", ""):
                return c["key"]
    return None


def find_target(text: str, posted: dt.datetime) -> tuple:
    """返信先の投稿の文字から、("note", キー) / ("series", 週のフォルダ, n) / ("none", 理由)。"""
    if m := NOTE_KEY.search(text):
        return ("note", m.group(1))
    if m := SERIES_POST.search(text):
        n = int(m.group(1))
        d = series_target(n, m.group(2))
        return ("series", d, n) if d else ("none", f"週の連載 第{n}回の台本が見つかりません")
    day = posted.astimezone(JST).date()
    key = daily_key(day)
    return ("note", key) if key else ("none", f"{day.month}/{day.day} のがん薬物療法ニュースの note 記事が見つかりません")


def apply(ref_text: str, posted: dt.datetime, word: str, wrong: str, right: str) -> str:
    """Bot から呼ぶ（別スレッドで）。報告の文を返す。"""
    out = [add_dict(word, wrong, right)]
    t = find_target(ref_text, posted)
    if t[0] == "series":
        out.append(add_series(t[1], t[2], word, wrong, right))
    elif t[0] == "note":
        import note_publish
        try:
            state = note_publish.add_yomi_errata(f"https://editor.note.com/notes/{t[1]}/edit/",
                                                 errata_line(word, wrong, right), word)
            out.append({"updated": "note の記事に足して「更新する」を押しました",
                        "saved": "note の記事（下書き）に足して保存しました",
                        "already": "note の記事には、もう書いてあります"}.get(state, state) + f"（{t[1]}）")
        except Exception as ex:   # noqa: BLE001
            out.append(f"note の記事を直せませんでした（{str(ex)[:200]}）")
    else:
        out.append(t[1] + "（記事・音声の投稿に返信してください）")
    return "\n".join(out)


if __name__ == "__main__":   # 試し: python src/yomi_errata.py "HER2:ヘルツー|ハーツー" <返信先の文>  （辞書と記事を本当に直す）
    p = parse(sys.argv[1])
    if not p:
        raise SystemExit("形: 表記:音声の読み|正しい読み")
    print(apply(sys.argv[2] if len(sys.argv) > 2 else "", dt.datetime.now(JST), *p))
