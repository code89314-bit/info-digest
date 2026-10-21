"""AI関連の新着をブログ・GitHubリリース・HN・Hugging Faceから集め、要約ダイジェストをDiscordに投稿する。

config.yaml の ai_digest.llm.provider で選別・要約に使うLLMを切り替える:
  openai_compat  ローカルLLMサーバー（Strata / Ollama / LM Studio / llama.cpp / vLLM）
  anthropic      Claude API（ANTHROPIC_API_KEY が必要）
  none           LLMを使わない
LLMが使えない・失敗したときは、キーワード一致した記事の見出しだけを投稿する。
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

- score: 読者にとっての価値 1〜5（5が最も価値が高い）

JSONのみを出力（前後に説明文やコードブロックを付けない）:
{{"items": [{{"i": 候補番号, "title_ja": "...", "summary_ja": "...", "tag": "...", "score": 3}}]}}

候補:
{candidates}"""


class LLMError(Exception):
    """LLMサーバーに届かない・エラーを返した（再試行しても無駄なもの）。"""


def llm_settings(cfg: dict) -> dict:
    """ai_digest.llm を既定値で補う。llm が無い古い config.yaml は従来どおり anthropic 扱い。"""
    llm = dict(cfg.get("llm") or {"provider": "anthropic"})
    llm.setdefault("provider", "openai_compat")
    llm.setdefault("timeout", 600)
    llm.setdefault("temperature", 0.2)
    llm.setdefault("max_tokens", 4000)
    llm.setdefault("json_mode", True)
    if llm["provider"] == "anthropic":
        # Claude のモデルは ai_digest.model（llm.model はローカルLLM用なので使わない）
        llm["model"] = cfg.get("model") or "claude-haiku-5-5"
        llm.setdefault("batch_size", cfg.get("max_candidates", 120))   # Claudeは長文に強いので従来どおり一括
        llm.setdefault("summary_chars", 300)
    else:
        llm.setdefault("batch_size", 25)
        llm.setdefault("summary_chars", 200)
    return llm


def strip_think(text: str) -> str:
    """Qwen系などが出す <think>...</think> を取り除く（閉じタグだけ残った場合はそこまで捨てる）。"""
    text = re.sub(r"<think>.*?</think>", "", text or "", flags=re.S)
    if "</think>" in text:
        text = text.split("</think>")[-1]
    return text.replace("<think>", "")


def parse_items(text: str) -> list | None:
    """LLMの出力から {"items": [...]} を取り出す。解釈できなければ None。"""
    m = re.search(r"\{.*\}", strip_think(text), flags=re.S)
    if not m:
        return None
    try:
        items = json.loads(m.group(0))["items"]
    except (json.JSONDecodeError, KeyError, TypeError):
        return None
    return items if isinstance(items, list) else None


def chat_anthropic(prompt: str, llm: dict) -> str:
    key = os.environ.get("ANTHROPIC_API_KEY")   # llm.api_key_env はローカルLLM用
    if not key:
        raise LLMError("ANTHROPIC_API_KEY が未設定")
    try:
        r = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={"x-api-key": key, "anthropic-version": "2023-06-01", "content-type": "application/json"},
            json={"model": llm["model"], "max_tokens": llm["max_tokens"],
                  "messages": [{"role": "user", "content": prompt}]},
            timeout=llm["timeout"],
        )
    except requests.RequestException as ex:
        raise LLMError(f"Claude API に接続できません: {ex}") from ex
    if r.status_code >= 400:
        raise LLMError(f"Claude API error: {r.status_code} {r.text[:500]}")
    return "".join(b.get("text", "") for b in r.json().get("content", []) if b.get("type") == "text")


