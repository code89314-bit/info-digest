"""週の連載: 自動の回（scripts/run_series.ps1）で台本ができたら、続けて NotebookLM の音声を作る（2026-11-23 本人が決定）。

`.local/series/last_run.json` が今回の起動のあとに書かれていて、status=ok・action=ep のときだけ、
`.claude/skills/series-script/scripts/nlm_episode.py <週のフォルダ> <n> --length long` を動かす（20〜30分）。
結果を last_run.json に足す（series_notify.py が Discord の知らせに載せる）:
  "audio": "ep4-nlm.mp3", "audio_minutes": 22.9   /  失敗したら "audio_error": "理由"
設定 config.yaml の series.audio（false で作らない）。音声の枠（1日5本、学習・ニュース・ギモン深堀りと共通）を1本使う。
できた音声は 10MB 以下に縮めて Discord（series.webhook_env）にも貼る（2026-11-25 本人。note の下書きの音声は公開するまで聴けないため）。
series.audio_discord: false で貼らない。貼れなかったら "audio_post_error" を足す。

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
import podcast_nlm  # noqa: E402
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
    # 「分」は文字化けしても拾えるようにする（-I では PYTHONIOENCODING が効かず、cp932 で出ることがあった 2026-11-24）
    m = re.search(r"saved:\s*(.+?\.mp3)\s*\(([\d.]+)", out)
    return (Path(m.group(1).strip()).name, float(m.group(2))) if m else None


def post_audio(cfg: dict, mp3: Path, n: int, title: str = "") -> None:
    """音声を 9.5MB 以下に縮めて（<名前>-discord.mp3）、series.webhook_env の Discord に貼る。"""
    import shutil
    ff = shutil.which("ffmpeg") or "ffmpeg"
    small = mp3.with_name(mp3.stem + "-discord.mp3")
    secs = podcast_nlm.compress(mp3, small, 9.5, ff)
    msg = f"🎧 週の連載 第{n}回{(' ' + title) if title else ''}（約{secs / 60:.0f}分・NotebookLM の音声）"
    common.post_discord_file(cfg.get("webhook_env") or "DISCORD_WEBHOOK_NOTE", small, msg)


def script_title(d: Path, n: int) -> str:
    m = re.search(r"^title:\s*(.+)$", (d / f"ep{n}-script.md").read_text(encoding="utf-8"), re.M)
    return m.group(1).strip() if m else ""


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
        p = subprocess.run([sys.executable, "-I", "-X", "utf8", str(SCRIPT), str(d), str(n), "--length", "long"],
                           capture_output=True, text=True, encoding="utf-8", errors="replace",
                           timeout=TIMEOUT, cwd=str(common.ROOT), env={**os.environ, "PYTHONIOENCODING": "utf-8"})
        out = (p.stdout or "") + (p.stderr or "")
        print(out[-3000:])
        got = parse(out)
        if got:
            run["audio"], run["audio_minutes"] = got
            if cfg.get("audio_discord", True):
                try:
                    post_audio(cfg, d / "audio" / got[0], n, script_title(d, n))
                    run["audio_posted"] = True
                except Exception as ex:   # noqa: BLE001  貼れなくても音声はできている
                    run["audio_post_error"] = series_notify.mask(str(ex))[:300]
        else:
            tail = [ln for ln in out.strip().splitlines() if ln.strip()][-1:] or [f"exit {p.returncode}"]
            run["audio_error"] = tail[0][:300]
    except subprocess.TimeoutExpired:
        run["audio_error"] = f"{TIMEOUT // 60}分で終わらなかった"
    series_notify.LAST.write_text(json.dumps(run, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
