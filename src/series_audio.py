"""週の連載: 自動の回（scripts/run_series.ps1）で台本ができたら、続けて NotebookLM の音声を作る（2026-11-23 本人が決定）。

`.local/series/last_run.json` が今回の起動のあとに書かれていて、status=ok・action=ep のときだけ、
`.claude/skills/series-script/scripts/nlm_episode.py <週のフォルダ> <n> --length long` を動かす（20〜30分）。
結果を last_run.json に足す（series_notify.py が Discord の知らせに載せる）:
  "audio": "ep4-nlm.mp3", "audio_minutes": 22.9   /  失敗したら "audio_error": "理由"
設定 config.yaml の series.audio（false で作らない）。音声の枠（1日5本、学習・ニュース・ギモン深堀りと共通）を1本使う。

usage: python src/series_audio.py --started <ISO 日時>
"""
import argparse
import os
import datetime as dt
import json
import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import common  # noqa: E402
import series_notify  # noqa: E402

SCRIPT = common.ROOT / ".claude" / "skills" / "series-script" / "scripts" / "nlm_episode.py"
TIMEOUT = 2 * 3600   # 混んでいて30分待ってやり直すことがあるので長めに


def target(run: dict | None) -> tuple[Path, int] | None:
    """音声を作る回（週のフォルダ, 回）。作らないときは None。"""
    if not run or run.get("status") != "ok" or run.get("action") != "ep" or not run.get("week") or not run.get("episode"):
        return None
    d = common.ROOT / ".local" / "series" / str(run["week"])
    n = int(run["episode"])
    return (d, n) if (d / f"ep{n}-script.md").exists() else None


def parse(out: str) -> tuple[str, float] | None:
    """nlm_episode.py の出力の「saved: <パス> (22.9 分)」から、ファイル名と分。"""
    m = re.search(r"saved:\s*(.+?)\s*\(([\d.]+)\s*分\)", out)
    return (Path(m.group(1).strip()).name, float(m.group(2))) if m else None


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--started", required=True)
    a = ap.parse_args()
    cfg = common.load_config().get("series") or {}
    if not cfg.get("audio", True):
        print("series.audio: false → 音声は作らない")
        return
    run = series_notify.read_last(dt.datetime.fromisoformat(a.started))
    t = target(run)
    if t is None:
        print("音声を作る回がない（台本ができていない・準備の日・作るものがない）")
        return
    d, n = t
    print(f"音声を作る: {d.name} 第{n}回", flush=True)
    try:
        p = subprocess.run([sys.executable, "-I", str(SCRIPT), str(d), str(n), "--length", "long"],
                           capture_output=True, text=True, encoding="utf-8", errors="replace",
                           timeout=TIMEOUT, cwd=str(common.ROOT), env={**os.environ, "PYTHONIOENCODING": "utf-8"})
        out = (p.stdout or "") + (p.stderr or "")
        print(out[-3000:])
        got = parse(out)
        if got:
            run["audio"], run["audio_minutes"] = got
        else:
            tail = [ln for ln in out.strip().splitlines() if ln.strip()][-1:] or [f"exit {p.returncode}"]
            run["audio_error"] = tail[0][:300]
    except subprocess.TimeoutExpired:
        run["audio_error"] = f"{TIMEOUT // 60}分で終わらなかった"
    series_notify.LAST.write_text(json.dumps(run, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
