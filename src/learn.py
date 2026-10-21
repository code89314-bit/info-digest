"""がん薬物療法の学習番組（毎日1テーマを掘り下げる podcast）。

Strata（ローカルLLM）: config.yaml の learn.categories（カテゴリと種リスト）から、具体的な学習テーマを作って
  テーマ帳（state/learn.json の catalog）に蓄える。各テーマに「掘り下げる核」と「資料探しの指示」を付ける。
毎日: 未消化のテーマを1つランダムに選び、NotebookLM のウェブ調査（source add-research）で一次資料
  （PMDA の資料・学会ガイドライン・論文など）を集め、要点を投稿して、掘り下げ podcast を添付する。
  扱ったテーマは done に記録して繰り返さない。

使い方: python src/learn.py            今日の1本
        python src/learn.py --refill   テーマ帳の補充だけ（確認用）
        python src/learn.py --list     テーマ帳を表示
"""
import datetime as dt
import json
import os
import random
import re
import sys

import ai_digest
from ai_digest import CHAT, LLMError, ja_fix, llm_settings, strip_think
from common import load_state, post_discord, save_state

SECTION = "learn"

TOPIC_PROMPT = """あなたは{audience}向けの教育担当です。カテゴリ「{category}」の題材「{seed}」から、
1回15分ほどの音声で掘り下げる学習テーマを{n}個考えてください。

- 漠然とした全体解説ではなく、現場で判断に迷う具体的な論点に絞る（例: 副作用の早期発見と休薬・再開の基準、
  レジメン選択の根拠となった試験、ガイドラインで推奨が変わった理由、治療の変遷とその根拠）
- {category_hint}
- 確信の持てない薬剤名・試験名・数値は書かない（後で一次資料を調べるので、論点が正しければよい）
- 既にあるテーマと重ならないこと: {existing}

各テーマ:
- title: 見出し（40字以内）
- core: 掘り下げる核心。何を理解すべきかを2〜3文
- research: 一次資料を探すための調査指示（日本語）。探す資料の種類を具体的に書く
  （例: PMDA の適正使用ガイド・RMP、添付文書、日本の学会の診療ガイドライン、根拠となった臨床試験の論文）

JSONのみを出力: {{"topics": [{{"title": "...", "core": "...", "research": "..."}}]}}"""

SUMMARY_PROMPT = """資料に基づいて、次の学習テーマの要点を{audience}向けに日本語でまとめてください。

テーマ: {title}
掘り下げる核心: {core}

- **太字の小見出し** と箇条書きで、全体で{max_chars}字以内
- 根拠（どの資料・試験・ガイドラインか）を簡潔に添える
- 資料に書かれていない数値・用量・推奨は補わない。資料が足りない点は「資料からは確認できない」と書く
- 最後に「現場でのポイント」を3つ"""


def pick_weighted(cats: list[dict], rng: random.Random) -> dict:
    return rng.choices(cats, weights=[float(c.get("weight", 1)) for c in cats])[0]


def refill(cfg: dict, state: dict, rng: random.Random) -> int:
    """未消化テーマが min_pending 未満なら、Strata にテーマを作らせて補充する。足した数を返す。"""
    cats = [c for c in (cfg.get("categories") or []) if c.get("seeds")]
    pending = [t for t in state["catalog"] if t["id"] not in state["done"]]
    added, tries = 0, 0
    llm = {**llm_settings(cfg), "json_mode": True}
    chat = CHAT.get(llm["provider"])
    if not chat or not cats:
        return 0
    used = state.setdefault("seeds_used", {})
    while len(pending) + added < int(cfg.get("min_pending", 10)) and tries < int(cfg.get("max_refill_calls", 4)):
        tries += 1
        cat = pick_weighted(cats, rng)
        seeds = list(cat["seeds"])
        fresh = [s for s in seeds if s not in used.get(cat["name"], [])] or seeds   # 一巡したらまた最初から
        seed = rng.choice(fresh)
        used.setdefault(cat["name"], []).append(seed)
        existing = [t["title"] for t in state["catalog"] if t["category"] == cat["name"] and t.get("seed") == seed]
        prompt = TOPIC_PROMPT.format(audience=cfg.get("audience") or "病院薬剤師", category=cat["name"], seed=seed,
                                     n=int(cfg.get("topics_per_seed", 3)), category_hint=cat.get("hint") or "",
                                     existing="、".join(existing) or "なし")
        try:
            text = chat(prompt, llm)
        except LLMError as ex:
            print("テーマを作れませんでした:", ex)
            break
        m = re.search(r"\{.*\}", strip_think(text), flags=re.S)
        try:
            topics = json.loads(m.group(0)).get("topics", []) if m else []
        except json.JSONDecodeError:
            topics = []
        for t in topics:
            if not (isinstance(t, dict) and t.get("title") and t.get("research")):
                continue
            tid = f"{cat['name']}:{seed}:{t['title']}"
            if any(x["id"] == tid for x in state["catalog"]):
                continue
            state["catalog"].append({"id": tid, "category": cat["name"], "seed": seed, "title": ja_fix(str(t["title"])),
                                     "core": ja_fix(str(t.get("core") or "")), "research": ja_fix(str(t["research"])),
                                     "added": dt.date.today().isoformat()})
            added += 1
        print(f"テーマ補充: {cat['name']}／{seed} → {len(topics)} 件")
    return added


