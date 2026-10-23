"""疑問深堀チャンネルの Discord Bot（常駐）。処理の中身は src/qa_deep.py。

- チャンネル（.env の DISCORD_QA_CHANNEL_ID）への書き込みのうち、Bot の持ち主（Discord の開発者ポータルの
  アプリの Owner。config の qa_bot.owner_ids で追加可）の書き込みだけを「疑問」として受け付ける。
- 疑問ごとにスレッドを作り、進み具合・結論・論点・資料・note の下書きURL・音声をそこに返す。
- 疑問は1つずつ順番に処理する（NotebookLM・Strata・note のブラウザを同時に使わないため）。
- 音声は、NotebookLM の1日の枠に空きがあるときだけ作る。空きが無ければノートを残して待ち、
  20分おきに確認して、空いたら作ってスレッドに貼り、note の下書きにも埋め込む。

起動: .venv\\Scripts\\pythonw.exe src\\qa_bot.py （タスク「info-digest qa bot」がログオン時に起動）
必要: .env に DISCORD_BOT_TOKEN と DISCORD_QA_CHANNEL_ID。開発者ポータルで Message Content Intent を ON。
"""
import asyncio
import datetime as dt
import logging
import os
import sys
import traceback

import discord

import qa_deep
from common import load_state, save_state

STATE = "qa_bot"
log = logging.getLogger("qa_bot")


def now_id() -> str:
    return dt.datetime.now().strftime("q%Y%m%d-%H%M%S")


def chunks(text: str, n: int = 1900) -> list[str]:
    out, cur = [], ""
    for line in text.splitlines():
        if len(cur) + len(line) + 1 > n:
            out.append(cur)
            cur = ""
        cur += line + "\n"
    return out + ([cur] if cur.strip() else [])


def summary_embeds(res: dict) -> list[discord.Embed]:
    doc, plan = res["doc"], res["plan"]
    e1 = discord.Embed(title=f"🔎 {doc['title']}"[:256], color=0x1E3A5F,
                       description=(f"**解釈**　{plan['interpretation']}\n\n**{doc['three_heading']}**\n"
                                    + "\n".join(f"- {x}" for x in doc["three_lines"]))[:4000])
    for s in (doc["sections"] + ([doc["wrapup"]] if doc["wrapup"]["paragraphs"] else []))[:6]:
        val = s["paragraphs"][0] + "".join(f"\n-# {t}" for t, _ in s.get("sources", [])[:3])
        e1.add_field(name=s["heading"][:256], value=val[:1024], inline=False)
    if doc["unknown"]:
        e1.add_field(name="資料で分からなかったこと", value=doc["unknown"][:1024], inline=False)
    links = "\n".join(f"- [{t[:80]}]({u})" for t, u in doc["links"][:10])
    e2 = discord.Embed(title=f"参考（{len(doc['links'])}件）", description=links[:4000] or "（なし）", color=0x5B7896)
    return [e1, e2]


