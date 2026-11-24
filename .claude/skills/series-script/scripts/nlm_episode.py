"""Make one series episode as a NotebookLM audio overview (two hosts) that follows the episode script (trial, 2026-11-23).

- A notebook with only this episode's script (readings removed, directions removed) and fact cards, so the hosts
  talk from these and nothing else.
- The instructions ask them to follow the script's order, use only numbers in the sources, and not mention a script.
  Readings (style/nlm_yomi.yaml) are added by podcast_nlm.readings_note (never spoken about), only for words in the script/facts.
- Uses the audio quota (shared ledger, learn.podcast settings). Download -> .local/series/<week>/audio/ep<n>-nlm.mp3.
  The opening (title call) and the closing are made separately and joined afterwards (nlm_episode_wrap).

usage: python nlm_episode.py <week_dir> <n> [--length long|default] [--keep]
"""
import argparse
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT / "src"))
import common  # noqa: E402
import podcast_nlm  # noqa: E402

INSTRUCTIONS = """日本の病院薬剤師向けの、週の連載の学習番組の1回分。
資料の「台本」の流れ（話題の順番・論点・数字・歴史の順）に沿って、2人で自然な会話として話す。台本のせりふをそのまま読む必要はない。
- 話す内容は資料（台本と事実カード）にあることだけ。資料にない数値・年・試験名・推奨は足さない。
- 「台本」「資料によると」「このノート」とは言わない。番組の名前や自己紹介も言わない（冒頭のタイトルコールと締めは別に付ける）。
- いきなり本題から始め、最後は「今日のポイント」を3つにまとめ、次回予告の問いで終える。
- リスナーは基礎の固い薬剤師。基礎の説明は挟まない。個別の患者への判断はしない。
"""


def plain(script: Path) -> str:
    """台本 -> 読みがな記号と演出を除いた、話者つきの平文。"""
    body = script.read_text(encoding="utf-8").split("---", 2)[2]
    out = []
    for ln in body.splitlines():
        ln = re.sub(r"\{([^|}]*)\|[^}]*\}", r"\1", ln)
        ln = re.sub(r"（[^）]*）", "", ln).replace("[間]", "").replace("[笑]", "")
        out.append(ln.rstrip())
    return re.sub(r"\n{3,}", "\n\n", "\n".join(out)).strip()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("week_dir")
    ap.add_argument("n", type=int)
    ap.add_argument("--length", default="long")
    ap.add_argument("--script", default="", help="script file name in week_dir (default ep<n>-script.md; e.g. ep2-script-2p.md)")
    ap.add_argument("--keep", action="store_true", help="keep the notebook (default: delete it after download)")
    a = ap.parse_args()
    d = Path(a.week_dir)
    common.load_dotenv()
    pod = dict((common.load_config().get("learn") or {}).get("podcast") or {})
    ins = INSTRUCTIONS
    if a.n > 1:   # 連載の続きの回: 前回とのつながり（振り返り・前回の予告の問いの答え）を落とさない
        ins += ("- これは連載の続きの回。台本にある「前回の振り返り」から入り、前回の予告の問いの答えは台本の位置で、"
                "前回からの宿題だとわかるように話す。\n")
    script = d / (a.script or f"ep{a.n}-script.md")
    two = not re.search(r"^司:", script.read_text(encoding="utf-8"), re.M)
    if two:   # 2人用の台本（2026-11-23 本人が採用）: NotebookLM の2人に、台本の2人の役をそのまま当てる
        # 性別は指定しない（試しでは男女が台本と逆になったが、途中で入れ替わらなかった。本人はそれでよいと判断）
        ins += ("- 台本の話者は2人。1人が「堀田」の役（解説の中心）、もう1人が「横井」の役（問い・確かめ・別の視点・まとめ）を、"
                "最後まで入れ替えずに、台本の割り振りどおりに受け持つ。名前は呼ばない。\n")
    pod.update(instructions=ins, length=a.length, format="deep-dive", language="ja",
               title=f"連載音声 第{a.n}回")
    limit = int(pod.get("daily_limit", 5))
    used = podcast_nlm.used_today(pod)
    print(f"音声の台帳: 今日 {used}/{limit}", flush=True)
    if used >= limit:
        sys.exit("今日の音声の枠を使い切っています")
    nb = podcast_nlm.Notebook(pod, [])
    try:
        title = re.search(r"^title:\s*(.+)$", script.read_text(encoding="utf-8"), re.M)
        body = plain(script)
        nb.add_text(f"第{a.n}回 台本", body)
        facts = d / f"ep{a.n}-facts.md"
        card = re.sub(r"\{([^|}]*)\|[^}]*\}", r"\1", facts.read_text(encoding="utf-8")) if facts.exists() else ""
        if card:
            nb.add_text(f"第{a.n}回 事実カード", card)
        pod["yomi_scope"] = body + "\n" + card   # 読み方は、台本と事実カードに出てくる言葉だけ渡す（2026-11-24 本人）
        print("読み方:", podcast_nlm.readings_note(only_in=pod["yomi_scope"]).split(": ", 1)[-1][:200] or "なし", flush=True)
        print("資料:", [s["title"] for s in nb.sources()], "題:", title.group(1) if title else "-", flush=True)
        out = d / "audio" / f"ep{a.n}-nlm{'-2p' if '-2p' in script.stem else ''}.mp3"
        out.parent.mkdir(exist_ok=True)
        nb.audio(out)
        ff = __import__("shutil").which("ffmpeg") or "ffmpeg"
        print(f"saved: {out} ({podcast_nlm.duration_sec(out, ff) / 60:.1f} 分)", flush=True)
    finally:
        if not a.keep:
            nb.close()


if __name__ == "__main__":
    main()
