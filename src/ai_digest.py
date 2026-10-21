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
    # OncLive などは見出しを <![CDATA[...]]> で包んでいる。タグ扱いで丸ごと消えないよう先に中身を取り出す
    text = re.sub(r"<!\[CDATA\[(.*?)\]\]>", r"\1", text or "", flags=re.S)
    text = html.unescape(re.sub(r"<[^>]+>", " ", text))
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


PROMPT = """あなたは情報キュレーターです。以下は過去1日ほどの新着情報の候補です。
読者は「インプレッション稼ぎの定型投稿」にうんざりしており、中身のある一次情報だけを求めています。

読者の関心（これに合うものだけ採用）:
{focus}

選定基準:
- 採用: {adopt}
- 除外: {exclude}
- 除外: 宣伝目的・中身の薄い記事、同じ話題の重複（最も一次情報に近いものを1つ残す）
- 優先テーマ: {interests}
- 最大 {max_items} 件。該当が少なければ少なくてよい（0件も可）。

各採用記事について、日本語で（英語の記事も日本語で書く）:
- title_ja: 内容が分かる簡潔な見出し（40字以内）
- summary_ja: 何が新しく、なぜ重要かを1〜2文（{summary_len}字以内）。誇張しない。候補に書かれていない事実を補わない
- 日本語は自然に。英語の病名・一般的な語は日本語の標準的な用語に訳す（薬剤名・製品名・試験名は原語のままでよい）。
  中国語の字体（简体字・繁體字）を使わない
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


# 量子化したローカルLLMが混ぜがちな中国語の語・字体 → 日本語。簡体字は日本語では使わない字なので機械的に置き換えてよい
JA_WORDS = {"随访": "フォローアップ", "监测": "モニタリング", "监视": "監視", "会诊": "コンサルト", "流程": "手順", "路径": "パス",
            "分层": "層別化", "等级": "等級", "生物标志物": "バイオマーカー", "标志物": "マーカー", "复发": "再発",
            "骨髓": "骨髄", "胰腺": "膵", "乳腺癌": "乳癌", "治疗": "治療", "诊断": "診断", "药物": "薬物"}
JA_CHARS = str.maketrans({"转": "転", "监": "監", "测": "測", "级": "級", "层": "層", "变": "変", "选": "選", "择": "択",
                          "疗": "療", "药": "薬", "剂": "剤", "应": "応", "对": "対", "发": "発", "髓": "髄", "诊": "診",
                          "检": "検", "评": "評", "价": "価", "险": "険", "适": "適", "证": "証", "标": "標", "准": "準",
                          "进": "進", "时": "時", "间": "間", "关": "関", "联": "連", "经": "経", "验": "験", "试": "試",
                          "结": "結", "组": "組", "报": "報", "处": "処", "胰": "膵", "调": "調"})


def ja_fix(text: str) -> str:
    """ローカルLLMの出力に混ざった中国語の語・字体を日本語に直す（NotebookLM の出力には不要）。"""
    if not text:
        return text
    for zh, ja in JA_WORDS.items():
        text = text.replace(zh, ja)
    return text.translate(JA_CHARS)


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


def parse_item_objects(text: str) -> list[dict]:
    """{"items": [...]} が途中で切れていても、読める {"i": ..., ...} を1件ずつ取り出す。"""
    items = parse_items(text)
    if items is not None:
        return [g for g in items if isinstance(g, dict)]
    out = []
    for m in re.finditer(r"\{[^{}]*\"i\"\s*:\s*\d+[^{}]*\}", strip_think(text)):
        try:
            out.append(json.loads(m.group(0)))
        except json.JSONDecodeError:
            continue
    return out


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
    adopt = (cfg.get("adopt") or "上の関心に直接役立つ新モデル・新機能・リリース・実用的な手法や検証").strip()
    return PROMPT.format(focus=focus, exclude=exclude, interests="、".join((cfg.get("interests") or [])) or "特になし",
                         adopt=adopt, summary_len=cfg.get("summary_len", 120),
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
            picked.append({**base, "title_ja": ja_fix(str(p.get("title_ja") or base["title"])),
                           "summary_ja": ja_fix(str(p.get("summary_ja", ""))),
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
    limit = cfg.get("max_items", 12)
    if llm.get("dedupe") and len(out) > 1:
        out = dedupe_with_llm(out[: limit * 2], llm, chat)
    return out[:limit]


DEDUPE_PROMPT = """以下は採用候補のニュースです（スコアの高い順）。出典は違っても「同じ出来事」を報じているものを探してください。
同じ出来事のグループでは、最も一次情報に近い・詳しい1件だけを残し、残りの番号を drop に入れます。
別の出来事（同じ薬でも別の適応・別の試験など）は残します。重複が無ければ drop は空にします。

