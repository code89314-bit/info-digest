"""共通処理: 設定読み込み、状態ファイル、Discord投稿。"""
import json
import mimetypes
import os
import time
from pathlib import Path

import requests
import yaml

ROOT = Path(__file__).resolve().parent.parent


def load_dotenv(path: Path = ROOT / ".env") -> None:
    """自宅サーバーで動かすとき用: リポジトリ直下の .env（KEY=VALUE の行）を環境変数に読み込む。

    既に設定されている環境変数は上書きしない。.env は .gitignore 済み（Webhook URL 等をコミットしないため）。
    """
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


load_dotenv()
# 既読管理の保存先。INFO_DIGEST_STATE_DIR で変えられる（自宅サーバーではリポジトリ外の state を使う）
STATE_DIR = Path(os.environ.get("INFO_DIGEST_STATE_DIR") or ROOT / "state")
UA = {"User-Agent": "info-digest/1.0 (+https://github.com/)"}


def load_config() -> dict:
    with open(ROOT / "config.yaml", encoding="utf-8") as f:
        return yaml.safe_load(f)


def load_state(name: str) -> dict:
    p = STATE_DIR / f"{name}.json"
    if p.exists():
        with open(p, encoding="utf-8") as f:
            return json.load(f)
    return {}


def save_state(name: str, state: dict) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    p = STATE_DIR / f"{name}.json"
    with open(p, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=1, sort_keys=True)


def trim_seen(seen: list, keep: int = 2000) -> list:
    """既読IDリストが無限に増えないよう末尾だけ残す。"""
    return seen[-keep:]


def http_get(url: str, timeout: int = 30) -> requests.Response:
    """GET。429（Redditなどのレート制限）は Retry-After か 15/30/60 秒待って最大3回やり直す。"""
    for attempt in range(4):
        r = requests.get(url, headers=UA, timeout=timeout)
        if r.status_code != 429 or attempt == 3:
            break
        try:
            wait = float(r.headers.get("Retry-After") or 0)
        except ValueError:
            wait = 0
        time.sleep(min(max(wait, 15 * 2 ** attempt), 120))
    r.raise_for_status()
    return r


def post_discord(webhook_env: str, content: str = "", embeds: list | None = None,
                 username: str | None = None, avatar_url: str | None = None, ids: list | None = None) -> None:
    """Discord Webhookに投稿。DRY_RUN=1 なら標準出力に表示するだけ。

    文字数上限(本文2000字・1メッセージ10embed)を超える場合は分割して送る。
    ids にリストを渡すと、投稿したメッセージのIDを足す（あとで delete_discord で消すため）。
    """
    embeds = embeds or []
    if os.environ.get("DRY_RUN") == "1":
        print("---- DRY_RUN ----")
        if content:
            print(content)
        for e in embeds:
            print(json.dumps(e, ensure_ascii=False, indent=1))
        return

    url = os.environ.get(webhook_env)
    if not url:
        raise SystemExit(f"環境変数 {webhook_env} が未設定です（GitHubのSecrets、自宅サーバーでは .env に登録してください）")

    for p in _payloads(content, embeds):
        if username:
            p["username"] = username
        if avatar_url:
            p["avatar_url"] = avatar_url
        mid = _send(url, p, wait=ids is not None)
        if ids is not None and mid:
            ids.append(mid)


def _payloads(content: str, embeds: list) -> list[dict]:
    """本文と embed を、Discord の1メッセージの上限に収まるように分ける。"""
    # embedの説明文は4096字まで
    for e in embeds:
        if "description" in e and len(e["description"]) > 4000:
            e["description"] = e["description"][:3990] + "…"

    # 1メッセージに embed は10個まで、かつ全embedの文字数合計は6000字まで（超えると 400/413）。
    # 5800字では 413 になったことがある（2026-10-30、朝のニュース。本文も含めて数える・余裕を取る）
    chunks, cur, size = [], [], len(content or "")
    for e in embeds:
        n = len(e.get("title", "")) + len(e.get("description", ""))
        if cur and (len(cur) == 10 or size + n > 5000):
            chunks.append(cur)
            cur, size = [], 0
        cur.append(e)
        size += n
    if cur:
        chunks.append(cur)
    chunks = chunks or [[]]
    contents = _split_text(content, 1900) if content else [""]

    payloads = []
    for i, c in enumerate(contents):
        payloads.append({"content": c, "embeds": []})
    for i, ch in enumerate(chunks):
        if i == 0 and payloads:
            payloads[-1]["embeds"] = ch
        else:
            payloads.append({"content": "", "embeds": ch})

    out = []
    for p in payloads:
        if p["content"] or p["embeds"]:
            p["allowed_mentions"] = {"parse": []}
            out.append(p)
    return out


API = "https://discord.com/api/v10"


def _bot_headers() -> dict:
    token = os.environ.get("DISCORD_BOT_TOKEN")
    if not token:
        raise SystemExit("環境変数 DISCORD_BOT_TOKEN が未設定です（.env に登録してください）")
    return {"Authorization": f"Bot {token}"}


