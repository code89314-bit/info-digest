"""週の連載: note の記事を公開して、週のマガジンに追加する（2026-11-25 本人が決定）。

本人が下書きを確かめて「公開して」と言ったときだけ使う（自動の回では動かさない。公開は1週遅れで本人が確かめてから）。
公開済みの記事を渡すと「更新する」になり、マガジンへの追加だけをする（追加済みならそのまま）。
マガジンは、公開設定の画面の「記事の追加 → マガジン」の中から、名前に週の題（state.json の title）を含むものを選ぶ
（本人がマガジンの名前の頭を「肺がん：」などに変えても見つかる）。マガジン自体の公開・非公開は変えない。
画面はブラウザを表示して動かす（headless では公開設定の画面に移らない）。

usage: python src/series_publish.py <週ID> <記事のキーまたは編集画面のURL>... [--tags a,b] [--dry-run]
例:    python src/series_publish.py 20261120-egfr-nsclc n02f6955ae9d5 --dry-run
"""
import argparse
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import common  # noqa: E402
import note_publish  # noqa: E402

STATE = common.ROOT / ".local" / "series" / "state.json"


def edit_url(key_or_url: str) -> str:
    """記事のキー（n で始まる）・記事のURL・編集画面のURL → 編集画面のURL。"""
    m = re.search(r"\b(n[0-9a-f]{12})\b", key_or_url)
    if not m:
        raise ValueError(f"記事のキーが読めません: {key_or_url}")
    return f"https://editor.note.com/notes/{m.group(1)}/edit/"


def week_title(week: str, path: Path = STATE) -> str:
    w = (json.loads(path.read_text(encoding="utf-8")).get("weeks") or {}).get(week)
    if not w or not w.get("title"):
        raise SystemExit(f"state.json に週 {week} の title がありません")
    return str(w["title"])


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("week")
    ap.add_argument("notes", nargs="+")
    ap.add_argument("--tags", default="", help="足すハッシュタグ（カンマ区切り。公開済みの記事には既にあるので普通は不要）")
    ap.add_argument("--magazine", default="", help="マガジンの名前の一部（既定: 週の題）")
    ap.add_argument("--dry-run", action="store_true", help="押さずに、マガジンの状態とボタン（投稿する/更新する）だけ見る")
    a = ap.parse_args()
    mag = a.magazine or week_title(a.week)
    tags = [t.strip() for t in a.tags.split(",") if t.strip()]
    log = common.ROOT / ".local" / "series" / a.week / "note" / "published.json"
    done = json.loads(log.read_text(encoding="utf-8")) if log.exists() else {}
    for n in a.notes:
        url = edit_url(n)
        try:
            got, state = note_publish.publish_with_magazine(url, mag, tags, dry_run=a.dry_run)
        except Exception as ex:   # noqa: BLE001  1本失敗しても次へ
            print(f"❌ {n}: {ex}", flush=True)
            continue
        print(f"✅ {n}: {state} {got}", flush=True)
        if not a.dry_run:
            done[n] = {"url": got, "magazine": mag, "state": state}
            log.parent.mkdir(parents=True, exist_ok=True)
            log.write_text(json.dumps(done, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
