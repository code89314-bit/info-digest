"""がん薬物療法の学習番組（毎日1テーマを掘り下げる podcast）。

Strata（ローカルLLM）: config.yaml の learn.categories（カテゴリと種リスト）から、具体的な学習テーマを作って
  テーマ帳（state/learn.json の catalog）に蓄える。各テーマに「掘り下げる核」と「資料探しの指示」を付ける。
毎日: 未消化のテーマを1つランダムに選び、NotebookLM のウェブ調査（source add-research）で一次資料
  （PMDA の資料・学会ガイドライン・論文など）を集め、要点を投稿して、掘り下げ podcast を添付する。
  扱ったテーマは done に記録して繰り返さない。
  薬・ガイドラインの名前（種）は config のまま、見出しと調査の指示に必ず入れる（ローカルのモデルが
  ベージニオをブレンレップと取り違え、眼毒性の回が3回続いた 2026-11）。資料を集めたあと NotebookLM に
  「テーマが資料と合っているか」を確かめさせ、合わなければ捨てて別のテーマにする。前の日と同じ種は避ける。
  週1回（learn.review.weekday）は、1〜2週間前のテーマを別の切り口で思い出す「復習」の回にする。

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
from common import load_state, post_discord, save_state, wait_until

SECTION = "learn"

TOPIC_PROMPT = """あなたは{audience}向けの教育担当です。カテゴリ「{category}」の題材「{seed}」から、
1回15分ほどの音声で掘り下げる学習テーマを{n}個考えてください。

- 漠然とした全体解説ではなく、現場で判断に迷う具体的な論点に絞る（例: 副作用の早期発見と休薬・再開の基準、
  レジメン選択の根拠となった試験、ガイドラインで推奨が変わった理由、治療の変遷とその根拠）
- {category_hint}
- 確信の持てない薬剤名・試験名・数値は書かない（後で一次資料を調べるので、論点が正しければよい）
- 題材「{seed}」の名前を言い換えない。ほかの薬・ほかのがん種の特徴（副作用・適応・学会）を混ぜない。
  {seed} に特有の副作用などに確信が持てなければ、一般的な論点（投与前の確認、減量・休薬の考え方など）にする
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
- 最後に「現場でのポイント」を3つ{extra}"""

# 資料を集めたあと、テーマが資料と合っているかを NotebookLM に確かめさせる（ローカルのモデルの取り違えを捨てる）
CHECK_PROMPT = """次の学習テーマが、資料の内容に合っているかを確かめてください。

題材: {seed}
テーマ: {title}
掘り下げる核心: {core}