class QABot(discord.Client):
    def __init__(self, cfg: dict, channel_id: int):
        intents = discord.Intents.default()
        intents.message_content = True
        super().__init__(intents=intents)
        self.cfg, self.channel_id = cfg, channel_id
        self.owners: set[int] = {int(x) for x in cfg.get("owner_ids") or []}
        self.queue: asyncio.Queue = asyncio.Queue()
        self.notebooks: dict[str, object] = {}   # 音声待ちのノート（qid → Notebook）

    async def setup_hook(self) -> None:
        self.loop.create_task(self.worker())
        self.loop.create_task(self.audio_loop())

    async def on_ready(self) -> None:
        app = await self.application_info()
        if app.owner:
            self.owners.add(app.owner.id)
        if getattr(app, "team", None):
            self.owners |= {m.id for m in app.team.members}
        log.info("ready: %s / channel %s / owners %s", self.user, self.channel_id, self.owners)

    async def on_message(self, msg: discord.Message) -> None:
        if msg.author.bot or msg.channel.id != self.channel_id or msg.author.id not in self.owners:
            return
        text = msg.content.strip()
        if len(text) < 5:
            return
        thread = await msg.create_thread(name=f"🔎 {text[:90]}", auto_archive_duration=1440)
        pos = self.queue.qsize()
        await thread.send("受け付けました。患者情報の確認 → 解釈 → 資料集め → まとめ → note 下書きの順に進めます"
                          + (f"（前に {pos} 件あります）" if pos else "") + "。20〜30分ほどかかります。")
        await self.queue.put((text, thread))

    # ---- 1件ずつ処理 ----
    async def worker(self) -> None:
        await self.wait_until_ready()
        while True:
            text, thread = await self.queue.get()
            qid = now_id()
            try:
                await self.handle(qid, text, thread)
            except Exception as ex:   # noqa: BLE001
                log.error("failed %s: %s", qid, traceback.format_exc())
                await thread.send(f"⚠️ 調べられませんでした: {str(ex)[:300]}")

    async def handle(self, qid: str, text: str, thread) -> None:
        res = await asyncio.to_thread(qa_deep.answer, text, self.cfg, qid=qid)
        if res["status"] == "blocked":
            pv = res["privacy"]
            msg = ("🛑 患者・職員を特定しうる情報が含まれている可能性があるため、外部（NotebookLM・note）には送っていません。\n"
                   + "".join(f"- {r}\n" for r in pv["reasons"]))
            if pv["rewrite"]:
                msg += f"\n言い換え案（問題なければ、これをチャンネルに書き直してください）:\n> {pv['rewrite']}"
            await thread.send(msg[:1900])
            return
        doc = res["doc"]
        note = (f"📝 note に下書き保存しました（非公開）: {res['note_url']}" if res["note_url"]
                else "📝 note の下書きは保存できませんでした（添付の html を手で貼ってください）")
        note += "".join(f"\n-# 未完了: {t}" for t in res["todo"])
        note += f"\n-# 確認メモ {len(res['memo'])} 件（添付の md の末尾）"
        await thread.send(embeds=summary_embeds(res))
        await thread.send(note[:1900], files=[discord.File(res["md"]), discord.File(res["html"])])
        nb = res["nb"]
        if not doc["audio"]:
            await asyncio.to_thread(nb.close)
            return
        state = load_state(STATE)
        state.setdefault("pending", []).append({"qid": qid, "nb": nb.nb, "thread": thread.id, "title": res["plan"]["title"],
                                                "note_url": res["note_url"], "created": dt.datetime.now().isoformat()})
        save_state(STATE, state)
        self.notebooks[qid] = nb
        if await self.try_audio():
            return
        await thread.send("🎧 音声は、NotebookLM の1日の作成枠に空きができたら作ってここに貼ります（note の下書きにも追加します）。")

    # ---- 音声（枠が空いたら） ----
    async def audio_loop(self) -> None:
        await self.wait_until_ready()
        while True:
            await asyncio.sleep(int(self.cfg.get("audio_check_minutes", 20)) * 60)
            try:
                await self.try_audio()
            except Exception:   # noqa: BLE001
                log.error("audio loop: %s", traceback.format_exc())

    async def try_audio(self) -> bool:
        """待っている音声を、枠がある限り古い順に作る。1件でも作れたら True。"""
        import podcast_nlm
        made = False
        while True:
            state = load_state(STATE)
            pending = state.get("pending") or []
            if not pending:
                return made
            item = pending[0]
            nb = self.notebooks.get(item["qid"]) or podcast_nlm.Notebook.attach(
                qa_deep.notebook_settings(self.cfg, qa_deep.load_style(self.cfg), item["title"]), item["nb"])
            if qa_deep.audio_slots(nb.pod, self.cfg) <= 0:
                return made
            thread = self.get_channel(item["thread"]) or await self.fetch_channel(item["thread"])
            await thread.send("🎧 音声を作っています（15〜20分）…")
            style = qa_deep.load_style(self.cfg)
            try:
                got = await asyncio.to_thread(qa_deep.add_audio, nb, style, item["title"], item["qid"],
                                              item["note_url"], self.cfg)
                await thread.send(f"🎧 **{item['title']}**（約{round(got['secs'] / 60)}分・NotebookLM の AI音声）"
                                  + ("\n-# note の下書きに埋め込みました" if item["note_url"] and not got["todo"] else "")
                                  + "".join(f"\n-# 未完了: {t}" for t in got["todo"]),
                                  file=discord.File(got["small"]))
            except Exception as ex:   # noqa: BLE001
                await thread.send(f"🎧 音声を作れませんでした: {str(ex)[:300]}")
            await asyncio.to_thread(nb.close)
            self.notebooks.pop(item["qid"], None)
            state = load_state(STATE)
            state["pending"] = [p for p in state.get("pending") or [] if p["qid"] != item["qid"]]
            save_state(STATE, state)
            made = True


def main() -> None:
    if "--log" in sys.argv:   # 常駐時: 標準出力も含めて .local/logs/qa_bot-YYYYMMDD.log へ（UTF-8）
        from common import ROOT
        path = ROOT / ".local" / "logs" / f"qa_bot-{dt.date.today():%Y%m%d}.log"
        path.parent.mkdir(parents=True, exist_ok=True)
        sys.stdout = sys.stderr = open(path, "a", encoding="utf-8", buffering=1)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s",
                        stream=sys.stderr)
    cfg = qa_deep.section_config()
    token, channel = os.environ.get("DISCORD_BOT_TOKEN"), os.environ.get("DISCORD_QA_CHANNEL_ID")
    if not cfg.get("enabled", True):
        print("qa_bot は無効です（config.yaml の qa_bot.enabled）")
        return
    if not token or not channel:
        raise SystemExit(".env に DISCORD_BOT_TOKEN と DISCORD_QA_CHANNEL_ID を書いてください")
    QABot(cfg, int(channel)).run(token, log_handler=None)


if __name__ == "__main__":
    sys.exit(main())