JSONのみを出力: {{"drop": [番号, ...]}}

候補:
{items}"""


def dedupe_with_llm(items: list[dict], llm: dict, chat) -> list[dict]:
    """出典違いで同じ出来事を報じた記事をLLMにまとめさせる（llm.dedupe: true のとき）。失敗したらそのまま返す。"""
    lines = [f"[{i}] {p['title_ja']} — {p['summary_ja'][:100]}（{p['source']}）" for i, p in enumerate(items)]
    try:
        text = chat(DEDUPE_PROMPT.format(items="\n".join(lines)), llm)
        m = re.search(r"\{.*\}", strip_think(text), flags=re.S)
        drop = {int(i) for i in json.loads(m.group(0)).get("drop", [])} if m else set()
    except (LLMError, json.JSONDecodeError, ValueError, TypeError, AttributeError) as ex:
        print("重複チェックを省略:", ex)
        return items
    if drop:
        print(f"  同じ出来事の重複を {len(drop & set(range(len(items))))} 件まとめました")
    return [p for i, p in enumerate(items) if i not in drop]


def select_with_claude(items: list[dict], cfg: dict) -> list[dict] | None:
    """互換用: provider を anthropic に固定して選別する。"""
    return select_with_llm(items, {**cfg, "llm": {**(cfg.get("llm") or {}), "provider": "anthropic"}})


def select_by_keywords(items: list[dict], cfg: dict) -> list[dict]:
    kws = [k.lower() for k in (cfg.get("fallback_keywords") or [])]
    hits = [it for it in items if not kws or any(k in (it["title"] + it["summary"]).lower() for k in kws)]
    return [{**it, "title_ja": it["title"], "summary_ja": "", "tag": "その他"}
            for it in hits[: cfg.get("max_items", 12)]]


def build_embeds(picked: list[dict], tags: list[str] | None = None, style: dict | None = None) -> list[dict]:
    """分類ごとに embed を作る。style（config の tag_style）で分類ごとの絵文字・色を指定できる。"""
    tags = tags or DEFAULT_TAGS
    style = style or {}
    embeds = []
    for n, tag in enumerate(tags):
        color = PALETTE[n % len(PALETTE)] if tag != "その他" else 0x90A4AE
        st = style.get(tag) or {}
        color = int(str(st["color"]).lstrip("#"), 16) if st.get("color") else color
        label = f"{st['emoji']} {tag}" if st.get("emoji") else tag
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
                embeds.append({"title": f"{label}（{part}）", "description": body.strip(), "color": color})
                body, part = "", part + 1
            body += entry
        if body:
            embeds.append({"title": label if part == 1 else f"{label}（{part}）",
                           "description": body.strip(), "color": color})
    return embeds


OVERVIEW_PROMPT = """あなたは専門ニュースの編集者です。読者: {audience}
以下は本日選んだ新着情報です。これだけを根拠に「今日の動向まとめ」を日本語で書いてください。

- {group_by}ごとに **太字の見出し** を立て、その下に箇条書きで「何が変わったか」「現場への影響・注意点」を簡潔に
- 情報に書かれていない数値・用量・承認状況・日付を補わない。不確かなことは断定しない
- 英語の病名・一般的な語は日本語の標準的な用語に訳す（薬剤名・試験名は原語のままでよい）。中国語の字体を使わない
{extra}- 全体で{max_chars}字以内。前置きや結びの挨拶は不要