- 核心に書かれた副作用・適応・試験・ガイドライン・学会が、資料にある「{seed}」についての記載で裏づけられるか
- 別の薬・別のがん種の話が「{seed}」のこととして混ざっていないか
JSONのみを出力: {{"ok": true または false, "reason": "理由を1文で"}}"""

REVIEW_NOTE = ("これは前に取り上げたテーマの「復習」の回。前回と同じ説明の繰り返しではなく、別の切り口"
               "（症例の場面・よくある誤解・問いかけ→答え）で、聞き手が自分で思い出しながら確かめられる構成にする。")
REVIEW_SUMMARY = "\n- 復習の回: はじめに「思い出してみよう」の問いを3つ置き、そのあとに答えと要点を書く"


class Mismatch(Exception):
    """テーマが資料と合わない（取り違え）。そのテーマは捨てる。"""


def named(topic: dict) -> dict:
    """見出しに種（薬・ガイドラインの名前）が無ければ先頭に付ける。名前は config のまま（言い換えさせない）。"""
    seed = re.sub(r"（[^）]*）", "", topic.get("seed") or "").strip()   # 「肺癌診療ガイドライン（日本肺癌学会）」→ 学会名は省く
    if seed and seed not in topic["title"]:
        return {**topic, "title": f"{seed}｜{topic['title']}"}
    return topic


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
            state["catalog"].append(named({"id": tid, "category": cat["name"], "seed": seed,
                                           "title": ja_fix(str(t["title"])),
                                           "core": ja_fix(str(t.get("core") or "")), "research": ja_fix(str(t["research"])),
                                           "added": dt.date.today().isoformat()}))
            added += 1
        print(f"テーマ補充: {cat['name']}／{seed} → {len(topics)} 件")
    return added


def choose(cfg: dict, state: dict, rng: random.Random) -> dict | None:
    """未消化テーマから、カテゴリの重みをつけてランダムに1つ。"""
    pending = [t for t in state["catalog"] if t["id"] not in state["done"]]
    if not pending:
        return None
    # 前の回と同じ種（薬・ガイドライン）は、ほかに選べるものがあれば避ける（同じ種から3件ずつ作るので続きやすい）
    pending = [t for t in pending if t.get("seed") != state.get("last_seed")] or pending
    weight = {c["name"]: float(c.get("weight", 1)) for c in (cfg.get("categories") or [])}
    weights = [weight.get(t["category"], 1) for t in pending]
    if not any(weights):
        weights = None
    return named(rng.choices(pending, weights=weights)[0])


def choose_review(cfg: dict, state: dict, rng: random.Random, today: dt.date) -> dict | None:
    """復習の回のテーマ: 1〜2週間前（review.min_days〜max_days 日前）に扱ったものから1つ。復習済みは除く。
    history（扱った日の記録）が無い昔の分は、done の並び（1日1件）で日数を見積もる。"""
    rv = cfg.get("review") or {}
    lo, hi = int(rv.get("min_days", 7)), int(rv.get("max_days", 14))
    reviewed = set(state.get("reviewed") or [])
    dated = {h["id"]: dt.date.fromisoformat(h["date"]) for h in state.get("history") or []}
    by_id = {t["id"]: t for t in state["catalog"]}
    done = [i for i in state["done"] if i in by_id and i not in reviewed and i not in set(state.get("discarded") or [])]
    ago = {i: (today - dated[i]).days if i in dated else len(state["done"]) - state["done"].index(i)
           for i in done}
    cands = [i for i in done if lo <= ago[i] <= hi] or [i for i in done if ago[i] > hi]
    if not cands:
        return None
    t = named(by_id[rng.choice(cands)])
    return {**t, "review": True, "title": f"復習｜{t['title']}"}


def is_review_day(cfg: dict, today: dt.date) -> bool:
    rv = cfg.get("review") or {}
    return bool(rv.get("enabled")) and "月火水木金土日"[today.weekday()] in str(rv.get("weekday") or "")


def check_topic(nb, topic: dict) -> None:
    """集めた資料でテーマの核心が裏づけられるかを NotebookLM に聞く。合わなければ Mismatch。
    答えが読めないときは止めない（確かめられなかっただけなので、そのまま作る）。"""
    ans = nb.ask(CHECK_PROMPT.format(seed=topic.get("seed") or "", title=topic["title"], core=topic["core"]))
    m = re.search(r"\{.*\}", ans or "", flags=re.S)
    try:
        res = json.loads(m.group(0)) if m else None
    except json.JSONDecodeError:
        res = None
    if not isinstance(res, dict):
        print("テーマの確かめ: 答えを読めませんでした（そのまま作ります）")
        return
    print(f"テーマの確かめ: {'合っている' if res.get('ok') else '合わない'}（{res.get('reason', '')}）")
    if res.get("ok") is False:
        raise Mismatch(f"テーマが資料と合いません（{str(res.get('reason') or '')[:150]}）")


def episode(cfg: dict, topic: dict, webhook: str, title: str) -> None:
    import podcast_nlm
    seed = topic.get("seed") or ""
    review = bool(topic.get("review"))
    pod = {**(cfg.get("podcast") or {}), "title": f"{title} {topic['title']}",
           "label": f"{'復習' if review else '学習'}：{topic['title']}",
           "instructions": ((cfg.get("podcast") or {}).get("instructions") or "").strip() +
           f"\nテーマ: {topic['title']}\n掘り下げる核心: {topic['core']}" + (f"\n{REVIEW_NOTE}" if review else "")}
    # 調査の指示に種の名前を必ず入れる（指示に薬の名前が無く、別の薬の資料が混ざった 2026-11-20）
    research = (f"題材: {seed}\n" if seed else "") + \
        f"{topic['research']}\n（テーマ: {topic['title']}。日本の一次資料を優先）"
    if os.environ.get("DRY_RUN") == "1":
        print(f"---- DRY_RUN (learn) ---- {topic['category']}｜{topic['title']}\n核心: {topic['core']}\n"
              f"調査指示: {research}")
        return
    nb = podcast_nlm.Notebook(pod, [])
    try:
        n = nb.research(research, mode=cfg.get("research_mode", "fast"),
                        fallback=f"{seed or topic['title'].split('｜')[-1]} 添付文書 ガイドライン PMDA")
        if n < int(cfg.get("min_sources", 2)):
            raise podcast_nlm.PodcastError(f"一次資料が {n} 件しか見つかりませんでした")
        if seed:
            check_topic(nb, topic)
        nb.add_text("学習テーマと核心", f"# {topic['title']}\n\n## 掘り下げる核心\n{topic['core']}\n")
        summary = nb.ask(SUMMARY_PROMPT.format(audience=cfg.get("audience") or "病院薬剤師", title=topic["title"],
                                               core=topic["core"], max_chars=cfg.get("max_chars", 1500),
                                               extra=REVIEW_SUMMARY if review else ""))
        icon = "🔁" if review else (cfg.get("icon") or "📚")
        content = f"{icon} **{title}｜{'復習' if review else topic['category']}**\n**{topic['title']}**"
        if cfg.get("notice"):
            content += f"\n-# {cfg['notice'].strip()}"
    except BaseException:
        nb.close()
        raise

    def post_summary() -> None:   # 音声まで作ってから、post_at の時刻に要点 → 音声の順で投稿する
        wait_until(cfg.get("post_at"))
        post_discord(webhook, username=title, avatar_url=cfg.get("avatar_url"), content=content,
                     embeds=[{"title": "📝 要点", "description": summary[:3900], "color": 0x8D6E63},
                             {"title": "🎯 掘り下げる核心", "description": topic["core"][:1000], "color": 0xA1887F}])

    podcast_nlm.run([topic], pod, webhook=webhook, username=title, notebook=nb, avatar_url=cfg.get("avatar_url"),
                    before_post=post_summary)


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
    webhook = cfg.get("webhook_env") or "DISCORD_WEBHOOK_ONCO"
    title = cfg.get("title") or "がん薬物療法 学習"
    today = dt.date.today()
    dry = os.environ.get("DRY_RUN") == "1"
    review_day = is_review_day(cfg, today)
    # 資料が無い・資料と合わない（取り違え）テーマは捨てて、別のテーマでやり直す（max_tries 回まで）
    tries = int(cfg.get("max_tries", 3))
    for attempt in range(tries):
        topic = choose_review(cfg, state, rng, today) if review_day else None
        review_day = review_day and topic is not None   # 復習できるものが無ければ、ふつうの回に
        topic = topic or choose(cfg, state, rng)
        if not topic:
            print("未消化のテーマがありません")
            return
        print(f"今日のテーマ: {topic['category']}｜{topic['title']}")
        try:
            episode(cfg, topic, webhook, title)
            break
        except Exception as ex:   # noqa: BLE001
            print("学習番組を作れませんでした:", ex)
            discard = isinstance(ex, Mismatch) or "見つかりませんでした" in str(ex)
            if discard and not dry:
                if topic.get("review"):
                    state.setdefault("reviewed", []).append(topic["id"])
                else:
                    state["done"].append(topic["id"])
                    state.setdefault("discarded", []).append(topic["id"])
                save_state(SECTION, state)
            if discard and attempt + 1 < tries:
                review_day = False   # 復習が駄目なら、やり直しはふつうの回で
                continue
            if not dry:
                wait_until(cfg.get("post_at"))
                post_discord(webhook, username=title, avatar_url=cfg.get("avatar_url"),
                             content=f"📚 今日の学習番組は作れませんでした: {str(ex)[:300]}")
            raise SystemExit(1)
    if not dry:
        if topic.get("review"):
            state.setdefault("reviewed", []).append(topic["id"])
        else:
            state["done"].append(topic["id"])
            state.setdefault("history", []).append({"id": topic["id"], "date": today.isoformat()})
        state["last_seed"] = topic.get("seed")
        save_state(SECTION, state)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
