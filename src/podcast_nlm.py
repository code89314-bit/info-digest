"""ダイジェストの内容から NotebookLM で podcast（音声概要）を作り、Discord に添付する。

使うもの（自宅サーバーのみ。GitHub Actions では動かさない）:
  - notebooklm CLI（notebooklm-py）。ログイン済みであること（`notebooklm login`）
  - ffmpeg（Discord の添付上限 10MB に収まるよう圧縮する）
設定は config.yaml の <区画>.podcast。音声生成の1日上限は、E:\\onco_tekisei の適正使用ガイド用と
同じ台帳（audio_usage.json）で合算して守る（Google 側のアカウント単位の制限を超えないため）。

流れ: 認証確認 → ノート作成 → 資料（動向まとめ＋記事一覧のテキスト、記事URL）追加 → 音声生成 →
完成待ち → mp3 ダウンロード → ノート削除 → 圧縮 → Discord に添付。
失敗したら理由を1行だけ Discord に投稿する（ダイジェスト本体の投稿には影響しない）。
"""
import datetime as dt
import json
import math
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

from common import ROOT, post_discord, post_discord_file

SOURCE_WAIT = 600        # 資料1件の取込待ち（秒）
ARTIFACT_WAIT = 2400     # 音声の完成待ち（秒）。15〜20分かかることがある


class PodcastError(Exception):
    pass


def _env(pod: dict) -> dict:
    env = dict(os.environ, PYTHONUTF8="1", PYTHONIOENCODING="utf-8")
    extra = os.path.expandvars(pod.get("cli_path_add") or "")
    if extra:
        env["PATH"] = env.get("PATH", "") + os.pathsep + extra
    return env


def _cli(pod: dict, args: list[str], *, parse_json: bool = True, check: bool = True, timeout: int = 900):
    cmd = [pod.get("cli") or "notebooklm", *args]
    p = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace",
                       stdin=subprocess.DEVNULL, env=_env(pod), timeout=timeout)
    out, err = (p.stdout or "").strip(), (p.stderr or "").strip()
    if p.returncode != 0:
        if check:
            raise PodcastError(f"notebooklm {args[0]} 失敗（{p.returncode}）: {(err or out)[:300]}")
        return None
    if not parse_json:
        return out
    try:
        data = json.loads(out) if out else None
    except json.JSONDecodeError:
        return out
    if isinstance(data, dict) and data.get("error"):
        if check:
            raise PodcastError(f"notebooklm {args[0]}: {data.get('message')}")
        return None
    return data


# ---- 1日上限の台帳（onco_tekisei と共通） ----
def _ledger_path(pod: dict) -> Path | None:
    p = pod.get("ledger")
    return Path(os.path.expandvars(p)) if p else None


def _ledger_load(pod: dict) -> dict:
    p = _ledger_path(pod)
    try:
        data = json.loads(p.read_text(encoding="utf-8")) if p else {}
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def used_today(pod: dict) -> int:
    return int(_ledger_load(pod).get(dt.date.today().isoformat(), 0))


def record_use(pod: dict) -> None:
    p = _ledger_path(pod)
    if not p:
        return
    data = _ledger_load(pod)
    today = dt.date.today().isoformat()
    data[today] = int(data.get(today, 0)) + 1
    cutoff = (dt.date.today() - dt.timedelta(days=60)).isoformat()
    p.write_text(json.dumps({d: c for d, c in data.items() if d >= cutoff}, ensure_ascii=False, indent=2),
                 encoding="utf-8")


# ---- NotebookLM ----
def ensure_auth(pod: dict) -> None:
    if _cli(pod, ["list", "--json"], check=False) is not None:
        return
    refresh = pod.get("auth_refresh") or []
    if refresh:
        print("NotebookLM の認証を自動更新します…")
        subprocess.run([os.path.expandvars(a) for a in refresh], env=_env(pod), stdin=subprocess.DEVNULL,
                       capture_output=True, timeout=600)
        if _cli(pod, ["list", "--json"], check=False) is not None:
            return
    raise PodcastError("NotebookLM の認証が切れています（このPCで `notebooklm login` を1回実行してください）")


def _id(data, *keys) -> str | None:
    if not isinstance(data, dict):
        return None
    for k in keys:
        v = data.get(k)
        if isinstance(v, dict) and v.get("id"):
            return v["id"]
        if isinstance(v, str) and v:
            return v
    return None


def source_text(picked: list[dict], overview: str | None, title: str) -> str:
    lines = [f"# {title}", ""]
    if overview:
        lines += ["## 今日の動向まとめ", "", overview, ""]
    lines += ["## 新着情報の一覧", ""]
    for p in picked:
        lines += [f"### {p['title_ja']}", f"- 分類: {p['tag']}", f"- 出典: {p['source']}", f"- URL: {p['link']}",
                  f"- 要約: {p['summary_ja']}", f"- 原題: {p['title']}", f"- 原文の冒頭: {p['summary'][:400]}", ""]
    return "\n".join(lines)