新着情報:
{items}"""


def write_overview(picked: list[dict], cfg: dict) -> str | None:
    """選ばれた記事から「動向まとめ」の文章をLLMに書かせる（ai_digest.overview）。失敗したら None。"""
    ov = cfg.get("overview") or {}
    llm = {**llm_settings(cfg), "json_mode": False}
    chat = CHAT.get(llm["provider"])
    if not chat or not picked:
        return None
    lines = [f"- [{p['tag']}] {p['title_ja']}：{p['summary_ja']}（{p['source']}）" for p in picked]
    extra = "".join(f"- {r}\n" for r in (ov.get("rules") or []))
    prompt = OVERVIEW_PROMPT.format(audience=ov.get("audience") or "この分野の専門家",
                                    group_by=ov.get("group_by") or "テーマ", extra=extra,
                                    max_chars=ov.get("max_chars", 1200), items="\n".join(lines))
    try:
        text = ja_fix(strip_think(chat(prompt, llm)).strip())
    except LLMError as ex:
        print("動向まとめを作れませんでした:", ex)
        return None
    return text[:3900] or None


NLM_ITEMS_PROMPT = """資料として追加したニュース記事について、{audience}向けに日本語の見出しと要約を書いてください。

- 各記事は、その記事の資料に書かれていることだけを根拠にする。書かれていない数値・用量・承認状況を補わない
- title_ja: 内容が分かる簡潔な見出し（40字以内）
- summary_ja: 何が新しく、なぜ重要かを1〜2文（{summary_len}字以内）
- 英語の病名・一般的な語は日本語の標準的な用語に訳す（薬剤名・試験名は原語のままでよい）
{extra}- 資料が読めず内容が分からない記事は出力しない

記事（番号・原題・URL）:
{items}

