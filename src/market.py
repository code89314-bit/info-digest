"""朝のニュースに「株式」の欄を添える（config.yaml の news.market。米国株・日本株）。

- 株価: Yahoo Finance のチャートAPI から、前営業日の終値と前日比（数字はLLMに作らせない）
- 原因・発表: Googleニュースなどの見出しを LLM に読ませ、S&P500の解釈・主な銘柄の値動きの原因・
  業績に影響しそうな発表をまとめさせる。根拠にした記事へのリンクを付ける
"""
import itertools
import json
import re
import time
import urllib.parse

import requests

from ai_digest import CHAT, LLMError, fetch_rss, ja_fix, llm_settings, strip_think

QUOTE_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{sym}?range=5d&interval=1d"
UA = {"User-Agent": "Mozilla/5.0 info-digest"}   # Yahoo は既定の UA だと 429 を返すことがある


def fetch_quote(sym: str) -> dict:
    """直近2営業日の終値から前日比を出す。{"close", "change"(%), "date"(現地の日付 mm/dd)}"""
    r = requests.get(QUOTE_URL.format(sym=urllib.parse.quote(sym)), headers=UA, timeout=20)
    r.raise_for_status()
    res = r.json()["chart"]["result"][0]
    off = res["meta"].get("gmtoffset", 0)
    rows = [(t, c) for t, c in zip(res["timestamp"], res["indicators"]["quote"][0]["close"]) if c is not None]
    (_, prev), (t, close) = rows[-2], rows[-1]
    return {"close": close, "change": (close / prev - 1) * 100, "date": time.strftime("%m/%d", time.gmtime(t + off))}


def fetch_quotes(quotes: list[dict]) -> list[dict]:
    out = []
    for q in quotes:
        try:
            out.append({**q, **fetch_quote(q["symbol"])})
        except Exception as ex:   # noqa: BLE001  1銘柄が取れなくても続ける
            print(f"⚠ 株価 {q['symbol']} 取得失敗: {ex}")
    return out


def collect_headlines(mk: dict) -> list[dict]:
    since = time.time() - mk.get("lookback_hours", 24) * 3600
    skip = mk.get("skip_titles") or []
    lists, titles = [], set()
    for src in (mk.get("sources") or []):
        try:
            got = fetch_rss(src, since)
        except Exception as ex:   # noqa: BLE001
            print(f"⚠ {src['name']} 取得失敗: {ex}")
            continue
        print(f"{src['name']}: {len(got)} 件")
        kept = []
        for it in got:
            key = re.sub(r"\s+", "", it["title"])
            if key in titles or any(s in it["title"] for s in skip):
                continue
            titles.add(key)
            kept.append(it)
        lists.append(kept)
    # 情報源から1件ずつ順に取る（先頭から詰めると、後ろの情報源＝日本株の見出しが上限で切れた 2026-10-30）
    out = [it for row in itertools.zip_longest(*lists) for it in row if it]
    return out[: int(mk.get("max_headlines", 80))]


PROMPT = """あなたは株式の市況を伝える記者です。読者: {audience}
【株価】（前営業日の終値と前日比。正確な数字。米国株と日本株は取引日が違うので［］の日付を見る）と
【記事の見出し】だけを根拠に、日本語で書いてください。

JSONのみを出力:
{{"sp500": "S&P500の値動きと、記事が伝える主な要因・市場の受け止め（2〜3文）", "sp500_refs": [見出し番号],
  "movers": [{{"name": "銘柄名（カタカナ）", "text": "値動きとその原因（1〜2文）", "refs": [見出し番号]}}],
  "releases": [{{"text": "業績に影響しそうな企業の発表（1文）", "refs": [見出し番号]}}]}}

- movers: 【株価】の銘柄のうち、前日比の大きいものや記事で話題になっているものを最大{max_movers}つ、値動きの大きい順に。
  原因は、その銘柄について見出しが直接伝えているもの（決算・発表・格付けなど）だけ。相場全体の動きや「注目が集まった」で代用しない。
  そうした見出しが無ければ、値動きのあとに「原因を伝える記事は見当たりません」と書き、refs は空
- releases: 【株価】の銘柄の企業による、新製品・提携・買収・設備投資・決算見通し・規制当局の判断など、業績に影響しそうな発表だけ。
  最大{max_releases}つ。株価の解説記事や投資のすすめ（「買うべき銘柄」など）は入れない。無ければ空
- 数字（終値・前日比）は【株価】の値を使う。見出しに無い数字・出来事を作らない。推測は見出しにある場合だけ「〜との見方」と書く
- 文末は「〜しました」「〜です」のです・ます調。値上がり・値下がりの向きは【株価】の符号と一致させる
- refs には根拠にした見出しの番号を1〜2個

【株価】
{quotes}

【記事の見出し】
{headlines}"""


def fmt_quote(q: dict) -> str:
    return f"{q['name']} {q['close']:,.2f}（{q['change']:+.2f}%）"


