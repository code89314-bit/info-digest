"""厚労省「医療用医薬品供給状況報告」の更新を検知し、変更点をDiscordに投稿する。

ページ: https://www.mhlw.go.jp/stf/seisakunitsuite/bunya/kenkou_iryou/iryou/kouhatu-iyaku/04_00003.html
最新のExcel一覧で「今回掲載時の更新有無」= New の品目だけを拾う。
"""
import io
import re
import sys
from urllib.parse import urljoin

import openpyxl

from common import http_get, load_config, load_state, post_discord, save_state

PAGE = "https://www.mhlw.go.jp/stf/seisakunitsuite/bunya/kenkou_iryou/iryou/kouhatu-iyaku/04_00003.html"
SYSTEM_URL = "https://iyakuhin-kyokyu.mhlw.go.jp/public/supply-status-list"


def find_latest_xlsx(html: str) -> tuple[str, str] | None:
    """ページ内のExcelリンク（URL, リンク文字列）を返す。"""
    links = re.findall(r'<a[^>]+href="([^"]+\.xlsx)"[^>]*>(.*?)</a>', html, flags=re.S | re.I)
    if not links:
        return None
    # 供給状況のファイル名を優先
    links.sort(key=lambda x: "kyoukyu" not in x[0])
    href, text = links[0]
    text = re.sub(r"<[^>]+>", "", text).strip()
    return urljoin(PAGE, href), text


def _norm(v) -> str:
    return re.sub(r"\s+", "", str(v)) if v is not None else ""


def parse_xlsx(data: bytes) -> list[dict]:
    """ヘッダー行を自動検出し、New の行を辞書のリストで返す。"""
    wb = openpyxl.load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    results = []
    for ws in wb.worksheets:
        rows = ws.iter_rows(values_only=True)
        header = None
        for _ in range(30):  # 先頭30行以内からヘッダーを探す
            try:
                row = next(rows)
            except StopIteration:
                break
            if any("更新有無" in _norm(c) for c in row):
                header = [_norm(c) for c in row]
                break
        if not header:
            continue

        def col(*keys, exclude=()):
            for i, h in enumerate(header):
                if any(k in h for k in keys) and not any(e in h for e in exclude):
                    return i
            return None

        c_new = col("更新有無")
        c_ing = col("成分名")
        c_name = col("品名", "販売名", exclude=("成分",))
        c_spec = col("規格")
        c_maker = col("製造販売業者", "製造販売会社", "会社名")
        c_ship = col("出荷対応")
        c_vol = col("出荷量", exclude=("見込", "予定"))
        c_eta = col("解除見込", "改善見込", "見込み時期")

        def g(row, i):
            return str(row[i]).strip() if i is not None and i < len(row) and row[i] is not None else ""

        for row in rows:
            if "new" not in g(row, c_new).lower():
                continue
            results.append({
                "ingredient": g(row, c_ing), "name": g(row, c_name), "spec": g(row, c_spec),
                "maker": g(row, c_maker), "ship": g(row, c_ship), "volume": g(row, c_vol),
                "eta": g(row, c_eta),
            })
        if results:
            break
    return results


def classify(ship: str) -> str:
    if "供給停止" in ship:
        return "stop"
    if "限定出荷" in ship:
        return "limited"
    if "通常出荷" in ship:
        return "normal"
    return "other"


def line(it: dict) -> str:
    name = it["name"] or it["ingredient"]
    maker = f"（{it['maker']}）" if it["maker"] else ""
    ship = re.sub(r"^[①-⑤➀-➄\d\.\s]+", "", it["ship"]) or "不明"
    eta = f"／見込み: {it['eta']}" if it["eta"] and it["eta"] not in ("-", "－") else ""
    return f"・{name}{maker} → {ship}{eta}"


def build_embeds(items: list[dict], label: str, url: str, cfg: dict) -> list[dict]:
    max_items = cfg.get("max_items_per_section", 25)
    watch = [w for w in cfg.get("watchlist", []) if w]
    groups = {"stop": [], "limited": [], "normal": [], "other": []}
    for it in items:
        groups[classify(it["ship"])].append(it)

    summary = (f"更新品目: **{len(items)}件**\n"
               f"🔴 供給停止 {len(groups['stop'])}｜🟠 限定出荷 {len(groups['limited'])}｜"
               f"🟢 通常出荷 {len(groups['normal'])}｜その他 {len(groups['other'])}\n"
               f"[Excel一覧]({url})｜[検索システム]({SYSTEM_URL})")
    embeds = [{"title": f"💊 {label}", "description": summary, "color": 0x1E88E5}]

    if watch:
        hits = [it for it in items
                if any(w in it["ingredient"] or w in it["name"] for w in watch)]
        if hits:
            embeds.append({"title": f"⭐ ウォッチリスト該当 {len(hits)}件",
                           "description": "\n".join(line(i) for i in hits[:max_items * 2]),
                           "color": 0x8E24AA})

    sections = [("stop", "🔴 供給停止", 0xD32F2F), ("limited", "🟠 限定出荷", 0xFB8C00),
                ("normal", "🟢 通常出荷に変更", 0x43A047)]
    for key, title, color in sections:
        lst = groups[key]
        if not lst:
            continue
        body = "\n".join(line(i) for i in lst[:max_items])
        if len(lst) > max_items:
            body += f"\n…ほか{len(lst) - max_items}件（Excel参照）"
        embeds.append({"title": f"{title}（{len(lst)}件）", "description": body, "color": color})
    return embeds


def main() -> None:
    cfg = load_config().get("drug_supply", {})
    state = load_state("drug_supply")

    html = http_get(PAGE).content.decode("utf-8", errors="replace")
    found = find_latest_xlsx(html)
    if not found:
        print("Excelリンクが見つかりません（ページ構成が変わった可能性）")
        return 1
    url, label = found
    if url == state.get("last_url"):
        print("更新なし:", label)
        return

    items = parse_xlsx(http_get(url, timeout=120).content)
    print(f"{label}: New {len(items)} 件")
    if items:
        post_discord("DISCORD_WEBHOOK_DRUG", embeds=build_embeds(items, label, url, cfg),
                     username="医薬品供給情報")
    else:
        post_discord("DISCORD_WEBHOOK_DRUG", username="医薬品供給情報",
                     content=f"💊 {label} が公開されました（New表示の品目はありません）\n<{url}>")

    state["last_url"] = url
    state["last_label"] = label
    save_state("drug_supply", state)


if __name__ == "__main__":
    sys.exit(main())