def chat_openai_compat(prompt: str, llm: dict) -> str:
    """POST {base_url}/chat/completions（Strata / Ollama / LM Studio / llama.cpp / vLLM 共通）。"""
    url = (llm.get("base_url") or "http://localhost:11434/v1").rstrip("/") + "/chat/completions"
    headers = {"content-type": "application/json"}
    key = os.environ.get(llm["api_key_env"]) if llm.get("api_key_env") else None
    if key:
        headers["Authorization"] = f"Bearer {key}"
    body = {"model": llm.get("model"), "messages": [{"role": "user", "content": prompt}],
            "temperature": llm["temperature"], "max_tokens": llm["max_tokens"], "stream": False,
            **(llm.get("extra_body") or {})}
    if llm["json_mode"]:
        body["response_format"] = {"type": "json_object"}
    try:
        r = requests.post(url, headers=headers, json=body, timeout=llm["timeout"])
        if r.status_code == 400 and llm["json_mode"]:   # response_format 非対応のサーバーなら外して再送
            print("response_format を外して再送します:", r.text[:200])
            body.pop("response_format")
            r = requests.post(url, headers=headers, json=body, timeout=llm["timeout"])
    except requests.RequestException as ex:
        raise LLMError(f"LLMサーバー {url} に接続できません: {ex}") from ex
    if r.status_code >= 400:
        raise LLMError(f"LLMサーバー error: {r.status_code} {r.text[:500]}")
    try:
        return r.json()["choices"][0]["message"].get("content") or ""
    except (ValueError, KeyError, IndexError, TypeError) as ex:
        raise LLMError(f"LLMサーバーの応答を読めません: {r.text[:300]}") from ex


CHAT = {"openai_compat": chat_openai_compat, "anthropic": chat_anthropic}


def build_prompt(batch: list[dict], cfg: dict, summary_chars: int) -> str:
    lines = [f"[{i}] ({it['source']}) {it['title']} — {it['summary'][:summary_chars]}" for i, it in enumerate(batch)]
    focus = (cfg.get("focus") or "AI・LLM全般").strip()
    exclude = (cfg.get("exclude") or "関心と無関係な話題").strip()
    return PROMPT.format(focus=focus, exclude=exclude, interests="、".join((cfg.get("interests") or [])) or "特になし",
                         max_items=cfg.get("max_items", 12), tags="／".join(get_tags(cfg)),
                         candidates="\n".join(lines))


def select_with_llm(items: list[dict], cfg: dict) -> list[dict] | None:
    """候補を batch_size 件ずつLLMで選別し、最後に重複除去・スコア順・件数上限をかける。

    LLMが使えない（provider: none、キー無し、接続・HTTPエラー、全バッチで解釈失敗）ときは None。
    """
    llm = llm_settings(cfg)
    chat = CHAT.get(llm["provider"])
    if not chat:
        return None
    cands = items[: cfg.get("max_candidates", 120)]
    size = max(1, int(llm["batch_size"]))
    tags = get_tags(cfg)
    picked, ok_batches = [], 0
    for start in range(0, len(cands), size):
        batch = cands[start:start + size]
        prompt = build_prompt(batch, cfg, llm["summary_chars"])
        result = None
        for attempt in range(2):   # 解釈できなければ1回だけ再試行
            try:
                text = chat(prompt, llm)
            except LLMError as ex:
                print(ex)
                return None
            result = parse_items(text)
            if result is not None:
                break
            print(f"LLMの出力を解釈できませんでした（{attempt + 1}回目）:", strip_think(text)[:300])
        if result is None:
            continue
        ok_batches += 1
        print(f"  候補 {start}〜{start + len(batch) - 1}: {len(result)} 件採用")
        for p in result:
            try:
                base = batch[int(p["i"])]
            except (KeyError, ValueError, IndexError, TypeError):
                continue
            try:
                score = float(p.get("score", 3))
            except (ValueError, TypeError):
                score = 3.0
            picked.append({**base, "title_ja": p.get("title_ja") or base["title"],
                           "summary_ja": p.get("summary_ja", ""),
                           "tag": p.get("tag") if p.get("tag") in tags else "その他", "score": score})
    if cands and ok_batches == 0:
        return None
    # 2段目: 同じ記事・同じ見出しの重複を除き、スコアの高い順に上限まで
    out, keys = [], set()
    for p in sorted(picked, key=lambda p: -p["score"]):   # 同点は元の順（安定ソート）
        k = {p["link"], p["id"], re.sub(r"\s+", "", p["title_ja"])}
        if keys & k:
            continue
        keys |= k
        out.append(p)
    return out[: cfg.get("max_items", 12)]


def select_with_claude(items: list[dict], cfg: dict) -> list[dict] | None:
    """互換用: provider を anthropic に固定して選別する。"""
    return select_with_llm(items, {**cfg, "llm": {**(cfg.get("llm") or {}), "provider": "anthropic"}})


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
        llm = llm_settings(cfg)
        picked = select_with_llm(items, cfg)
        mode = f"LLM選定（{llm['provider']}: {llm.get('model')}）"
        if picked is None:
            picked = select_by_keywords(items, cfg)
            mode = "キーワード一致（LLM未設定またはエラー）"
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
