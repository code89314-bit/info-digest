"""しらべの専門ノート（NotebookLM）。設定は style/nlm_notebooks.yaml、作ったノートのIDは .local/nlm_notebooks.json。

- build: ノートを作り（あればそれに足す）、カスタム指示・「役割と答え方」の資料・URL・ファイルを入れ、
         ウェブ調査で資料を集めて、一次資料でないものは消す
- route: 質問がどのノートの分野かを Strata に選ばせる（薬の名前は扱わせない。分野を選ぶだけ）
- ask:   ノートに聞く。資料に答えが無ければ None（呼び出し側は、その場のウェブ調査に回す）

使い方: .venv\\Scripts\\python.exe src\\nlm_notebooks.py build hoken   （list でノートと資料の数）
"""
import json
import re
import sys
from urllib.parse import urlparse

import yaml

from common import ROOT, load_config

STYLE = ROOT / "style" / "nlm_notebooks.yaml"
IDS = ROOT / ".local" / "nlm_notebooks.json"
NO_SOURCE = "【資料なし】"   # ノートの資料に答えが無いとき、1行目に書かせる印

ROUTE_PROMPT = """次の質問は、下のどのノート（分野）で調べるのがよいですか。
分野がはっきり合うときだけ選び、どれにも当てはまらない・迷うときは "none"。

質問:
{question}

ノート:
{notebooks}

JSONのみ: {{"notebook": "キー または none"}}"""

ASK_PROMPT = """{question}
{context}
（このノートの資料に、この質問への答えがまったく書かれていないときは、1行目に「{mark}」とだけ書いて終える）"""


def load_style() -> dict:
    return yaml.safe_load(STYLE.read_text(encoding="utf-8")) or {}


def notebooks() -> dict:
    return load_style().get("notebooks") or {}


