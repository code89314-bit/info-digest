"""GPU の持ち主の切り替え（このPCは GPU 1枚なので、同時に動かせるのは1つ）。

  llm   : Strata（Qwen、:8080）。ふだん。しらべ・ギモン深堀り・定時の配信・持参薬アプリが使う
  luna  : llama.cpp の Gemma 4 26B（無検閲・画像入力、:8090）。ルナと話すときだけ
  comfy : ComfyUI（Docker、:8188）。ルナが絵を描くときだけ

切り替えは D:\\Strata\\scripts の luna-mode.ps1 / comfy-mode.ps1 / llm-mode.ps1 が行う（時間がかかるので別スレッドで呼ぶ）。
優先順位: 定時の配信 > しらべ（ふだん）> ルナ。
  - 定時の配信（run_ai_digest.ps1）は実行中 .local/state/llm_busy.<section>.lock を置き、luna-mode.flag / comfy-mode.flag が
    あれば先に llm に戻す
  - Bot は、配信の前後（config の strata_chat.luna.schedule）と lock があるあいだは、luna / comfy に切り替えない
"""
import datetime as dt
import os
import subprocess
import tempfile
from pathlib import Path

import requests

from common import ROOT

SCRIPTS = Path(r"D:\Strata\scripts")
LUNA_FLAG = Path(r"D:\Strata\luna-mode.flag")
COMFY_FLAG = Path(r"D:\Strata\comfy-mode.flag")
LOCK_STALE_HOURS = 4


class Busy(Exception):
    """定時の配信などで、いまは GPU を貸せない。"""


def busy_locks() -> list[Path]:
    """配信ごとの lock（llm_busy.<section>.lock。重なって走ることがあるので1本ずつ別の lock）。"""
    return sorted((Path(os.environ.get("INFO_DIGEST_STATE_DIR") or ROOT / "state")).glob("llm_busy*.lock"))


def _up(url: str, headers: dict | None = None) -> bool:
    try:
        return requests.get(url, headers=headers or {}, timeout=5).status_code < 500
    except requests.RequestException:
        return False


def current() -> str:
    """いま動いているもの: "luna" / "comfy" / "llm" / "none"。"""
    if _up("http://127.0.0.1:8090/health"):
        return "luna"
    if _up("http://127.0.0.1:8188/system_stats"):
        return "comfy"
    if _up("http://127.0.0.1:8080/health"):
        return "llm"
    return "none"


def comfy_activity() -> tuple[bool, str]:
    """ComfyUI の稼働状況: (いま生成中・順番待ちがあるか, 最後に終わったジョブの id)。id が変われば、
    見ていないあいだ（1分ごとに見る）に短いジョブが走ったということ。ComfyUI に繋がらなければ (False, "")。"""
    try:
        q = requests.get("http://127.0.0.1:8188/queue", timeout=5).json()
        h = requests.get("http://127.0.0.1:8188/history", params={"max_items": 1}, timeout=5).json()
    except (requests.RequestException, ValueError):
        return False, ""
    return bool(q.get("queue_running") or q.get("queue_pending")), next(iter(h), "")


def blocked_reason(cfg: dict, now: dt.datetime | None = None) -> str:
    """luna / comfy に切り替えてはいけない理由（無ければ ""）。"""
    now = now or dt.datetime.now()
    for lock in busy_locks():
        try:
            age = now - dt.datetime.fromtimestamp(lock.stat().st_mtime)
            if age < dt.timedelta(hours=LOCK_STALE_HOURS):
                return f"定時の配信（{lock.read_text(encoding='utf-8').split()[0]}）を実行中"
        except OSError:   # 配信が終わって消えたところ
            continue
    lcfg = cfg.get("luna") or {}
    before = int(lcfg.get("guard_before_minutes", 15))
    # 始まってからは lock（配信の実行中）で判断する。始まりの数分だけは、lock が置かれる前の隙間として止める
    # （以前は schedule の minutes のあいだずっと止めていたので、配信が早く終わってもルナが Gemma に戻れなかった。2026-10-28）
    after = int(lcfg.get("guard_after_minutes", 5))
    for s in lcfg.get("schedule") or []:
        days = s.get("weekdays")   # 0=月 … 6=日。無ければ毎日
        if days is not None and now.weekday() not in days:
            continue
        h, m = (int(x) for x in str(s["at"]).split(":"))
        start = now.replace(hour=h, minute=m, second=0, microsecond=0)
        if start - dt.timedelta(minutes=before) <= now <= start + dt.timedelta(minutes=after):
            return f"{s['at']} の定時の配信（{s.get('name', '')}）の時間帯"
    return ""


def _run(script: str, timeout: int) -> None:
    # 出力はパイプでなく一時ファイルに。スクリプトが起動した llama-server / Docker Desktop がパイプを受け継いで
    # 開いたままにするので、パイプだとスクリプトが終わっても EOF が来ず timeout まで待ち続ける（2026-10-28）
    with tempfile.TemporaryFile() as out:
        r = subprocess.run(["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(SCRIPTS / script)],
                           stdin=subprocess.DEVNULL, stdout=out, stderr=subprocess.STDOUT, timeout=timeout)
        if r.returncode != 0:
            out.seek(0)
            raise RuntimeError(f"{script} が失敗しました: {out.read().decode('utf-8', 'replace')[-400:]}")


def to_luna(cfg: dict) -> None:
    if current() == "luna":
        return
    why = blocked_reason(cfg)
    if why:
        raise Busy(why)
    _run("luna-mode.ps1", 900)
    if current() != "luna":
        raise RuntimeError("ルナのモデル（llama-server :8090）が起動しませんでした（D:\\llama.cpp\\luna-server.log）")


def to_comfy(cfg: dict) -> None:
    """ルナの絵のため。luna-mode.flag を立てたまま ComfyUI に（配信が来たら llm に戻されてよい印）。"""
    if current() == "comfy":
        return
    why = blocked_reason(cfg)
    if why:
        raise Busy(why)
    LUNA_FLAG.write_text(dt.datetime.now().isoformat(), encoding="utf-8")
    _run("comfy-mode.ps1", 900)
    for _ in range(60):   # comfy-mode.ps1 はコンテナを起こすだけなので、ComfyUI が答えるまで待つ
        if _up("http://127.0.0.1:8188/system_stats"):
            return
        import time
        time.sleep(5)
    raise RuntimeError("ComfyUI（:8188）が起動しませんでした")


def to_llm() -> None:
    if current() == "llm" and not LUNA_FLAG.exists() and not COMFY_FLAG.exists():
        return
    _run("llm-mode.ps1", 900)


def held_by_bot() -> bool:
    """いま GPU を Bot（ルナ）が借りているか。手で comfy-mode にしたときは False（触らない）。"""
    return LUNA_FLAG.exists()
