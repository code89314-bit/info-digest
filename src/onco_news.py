"""がん薬物療法ニュース（週単位・曜日テーマ）。

毎日: 新着を集めて Strata が選別・分類し、週間アーカイブ（state/onco_archive.json）にためる。投稿はしない。
      ただし急ぎの安全性情報（urgent）だけは、その日のうちにテキストで投稿する。
曜日: 直近1週間のアーカイブから、その曜日のテーマ（config.yaml の onco.schedule）に合う記事を NotebookLM に読ませ、
      トピックを抽出したまとめ＋各記事の要約を投稿し、同じノートで podcast を作って添付する。
      記事が少ない週（episode.min_items 未満）のテーマは作らず、土曜の週間まとめに任せる。

使い方: python src/onco_news.py            収集 → 今日の曜日の回
        python src/onco_news.py --collect  収集だけ
        python src/onco_news.py --episode mon   収集せず、指定した曜日の回だけ作る（確認用）
"""
import datetime as dt
import os
import sys
import time

import ai_digest
from ai_digest import (build_embeds, collect, get_tags, llm_settings, select_by_keywords, select_with_llm,
                       summarize_with_notebooklm, write_overview)
from common import load_state, post_discord, save_state, trim_seen

SECTION = "onco"
DAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
JA_DAY = dict(zip(DAYS, "月火水木金土日"))


def now_jst() -> dt.datetime:
    return dt.datetime.utcfromtimestamp(time.time() + 9 * 3600)


def collect_into_archive(cfg: dict, webhook: str, title: str) -> None:
    """新着を選別して週間アーカイブにためる。急ぎの安全性情報だけ即日投稿する。"""
    state = load_state(SECTION)
    seen = state.get("seen", [])
    items = collect(cfg, set(seen))
    print(f"候補 {len(items)} 件")
    picked = []
    if items:
        llm = llm_settings(cfg)
        picked = select_with_llm(items, cfg)
        if picked is None:
            picked = select_by_keywords(items, cfg)
            print(f"キーワード一致（LLM未設定またはエラー）: {len(picked)} 件")
        else:
            print(f"LLM選定（{llm['provider']}: {llm.get('model')}）: {len(picked)} 件")

    today = now_jst().date().isoformat()
    archive = load_state(f"{SECTION}_archive").get("items", [])
    have = {a["id"] for a in archive}
    keep = (now_jst().date() - dt.timedelta(days=int(cfg.get("archive_days", 21)))).isoformat()
    fields = ("id", "title", "link", "source", "summary", "tag", "score", "title_ja", "summary_ja")
    archive = [a for a in archive if a.get("date", today) >= keep] + \
        [{**{k: p.get(k) for k in fields}, "date": today} for p in picked if p["id"] not in have]
    save_state(f"{SECTION}_archive", {"items": archive})

    urgent = cfg.get("urgent") or {}
    hot = [p for p in picked if urgent.get("enabled") and p["tag"] in (urgent.get("tags") or [])
           and float(p.get("score", 0)) >= float(urgent.get("min_score", 4))]
    if hot:
        content = f"⚠️ **{title}｜安全性情報（速報）**"
        if cfg.get("notice"):
            content += f"\n-# {cfg['notice'].strip()}"
        post_discord(webhook, username=title, content=content, embeds=build_embeds(hot, get_tags(cfg)))
        print(f"安全性情報を速報しました: {len(hot)} 件")

    state["seen"] = trim_seen(seen + [it["id"] for it in items], 5000)
    save_state(SECTION, state)
    print(f"アーカイブ: {len(archive)} 件（今日 +{len([p for p in picked if p['id'] not in have])}）")


