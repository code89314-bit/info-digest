"""朝のニュースに「沿線の情報」の欄を添える（config.yaml の news.local。いまは JR横浜線 町田〜橋本）。

- 集める: Googleニュースの検索RSS などの見出し（直近 lookback_days 日）
- 選ぶ・読む: LLM が見出しから「申込・エントリーが要るもの」「イベント」「新しいお店」を選び、見出しにある日付・締切を抜き出す
- 覚える: 見つけたものは .local/state（load_state）に残し、締切・開催日が近づいたら毎朝「⏰ もうすぐ」に出す（忘れないように）
日付は見出しに書いてあるものだけ（LLM に作らせない）。
"""
import datetime as dt
import json
import re
import time

from ai_digest import CHAT, LLMError, fetch_rss, ja_fix, llm_settings, strip_think
from common import load_state, save_state

STATE = "local_events"
KINDS = {"entry": "✍️ 申込・エントリーが必要", "event": "🎪 イベント", "opening": "🍽️ 新しいお店・施設"}

PROMPT = """あなたは地域情報の編集者です。読者: {audience}
対象の地域: {area}

【見出し】から、読者に役立つものを選んで JSON にしてください。選ぶもの:
- entry: 申込・予約・抽選エントリー・応募が必要なもの（イベントの事前申込、人気商品の抽選販売など）。
  見出しに「申込」「予約」「抽選」「エントリー」「応募」「募集」などの言葉があるものだけ。無ければ event か opening にする
- event: 対象の地域で開かれるイベント（祭り・展示・公開・体験・マルシェなど）
- opening: 対象の地域に新しく開店・開業する（した）お店・施設
{extra}
選ばないもの: {exclude}
対象の地域の外の出来事は選ばない（例外: 地域を問わない全国のものは、上で「全国でも」と書いたものだけ）。

すでに知らせたもの（同じ出来事なら選ばない）:
{known}

- name: 何か分かる短い名前（30字以内。店名・イベント名を入れる）
- place: 場所（駅名・施設名。見出しに無ければ ""）
- date: 開催日・開店日（"MM-DD"。期間なら開始日。見出しに書いてある日付だけ。無ければ ""）
- date_end: 期間の終わり（"MM-DD" か ""）
- deadline: 申込・エントリーの締切（"MM-DD"。見出しに書いてあるものだけ。無ければ ""）
- entry_start: 申込・エントリーの開始日（"MM-DD" か ""）
- note: 補足（1文。見出しにあることだけ。「要申込」「先着」「無料」など）
- ref: 根拠の見出し番号
日付を推測で埋めない。曜日は書かない（こちらで付ける）。最大{max_items}件。
選ぶ順: entry をすべて → 開催日の近い event → opening（開店日が近いもの・最近のもの）。

JSONのみ: {{"items": [{{"kind": "entry|event|opening", "name": "...", "place": "...", "date": "", "date_end": "",
"deadline": "", "entry_start": "", "note": "...", "ref": 0}}]}}

【見出し】
{headlines}"""


def collect(lc: dict) -> list[dict]:
    since = time.time() - float(lc.get("lookback_days", 7)) * 86400
    skip = lc.get("skip_titles") or []
    out, keys = [], set()
    for src in lc.get("sources") or []:
        try:
            got = fetch_rss(src, since)
        except Exception as ex:   # noqa: BLE001
            print(f"⚠ {src['name']} 取得失敗: {ex}")
            continue
        print(f"{src['name']}: {len(got)} 件")
        for it in got:
            key = title_key(it["title"])
            if key in keys or any(s in it["title"] for s in skip):
                continue
            keys.add(key)
            out.append(it)
    return out


def title_key(title: str) -> str:
    """見出しの同一判定用（媒体名「 - タウンニュース」と空白を除く）。"""
    return re.sub(r"\s+", "", title.rsplit(" - ", 1)[0])


def to_date(mmdd: str, today: dt.date) -> dt.date | None:
    """"MM-DD" を日付に。年は書かれないので、今日から見て2か月以上前なら来年とみなす。"""
    m = re.fullmatch(r"(\d{1,2})-(\d{1,2})", (mmdd or "").strip())
    if not m:
        return None
    try:
        d = dt.date(today.year, int(m.group(1)), int(m.group(2)))
    except ValueError:
        return None
    return d.replace(year=d.year + 1) if (today - d).days > 60 else d


