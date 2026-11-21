"""週の連載: 夜中の自動の回（scripts/run_series.ps1）が終わったら、結果を Discord に知らせる。

Claude Code のスキル `/series-script auto` が、最後に `.local/series/last_run.json` を書く:
  {"date": "2026-11-23", "status": "ok" | "error" | "skip", "action": "ep" | "prep" | "none",
   "week": "...", "episode": 3, "title": "...", "chars": 7000, "minutes": 23.3,
   "notes": ["要確認: ..."], "message": "..."}
ファイルが無い・今回の起動より古いときは「途中で止まった」として、ログの最後を添えて知らせる（Webhook の URL は伏せる）。
投稿先は config.yaml の series.webhook_env（既定 DISCORD_WEBHOOK_NOTE）。

usage: python src/series_notify.py --started <ISO 日時> --log <ログのパス>
"""
import argparse
import datetime as dt
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import common  # noqa: E402

LAST = common.ROOT / ".local" / "series" / "last_run.json"
COLOR = {"ok": 0x26A69A, "skip": 0x90A4AE, "error": 0xE53935}


def mask(text: str) -> str:
    """ログに出ていることのある秘密（Webhook の URL・キー）を伏せる。"""
    text = re.sub(r"https://(discord(app)?\.com)/api/webhooks/\S+", r"https://\1/api/webhooks/***", text)
    return re.sub(r"(AIza[0-9A-Za-z_-]{10})[0-9A-Za-z_-]+", r"\1***", text)


def read_last(started: dt.datetime, path: Path = LAST) -> dict | None:
    """今回の起動のあとに書かれた last_run.json だけを返す。"""
    if not path.exists() or dt.datetime.fromtimestamp(path.stat().st_mtime) < started:
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except ValueError:
        return None


def embed(run: dict | None, log_tail: str = "") -> dict:
    if run is None:
        return {"title": "📻 週の連載: 夜中の回が途中で止まりました", "color": COLOR["error"],
                "description": "結果のファイル（last_run.json）が書かれませんでした。ログの最後:\n```\n"
                               + mask(log_tail)[-1500:] + "\n```"}
    st = run.get("status", "error")
    if run.get("action") == "ep" and st == "ok":
        title = f"📻 週の連載: 第{run.get('episode')}回ができました"
    elif run.get("action") == "prep" and st == "ok":
        title = "📻 週の連載: 次の週の準備ができました"
    elif st == "skip":
        title = "📻 週の連載: 今日は作るものがありません"
    else:
        title = "📻 週の連載: うまくいきませんでした"
    rows = []
    if run.get("week"):
        rows.append(f"週: {run['week']}")
    if run.get("title"):
        rows.append(f"題: **{run['title']}**")
    if run.get("chars"):
        rows.append(f"台本: 約{int(run['chars']):,}字（約{float(run.get('minutes') or run['chars'] / 300):.0f}分）")
    if run.get("message"):
        rows.append(str(run["message"]))
    notes = [str(n) for n in run.get("notes") or []]
    if notes:
        rows.append("気になる点:\n" + "\n".join(f"• {n}" for n in notes[:10]))
    rows.append("-# 出力は .local\\series\\ の週のフォルダ。公開は本人が確かめてから")
    return {"title": title, "color": COLOR.get(st, COLOR["error"]), "description": mask("\n".join(rows))[:3900]}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--started", required=True)
    ap.add_argument("--log", default="")
    a = ap.parse_args()
    common.load_dotenv()
    cfg = (common.load_config().get("series") or {})
    started = dt.datetime.fromisoformat(a.started)
    tail = ""
    if a.log and Path(a.log).exists():
        tail = "\n".join(Path(a.log).read_text(encoding="utf-8", errors="replace").splitlines()[-25:])
    common.post_discord(cfg.get("webhook_env") or "DISCORD_WEBHOOK_NOTE", embeds=[embed(read_last(started), tail)])


if __name__ == "__main__":
    main()