def make_podcast(picked: list[dict], overview: str | None, pod: dict, out_path: Path) -> None:
    ensure_auth(pod)
    title = f"{pod.get('title') or 'ニュース'} {dt.date.today():%Y-%m-%d}"
    nb = _id(_cli(pod, ["create", title, "--json"]), "notebook", "id")
    if not nb:
        raise PodcastError("ノートを作れませんでした")
    ok = False
    try:
        src_ids = []
        with tempfile.TemporaryDirectory() as tmp:
            md = Path(tmp) / "digest.md"
            md.write_text(source_text(picked, overview, title), encoding="utf-8")
            sid = _id(_cli(pod, ["source", "add", str(md), "-n", nb, "--title", title, "--json"]),
                      "source", "source_id", "id")
            if not sid:
                raise PodcastError("まとめテキストを資料に追加できませんでした")
            src_ids.append(sid)
        for p in picked[: int(pod.get("max_url_sources", 8))]:   # 記事本文も読ませる（取れないURLは飛ばす）
            sid = _id(_cli(pod, ["source", "add", p["link"], "-n", nb, "--json"], check=False),
                      "source", "source_id", "id")
            if sid:
                src_ids.append(sid)
        for sid in src_ids:
            _cli(pod, ["source", "wait", sid, "-n", nb, "--timeout", str(SOURCE_WAIT)],
                 parse_json=False, check=sid == src_ids[0], timeout=SOURCE_WAIT + 60)

        _cli(pod, ["generate", "audio", (pod.get("instructions") or "").strip(), "-n", nb,
                   "--format", pod.get("format", "deep-dive"), "--length", pod.get("length", "default"),
                   "--language", pod.get("language", "ja"), "--retry", "2", "--json"], timeout=600)
        record_use(pod)   # Google が受け付けた時点で1件消費
        arts = (_cli(pod, ["artifact", "list", "-n", nb, "--json"]) or {}).get("artifacts", [])
        audio = [a for a in arts if "audio" in str(a.get("type", "")).lower()]
        if not audio:
            raise PodcastError("音声が生成されませんでした")
        art = audio[-1]["id"]
        _cli(pod, ["artifact", "wait", art, "-n", nb, "--timeout", str(ARTIFACT_WAIT)],
             parse_json=False, timeout=ARTIFACT_WAIT + 120)
        _cli(pod, ["download", "audio", str(out_path), "-a", art, "-n", nb], parse_json=False, timeout=900)
        if not out_path.exists():
            raise PodcastError("mp3 をダウンロードできませんでした")
        ok = True
    finally:
        if ok or not pod.get("keep_notebook_on_error", True):
            _cli(pod, ["delete", "-n", nb, "-y"], parse_json=False, check=False)
        else:
            print(f"エラーのためノートを残しました（調査後に `notebooklm delete -n {nb} -y`）")


# ---- 圧縮（Discord の添付上限に収める） ----
def duration_sec(path: Path, ffmpeg: str) -> float:
    p = subprocess.run([ffmpeg, "-i", str(path)], capture_output=True, text=True, encoding="utf-8", errors="replace")
    m = re.search(r"Duration: (\d+):(\d+):(\d+(?:\.\d+)?)", p.stderr or "")
    if not m:
        raise PodcastError("音声の長さを読めませんでした")
    h, mi, s = m.groups()
    return int(h) * 3600 + int(mi) * 60 + float(s)


def bitrate_for(seconds: float, max_mb: float) -> int:
    """max_mb に収まるビットレート（kbps）。声なので 24〜64kbps のモノラルで十分。"""
    kbps = math.floor(max_mb * 8 * 1000 / max(seconds, 1) * 0.97)   # 3% はヘッダ等の余裕
    return max(24, min(64, kbps))


def compress(src: Path, dst: Path, max_mb: float, ffmpeg: str) -> float:
    secs = duration_sec(src, ffmpeg)
    kbps = bitrate_for(secs, max_mb)
    subprocess.run([ffmpeg, "-y", "-loglevel", "error", "-i", str(src), "-ac", "1", "-ar", "24000",
                    "-b:a", f"{kbps}k", str(dst)], check=True)
    if dst.stat().st_size > max_mb * 1e6:
        raise PodcastError(f"圧縮しても {dst.stat().st_size / 1e6:.1f}MB で上限 {max_mb}MB を超えます"
                           f"（{secs / 60:.0f}分）。podcast.length を short にしてください")
    return secs


def run(picked: list[dict], overview: str | None, pod: dict, *, webhook: str, username: str) -> None:
    """podcast を作って添付する。どこで失敗してもダイジェスト本体には影響させない。"""
    if os.environ.get("DRY_RUN") == "1":
        print(f"---- DRY_RUN (podcast) ---- {len(picked)}件から NotebookLM で音声を作る予定（実行はしない）")
        return
    limit = int(pod.get("daily_limit", 3))
    if used_today(pod) >= limit:
        post_discord(webhook, username=username,
                     content=f"🎧 今日の podcast は作りませんでした（NotebookLM の音声は1日{limit}本まで。上限に達しています）")
        return
    out_dir = ROOT / (pod.get("out_dir") or ".local/podcasts")
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M")
    raw, small = out_dir / f"{pod.get('file_prefix', 'podcast')}-{stamp}.mp3", \
        out_dir / f"{pod.get('file_prefix', 'podcast')}-{stamp}-discord.mp3"
    ffmpeg = pod.get("ffmpeg") or shutil.which("ffmpeg") or "ffmpeg"
    try:
        print("podcast を作成中（NotebookLM、15〜20分かかることがあります）…")
        make_podcast(picked, overview, pod, raw)
        secs = compress(raw, small, float(pod.get("max_mb", 9.5)), ffmpeg)
        post_discord_file(webhook, small, username=username,
                          content=f"🎧 **今日のニュース podcast**（{dt.date.today():%m/%d}・約{round(secs / 60)}分・NotebookLM）")
        print(f"podcast を投稿しました: {small.name}")
        for old in sorted(out_dir.glob("*.mp3"))[:-int(pod.get("keep_files", 20))]:   # 古いものは消す
            old.unlink(missing_ok=True)
    except Exception as ex:   # noqa: BLE001  音声は「おまけ」。失敗理由だけ伝える
        print("podcast 失敗:", ex)
        post_discord(webhook, username=username, content=f"🎧 今日の podcast は作れませんでした: {str(ex)[:300]}")