def choose(cfg: dict, state: dict, rng: random.Random) -> dict | None:
    """未消化テーマから、カテゴリの重みをつけてランダムに1つ。"""
    pending = [t for t in state["catalog"] if t["id"] not in state["done"]]
    if not pending:
        return None
    weight = {c["name"]: float(c.get("weight", 1)) for c in (cfg.get("categories") or [])}
    return rng.choices(pending, weights=[weight.get(t["category"], 1) for t in pending])[0]


def episode(cfg: dict, topic: dict, webhook: str, title: str) -> None:
    import podcast_nlm
    pod = {**(cfg.get("podcast") or {}), "title": f"{title} {topic['title']}",
           "label": f"学習：{topic['title']}",
           "instructions": ((cfg.get("podcast") or {}).get("instructions") or "").strip() +
           f"\nテーマ: {topic['title']}\n掘り下げる核心: {topic['core']}"}
    if os.environ.get("DRY_RUN") == "1":
        print(f"---- DRY_RUN (learn) ---- {topic['category']}｜{topic['title']}\n核心: {topic['core']}\n"
              f"調査指示: {topic['research']}")
        return
    nb = podcast_nlm.Notebook(pod, [])
    try:
        n = nb.research(f"{topic['research']}\n（テーマ: {topic['title']}。日本の一次資料を優先）",
                        mode=cfg.get("research_mode", "fast"))
        if n < int(cfg.get("min_sources", 2)):
            raise podcast_nlm.PodcastError(f"一次資料が {n} 件しか見つかりませんでした")
        nb.add_text("学習テーマと核心", f"# {topic['title']}\n\n## 掘り下げる核心\n{topic['core']}\n")
        summary = nb.ask(SUMMARY_PROMPT.format(audience=cfg.get("audience") or "病院薬剤師", title=topic["title"],
                                               core=topic["core"], max_chars=cfg.get("max_chars", 1500)))
        content = f"{cfg.get('icon') or '📚'} **{title}｜{topic['category']}**\n**{topic['title']}**"
        if cfg.get("notice"):
            content += f"\n-# {cfg['notice'].strip()}"
        post_discord(webhook, username=title, avatar_url=cfg.get("avatar_url"), content=content,
                     embeds=[{"title": "📝 要点", "description": summary[:3900], "color": 0x8D6E63},
                             {"title": "🎯 掘り下げる核心", "description": topic["core"][:1000], "color": 0xA1887F}])
    except BaseException:
        nb.close()
        raise
    podcast_nlm.run([topic], pod, webhook=webhook, username=title, notebook=nb, avatar_url=cfg.get("avatar_url"))


def main(argv: list[str]) -> None:
    cfg = ai_digest.section_config(SECTION)
    if not cfg:
        raise SystemExit(f"config.yaml に {SECTION}: がありません")
    state = load_state(SECTION)
    state.setdefault("catalog", [])
    state.setdefault("done", [])
    rng = random.Random()
    if "--list" in argv:
        for t in state["catalog"]:
            print("済" if t["id"] in state["done"] else "未", t["category"], "|", t["title"])
        return
    refill(cfg, state, rng)
    save_state(SECTION, state)
    if "--refill" in argv:
        return
    topic = choose(cfg, state, rng)
    if not topic:
        print("未消化のテーマがありません")
        return
    print(f"今日のテーマ: {topic['category']}｜{topic['title']}")
    webhook = cfg.get("webhook_env") or "DISCORD_WEBHOOK_ONCO"
    title = cfg.get("title") or "がん薬物療法 学習"
    try:
        episode(cfg, topic, webhook, title)
    except Exception as ex:   # noqa: BLE001
        print("学習番組を作れませんでした:", ex)
        if os.environ.get("DRY_RUN") != "1":
            post_discord(webhook, username=title, avatar_url=cfg.get("avatar_url"), content=f"📚 今日の学習番組は作れませんでした: {str(ex)[:300]}")
        if "見つかりませんでした" in str(ex):   # 資料が無いテーマは捨てて次回は別のものを
            state["done"].append(topic["id"])
            save_state(SECTION, state)
        raise SystemExit(1)
    if os.environ.get("DRY_RUN") != "1":
        state["done"].append(topic["id"])
        save_state(SECTION, state)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