def episode_items(cfg: dict, day: str) -> tuple[dict, list[dict]]:
    """その曜日のテーマと、直近 lookback_days 日のアーカイブから選んだ記事（スコア順）。"""
    ep = (cfg.get("schedule") or {}).get(day)
    if not ep:
        return {}, []
    opts = cfg.get("episode") or {}
    since = (now_jst().date() - dt.timedelta(days=int(opts.get("lookback_days", 7)))).isoformat()
    tags = ep.get("tags") or []
    items = [a for a in load_state(f"{SECTION}_archive").get("items", [])
             if a.get("date", "") >= since and (not tags or a.get("tag") in tags)]
    items.sort(key=lambda a: -float(a.get("score") or 0))
    return ep, items[: int(ep.get("max_items") or opts.get("max_items", 15))]


def make_episode(cfg: dict, day: str, webhook: str, title: str) -> None:
    ep, items = episode_items(cfg, day)
    if not ep:
        print(f"{JA_DAY[day]}曜日は休み")
        return
    theme = ep.get("theme") or day
    min_items = int((cfg.get("episode") or {}).get("min_items", 2))
    if len(items) < min_items:
        print(f"{JA_DAY[day]}曜「{theme}」は今週 {len(items)} 件のため作りません（週間まとめに任せます）")
        return
    print(f"{JA_DAY[day]}曜「{theme}」: {len(items)} 件")

    # 動向まとめは「このテーマのトピックを抽出してまとめる」指示にする
    ov = dict(cfg.get("overview") or {})
    ov["enabled"] = True
    ov["group_by"] = ep.get("group_by") or ov.get("group_by") or "トピック"
    ov["rules"] = list(ov.get("rules") or []) + [f"テーマ「{theme}」の観点で、直近1週間の主なトピックを抽出してまとめる"] + \
        list(ep.get("rules") or [])
    ov["max_chars"] = ep.get("max_chars") or ov.get("max_chars", 1200)
    ecfg = {**cfg, "overview": ov}
    pod = {**(cfg.get("podcast") or {}), "title": f"{title} {theme}",
           "instructions": ((cfg.get("podcast") or {}).get("instructions") or "").strip() + "\n" +
           f"今回のテーマは「{theme}」。" + (ep.get("instructions") or "").strip(),
           "label": f"{JA_DAY[day]}曜「{theme}」podcast"}
    for k in ("format", "length"):
        if ep.get(k):
            pod[k] = ep[k]

    notebook, overview = None, None
    if pod.get("enabled") and os.environ.get("DRY_RUN") != "1":
        import podcast_nlm
        try:
            notebook = podcast_nlm.Notebook(pod, items)
            items, overview = summarize_with_notebooklm(notebook, items, ecfg)
        except Exception as ex:   # noqa: BLE001  NotebookLM が使えなければ Strata の要約で投稿
            print("NotebookLM を使えませんでした（Strataの要約で投稿します）:", ex)
            if notebook:
                notebook.close()
            notebook = None
    overview = overview or write_overview(items, ecfg)
    embeds = build_embeds(items, get_tags(cfg))
    if overview:
        embeds.insert(0, {"title": f"📋 今週の「{theme}」トピックス", "description": overview, "color": 0x5C6BC0})
    content = f"{cfg.get('icon') or '💊'} **{title}｜{JA_DAY[day]}曜：{theme}**（直近1週間 {len(items)}件）"
    if cfg.get("notice"):
        content += f"\n-# {cfg['notice'].strip()}"
    try:
        post_discord(webhook, username=title, content=content, embeds=embeds)
    except BaseException:
        if notebook:
            notebook.close()
        raise
    if pod.get("enabled"):
        import podcast_nlm
        podcast_nlm.run(items, pod, webhook=webhook, username=title, notebook=notebook)


def main(argv: list[str]) -> None:
    cfg = ai_digest.section_config(SECTION)
    if not cfg:
        raise SystemExit(f"config.yaml に {SECTION}: がありません")
    webhook = cfg.get("webhook_env") or "DISCORD_WEBHOOK_ONCO"
    title = cfg.get("title") or "がん薬物療法ニュース"
    if "--episode" in argv:
        make_episode(cfg, argv[argv.index("--episode") + 1], webhook, title)
        return
    collect_into_archive(cfg, webhook, title)
    if "--collect" not in argv:
        make_episode(cfg, DAYS[now_jst().weekday()], webhook, title)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