def post_channel(channel_env: str, content: str = "", embeds: list | None = None, ids: list | None = None) -> None:
    """Bot（DISCORD_BOT_TOKEN）でチャンネル（ID は環境変数 channel_env）に投稿する。DRY_RUN=1 なら表示するだけ。
    Webhook を作らなくても、Bot が書き込めるチャンネルなら投稿できる（名前・アイコンは Bot のもの）。"""
    embeds = embeds or []
    if os.environ.get("DRY_RUN") == "1":
        return post_discord("", content=content, embeds=embeds)
    ch = os.environ.get(channel_env)
    if not ch:
        raise SystemExit(f"環境変数 {channel_env} が未設定です（.env にチャンネルIDを登録してください）")
    for p in _payloads(content, embeds):
        for attempt in range(4):
            r = requests.post(f"{API}/channels/{ch}/messages", headers=_bot_headers(), json=p, timeout=30)
            if r.status_code == 429:
                time.sleep(float(r.json().get("retry_after", 2)) + 0.5)
                continue
            if r.status_code >= 400:
                print("Discord error:", r.status_code, r.text[:300])
            r.raise_for_status()
            time.sleep(0.6)
            if ids is not None:
                ids.append(str(r.json().get("id")))
            break


def delete_channel_message(channel_env: str, message_id: str) -> bool:
    """post_channel で投稿したメッセージを消す。消せた（もう無い場合も含む）なら True。"""
    ch = os.environ.get(channel_env)
    if not ch:
        return False
    for attempt in range(4):
        r = requests.delete(f"{API}/channels/{ch}/messages/{message_id}", headers=_bot_headers(), timeout=30)
        if r.status_code == 429:
            time.sleep(float(r.json().get("retry_after", 2)) + 0.5)
            continue
        time.sleep(0.6)
        return r.status_code in (204, 404)
    return False


def _webhook_url(url: str, path: str = "", query: str = "") -> str:
    """Webhook の URL に /messages/… や ?wait=true を足す（thread_id などの元の ? も残す）。"""
    base, _, q = url.partition("?")
    q = "&".join(x for x in (q, query) if x)
    return base.rstrip("/") + path + (f"?{q}" if q else "")


def delete_discord(webhook_env: str, message_id: str) -> bool:
    """Webhook で投稿したメッセージを消す。消せた（もう無い場合も含む）なら True。"""
    url = os.environ.get(webhook_env)
    if not url:
        return False
    for attempt in range(4):
        r = requests.delete(_webhook_url(url, f"/messages/{message_id}"), timeout=30)
        if r.status_code == 429:
            time.sleep(float(r.json().get("retry_after", 2)) + 0.5)
            continue
        time.sleep(0.6)
        return r.status_code in (204, 404)
    return False


def post_discord_file(webhook_env: str, path: Path, content: str = "", username: str | None = None,
                      avatar_url: str | None = None) -> None:
    """ファイル（podcast の mp3、note 下書きの md・html など）を1つ添付して投稿。DRY_RUN=1 なら表示するだけ。

    Webhook の添付上限はサーバーのブースト無しで 10MB。超えると 413 になる。
    """
    path = Path(path)
    if os.environ.get("DRY_RUN") == "1":
        print("---- DRY_RUN (file) ----")
        print(content, f"[{path.name} {path.stat().st_size / 1e6:.1f}MB]")
        return
    url = os.environ.get(webhook_env)
    if not url:
        raise SystemExit(f"環境変数 {webhook_env} が未設定です（自宅サーバーでは .env に登録してください）")
    payload = {"content": content, "allowed_mentions": {"parse": []}}
    if username:
        payload["username"] = username
    if avatar_url:
        payload["avatar_url"] = avatar_url
    for attempt in range(4):
        with open(path, "rb") as fh:
            r = requests.post(url, data={"payload_json": json.dumps(payload, ensure_ascii=False)},
                              files={"files[0]": (path.name, fh, mimetypes.guess_type(path.name)[0] or "application/octet-stream")}, timeout=300)
        if r.status_code == 429:
            time.sleep(float(r.json().get("retry_after", 2)) + 0.5)
            continue
        if r.status_code >= 400:
            print("Discord error:", r.status_code, r.text[:500])
        r.raise_for_status()
        return
    raise RuntimeError("Discordへの添付がレート制限で失敗しました")


def _send(url: str, payload: dict, wait: bool = False) -> str | None:
    """投稿する。wait=True なら Discord の返事から投稿のIDを返す。"""
    for attempt in range(4):
        r = requests.post(_webhook_url(url, query="wait=true") if wait else url, json=payload, timeout=30)
        if r.status_code == 429:  # レート制限
            wait = float(r.json().get("retry_after", 2))
            time.sleep(wait + 0.5)
            continue
        if r.status_code >= 400:
            print("Discord error:", r.status_code, r.text[:500])
        r.raise_for_status()
        time.sleep(0.6)
        if wait:
            try:
                return str(r.json().get("id") or "") or None
            except ValueError:
                return None
        return None
    raise RuntimeError("Discordへの投稿がレート制限で失敗しました")


def _split_text(text: str, limit: int) -> list[str]:
    out, buf = [], ""
    for line in text.splitlines(keepends=True):
        if len(buf) + len(line) > limit:
            if buf:
                out.append(buf)
            while len(line) > limit:
                out.append(line[:limit])
                line = line[limit:]
            buf = line
        else:
            buf += line
    if buf:
        out.append(buf)
    return out
