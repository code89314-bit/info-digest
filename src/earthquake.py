"""地震・津波情報をP2P地震情報 JSON API v2 から取得し、Discordに投稿する。

データ元: https://www.p2pquake.net/develop/json_api_v2/ （気象庁発表情報の中継）
"""
import sys

from common import http_get, load_config, load_state, post_discord, save_state, trim_seen

API = "https://api.p2pquake.net/v2/history?codes=551&codes=552&limit=30"

SCALE = {10: "1", 20: "2", 30: "3", 40: "4", 45: "5弱", 46: "5弱以上（推定）",
         50: "5強", 55: "6弱", 60: "6強", 70: "7"}
ISSUE_TYPE = {"ScalePrompt": "震度速報", "Destination": "震源に関する情報",
              "ScaleAndDestination": "震源・震度に関する情報", "DetailScale": "各地の震度に関する情報",
              "Foreign": "遠地地震に関する情報", "Other": "その他の情報"}
TSUNAMI = {"None": "この地震による津波の心配はありません", "Unknown": "津波の有無は不明",
           "Checking": "津波の有無を調査中", "NonEffective": "若干の海面変動（被害の心配なし）",
           "Watch": "⚠️ 津波注意報 発表中", "Warning": "🚨 津波警報等 発表中"}
GRADE = {"MajorWarning": "大津波警報", "Warning": "津波警報", "Watch": "津波注意報", "Unknown": "不明"}

# 震度に応じたembedの色
def _color(scale: int) -> int:
    if scale >= 55:
        return 0x8B0000
    if scale >= 45:
        return 0xE53935
    if scale >= 40:
        return 0xFB8C00
    if scale >= 30:
        return 0xFDD835
    return 0x90A4AE


def scale_label(v) -> str:
    return SCALE.get(v, "不明") if isinstance(v, int) and v > 0 else "不明"


def format_quake(item: dict) -> dict:
    eq = item.get("earthquake", {})
    hypo = eq.get("hypocenter", {}) or {}
    max_scale = eq.get("maxScale", -1)
    mag = hypo.get("magnitude", -1)
    depth = hypo.get("depth", -1)
    issue = item.get("issue", {})

    lines = [f"**発生時刻**: {eq.get('time', '不明')}"]
    if hypo.get("name"):
        lines.append(f"**震源**: {hypo['name']}")
    if isinstance(depth, (int, float)) and depth >= 0:
        lines.append(f"**深さ**: {'ごく浅い' if depth == 0 else f'約{int(depth)}km'}")
    if isinstance(mag, (int, float)) and mag >= 0:
        lines.append(f"**規模**: M{mag}")
    lines.append(f"**津波**: {TSUNAMI.get(eq.get('domesticTsunami'), '情報なし')}")

    # 震度の大きい地点を都道府県ごとにまとめる（上位のみ）
    points = item.get("points") or []
    by_scale: dict[int, list[str]] = {}
    for p in points:
        s = p.get("scale", -1)
        if s >= 30:
            by_scale.setdefault(s, []).append(p.get("addr", ""))
    for s in sorted(by_scale, reverse=True)[:4]:
        names = by_scale[s]
        shown = "、".join(names[:12]) + (f" ほか{len(names) - 12}地点" if len(names) > 12 else "")
        lines.append(f"**震度{scale_label(s)}**: {shown}")

    corr = "（訂正）" if issue.get("correct") not in (None, "None", "") else ""
    title = f"{'最大震度' + scale_label(max_scale) if max_scale > 0 else '地震情報'}" \
            f"｜{hypo.get('name') or '震源調査中'}{corr}"
    return {
        "title": title,
        "description": "\n".join(lines),
        "color": _color(max_scale),
        "footer": {"text": f"{ISSUE_TYPE.get(issue.get('type'), '地震情報')}｜気象庁（P2P地震情報経由）"},
    }


def format_tsunami(item: dict) -> dict:
    if item.get("cancelled"):
        return {"title": "津波予報 解除", "description": "津波予報（警報・注意報）はすべて解除されました。",
                "color": 0x43A047, "footer": {"text": "気象庁（P2P地震情報経由）"}}
    by_grade: dict[str, list[str]] = {}
    for a in item.get("areas") or []:
        name = a.get("name", "") + ("（直ちに来襲）" if a.get("immediate") else "")
        by_grade.setdefault(a.get("grade", "Unknown"), []).append(name)
    lines = []
    for g in ("MajorWarning", "Warning", "Watch", "Unknown"):
        if g in by_grade:
            lines.append(f"**{GRADE[g]}**: " + "、".join(by_grade[g]))
    top = next((g for g in ("MajorWarning", "Warning", "Watch") if g in by_grade), "Unknown")
    color = {"MajorWarning": 0x6A1B9A, "Warning": 0xD32F2F, "Watch": 0xFBC02D}.get(top, 0x90A4AE)
    return {"title": f"🌊 {GRADE[top]} 発表", "description": "\n".join(lines) or "詳細不明",
            "color": color, "footer": {"text": "気象庁（P2P地震情報経由）"}}


def should_post(item: dict, cfg: dict) -> bool:
    if item.get("code") == 552:
        return cfg.get("tsunami", True)
    eq = item.get("earthquake", {})
    issue_type = item.get("issue", {}).get("type")
    if issue_type == "Foreign":
        # 遠地地震は国内に津波の影響がある場合のみ
        return eq.get("domesticTsunami") in ("Watch", "Warning", "Checking")
    if eq.get("domesticTsunami") in ("Watch", "Warning"):
        return True
    return eq.get("maxScale", -1) >= cfg.get("min_scale", 30)


def main() -> None:
    cfg = load_config().get("earthquake") or {}
    state = load_state("earthquake")
    seen = state.get("seen", [])
    first_run = not state

    items = http_get(API).json()
    items = sorted(items, key=lambda x: x.get("time", ""))  # 古い順に投稿

    new_ids = []
    embeds = []
    for it in items:
        iid = it.get("id") or it.get("_id")
        if not iid or iid in seen:
            continue
        new_ids.append(iid)
        if first_run:
            continue  # 初回は既存分を既読にするだけ（過去分を大量投稿しない）
        if not should_post(it, cfg):
            continue
        embeds.append(format_tsunami(it) if it.get("code") == 552 else format_quake(it))

    if embeds:
        post_discord("DISCORD_WEBHOOK_EARTHQUAKE", embeds=embeds, username="地震・津波情報")
    print(f"新着 {len(new_ids)} 件 / 投稿 {len(embeds)} 件" + ("（初回：既読化のみ）" if first_run else ""))

    state["seen"] = trim_seen(seen + new_ids, 500)
    save_state("earthquake", state)


if __name__ == "__main__":
    sys.exit(main())