def ids() -> dict:
    try:
        return json.loads(IDS.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def save_id(key: str, nb_id: str) -> None:
    data = ids()
    data[key] = nb_id
    IDS.parent.mkdir(parents=True, exist_ok=True)
    IDS.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def pod_settings(extra: dict | None = None) -> dict:
    """CLI の場所・認証の設定は onco.podcast と共通。"""
    return {**((load_config().get("onco") or {}).get("podcast") or {}), **(extra or {})}


def role_text(nb: dict, rules: str) -> str:
    return (nb.get("persona") or "").strip() + "\n" + (rules or "").strip()


def build(key: str, *, research: bool | set = True) -> str:
    """ノートを作って資料を入れる（作ってあれば足すだけ）。ノートのIDを返す。"""
    import podcast_nlm
    import qa_deep
    style = load_style()
    nb = (style.get("notebooks") or {})[key]
    pod = pod_settings({"title": nb["name"]})
    nb_id = ids().get(key)
    if nb_id:
        book = podcast_nlm.Notebook.attach(pod, nb_id)
    else:
        book = podcast_nlm.Notebook(pod, [])
        podcast_nlm._cli(pod, ["rename", "-n", book.nb, nb["name"]], parse_json=False, check=False)
        save_id(key, book.nb)
        print(f"ノートを作りました: {nb['name']} ({book.nb})")
    role = role_text(nb, style.get("common_rules"))
    podcast_nlm._cli(pod, ["configure", "-n", book.nb, "--persona", role], parse_json=False)
    # 「役割と答え方」の資料は毎回入れ直す（題名の指定が効かず、ファイル名 note.md になることがある）
    podcast_nlm._cli(pod, ["source", "delete-by-title", "note.md", "-n", book.nb, "-y"], parse_json=False, check=False)
    podcast_nlm._cli(pod, ["source", "delete-by-title", "役割と答え方", "-n", book.nb, "-y"], parse_json=False, check=False)
    book.add_text("役割と答え方", f"# {nb['name']}：役割と答え方\n\n{role}\n")
    urls = [u for u in nb.get("urls") or [] if u not in {s["url"] for s in book.sources()}]
    if urls:
        print(f"URL の資料: {len(book.add_urls(urls))}/{len(urls)} 件")
    for f in nb.get("files") or []:
        podcast_nlm._cli(pod, ["source", "add", f, "-n", book.nb, "--json"], check=False)
    prompts = nb.get("research") or []
    for i, prompt in enumerate([prompts] if isinstance(prompts, str) else prompts if research else []):
        if research is not True and i not in research:   # build(..., research={2, 3}) で一部だけやり直す
            continue
        before = {s["url"] for s in book.sources()}
        try:
            book.research(prompt, mode=nb.get("research_mode", "fast"))
        except Exception as ex:   # noqa: BLE001  1つ失敗しても次へ
            print(f"ウェブ調査 {i} に失敗: {str(ex)[:200]}")
        data = podcast_nlm._cli(pod, ["source", "list", "-n", book.nb, "--json"], check=False) or {}
        added, dropped = 0, 0
        for s in data.get("sources", []) if isinstance(data, dict) else []:
            url = s.get("url") or ""
            if not url or url in before:
                continue
            if qa_deep.is_primary(url, s.get("title") or ""):
                added += 1
            else:
                podcast_nlm._cli(pod, ["source", "delete", s["id"], "-n", book.nb, "-y"], parse_json=False, check=False)
                dropped += 1
        print(f"ウェブ調査 {i}: 一次資料 {added} 件を足し、それ以外の {dropped} 件を外しました", flush=True)
    print(f"資料: {len(book.sources())} 件")
    return book.nb


def route(question: str, cfg: dict) -> str | None:
    """質問に合うノートのキー（無ければ None）。作ってあるノートだけから選ぶ。"""
    import qa_deep
    have = ids()
    nbs = {k: v for k, v in notebooks().items() if k in have and v.get("route", True)}
    if not nbs:
        return None
    lines = "\n".join(f"- {k}: {v.get('description', '')}" for k, v in nbs.items())
    try:
        got = qa_deep.strata_obj(ROUTE_PROMPT.format(question=question, notebooks=lines), cfg)
    except Exception as ex:   # noqa: BLE001  選べなければ、その場のウェブ調査
        print("ノートを選べませんでした:", ex)
        return None
    key = str(got.get("notebook") or "").strip()
    return key if key in nbs else None


COMPANION_NOTE = "（この質問の点数・要件の経緯だけを答える）"
MAIN_NOTE = "（いまのルールと疑義解釈だけを答える。経緯は別の資料で調べるので書かない）"


def ask(key: str, question: str, context: str = "") -> dict | None:
    """ノートに聞く。companion（経緯のノート）があれば同時に聞いて「経緯」として後ろに並べる。
    {"text", "sources": [(title, url)], "name"}。メインのノートの資料に答えが無ければ None。"""
    from concurrent.futures import ThreadPoolExecutor
    comp = notebooks()[key].get("companion")
    if not comp or comp not in ids():
        return ask_one(key, question, context)
    with ThreadPoolExecutor(2) as ex:
        # 経緯は経緯のノートに任せる（いまのルールのノートが資料の無い経緯を書き、年度を取り違えた 2026-10-30）
        main = ex.submit(ask_one, key, question + "\n" + MAIN_NOTE, context)
        sub = ex.submit(ask_one, comp, question + "\n" + COMPANION_NOTE, context)
        got, hist = main.result(), None
        try:
            hist = sub.result()
        except Exception as e:   # noqa: BLE001  経緯が取れなくても、いまのルールは出す
            print("経緯のノートに聞けませんでした:", e)
    if not got:
        return None
    if hist:
        got["text"] += "\n\n**📜 経緯**\n" + hist["text"]
        got["sources"] += [s for s in hist["sources"] if s not in got["sources"]]
    return got


def ask_one(key: str, question: str, context: str = "") -> dict | None:
    """ノート1冊に聞く（会話は毎回新しく）。資料に答えが無ければ None。"""
    import podcast_nlm
    nb = notebooks()[key]
    pod = pod_settings()
    nb_id = ids()[key]
    podcast_nlm.ensure_auth(pod)
    data = podcast_nlm._cli(pod, ["ask", ASK_PROMPT.format(question=question, context=context, mark=NO_SOURCE),
                                  "-n", nb_id, "--new", "--json"], timeout=600)
    text = podcast_nlm.strip_citations(data.get("answer", "") if isinstance(data, dict) else str(data or "")).strip()
    text = re.sub(r"\n+💡[^\n]*\s*$", "", text)   # 最後の「💡 〜いかがでしょうか？」（次の質問の勧め）
    if not text or text.startswith(NO_SOURCE):
        return None
    cited = cited_ids(data)
    srcs = podcast_nlm._cli(pod, ["source", "list", "-n", nb_id, "--json"], check=False) or {}
    used = [(s.get("title") or urlparse(s.get("url") or "").netloc, s.get("url") or "")
            for s in (srcs.get("sources", []) if isinstance(srcs, dict) else [])
            if s.get("id") in cited and s.get("url")]
    return {"text": text, "sources": used, "name": nb["name"]}


def cited_ids(data) -> set:
    """ask --json の答えで引用された資料のID。"""
    out = set()
    for r in (data.get("references") or []) if isinstance(data, dict) else []:
        sid = r.get("source_id") if isinstance(r, dict) else None
        if sid:
            out.add(sid)
    return out


def main() -> None:
    from common import load_dotenv
    load_dotenv()
    if len(sys.argv) >= 3 and sys.argv[1] == "build":
        only = {int(a) for a in sys.argv[3:] if a.isdigit()}   # 調査の番号（0始まり）を並べると、それだけやる
        build(sys.argv[2], research=False if "--no-research" in sys.argv else only or True)
    elif len(sys.argv) >= 2 and sys.argv[1] == "list":
        for k, v in notebooks().items():
            print(f"{k}: {v['name']}  {ids().get(k) or '（未作成）'}")
    else:
        print(__doc__)


if __name__ == "__main__":
    main()
