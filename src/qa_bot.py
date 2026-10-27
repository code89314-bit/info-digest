"""疑問深堀チャンネルの Discord Bot（常駐）。処理の中身は src/qa_deep.py。

- チャンネル（.env の DISCORD_QA_CHANNEL_ID）への書き込みのうち、Bot の持ち主（Discord の開発者ポータルの
  アプリの Owner。config の qa_bot.owner_ids で追加可）の書き込みだけを「疑問」として受け付ける。
- 疑問ごとにスレッドを作り、進み具合・結論・論点・資料・note の下書きURL・音声をそこに返す。
- 疑問は1つずつ順番に処理する（NotebookLM・Strata・note のブラウザを同時に使わないため）。
- 音声は、NotebookLM の1日の枠に空きがあるときだけ作る。空きが無ければノートを残して待ち、
  20分おきに確認して、空いたら作ってスレッドに貼り、note の下書きにも埋め込む。

- 会話チャンネル（.env の DISCORD_CHAT_CHANNEL_ID）では、ペルソナとして Strata が検索つきで答える（src/strata_chat.py）。

起動: .venv\\Scripts\\pythonw.exe src\\qa_bot.py （タスク「info-digest qa bot」がログオン時に起動）
必要: .env に DISCORD_BOT_TOKEN と DISCORD_QA_CHANNEL_ID。開発者ポータルで Message Content Intent を ON。
"""
import asyncio
import datetime as dt
import logging
import os
import re
import sys
import traceback

import discord

import qa_deep
import strata_chat
from common import load_state, save_state

STATE = "qa_bot"
PERSONA_CMDS = {"!ペルソナ", "!persona", "！ペルソナ", "！persona"}   # トークチャンネルでのペルソナ切り替え
MEMORY_CMDS = {"!記憶", "！記憶", "!memory"}                             # 覚えている嗜好の一覧
FORGET_CMDS = {"!忘れて", "！忘れて", "!forget"}                        # 嗜好を番号で消す
NOVEL_MARK = "📖 "                                                      # 小説の投稿の頭（履歴では短くする）
SOURCES_MARK = "\n-# 🔎"   # 会話の返信の末尾（検索語・出典）。履歴として Strata に渡すときは切り落とす
log = logging.getLogger("qa_bot")


def now_id() -> str:
    return dt.datetime.now().strftime("q%Y%m%d-%H%M%S")