def fmt_day(d: dt.date | None) -> str:
    return f"{d.month}/{d.day}({'月火水木金土日'[d.weekday()]})" if d else ""


def extract(heads: list[dict], known: list[str], lc: dict, cfg: dict) -> list[dict]:
    llm = llm_settings(cfg)
    chat = CHAT.get(llm["provider"])
    if not chat or not heads:
        return []
    prompt = PROMPT.format(audience=lc.get("audience") or "沿線に住む社会人", area=lc.get("area") or "",
                           extra=(lc.get("extra") or "").strip(), exclude=(lc.get("exclude") or "").strip(),
                           known="\n".join(f"- {k}" for k in known[-80:]) or "（なし）",
                           max_items=int(lc.get("max_items_per_batch", 10)),
                           headlines="\n".join(f"[{i}] {h['title']}" for i, h in enumerate(heads)))
    for attempt in range(2):
        try:
            text = strip_think(chat(prompt, llm))
        except LLMError as ex:
            print("沿線の欄を書けませんでした:", ex)
            return []
        m = re.search(r"\{.*\}", text, flags=re.S)
        try:
            data = json.loads(m.group(0)) if m else None
        except json.JSONDecodeError:
            data = None
        if isinstance(data, dict):
            return [x for x in data.get("items") or [] if isinstance(x, dict) and x.get("kind") in KINDS and x.get("name")]
        print(f"沿線の欄: LLMの出力を解釈できませんでした（{attempt + 1}回目）")
    return []


def remember(found: list[dict], heads: list[dict], state: dict, today: dt.date) -> list[dict]:
    """見つけたものを覚える。新しく覚えたものを返す。"""
    items = state.setdefault("items", [])
    new = []
    for f in found:
        try:
            h = heads[int(f.get("ref"))]
        except (ValueError, IndexError, TypeError):
            continue
        if same_as_any(str(f["name"]), [i["name"] for i in items]):
            continue
        if GUESS.search(str(f.get("note") or "")):   # 「〜の可能性あり（詳細不明）」のような推測は載せない
            continue
        rec = {"kind": f["kind"], "name": ja_fix(str(f["name"]))[:40], "place": str(f.get("place") or "")[:30],
               "note": ja_fix(str(f.get("note") or ""))[:80], "link": h["link"],
               "site": h["title"].rsplit(" - ", 1)[-1] if " - " in h["title"] else h.get("source") or "記事",
               "found": today.isoformat()}
        for k in ("date", "date_end", "deadline", "entry_start"):
            d = to_date(str(f.get(k) or ""), today)
            rec[k] = d.isoformat() if d else ""
        dl, end = _d(rec, "deadline"), _d(rec, "date_end") or _d(rec, "date")
        if (dl and dl < today) or (rec["kind"] == "event" and end and end < today):   # 締切・開催が終わったもの
            continue
        items.append(rec)
        new.append(rec)
    return new


GUESS = re.compile(r"可能性|詳細不明|不明|と思われ|かもしれ")


def norm_name(name: str) -> str:
    """名前の同一判定用: 空白・記号・「抽選予約」などの添え字を除く。"""
    s = re.sub(r"[\s「」『』【】()（）・!！?？、。~〜\-]", "", name)
    return re.sub(r"(抽選予約|抽選販売|抽選|予約|開催|オープン|開店|のお知らせ)$", "", s)


def same_as_any(name: str, others: list[str]) -> bool:
    """同じ出来事らしい名前がもうあるか（片方がもう片方を含む・よく似ている）。"""
    import difflib
    a = norm_name(name)
    for o in others:
        b = norm_name(o)
        if a and b and (a in b or b in a or difflib.SequenceMatcher(None, a, b).ratio() >= 0.7):
            return True
    return False


def prioritize(recs: list[dict], today: dt.date) -> list[dict]:
    """申込が要るもの → イベント（開催日の近い順）→ お店。日付の無いものは各分類の後ろ。"""
    order = list(KINDS)

    def key(r):
        d = _d(r, "deadline") or _d(r, "entry_start") or _d(r, "date")
        return (order.index(r["kind"]), d is None, abs((d - today).days) if d else 0)
    return sorted(recs, key=key)


def _d(rec: dict, key: str) -> dt.date | None:
    return dt.date.fromisoformat(rec[key]) if rec.get(key) else None


