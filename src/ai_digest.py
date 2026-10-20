"""AI・LLM関連の新着を公式ブログ・論文・HNから集め、要約ダイジェストをDiscordに投稿する。

ANTHROPIC_API_KEY があれば Claude が「中身のある情報」だけを選んで日本語要約する。
無ければキーワード一致した記事の見出しだけを投稿する。
"""
import calendar
import html
import json
import os
import re
import sys
import time

import feedparser
import requests

from common import http_get, load_config, load_state, post_discord, save_state, trim_seen

TAG_ORDER = ["モデル・製品", "研究・論文", "ツール・OSS", "業界・政策", "その他"]
TAG_COLOR = {"モデル・製品": 0x7E57C2, "研究・論文": 0x26A69A, "ツール・OSS": 0x42A5F5,
             "業界・政策": 0xFFA726, "その他": 0x90A4AE}


def clean(text: str, n: int = 400) -> str:
    text = html.unescape(re.sub(r"<[^>]+>", " ", text or ""))
    text = re.sub(r"\s+", " ", text).strip()
    return text[:n]


def fetch_rss(src: dict, since: float) -> list[dict]:
    feed = feedparser.parse(http_get(src["url"]).content)
    out = []
    for e in feed.entries:
        ts = None
        for k in ("published_parsed", "updated_parsed"):
            if e.get(k):
                ts = calendar.timegm(e[k])
                break
        if ts and ts < since:
            continue
        link = e.get("link", "")
        out.append({"id": e.get("id") or link, "title": clean(e.get("title", ""), 200),
                    "link": link, "summary": clean(e.get("summary", "")), "source": src["name"]})
    return out


def fetch_hf_papers(src: dict, since: float) -> list[dict]:
    data = http_get(src.get("url", "https://huggingface.co/api/daily_papers")).json()
    min_up = src.get("min_upvotes", 20)
    out = []
    for d in data:
        p = d.get("paper", d)
        if (p.get("upvotes") or 0) < min_up:
            continue
        pid = p.get("id")
        if not pid:
            continue
        out.append({"id": f"hfpaper:{pid}", "title": clean(p.get("title") or d.get("title", ""), 200),
                    "link": f"https://huggingface.co/papers/{pid}",
                    "summary": clean(p.get("summary", "")), "source": f"{src['name']} (👍{p.get('upvotes')})"})
    return out


def collect(cfg: dict, seen: set) -> list[dict]:
    since = time.time() - cfg.get("lookback_hours", 36) * 3600
    items, ids = [], set()
    for src in (cfg.get("sources") or []):
        try:
            fn = fetch_hf_papers if src.get("type") == "hf_papers" else fetch_rss
            got = fn(src, since)
            print(f"{src['name']}: {len(got)} 件")
        except Exception as ex:  # 1つのフィードが壊れても他は続行
            print(f"⚠ {src['name']} 取得失敗: {ex}")
            continue
        for it in got:
            if it["id"] in seen or it["id"] in ids or not it["title"]:
                continue
            ids.add(it["id"])
            items.append(it)
    return items


PROMPT = """あなたはAI・LLM分野の情報キュレーターです。以下は過去1日ほどの新着記事の候補です。
読者は「インプレッション稼ぎの定型投稿」にうんざりしており、中身のある一次情報だけを求めています。

選定基準:
- 採用: 新モデル・新機能の公式発表、重要な研究成果、実用的なツール/OSS、規制・業界の大きな動き、技術的に深い解説
- 除外: 宣伝目的・中身の薄い記事、同じ話題の重複（最も一次情報に近いものを1つ残す）、AI以外の話題
- 興味のあるテーマ（優先）: {interests}
- 最大 {max_items} 件。該当が少なければ少なくてよい（0件も可）。

各採用記事について、日本語で:
- title_ja: 内容が分かる簡潔な見出し（40字以内）
- summary_ja: 何が新しく、なぜ重要かを1〜2文（120字以内）。誇張しない
- tag: {tags} のいずれか

JSONのみを出力（前後に説明文やコードブロックを付けない）:
{{"items": [{{"i": 候補番号, "title_ja": "...", "summary_ja": "...", "tag": "..."}}]}}

候補:
{candidates}"""