JSONだけを出力（前後に説明文やコードブロックを付けない）:
{{"items": [{{"i": 番号, "title_ja": "...", "summary_ja": "..."}}]}}"""


def summarize_with_notebooklm(nb, picked: list[dict], cfg: dict) -> tuple[list[dict], str | None]:
    """Strata が選んだ記事の見出し・要約と「動向まとめ」を、記事本文を読んだ NotebookLM に書かせる。

    質問は2回だけ（NotebookLM のチャットにも1日の回数制限があるため）。書けなかった記事は Strata の要約のまま。
    """
    ov = cfg.get("overview") or {}
    audience = ov.get("audience") or "この分野の専門家"
    extra = "".join(f"- {r}\n" for r in (ov.get("rules") or []))
    out = [dict(p) for p in picked]
    done = set()
    # 長い回答は途中で切れることがあるので、取り出せなかった記事だけをもう一度聞く（最大2回）
    for attempt in range(2):
        todo = [i for i in range(len(picked)) if i not in done]
        if not todo:
            break
        lines = [f"[{i}] {picked[i]['title']} — {picked[i]['link']}" for i in todo]
        try:
            text = nb.ask(NLM_ITEMS_PROMPT.format(audience=audience, summary_len=cfg.get("summary_len", 120),
                                                  extra=extra, items="\n".join(lines)))
        except Exception as ex:   # noqa: BLE001
            print("NotebookLM の要約に失敗（Strataの要約を使います）:", ex)
            break
        got = parse_item_objects(text)
        if not got:
            print("NotebookLM の回答を解釈できませんでした:", text[:300])
        for g in got:
            try:
                i = int(g["i"])
            except (KeyError, ValueError, TypeError):
                continue
            if i in todo and g.get("title_ja") and g.get("summary_ja"):
                out[i].update(title_ja=str(g["title_ja"]), summary_ja=str(g["summary_ja"]))
                done.add(i)
    print(f"NotebookLM が要約した記事: {len(done)}/{len(picked)} 件")
    overview = None
    if ov.get("enabled"):
        items = "\n".join(f"- [{p['tag']}] {p['title_ja']}：{p['summary_ja']}（{p['source']}）" for p in out)
        try:
            overview = nb.ask(OVERVIEW_PROMPT.format(
                audience=audience, group_by=ov.get("group_by") or "テーマ", extra=extra,
                max_chars=ov.get("max_chars", 1200), items=items)
                + "\n\n資料（記事本文）の内容に基づいて書き、上の一覧と食い違う場合は資料を優先する。")[:3900] or None
        except Exception as ex:   # noqa: BLE001
            print("NotebookLM の動向まとめに失敗:", ex)
    return out, overview


def section_config(section: str) -> dict:
    """config.yaml の区画（ai_digest / ai_global など）。llm を書いていない区画は ai_digest の llm を使う。"""
    full = load_config()
    cfg = dict(full.get(section) or {})
    if "llm" not in cfg and section != "ai_digest":
        cfg["llm"] = (full.get("ai_digest") or {}).get("llm")
    return cfg


def main(section: str = "ai_digest", seed: bool = False) -> None:
    """section: config.yaml の区画名。既読管理（state/<section>.json）も区画ごとに別。

    seed=True: 今ある記事を既読にするだけで投稿しない（日付の無いRSSを足したときの初回用）。
    """
    cfg = section_config(section)
    if not cfg:
        raise SystemExit(f"config.yaml に {section}: がありません")
    state = load_state(section)
    seen = state.get("seen", [])
    webhook = cfg.get("webhook_env") or "DISCORD_WEBHOOK_AI"
    title = cfg.get("title") or "AI・LLMダイジェスト"
    icon = cfg.get("icon") or "🤖"

    items = collect(cfg, set(seen))
    print(f"候補 {len(items)} 件")
    pod = cfg.get("podcast") or {}
    picked, overview, notebook = [], None, None
    if items and not seed:
        llm = llm_settings(cfg)
        picked = select_with_llm(items, cfg)
        mode = f"LLM選定（{llm['provider']}: {llm.get('model')}）"
        if picked is None:
            picked = select_by_keywords(items, cfg)
            mode = "キーワード一致（LLM未設定またはエラー）"
        print(f"{mode}: {len(picked)} 件")

        if picked:
            jst = time.strftime("%m/%d %H:%M", time.gmtime(time.time() + 9 * 3600))
            # podcast.summarize: 選んだ記事を NotebookLM に読ませ、要約・動向まとめもそちらに書かせる
            if pod.get("enabled") and pod.get("summarize") and os.environ.get("DRY_RUN") != "1":
                import podcast_nlm
                try:
                    notebook = podcast_nlm.Notebook(pod, picked)
                    picked, overview = summarize_with_notebooklm(notebook, picked, cfg)
                except Exception as ex:   # noqa: BLE001  NotebookLM が使えなければ Strata の要約で投稿
                    print("NotebookLM を使えませんでした（Strataの要約で投稿します）:", ex)
                    if notebook:
                        notebook.close()
                    notebook = None
            embeds = build_embeds(picked, get_tags(cfg), cfg.get("tag_style"))
            if (cfg.get("overview") or {}).get("enabled"):
                overview = overview or write_overview(picked, cfg)
                if overview:
                    embeds.insert(0, {"title": "📋 今日の動向まとめ", "description": overview, "color": 0x5C6BC0})
            content = f"{icon} **{title}**（{jst}）候補{len(items)}件から{len(picked)}件"
            if cfg.get("notice"):
                content += f"\n-# {cfg['notice'].strip()}"
            try:
                post_discord(webhook, username=title, avatar_url=cfg.get("avatar_url"), content=content, embeds=embeds)
            except BaseException:
                if notebook:   # 投稿に失敗したらノートを残さない（次回また作り直す）
                    notebook.close()
                raise

    state["seen"] = trim_seen(seen + [it["id"] for it in items], 3000)
    save_state(section, state)   # 音声づくり（長い・失敗しうる）の前に既読を確定させる
    if seed:
        print(f"既読にしました: {len(items)} 件（投稿なし）")

    if pod.get("enabled") and len(picked) >= int(pod.get("min_items", 1)):
        import podcast_nlm   # NotebookLM を使うときだけ読み込む
        podcast_nlm.run(picked, pod, webhook=webhook, username=title, notebook=notebook,
                        avatar_url=cfg.get("avatar_url"))
    elif notebook:
        notebook.close()


if __name__ == "__main__":
    # 使い方: python src/ai_digest.py            → ai_digest（日本語記事）
    #         python src/ai_digest.py ai_global  → ai_global（海外の話題）
    #         python src/ai_digest.py onco       → onco（がん薬物療法）
    #         python src/ai_digest.py onco --seed  → 今ある記事を既読にするだけ（初回用）
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    sys.exit(main(args[0] if args else "ai_digest", seed="--seed" in sys.argv))