def line(rec: dict) -> str:
    parts = []
    dl, st, d0, d1 = _d(rec, "deadline"), _d(rec, "entry_start"), _d(rec, "date"), _d(rec, "date_end")
    if st:
        parts.append(f"受付 {fmt_day(st)}〜" + (fmt_day(dl) if dl else ""))
    elif dl:
        parts.append(f"締切 {fmt_day(dl)}")
    if d0 and d0 not in (st, dl):
        parts.append(fmt_day(d0) + (f"〜{fmt_day(d1)}" if d1 and d1 != d0 else ""))
    when = f"【{' / '.join(parts)}】" if parts else ""
    place = f"＠{rec['place']}" if rec.get("place") else ""
    note = f"（{rec['note']}）" if rec.get("note") else ""
    site = rec["site"].replace("[", "［").replace("]", "］")
    return f"• {when}**{rec['name']}**{place}{note} [{site}]({rec['link']})"


def soon(items: list[dict], today: dt.date, days: int) -> list[tuple[dt.date, str, dict]]:
    """締切・受付開始・開催日が今日から days 日以内のもの（覚えているものから）。"""
    out = []
    for r in items:
        for key, label in (("entry_start", "受付開始"), ("deadline", "締切"), ("date", "開催")):
            d = _d(r, key)
            if d and 0 <= (d - today).days <= days:
                out.append((d, label, r))
                break
    return sorted(out, key=lambda x: x[0])


def build_embeds(cfg: dict, today: dt.date | None = None) -> list[dict]:
    """news.local の欄（もうすぐ／申込／イベント／お店。中身のある分類だけ）。"""
    lc = cfg.get("local") or {}
    today = today or dt.date.today()
    state = load_state(STATE)
    heads = [h for h in collect(lc) if title_key(h["title"]) not in set(state.get("seen") or [])]
    known = [i["name"] for i in state.get("items") or []]
    # 見出しは batch 件ずつ読ませる（226件を一度に渡すと、先頭の情報源の見出しからしか選ばなかった 2026-10-30）
    heads = heads[: int(lc.get("max_headlines", 150))]
    size, new = int(lc.get("batch", 50)), []
    for i in range(0, len(heads), size):
        part = heads[i:i + size]
        new += remember(extract(part, known + [r["name"] for r in new], lc, cfg), part, state, today)
    # 載せるのは分類ごとに max_per_kind 件まで。あふれたものも覚えておき、日付が近づいたら「⏰ もうすぐ」に出る
    caps = {"entry": 10, "event": 10, "opening": 8, **(lc.get("max_per_kind") or {})}
    new = [r for k in KINDS for r in prioritize([r for r in new if r["kind"] == k], today)[: int(caps[k])]]
    # 終わったもの（締切も開催日も過ぎた・30日より前に見つけて日付の無いもの）は忘れる
    def alive(r):
        ds = [d for d in (_d(r, "deadline"), _d(r, "date_end") or _d(r, "date")) if d]
        return any(d >= today for d in ds) if ds else (today - dt.date.fromisoformat(r["found"])).days <= 30
    state["items"] = [r for r in state.get("items") or [] if alive(r)]
    state["seen"] = (list(state.get("seen") or []) + [title_key(h["title"]) for h in heads])[-3000:]
    save_state(STATE, state)
    print(f"沿線: 見出し {len(heads)} 件 → 新しく {len(new)} 件")

    # 欄は分類ごとに分ける（Googleニュースのリンクは1本250字ほどあり、1つの欄の上限 4096字に収まらない）
    ids = {id(r) for r in new}
    near = [(d, lab, r) for d, lab, r in soon(state["items"], today, int(lc.get("remind_days", 7))) if id(r) not in ids]
    sections = [("⏰ もうすぐ", [f"• {fmt_day(d)} {lab}：**{r['name']}**" + (f"（{r['note']}）" if r.get("note") else "")
                                 + f" [{r['site']}]({r['link']})" for d, lab, r in near])]
    sections += [(label, [line(r) for r in new if r["kind"] == kind]) for kind, label in KINDS.items()]
    title, embeds = lc.get("title") or "📍 沿線の情報", []
    for label, rows in sections:
        desc = ""
        for ln in rows:   # 行の途中（リンクの途中）で切らない
            if len(desc) + len(ln) + 1 > int(lc.get("max_chars", 3800)):
                break
            desc += ln + "\n"
        if desc:
            embeds.append({"title": f"{title}｜{label}", "description": desc.strip(), "color": 0xF06292})
    return embeds