def chunks(text: str, n: int = 1900) -> list[str]:
    """Discord の文字数制限（2000字）に収まるよう、行の切れ目で分ける。1行が長すぎるときは、句点か n 字で切る。"""
    lines = []
    for line in text.splitlines():
        while len(line) > n:
            cut = line.rfind("。", 0, n) + 1 or n
            lines.append(line[:cut])
            line = line[cut:]
        lines.append(line)
    out, cur = [], ""
    for line in lines:
        if cur and len(cur) + len(line) + 1 > n:
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
    def __init__(self, cfg: dict, channel_id: int, chat_channel_id: int | None = None):
        intents = discord.Intents.default()
        intents.message_content = True
        super().__init__(intents=intents)
        self.cfg, self.channel_id = cfg, channel_id
        self.owners: set[int] = {int(x) for x in cfg.get("owner_ids") or []}
        self.queue: asyncio.Queue = asyncio.Queue()
        self.notebooks: dict[str, object] = {}   # 音声待ちのノート（qid → Notebook）
        self.chat_channel_id = chat_channel_id
        self.chat_lock = asyncio.Lock()          # 会話は1件ずつ（Strata は同時に1件しか処理しない）

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
        if msg.author.bot or msg.author.id not in self.owners:
            return
        if self.chat_channel_id and msg.channel.id == self.chat_channel_id:
            self.loop.create_task(self.chat(msg))
            return
        if msg.channel.id != self.channel_id:
            return
        text = msg.content.strip()
        if len(text) < 5:
            return
        thread = await msg.create_thread(name=f"🔎 {text[:90]}", auto_archive_duration=1440)
        pos = self.queue.qsize()
        await thread.send("受け付けました。患者情報の確認 → 解釈 → 資料集め → まとめ → note 下書きの順に進めます"
                          + (f"（前に {pos} 件あります）" if pos else "") + "。20〜30分ほどかかります。")
        await self.queue.put((text, thread))

    # ---- 会話（ペルソナ・検索つき） ----
    async def chat(self, msg: discord.Message) -> None:
        if not msg.content.strip():
            return
        ccfg = strata_chat.section_config()
        cmd = msg.content.strip().split()[0].lower()
        if cmd in PERSONA_CMDS:
            await self.switch_persona(msg, ccfg)
            return
        if cmd in MEMORY_CMDS | FORGET_CMDS:
            await self.memory_cmd(msg, ccfg, cmd in FORGET_CMDS)
            return
        async with self.chat_lock:
            history = await self.chat_history(msg, ccfg)
            try:
                async with msg.channel.typing():
                    first = await asyncio.to_thread(strata_chat.reply, history, ccfg)
                await self.chat_send(msg, first["text"])
                history.append({"role": "assistant", "name": "アシスタント", "content": first["text"]})
                if first["skill"] == "web_search":
                    queries = strata_chat.search_queries(first["arg"])
                    async with msg.channel.typing():
                        res = await asyncio.to_thread(strata_chat.research, history, queries, ccfg)
                    text = res["text"] + SOURCES_MARK + " 検索: " + " / ".join(queries)
                    text += "".join(f"\n-# [{t[:60]}](<{u}>)" for t, u in res["sources"])
                    await self.chat_send(msg, text)
                    history.append({"role": "assistant", "name": "アシスタント", "content": res["text"]})
                elif first["skill"] == "novel":
                    async with msg.channel.typing():
                        nov = await asyncio.to_thread(strata_chat.write_novel, history, first["arg"], ccfg)
                    await self.chat_send(msg, f"{NOVEL_MARK}『{nov['title']}』\n\n" + nov["text"].split("\n", 1)[-1].strip(),
                                         file=nov["path"])
                    history.append({"role": "assistant", "name": "アシスタント", "content": f"（小説『{nov['title']}』を書いた）"})
            except Exception as ex:   # noqa: BLE001
                log.error("chat: %s", traceback.format_exc())
                await msg.reply(f"⚠️ 答えられませんでした: {str(ex)[:300]}", mention_author=False)
                return
            try:   # 嗜好の記憶（memory: true のペルソナだけ）。返事のあとに、同じ順番待ちの中で行う
                persona = strata_chat.load_persona(ccfg)
                added = await asyncio.to_thread(strata_chat.update_memory, persona, history, ccfg)
                if added and ccfg.get("memory", {}).get("announce", True):
                    await msg.channel.send("-# 🧠 覚えたこと: " + " / ".join(a[:40] for a in added)[:1800])
            except Exception:   # noqa: BLE001
                log.error("memory: %s", traceback.format_exc())

    async def chat_history(self, msg: discord.Message, ccfg: dict) -> list[dict]:
        """トークチャンネルの直近の発言（古い順）。最後にペルソナを切り替えたところより前と、コマンドとその返事は読まない。"""
        history = []
        async for m in msg.channel.history(limit=int(ccfg.get("history", 12)), before=msg, oldest_first=False):
            words = m.content.split()
            if words and words[0].lower() in PERSONA_CMDS:
                if len(words) > 1:
                    break   # ペルソナを切り替えたところより前は、別の人との会話なので読まない
                continue
            if (words and words[0].lower() in MEMORY_CMDS | FORGET_CMDS) or m.content.startswith(("ペルソナ一覧", "🧠")):
                continue
            if m.author.id in self.owners and not m.author.bot:
                history.append({"role": "user", "name": "利用者", "content": m.content})
            elif (m.author.bot or m.webhook_id) and m.content and not m.content.startswith(("⚠️", "-#")):
                text = m.content.split(SOURCES_MARK)[0]
                if text.startswith(NOVEL_MARK):   # 小説の本文は長いので、履歴では題名と冒頭だけ（2通目以降は読まない）
                    if not text.startswith(NOVEL_MARK + "『"):
                        continue
                    text = "（書いた小説）" + text[len(NOVEL_MARK):][:300]
                history.append({"role": "assistant", "name": "アシスタント", "content": text[:1500]})
        history = [h for h in reversed(history) if h["content"].strip()]
        history.append({"role": "user", "name": "利用者", "content": msg.content})
        return history

    async def memory_cmd(self, msg: discord.Message, ccfg: dict, forget: bool) -> None:
        """「!記憶」で覚えている嗜好の一覧、「!忘れて 3 5」で番号の項目を消す。"""
        persona = strata_chat.load_persona(ccfg)
        if not persona.get("memory"):
            await msg.reply(f"🧠 {persona['name']}は嗜好を記憶しない設定です（ペルソナの memory: true で有効）。", mention_author=False)
            return
        if forget:
            nums = [int(x) for x in re.findall(r"\d+", msg.content)]
            gone = strata_chat.forget(persona["id"], nums)
            await msg.reply("🧠 忘れました: " + (" / ".join(gone) if gone else "（該当なし。番号は「!記憶」で確認）"),
                            mention_author=False)
            return
        items = strata_chat.load_memory(persona["id"])
        lines = [f"{i}. [{strata_chat.KIND_LABEL.get(m['kind'], m['kind'])}] {m['text']}" for i, m in enumerate(items, 1)]
        text = f"🧠 {persona['name']}が覚えていること（{len(items)}件。「!忘れて 番号」で消せます）\n" + ("\n".join(lines) or "（まだありません）")
        for part in chunks(text):
            await msg.channel.send(part)

    async def switch_persona(self, msg: discord.Message, ccfg: dict) -> None:
        """「!ペルソナ」で一覧、「!ペルソナ 名前」で切り替え（切り替えたら、そのペルソナがあいさつする）。"""
        words = msg.content.split(maxsplit=1)
        now = strata_chat.load_persona(ccfg)
        if len(words) == 1:
            lines = [f"{'▶' if p['id'] == now['id'] else '・'} {p['name']}（{p['id']}）"
                     for p in strata_chat.personas(ccfg).values()]
            await msg.reply("ペルソナ一覧（「!ペルソナ 名前」で切り替え）\n" + "\n".join(lines), mention_author=False)
            return
        p = strata_chat.find_persona(ccfg, words[1])
        if not p:
            await msg.reply(f"⚠️ 「{words[1][:50]}」というペルソナはありません。「!ペルソナ」で一覧を出せます。",
                            mention_author=False)
            return
        strata_chat.set_persona(p["id"])
        await self.chat_send(msg, p.get("greeting") or f"{p['name']}に切り替えました。")

    async def chat_send(self, msg: discord.Message, text: str, file=None) -> None:
        """ペルソナの名前・アイコンで（.env の DISCORD_CHAT_WEBHOOK_URL があれば Webhook、無ければ Bot の返信）。
        小説（NOVEL_MARK で始まる）は、分割した2通目以降にも印を付ける（履歴で読み飛ばすため）。file は最後の1通に添付。"""
        url = os.environ.get("DISCORD_CHAT_WEBHOOK_URL")
        persona = strata_chat.load_persona(strata_chat.section_config())
        parts = chunks(text)
        if text.startswith(NOVEL_MARK):
            parts = [parts[0]] + [NOVEL_MARK + p for p in parts[1:]]
        for i, part in enumerate(parts):
            kw = {"file": discord.File(file)} if file and i == len(parts) - 1 else {}
            if url:
                await discord.Webhook.from_url(url, client=self).send(
                    part, username=persona.get("name") or "Strata", avatar_url=persona.get("avatar_url") or None, **kw)
            elif i == 0:
                await msg.reply(part, mention_author=False, **kw)
            else:
                await msg.channel.send(part, **kw)

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
    chat_channel = os.environ.get("DISCORD_CHAT_CHANNEL_ID")
    if not strata_chat.section_config().get("enabled", True):
        chat_channel = None
    QABot(cfg, int(channel), int(chat_channel) if chat_channel else None).run(token, log_handler=None)


if __name__ == "__main__":
    sys.exit(main())