def market_of(q: dict) -> str:
    """Yahoo の記号の末尾 .T は東証。それ以外は米国として扱う。"""
    return "日本" if q["symbol"].upper().endswith(".T") else "米国"


def short_names(q: dict) -> list[str]:
    """銘柄の呼び方（「楽天グループ」→「楽天」も。aliases があればそれも）。"""
    name = q["name"]
    short = re.sub(r"(グループ|ホールディングス)$", "", name)
    return list(dict.fromkeys([name, short, *(q.get("aliases") or [])]))


def close_dates(quotes: list[dict]) -> str:
    """見出しの日付。市場が1つなら「10/29」、日米が混ざれば「米国 10/29・日本 10/30」。"""
    by = {}
    for q in quotes:
        by.setdefault(market_of(q), q["date"])
    return "・".join(f"{m} {d}" for m, d in by.items()) if len(by) > 1 else quotes[0]["date"]


def parse_json(text: str) -> dict | None:
    m = re.search(r"\{.*\}", strip_think(text), flags=re.S)
    if not m:
        return None
    try:
        data = json.loads(m.group(0))
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


def write_report(quotes: list[dict], heads: list[dict], mk: dict, cfg: dict) -> dict | None:
    llm = llm_settings(cfg)
    chat = CHAT.get(llm["provider"])
    if not chat or not quotes:
        return None
    prompt = PROMPT.format(audience=mk.get("audience") or "個人投資家", max_movers=mk.get("max_movers", 5),
                           max_releases=mk.get("max_releases", 4),
                           quotes="\n".join(f"- {fmt_quote(q)}［{market_of(q)} {q['date']} 終値］" for q in quotes),
                           headlines="\n".join(f"[{i}] {h['title']}" for i, h in enumerate(heads)) or "（なし）")
    for attempt in range(2):
        try:
            data = parse_json(chat(prompt, llm))
        except LLMError as ex:
            print("米国株の欄を書けませんでした:", ex)
            return None
        if data is not None:
            return data
        print(f"米国株の欄: LLMの出力を解釈できませんでした（{attempt + 1}回目）")
    return None


def links(refs, heads: list[dict]) -> str:
    out = []
    for r in (refs if isinstance(refs, list) else [])[:1]:   # Googleニュースのリンクは長いので1つだけ
        try:
            h = heads[int(r)]
        except (ValueError, IndexError, TypeError):
            continue
        site = h["title"].rsplit(" - ", 1)[-1] if " - " in h["title"] else "記事"
        out.append(f"[{site.replace('[', '［').replace(']', '］')}]({h['link']})")
    return f"（{'・'.join(out)}）" if out else ""


def build_embed(cfg: dict) -> dict | None:
    """news.market の欄（embed 1つ）。株価が1つも取れなければ None。"""
    mk = cfg.get("market") or {}
    quotes = fetch_quotes(mk.get("quotes") or [])
    if not quotes:
        return None
    heads = collect_headlines(mk)
    print(f"株式: 株価 {len(quotes)} 銘柄、見出し {len(heads)} 件")
    rep = write_report(quotes, heads, mk, cfg) or {}
    index, stocks = quotes[0], quotes[1:]   # quotes の先頭は指数（S&P500）
    body = f"**{fmt_quote(index)}**\n"
    if rep.get("sp500"):
        body += ja_fix(str(rep["sp500"])) + links(rep.get("sp500_refs"), heads) + "\n"
    if stocks:
        ranked = sorted(stocks, key=lambda q: -abs(q["change"]))
        body += "\n" + "｜".join(f"{q['name']} {q['change']:+.1f}%" for q in ranked) + "\n"
    movers = [m for m in (rep.get("movers") or []) if isinstance(m, dict) and m.get("text")]
    if movers:
        body += "\n**主な銘柄の値動きと原因**\n" + "".join(
            f"• **{m.get('name', '')}**：{ja_fix(str(m['text']))}{links(m.get('refs'), heads)}\n" for m in movers)
    # 発表は【株価】の銘柄の企業のものだけ（LLM がインテル・アマゾンなど対象外の企業の発表を入れた 2026-10-30）
    rels = [r for r in (rep.get("releases") or []) if isinstance(r, dict) and r.get("text")
            and any(n in str(r["text"]) for q in stocks for n in short_names(q))]
    if rels:
        body += "\n**業績に影響しそうな発表**\n" + "".join(
            f"• {ja_fix(str(r['text']))}{links(r.get('refs'), heads)}\n" for r in rels)
    if not rep:
        body += "\n-# 解説はLLMが使えなかったため省略しました"
    # 3000字に収める（Googleニュースのリンクは1本250字ほどある。長いと他の欄と合わせて
    # Discord の1通の上限を超え 413 になった 2026-10-30）。行の途中（リンクの途中）で切らない
    desc = ""
    for line in body.strip().split("\n"):
        if len(desc) + len(line) + 1 > 3000:
            break
        desc += line + "\n"
    return {"title": f"📈 株式（{close_dates(quotes)} 終値）", "description": desc.strip(), "color": 0x1E88E5}
