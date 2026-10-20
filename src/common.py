"""共通処理: 設定読み込み、状態ファイル、Discord投稿。"""
import json
import os
import time
from pathlib import Path

import requests
import yaml

ROOT = Path(__file__).resolve().parent.parent
STATE_DIR = ROOT / "state"
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
    STATE_DIR.mkdir(exist_ok=True)
    p = STATE_DIR / f"{name}.json"
    with open(p, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=1, sort_keys=True)


def trim_seen(seen: list, keep: int = 2000) -> list:
    """既読IDリストが無限に増えないよう末尾だけ残す。"""
    return seen[-keep:]


def http_get(url: str, timeout: int = 30) -> requests.Response:
    r = requests.get(url, headers=UA, timeout=timeout)
    r.raise_for_status()
    return r


def post_discord(webhook_env: str, content: str = "", embeds: list | None = None,
                 username: str | None = None) -> None:
    """Discord Webhookに投稿。DRY_RUN=1 なら標準出力に表示するだけ。

    文字数上限(本文2000字・1メッセージ10embed)を超える場合は分割して送る。
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
        raise SystemExit(f"環境変数 {webhook_env} が未設定です（GitHubのSecretsに登録してください）")

    # embedの説明文は4096字まで
    for e in embeds:
        if "description" in e and len(e["description"]) > 4000:
            e["description"] = e["description"][:3990] + "…"

    chunks = [embeds[i:i + 10] for i in range(0, len(embeds), 10)] or [[]]
    contents = _split_text(content, 1900) if content else [""]

    payloads = []
    for i, c in enumerate(contents):
        payloads.append({"content": c, "embeds": []})
    for i, ch in enumerate(chunks):
        if i == 0 and payloads:
            payloads[-1]["embeds"] = ch
        else:
            payloads.append({"content": "", "embeds": ch})

    for p in payloads:
        if not p["content"] and not p["embeds"]:
            continue
        if username:
            p["username"] = username
        p["allowed_mentions"] = {"parse": []}
        _send(url, p)


def _send(url: str, payload: dict) -> None:
    for attempt in range(4):
        r = requests.post(url, json=payload, timeout=30)
        if r.status_code == 429:  # レート制限
            wait = float(r.json().get("retry_after", 2))
            time.sleep(wait + 0.5)
            continue
        if r.status_code >= 400:
            print("Discord error:", r.status_code, r.text[:500])
        r.raise_for_status()
        time.sleep(0.6)
        return
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
