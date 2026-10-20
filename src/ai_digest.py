"""AI関連の新着をブログ・GitHubリリース・HN・Hugging Faceから集め、要約ダイジェストをDiscordに投稿する。

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

DEFAULT_TAGS = ["モデル・製品", "研究・論文", "ツール・OSS", "業界・政策", "その他"]
PALETTE = [0x7E57C2, 0x26A69A, 0x42A5F5, 0xFFA726, 0xEC407A, 0x90A4AE]


def get_tags(cfg: dict) -> list[str]:
    tags = list(cfg.get("tags") or DEFAULT_TAGS)
    if "その他" not in tags:
        tags.append("その他")
    return tags


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


def fetch_hf_models(src: dict, since: float) -> list[dict]:
    """Hugging Faceで話題（トレンド）のモデル。初登場のものだけが候補になる。"""
    url = src.get("url", "https://huggingface.co/api/models?sort=trendingScore&direction=-1&limit=40")
    allowed = set(src.get("pipeline_tags") or [])
    out = []
    for m in http_get(url).json():
        mid = m.get("id") or m.get("modelId")
        if not mid or (allowed and m.get("pipeline_tag") not in allowed):
            continue
        tags = [t for t in (m.get("tags") or []) if ":" not in t][:12]
        out.append({"id": f"hfmodel:{mid}", "title": mid, "link": f"https://huggingface.co/{mid}",
                    "summary": clean(f"種類: {m.get('pipeline_tag')} / ❤{m.get('likes')} / "
                                     f"DL {m.get('downloads')} / タグ: {', '.join(tags)}"),
                    "source": f"{src['name']} (❤{m.get('likes')})"})
    return out


def collect(cfg: dict, seen: set) -> list[dict]:
    since = time.time() - cfg.get("lookback_hours", 36) * 3600
    items, ids = [], set()
    for src in (cfg.get("sources") or []):
        try:
            fn = {"hf_papers": fetch_hf_papers, "hf_models": fetch_hf_models}.get(src.get("type"), fetch_rss)
            got = fn(src, since)
            print(f"{src['name']}: {len(got)} 件")
        except Exception as ex:  # 1つのフィードが壊れても他は続行
            print(f"⚠ {src['name']} 取得失敗: {ex}")
            continue
        for it in got:
            if it["id"] in seen or it["id"] in ids or it["link"] in ids or not it["title"]:
                continue
            ids.update({it["id"], it["link"]})
            items.append(it)
    return items


PROMPT = """あなたは情報キュレーターです。以下は過去1日ほどの新着記事・リリース・モデルの候補です。
読者は「インプレッション稼ぎの定型投稿」にうんざりしており、中身のある一次情報だけを求めています。

読者の関心（これに合うものだけ採用）:
{focus}

選定基準:
- 採用: 上の関心に直接役立つ新モデル・新機能・リリース・実用的な手法や検証
- 除外: {exclude}
- 除外: 宣伝目的・中身の薄い記事、同じ話題の重複（最も一次情報に近いものを1つ残す）
- 優先テーマ: {interests}
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
    focus = (cfg.get("focus") or "AI・LLM全般").strip()
    exclude = (cfg.get("exclude") or "関心と無関係な話題").strip()
    prompt = PROMPT.format(focus=focus, exclude=exclude, interests="、".join((cfg.get("interests") or [])) or "特になし",
                           max_items=cfg.get("max_items", 12), tags="／".join(get_tags(cfg)),
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
                    "tag": p.get("tag") if p.get("tag") in get_tags(cfg) else "その他"})
    return out


def select_by_keywords(items: list[dict], cfg: dict) -> list[dict]:
    kws = [k.lower() for k in (cfg.get("fallback_keywords") or [])]
    hits = [it for it in items if not kws or any(k in (it["title"] + it["summary"]).lower() for k in kws)]
    return [{**it, "title_ja": it["title"], "summary_ja": "", "tag": "その他"}
            for it in hits[: cfg.get("max_items", 12)]]


def build_embeds(picked: list[dict], tags: list[str] | None = None) -> list[dict]:
    tags = tags or DEFAULT_TAGS
    embeds = []
    for n, tag in enumerate(tags):
        color = PALETTE[n % len(PALETTE)] if tag != "その他" else 0x90A4AE
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
                embeds.append({"title": f"{tag}（{part}）", "description": body.strip(), "color": color})
                body, part = "", part + 1
            body += entry
        if body:
            embeds.append({"title": tag if part == 1 else f"{tag}（{part}）",
                           "description": body.strip(), "color": color})
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
            title = cfg.get("title") or "AI・LLMダイジェスト"
            post_discord("DISCORD_WEBHOOK_AI", username=title,
                         content=f"🤖 **{title}**（{jst}）候補{len(items)}件から{len(picked)}件",
                         embeds=build_embeds(picked, get_tags(cfg)))

    state["seen"] = trim_seen(seen + [it["id"] for it in items], 3000)
    save_state("ai_digest", state)


if __name__ == "__main__":
    sys.exit(main())