def select_with_claude(items: list[dict], cfg: dict) -> list[dict] | None:
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        return None
    cands = items[: cfg.get("max_candidates", 120)]
    lines = [f"[{i}] ({it['source']}) {it['title']} — {it['summary'][:300]}" for i, it in enumerate(cands)]
    prompt = PROMPT.format(interests="、".join((cfg.get("interests") or [])) or "特になし",
                           max_items=cfg.get("max_items", 12), tags="／".join(TAG_ORDER),
                           candidates="\n".join(lines))
    r = requests.post(
        "https://api.anthropic.com/v1/messages",
        headers={"x-api-key": key, "anthropic-version": "2023-06-01", "content-type": "application/json"},
        json={"model": cfg.get("model", "claude-haiku-5-5"), "max_tokens": 4000,
              "messages": [{"role": "user", "content": prompt}]},
        timeout=180,
    )
    if r.status_code >= 400:
        print("Claude API error:", r.status_code, r.text[:500])
        return None
    text = "".join(b.get("text", "") for b in r.json().get("content", []) if b.get("type") == "text")
    m = re.search(r"\{.*\}", text, flags=re.S)
    try:
        picked = json.loads(m.group(0))["items"] if m else []
    except (json.JSONDecodeError, KeyError):
        print("Claudeの出力を解釈できませんでした:", text[:300])
        return None
    out = []
    for p in picked:
        try:
            base = cands[int(p["i"])]
        except (KeyError, ValueError, IndexError, TypeError):
            continue
        out.append({**base, "title_ja": p.get("title_ja") or base["title"],
                    "summary_ja": p.get("summary_ja", ""),
                    "tag": p.get("tag") if p.get("tag") in TAG_ORDER else "その他"})
    return out


def select_by_keywords(items: list[dict], cfg: dict) -> list[dict]:
    kws = [k.lower() for k in (cfg.get("fallback_keywords") or [])]
    hits = [it for it in items if not kws or any(k in (it["title"] + it["summary"]).lower() for k in kws)]
    return [{**it, "title_ja": it["title"], "summary_ja": "", "tag": "その他"}
            for it in hits[: cfg.get("max_items", 12)]]


def build_embeds(picked: list[dict]) -> list[dict]:
    embeds = []
    for tag in TAG_ORDER:
        group = [p for p in picked if p["tag"] == tag]
        if not group:
            continue
        body, part = "", 1
        for p in group:
            title = p["title_ja"].replace("[", "［").replace("]", "］")
            entry = f"**[{title}]({p['link']})**\n"
            if p["summary_ja"]:
                entry += f"{p['summary_ja']}\n"
            entry += f"_{p['source']}_\n\n"
            if len(body) + len(entry) > 3800:
                embeds.append({"title": f"{tag}（{part}）", "description": body.strip(), "color": TAG_COLOR[tag]})
                body, part = "", part + 1
            body += entry
        if body:
            embeds.append({"title": tag if part == 1 else f"{tag}（{part}）",
                           "description": body.strip(), "color": TAG_COLOR[tag]})
    return embeds


def main() -> None:
    cfg = load_config().get("ai_digest") or {}
    state = load_state("ai_digest")
    seen = state.get("seen", [])

    items = collect(cfg, set(seen))
    print(f"候補 {len(items)} 件")
    if items:
        picked = select_with_claude(items, cfg)
        mode = "Claude選定"
        if picked is None:
            picked = select_by_keywords(items, cfg)
            mode = "キーワード一致（APIキー未設定またはエラー）"
        print(f"{mode}: {len(picked)} 件")

        if picked:
            jst = time.strftime("%m/%d %H:%M", time.gmtime(time.time() + 9 * 3600))
            post_discord("DISCORD_WEBHOOK_AI", username="AI・LLMダイジェスト",
                         content=f"🤖 **AI・LLMダイジェスト**（{jst}）候補{len(items)}件から{len(picked)}件",
                         embeds=build_embeds(picked))

    state["seen"] = trim_seen(seen + [it["id"] for it in items], 3000)
    save_state("ai_digest", state)


if __name__ == "__main__":
    sys.exit(main())
