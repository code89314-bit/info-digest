"""疑問深堀チャンネルの Discord Bot（常駐）。処理の中身は src/qa_deep.py。

- チャンネル（.env の DISCORD_QA_CHANNEL_ID）への書き込みのうち、Bot の持ち主（Discord の開発者ポータルの
  アプリの Owner。config の qa_bot.owner_ids で追加可）の書き込みだけを「疑問」として受け付ける。
- 疑問ごとにスレッドを作り、進み具合・結論・論点・資料・note の下書きURL・音声をそこに返す。
- 疑問は1つずつ順番に処理する（NotebookLM・Strata・note のブラウザを同時に使わないため）。
- 音声は、NotebookLM の1日の枠に空きがあるときだけ作る。空きが無ければノートを残して待ち、
  20分おきに確認して、空いたら作ってスレッドに貼り、note の下書きにも埋め込む。

- 会話チャンネル（.env の DISCORD_CHAT_CHANNEL_ID）では、ペルソナとして Strata が検索つきで答える（src/strata_chat.py）。
- ルナ専用チャンネル（.env の DISCORD_LUNA_CHANNEL_ID）は、ルナ用の別の Bot（DISCORD_LUNA_BOT_TOKEN）が受けて、
  ルナ自身の名前で答える。処理は同じプロセスの QABot.chat（GPU の切り替えと順番待ちを共有するため）。
  会話は Strata、小説だけ Gemma（書く間だけ切り替えて、終わったら Strata に戻す）、絵は ComfyUI。

起動: .venv\\Scripts\\pythonw.exe src\\qa_bot.py （タスク「info-digest qa bot」がログオン時に起動）
必要: .env に DISCORD_BOT_TOKEN と DISCORD_QA_CHANNEL_ID。開発者ポータルで Message Content Intent を ON。
"""
import asyncio
import base64
import datetime as dt
import io
import json
import logging
import os
import random
import re
import sys
import time
import traceback
from pathlib import Path

import discord

import comfy_client
import gpu_mode
import qa_deep
import strata_chat
from common import load_state, save_state

STATE = "qa_bot"
PERSONA_CMDS = {"!ペルソナ", "!persona", "！ペルソナ", "！persona"}   # トークチャンネルでのペルソナ切り替え
MEMORY_CMDS = {"!記憶", "！記憶", "!memory"}                             # 覚えている嗜好の一覧
FORGET_CMDS = {"!忘れて", "！忘れて", "!forget"}                        # 嗜好を番号で消す
CRAFT_CMDS = {"!作法", "！作法", "!craft"}                               # 小説の作法の一覧・追加
COMFY_CMDS = {"!comfyui", "！comfyui"}                                   # ルナ: ComfyUI を起動してお絵描きモードに
GEMMA_CMDS = {"!gemma", "！gemma"}                                       # ルナ: お絵描きモードを抜けて Gemma に
CLEAR_CMDS = {"!消去", "！消去", "!clear"}                                # ルナのチャンネル: 古い順（「新しい」で新しい順）に N 件消す
HELP_CMDS = {"!", "！", "!help", "！help", "!ヘルプ", "！ヘルプ"}          # コマンドの一覧
# 執筆モード（ルナ）: 入る言葉 / やめる言葉 / Strata・ComfyUI の担当なので抜けてから受ける言葉
#   「小説を書いて」なども Strata に考えさせず（遅いので）、言葉で見つけて Gemma に切り替えるか確認する
WRITING_ENTER = re.compile(r"執筆モード|執筆して|執筆しよう|小説(を|が|も)?\s*(書|かい|か(け|く|こ)|お願い|おねがい|頼|たの|読みたい|ちょうだい|ください|ほしい|欲しい)")
# 執筆中に「別の小説」「キャラを変える」→ 前の小説は忘れて、キャラ・シチュエーションの打ち合わせからやり直す
NEW_STORY = re.compile(r"別の(小説|話|お話|物語)|新しい(小説|話|お話|物語)|キャラ(クター)?(を|も)?\s*(変え|かえ|替え|換え)")
WRITING_EXIT = re.compile(r"執筆(を)?(やめ|止め|おわ|終わ|終え)|執筆モード(を)?(終|おわ|抜け|出)|会話しよう|おしゃべりしよう|"
                          r"普通に話そう|(strata|ストラタ)に戻|いつものルナ", re.I)
WRITING_LEAVE = re.compile(r"プロンプト|絵にして|絵を描|絵が見たい|イラスト")   # 「描いて」は執筆中は「描写して」の意味が多いので入れない
# モードを変える前の確認（ask_switch）への返事
YES_RE = re.compile(r"^(はい|うん|いいよ|いい|ok|おk|おけ|お願い|おねがい|yes|y|やって|どうぞ|ええ|そうして|切り替えて)[!！。、w～ー〜♪💛❤]*$", re.I)
NO_RE = re.compile(r"^(いいえ|いや|だめ|ダメ|やめて?|no|n|ううん|キャンセル|しない|いらない|まだ)[!！。、w～ー〜]*$", re.I)
NOVEL_MARK = "📖 "                                                      # 小説の投稿の頭（履歴では短くする）
PROMPTS_MARK = "🖋️ "                                                    # プロンプトを作った報告
DRAW_MARK = "🎨 "                                                       # 描いた絵


def image_attachments(msg: discord.Message) -> list:
    return [a for a in msg.attachments if (a.content_type or "").startswith("image/")]


async def image_data_uri(att, max_side: int = 1024) -> str:
    """Discord の添付画像を、長辺 max_side の JPEG の data URI に（モデルに渡す用）。読めなければ ""。"""
    from PIL import Image
    try:
        im = Image.open(io.BytesIO(await att.read())).convert("RGB")
    except Exception:   # noqa: BLE001
        return ""
    im.thumbnail((max_side, max_side))
    buf = io.BytesIO()
    im.save(buf, "JPEG", quality=88)
    return "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode()
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
    def __init__(self, cfg: dict, channel_id: int, chat_channel_id: int | None = None, luna_channel_id: int | None = None):
        intents = discord.Intents.default()
        intents.message_content = True
        super().__init__(intents=intents)
        self.cfg, self.channel_id = cfg, channel_id
        self.owners: set[int] = {int(x) for x in cfg.get("owner_ids") or []}
        self.queue: asyncio.Queue = asyncio.Queue()
        self.notebooks: dict[str, object] = {}   # 音声待ちのノート（qid → Notebook）
        self.chat_channel_id = chat_channel_id
        self.luna_channel_id = luna_channel_id   # ここへの書き込みは LunaBot が受ける（この Bot は無視する）
        self.luna_bot: discord.Client | None = None   # main() が LunaBot を入れる（執筆モードの終了の通知・名前の変更用）
        self.pending: dict[int, dict] = {}       # チャンネル → モードを変える前の確認の答え待ち（ask_switch）
        self.chat_lock = asyncio.Lock()          # 会話は1件ずつ（Strata は同時に1件しか処理しない）
        self.last_chat = time.monotonic()        # 最後に話した時刻（GPU を借りたまましばらく無ければ Strata に戻す）
        self.comfy_at = time.time()              # お絵描きモードで ComfyUI が最後に動いていた時刻（idle_loop が1分ごとに見る）
        self.comfy_last = ""                     # そのとき最後に終わっていたジョブの id

    async def setup_hook(self) -> None:
        self.loop.create_task(self.worker())
        self.loop.create_task(self.audio_loop())
        if self.chat_channel_id or self.luna_channel_id:
            self.loop.create_task(self.idle_loop())

    async def on_ready(self) -> None:
        app = await self.application_info()
        if app.owner:
            self.owners.add(app.owner.id)
        if getattr(app, "team", None):
            self.owners |= {m.id for m in app.team.members}
        log.info("ready: %s / channel %s / owners %s", self.user, self.channel_id, self.owners)

    async def on_message(self, msg: discord.Message) -> None:
        if msg.author.bot or msg.author.id not in self.owners or msg.channel.id == self.luna_channel_id:
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
    async def chat(self, msg: discord.Message, persona_id: str | None = None) -> None:
        """persona_id: ペルソナ専用チャンネル（LunaBot から呼ぶ）。返事・入力中の表示は msg を受けた Bot の名前で出る。"""
        if not msg.content.strip() and not image_attachments(msg):
            return
        if persona_id:
            strata_chat.PERSONA_OVERRIDE.set(persona_id)   # この会話の処理（タスク）の中だけ
        ccfg = strata_chat.section_config()
        cmd = (msg.content.strip().split() or [""])[0].lower()
        if cmd in PERSONA_CMDS:
            if persona_id:
                await msg.reply("このチャンネルはルナ専用だよ。ほかの子と話すなら、トークチャンネルでね", mention_author=False)
            else:
                await self.switch_persona(msg, ccfg)
            return
        if cmd in MEMORY_CMDS | FORGET_CMDS:
            await self.memory_cmd(msg, ccfg, cmd in FORGET_CMDS)
            return
        if cmd in CRAFT_CMDS:
            await self.craft_cmd(msg, ccfg)
            return
        if cmd in HELP_CMDS:
            await msg.reply(self.help_text(bool(persona_id)), mention_author=False)
            return
        if cmd in CLEAR_CMDS and persona_id:
            await self.clear_cmd(msg)
            return
        if cmd in COMFY_CMDS | GEMMA_CMDS:
            async with self.chat_lock:
                try:
                    await self.mode_cmd(msg, strata_chat.load_persona(ccfg), ccfg, cmd in COMFY_CMDS)
                except gpu_mode.Busy as ex:
                    await msg.reply(f"⏳ いまは{ex}なので、GPU を貸せません。終わってからもう一度どうぞ。", mention_author=False)
                except Exception as ex:   # noqa: BLE001
                    log.error("mode: %s", traceback.format_exc())
                    await msg.reply(f"⚠️ 切り替えられませんでした: {str(ex)[:300]}", mention_author=False)
            return
        async with self.chat_lock:
            persona = strata_chat.load_persona(ccfg)
            try:
                if await self.draw_input(msg, persona, ccfg):
                    return   # お絵描きモード（ComfyUI）中の番号・枚数・0
                pend = self.pending.pop(msg.channel.id, None)
                if pend and await self.answer_pending(msg, pend, persona, ccfg):
                    return   # 「はい／いいえ」への返事として処理した
                if persona.get("novel_gpu") and await self.writing_mode(msg, persona, ccfg):
                    return   # 執筆モードで答えた（記憶の更新も済み）
            except gpu_mode.Busy as ex:
                await msg.reply(f"⏳ いまは{ex}なので、GPU を貸せません。終わってからもう一度どうぞ。", mention_author=False)
                return
            except Exception as ex:   # noqa: BLE001
                log.error("writing: %s", traceback.format_exc())
                await msg.reply(f"⚠️ 書けませんでした: {str(ex)[:300]}", mention_author=False)
                return
            await self.strata_turn(msg, persona, ccfg)

    async def ask_switch(self, msg: discord.Message, kind: str, question: str, **data) -> None:
        """モードが変わる前に「はい／いいえ」で確認する。答えは次の発言（answer_pending）で受ける。"""
        self.pending[msg.channel.id] = {"kind": kind, "msg": msg, **data}
        await msg.channel.send(f"-# 🔀 {question}（はい／いいえ）")

    async def answer_pending(self, msg: discord.Message, pend: dict, persona: dict, ccfg: dict) -> bool:
        """確認への返事。はい → 切り替えて元の依頼を続ける / いいえ → 切り替えない。どちらでもなければ False
        （確認は取り消して、この発言をふつうに受ける）。"""
        text = msg.content.strip()
        yes, no = bool(YES_RE.match(text)), bool(NO_RE.match(text))
        if not yes and not no:
            return False
        orig = pend["msg"]
        if pend["kind"] == "enter_writing":
            if yes and await self.enter_writing(orig, persona, ccfg, pend["request"]):
                await self.writing(orig, persona, ccfg)
            elif no:
                await self.chat_send(msg, "わかった、じゃあ今はおしゃべりしてよ💛")
        elif pend["kind"] == "leave_writing":
            if yes:
                await self.end_writing(persona, msg.channel)
                await self.strata_turn(orig, persona, ccfg)   # 抜けたあと、元の発言にいつものルナ（Strata）として答える
            else:
                await self.writing(orig, persona, ccfg)       # 執筆の注文として受ける
        elif pend["kind"] == "draw":
            if yes:
                await self.draw(orig, persona, pend["path"], pend["arg"], ccfg)
            else:
                await self.chat_send(msg, "了解、描くのはまた今度ね")
        elif pend["kind"] == "draw_session":
            if yes:
                await self.start_draw(orig, persona, pend["path"], ccfg)
            else:
                await self.chat_send(msg, "了解、描くのはまた今度ね。描きたくなったら「描いて」って言ってね")
        return True

    async def strata_turn(self, msg: discord.Message, persona: dict, ccfg: dict) -> None:
        """ふだんの会話（Strata）。スキル（検索・小説・プロンプト・描く）もここから。"""
        if not await self.ensure_gpu(msg, persona, ccfg):
            return
        self.last_chat = time.monotonic()
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
                # 執筆モード（Gemma）に入って書く（確認してから）。配信の時間帯などで入れなければ、Strata で1本だけ書く
                if persona.get("novel_gpu") and not gpu_mode.blocked_reason(ccfg):
                    await self.ask_switch(msg, "enter_writing", self.writing_question(),
                                          request=first["arg"] + "\n\n（相手の依頼）" + msg.content)
                    return
                async with msg.channel.typing():
                    nov = await asyncio.to_thread(strata_chat.write_novel, history, first["arg"], ccfg, False)
                await self.chat_send(msg, f"{NOVEL_MARK}『{nov['title']}』\n\n" + nov["text"].split("\n", 1)[-1].strip(),
                                     file=nov["path"])
                if nov.get("studied"):
                    await msg.channel.send("-# 📚 下調べ: " + " / ".join(nov["studied"]))
                history.append({"role": "assistant", "name": "アシスタント", "content": f"（小説『{nov['title']}』を書いた）"})
            elif first["skill"] == "prompts":
                # 描画セット（人物の候補5つ＋シチュエーション20個）を Strata で作り、お絵描きモードに入るか確認する
                theme = first["arg"].partition("| 描く")[0]
                async with msg.channel.typing():
                    ds = await asyncio.to_thread(strata_chat.make_draw_set, history, theme.strip(), ccfg)
                doc = ds["doc"]
                who = "\n".join(f"- {c.get('name_ja', '?')}: " + (f"`{c['danbooru']['tag']}`（Danbooru {c['danbooru']['posts']}件）"
                                                                  if c["danbooru"].get("tag") else "Danbooru に無し（外見のタグで描く）")
                                for c in doc["characters"]) or "- （人物を読み取れなかった）"
                cands = "\n".join(f"{i}. {c['label']}" for i, c in enumerate(doc["candidates"], 1))
                await self.chat_send(msg, f"{PROMPTS_MARK}描く準備ができたよ（{ds['path'].name}）\n**人物**\n{who}\n"
                                          f"**人物の候補（背景なしで描いて選んでもらう）**\n{cands}\n"
                                          f"**シチュエーション** {len(doc['situations'])} 個（例: {doc['situations'][0]['caption'][:100]}）"
                                          + (f"\n-# 📚 下調べ: {' / '.join(doc['studied'])}" if doc["studied"] else ""),
                                     file=ds["path"])
                history.append({"role": "assistant", "name": "アシスタント", "content": "（描画セットを作った）"})
                await self.ask_switch(msg, "draw_session", "お絵描きモード（ComfyUI）に切り替えて、人物の候補から描く？ "
                                      "1枚目は10分ほどかかって、お絵描きモードのあいだはおしゃべりできないよ（!gemma で戻る）", path=ds["path"])
            elif first["skill"] == "draw":
                path = strata_chat.latest_draw_set()
                if path:
                    await self.ask_switch(msg, "draw_session", f"お絵描きモード（ComfyUI）に切り替えて、「{path.stem[16:46]}」の人物の候補から描く？ "
                                          "1枚目は10分ほどかかって、お絵描きモードのあいだはおしゃべりできないよ（!gemma で戻る）", path=path)
                elif strata_chat.latest_prompts():   # 描画セットより前の、プロンプト案だけのファイル
                    await self.ask_switch(msg, "draw", "お絵描きモード（ComfyUI）に切り替えて描く？ 1枚目は10分ほどかかって、"
                                          "お絵描きモードのあいだはおしゃべりできないよ（!gemma で戻る）",
                                          path=strata_chat.latest_prompts(), arg=first["arg"])
                else:
                    await self.chat_send(msg, "まだ描く準備ができてないよ。先に「〇〇の絵のプロンプト作って」って言ってね")
        except gpu_mode.Busy as ex:
            await msg.reply(f"⏳ いまは{ex}なので、GPU を貸せません。終わってからもう一度どうぞ。", mention_author=False)
            return
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
            if (words and words[0].lower() in MEMORY_CMDS | FORGET_CMDS | CRAFT_CMDS | HELP_CMDS | COMFY_CMDS | GEMMA_CMDS) or \
                    m.content.startswith(("ペルソナ一覧", "🧠", "✍️", "📋")):
                continue
            if m.author.id == msg.author.id:   # 話している本人（ルナのチャンネルは別アカウントのことがある）
                history.append({"role": "user", "name": "利用者", "content": m.content, "msg": m})
            elif (m.author.bot or m.webhook_id) and m.content and not m.content.startswith(("⚠️", "-#")):
                text = m.content.split(SOURCES_MARK)[0]
                if text.startswith(NOVEL_MARK):   # 小説の本文は長いので、履歴では題名と冒頭だけ（2通目以降は読まない）
                    if not text.startswith(NOVEL_MARK + "『"):
                        continue
                    text = "（書いた小説）" + text[len(NOVEL_MARK):][:300]
                if text.startswith((PROMPTS_MARK, DRAW_MARK)):
                    text = text.splitlines()[0]
                history.append({"role": "assistant", "name": "アシスタント", "content": text[:1500]})
        history = [h for h in reversed(history) if h["content"].strip() or h.get("msg")]
        history.append({"role": "user", "name": "利用者", "content": msg.content, "msg": msg})
        # 画像は、画像を読めるペルソナのときだけ、直近2つの発言の分を一緒に渡す
        persona = strata_chat.load_persona(ccfg)
        users = [h for h in history if h.get("msg")]
        for h in users:
            m = h.pop("msg")
            if persona.get("vision") and h in users[-2:] and image_attachments(m):
                urls = [await image_data_uri(a) for a in image_attachments(m)[:3]]
                h["content"] = [{"type": "text", "text": h["content"] or "（画像）"}] + \
                    [{"type": "image_url", "image_url": {"url": u}} for u in urls if u]
            elif not h["content"].strip():
                h["content"] = "（画像を貼った）"
        return history

    # ---- GPU（ペルソナごとのモデル・ComfyUI） ----
    async def ensure_gpu(self, msg: discord.Message, persona: dict, ccfg: dict) -> bool:
        """会話のモデルを用意する。ルナ（novel_gpu）は Gemma に切り替えて Gemma で話す（配信の時間帯などで借りられなければ
        Strata）。ほかのペルソナは Strata で、Gemma / ComfyUI に貸したままなら Strata に戻す。用意できなければ False。"""
        try:
            if persona.get("novel_gpu") and not gpu_mode.blocked_reason(ccfg):
                if gpu_mode.current() != "luna":
                    note = await msg.channel.send("-# ⏳ Gemma に切り替えています（2〜3分）…")
                    try:
                        await asyncio.to_thread(gpu_mode.to_luna, ccfg)
                    finally:
                        await note.delete()
                strata_chat.USE_NOVEL_LLM.set(True)   # この発言の処理（会話・記憶・プロンプト作り）は Gemma で
                return True
            if gpu_mode.held_by_bot():
                note = await msg.channel.send("-# ⏳ Strata に戻しています（2〜3分）…")
                await asyncio.to_thread(gpu_mode.to_llm)
                await note.delete()
            return True
        except Exception as ex:   # noqa: BLE001
            log.error("gpu: %s", traceback.format_exc())
            await msg.reply(f"⚠️ モデルを切り替えられませんでした: {str(ex)[:300]}", mention_author=False)
        return False

    # ---- 執筆モード（ルナ: 小説は Gemma で書き、書き直し・続きの注文を受け続ける。名前は「ルナ（執筆中）」） ----
    async def writing_mode(self, msg: discord.Message, persona: dict, ccfg: dict) -> bool:
        """執筆モードの入口。執筆モードで答えたら True（ふだんの会話＝Strata に回すなら False）。"""
        text = msg.content.strip()
        if strata_chat.writing_state(persona["id"]).get("active"):
            if WRITING_EXIT.search(text):   # はっきり「やめて」と言われたら、そのまま抜ける
                await self.end_writing(persona, msg.channel)
                return False   # 抜けたあと、いつものルナ（Gemma のまま）としてこの発言に答える
            if NEW_STORY.search(text):      # 別の小説・キャラ変更: 前の小説は忘れて、打ち合わせ（キャラ・シチュエーションの問答）から
                strata_chat.restart_writing(text, ccfg)
                await msg.channel.send("-# 🔄 新しい小説だね。前の話はいったん置いて、キャラとシチュエーションの確認から始めるよ")
                await self.writing(msg, persona, ccfg)
                return True
            if WRITING_LEAVE.search(text):  # プロンプト・絵の話はふだんの会話・ComfyUI の担当。抜けるか確認してから
                await self.ask_switch(msg, "leave_writing", "執筆モードを抜けて、いつものルナに戻る？"
                                      "「いいえ」なら執筆の注文として受けるね")
                return True
            await self.writing(msg, persona, ccfg)
            return True
        if WRITING_ENTER.search(text) and not WRITING_EXIT.search(text) and not gpu_mode.blocked_reason(ccfg):
            # 入る前の直近5往復ほどを依頼として持ち込む（最後の行がこの発言）
            history = await self.chat_history(msg, ccfg)
            lines = [f"{h['name']}: {strata_chat.text_of(h['content'])[:600]}" for h in history[-11:]]
            await self.ask_switch(msg, "enter_writing", self.writing_question(), request="\n".join(lines))
            return True
        return False   # 配信の時間帯などで Gemma に切り替えられないときは、Strata が受ける（1本だけ書く）

    @staticmethod
    def writing_question() -> str:
        if gpu_mode.current() == "luna":
            return "執筆モードに入って書く？"
        return "執筆モード（Gemma）に切り替えて書く？ 切り替えに2〜3分かかるよ"

    async def enter_writing(self, msg: discord.Message, persona: dict, ccfg: dict, request: str) -> bool:
        """GPU を Gemma に切り替え、依頼の作品・キャラを下調べして執筆モードに入る。入れなければ False。"""
        blocked = gpu_mode.blocked_reason(ccfg)
        if blocked:
            await msg.channel.send(f"-# いまは{blocked}なので、執筆モード（Gemma）には入れません。Strata のまま書きます")
            return False
        note = await msg.channel.send("-# ✍️ 執筆モードに入ります（Gemma に切り替え中、2〜3分）…")
        try:
            await asyncio.to_thread(gpu_mode.to_luna, ccfg)
            async with msg.channel.typing():
                st = await asyncio.to_thread(strata_chat.start_writing, request, ccfg)
        except Exception:
            await asyncio.to_thread(gpu_mode.to_llm)
            raise
        finally:
            await note.delete()
        await self.set_nick(msg.channel, persona, "執筆中")
        if st.get("studied"):
            await msg.channel.send("-# 📚 下調べ: " + " / ".join(st["studied"]))
        self.last_chat = time.monotonic()
        return True

    async def writing(self, msg: discord.Message, persona: dict, ccfg: dict) -> None:
        """執筆モードの1往復（Gemma）。小説なら本文とファイル、相談なら返事。そのあと記憶（作法など）を Gemma で更新。"""
        if gpu_mode.current() != "luna":   # 配信・ギモン深堀りに GPU を返したあと
            try:
                note = await msg.channel.send("-# ⏳ Gemma に戻しています（2〜3分）…")
                await asyncio.to_thread(gpu_mode.to_luna, ccfg)
                await note.delete()
            except gpu_mode.Busy as ex:
                await self.end_writing(persona, msg.channel, to_strata=True)
                await msg.channel.send(f"-# いまは{ex}なので、執筆モードを終えたよ。終わってからもう一度頼んでね")
                return
        images = [await image_data_uri(a) for a in image_attachments(msg)[:3]] if persona.get("vision") else []
        async with msg.channel.typing():
            res = await asyncio.to_thread(strata_chat.writing_turn, msg.content.strip(), [u for u in images if u], ccfg)
        if res.get("studied"):
            await msg.channel.send("-# 📚 下調べ: " + " / ".join(res["studied"]))
        if res["kind"] == "novel":
            await self.chat_send(msg, f"{NOVEL_MARK}『{res['title']}』\n\n" + res["text"].split("\n", 1)[-1].strip(),
                                 file=res["path"])
            try:   # 次の展開の提案4つ（番号か自由回答で答えてもらう）
                async with msg.channel.typing():
                    sugg = await asyncio.to_thread(strata_chat.suggest_next, ccfg)
                if sugg:
                    await self.chat_send(msg, "💡 次はどうする？ 番号か、自由に教えてね\n"
                                         + "\n".join(f"**{i}.** {s}" for i, s in enumerate(sugg, 1)))
            except Exception:   # noqa: BLE001
                log.error("suggest: %s", traceback.format_exc())
        else:
            await self.chat_send(msg, res["text"])
        self.last_chat = time.monotonic()
        try:
            hist = [{"role": "user", "content": msg.content}, {"role": "assistant", "content": res["text"][:1500]}]
            added = await asyncio.to_thread(strata_chat.update_memory, strata_chat.novel_persona(ccfg), hist, ccfg)
            if added and ccfg.get("memory", {}).get("announce", True):
                await msg.channel.send("-# 🧠 覚えたこと: " + " / ".join(a[:40] for a in added)[:1800])
        except Exception:   # noqa: BLE001
            log.error("memory: %s", traceback.format_exc())

    async def end_writing(self, persona: dict, channel, to_strata: bool = False) -> None:
        """執筆モードを終える（依頼・下調べ・最後の本文は次の執筆に持ち越す）。名前を戻す。
        GPU はふつう Gemma のまま（ルナのふだんの会話も Gemma）。to_strata: 無言・配信・ギモン深堀りのときは Strata に戻す。"""
        strata_chat.end_writing(persona["id"])
        await self.set_nick(channel, persona, None)
        if to_strata and gpu_mode.held_by_bot():
            note = await channel.send("-# ⏳ 執筆モードを終えて、Strata に戻しています（2〜3分）…")
            await asyncio.to_thread(gpu_mode.to_llm)
            await note.delete()
        self.last_chat = time.monotonic()

    async def set_nick(self, channel, persona: dict, label: str | None) -> None:
        """ルナの Bot のサーバーでの表示名を「ルナ（執筆中）」「ルナ（お絵描き中）」⇔ 元に（ニックネームの変更権限が無ければ何もしない）。"""
        guild = getattr(channel, "guild", None)
        if not guild:
            return
        try:
            await guild.me.edit(nick=f"{persona['name']}（{label}）" if label else None)
        except discord.HTTPException as ex:
            log.warning("nick: %s", ex)

    # ---- お絵描きモード（ComfyUI。名前は「ルナ（お絵描き中）」で、LLM は止めているのでおしゃべりはしない） ----
    #   入る: 「描く？」の確認に「はい」/「!comfyui」。描画セットがあれば人物の候補5枚 → 番号で選ぶ → シチュエーション → 数字で追加
    #   出る: 「!gemma」か「0」（Gemma に戻る）。ComfyUI が動かず発言もないまま30分（draw_idle_minutes）でも Gemma に戻る
    async def mode_cmd(self, msg: discord.Message, persona: dict, ccfg: dict, comfy: bool) -> None:
        """「!comfyui」「!gemma」。"""
        if not persona.get("novel_gpu"):
            await msg.reply("これはルナのチャンネルで使ってね", mention_author=False)
            return
        drawing = strata_chat.draw_session(persona["id"]).get("active")
        if comfy:
            if drawing and gpu_mode.current() == "comfy":
                await msg.channel.send("-# 🎨 もうお絵描き中だよ。!gemma で戻るね")
                return
            await self.enter_draw(msg.channel, persona, ccfg)
            await msg.channel.send("-# 🎨 ComfyUI を起動したよ（http://127.0.0.1:8188）。お絵描き中はおしゃべりできないよ。"
                                   "!gemma で戻るね")
        elif drawing:
            await self.end_draw(persona, msg.channel, "お絵描きモードを終えたよ")
        elif await self.ensure_gpu(msg, persona, ccfg):
            self.last_chat = time.monotonic()
            await msg.channel.send("-# いまは Gemma だよ" if gpu_mode.current() == "luna"
                                   else f"-# いまは{gpu_mode.blocked_reason(ccfg)}なので、Strata で答えるね")

    async def enter_draw(self, channel, persona: dict, ccfg: dict, path=None) -> dict:
        """ComfyUI に切り替えてお絵描きモードに入る（執筆モードなら終える）。path: 描画セット（無ければ描くものは決めずに待つ）。"""
        note = await channel.send("-# 🎨 お絵描きモードに入ります（ComfyUI に切り替え中、1〜2分）…")
        try:
            await asyncio.to_thread(gpu_mode.to_comfy, ccfg)
        finally:
            await note.delete()
        if strata_chat.writing_state(persona["id"]).get("active"):
            strata_chat.end_writing(persona["id"])
        st = {"active": True, "file": str(path) if path else None, "phase": "pick" if path else "free",
              "chosen": None, "next": 0, "drawn": 0, "last": time.time()}
        strata_chat.save_draw_session(persona["id"], st)
        self.comfy_at = time.time()
        await self.set_nick(channel, persona, "お絵描き中")
        return st

    async def start_draw(self, msg: discord.Message, persona: dict, path, ccfg: dict) -> None:
        st = await self.enter_draw(msg.channel, persona, ccfg, path)
        await self.draw_candidates(msg, persona, st, ccfg)

    def _draw_one(self, doc: dict, cand: int, sit: dict | None, ccfg: dict, seed: int | None, name: str):
        """1枚描いて保存する（ComfyUI）。返り値: ファイルのパス。"""
        mcfg = ccfg.get("moss") or {}
        prompt, size = strata_chat.draw_prompt(doc, cand, sit, mcfg)
        w, h = strata_chat.SIZES.get(size, strata_chat.SIZES["portrait"])
        png = comfy_client.generate(prompt, doc["negative"], w, h, mcfg, seed=seed)
        out_dir = strata_chat.ROOT / ".local" / "luna" / "images"
        out_dir.mkdir(parents=True, exist_ok=True)
        file = out_dir / name
        file.write_bytes(png)
        return file

    async def draw_candidates(self, msg: discord.Message, persona: dict, st: dict, ccfg: dict) -> None:
        """人物だけ（背景なし）の候補を、同じシードで1枚ずつ（書き方の違いだけを比べる）。"""
        doc = json.loads(Path(st["file"]).read_text(encoding="utf-8"))
        seed = random.randint(0, 2**31 - 1)
        stem = Path(st["file"]).stem
        await msg.channel.send(f"-# 🎨 まず人物だけの候補を {len(doc['candidates'])} 枚描くね（1枚目はモデルの読み込みで10分ほど）")
        for i, c in enumerate(doc["candidates"]):
            async with msg.channel.typing():
                file = await asyncio.to_thread(self._draw_one, doc, i, None, ccfg, seed, f"{stem}-c{i + 1}-{seed}.png")
            await self.chat_send(msg, f"{DRAW_MARK}候補{i + 1}: {c['label']}", file=file)
        st.update(phase="pick", last=time.time())
        strata_chat.save_draw_session(persona["id"], st)
        await self.chat_send(msg, f"👆 どれが一番それっぽい？ 番号（1〜{len(doc['candidates'])}）で選んでね。"
                                  "「もう一度」で別のシードで描き直し、0 で終わり")

    async def draw_situations(self, msg: discord.Message, persona: dict, st: dict, ccfg: dict, k: int) -> None:
        """選んだ人物・絵柄で、シチュエーションを k 枚（シードは毎回ランダム）。1巡目は順番に、そのあとはランダムに選ぶ。"""
        doc = json.loads(Path(st["file"]).read_text(encoding="utf-8"))
        sits = doc["situations"]
        stem = Path(st["file"]).stem
        for _ in range(k):
            sit = sits[st["next"]] if st["next"] < len(sits) else random.choice(sits)
            st["next"] += 1
            st["drawn"] += 1
            async with msg.channel.typing():
                file = await asyncio.to_thread(self._draw_one, doc, st["chosen"], sit, ccfg, None, f"{stem}-s{st['drawn']:03d}.png")
            await self.chat_send(msg, f"{DRAW_MARK}#{st['drawn']} " + (sit.get("caption") or "")[:300], file=file)
            st["last"] = time.time()
            strata_chat.save_draw_session(persona["id"], st)
        await self.chat_send(msg, "✨ さらに描くなら枚数を数字で教えてね（例: 10）。0 で終わり")

    async def draw_input(self, msg: discord.Message, persona: dict, ccfg: dict) -> bool:
        """お絵描きモード中の発言。お絵描きモードでなければ False。"""
        st = strata_chat.draw_session(persona["id"])
        if not st.get("active"):
            return False
        text = msg.content.strip().translate(str.maketrans("０１２３４５６７８９", "0123456789"))
        m = re.fullmatch(r"(\d+)\s*(枚|番)?", text)
        if m and int(m.group(1)) == 0 or WRITING_EXIT.search(text) or re.fullmatch(r"(終わり|おわり|終了|やめる|やめて)[!！。]*", text):
            await self.end_draw(persona, msg.channel, "お絵描きモードを終えたよ")
            return True
        if gpu_mode.current() != "comfy":   # 配信などで GPU を返したあと
            await self.end_draw(persona, msg.channel, "ComfyUI が止まっていたので、お絵描きモードを終えたよ", to=None)
            return True
        st["last"] = time.time()
        if st["phase"] == "free":   # 「!comfyui」で入った（描くものは決めていない。ComfyUI の画面で触る）
            strata_chat.save_draw_session(persona["id"], st)
            await msg.channel.send("-# 🎨 お絵描き中（ComfyUI）だから、おしゃべりはできないよ。!gemma で戻るね")
            return True
        doc = json.loads(Path(st["file"]).read_text(encoding="utf-8"))
        if st["phase"] == "pick":
            if re.search(r"もう一度|もういちど|やり直|描き直", text):
                await self.draw_candidates(msg, persona, st, ccfg)
            elif m and 1 <= int(m.group(1)) <= len(doc["candidates"]):
                st.update(chosen=int(m.group(1)) - 1, phase="run")
                strata_chat.save_draw_session(persona["id"], st)
                await msg.channel.send(f"-# 🎨 候補{m.group(1)}（{doc['candidates'][st['chosen']]['label']}）で人物と絵柄を固定して、"
                                       f"シチュエーションを {len(doc['situations'])} 枚描くね")
                await self.draw_situations(msg, persona, st, ccfg, len(doc["situations"]))
            else:
                await msg.channel.send(f"-# 🎨 お絵描き中（おしゃべりはできないよ）。"
                                       f"番号（1〜{len(doc['candidates'])}）で選ぶか、「もう一度」、0 か !gemma で終わり")
        elif m:
            await self.draw_situations(msg, persona, st, ccfg, min(int(m.group(1)), 50))
        else:
            await msg.channel.send("-# 🎨 お絵描き中（おしゃべりはできないよ）。枚数を数字で、0 か !gemma で終わり")
        return True

    async def end_draw(self, persona: dict, channel, why: str, to: str | None = "gemma") -> None:
        """お絵描きモードを終える。to: "gemma"（ふつう。配信の時間帯で貸せなければ Strata）/ "llm"（配信・ギモン深堀り）/
        None（ComfyUI がもう止まっているので切り替えない）。"""
        st = strata_chat.draw_session(persona["id"])
        strata_chat.save_draw_session(persona["id"], {"active": False, "last_file": st.get("file"), "drawn": st.get("drawn", 0)})
        await self.set_nick(channel, persona, None)
        if to == "gemma":
            note = await channel.send(f"-# ⏳ {why}。Gemma に戻しています（2〜3分）…")
            try:
                await asyncio.to_thread(gpu_mode.to_luna, strata_chat.section_config())
            except gpu_mode.Busy:
                await asyncio.to_thread(gpu_mode.to_llm)
            finally:
                await note.delete()
        elif to == "llm" and gpu_mode.held_by_bot():
            note = await channel.send(f"-# ⏳ {why}。Strata に戻しています（2〜3分）…")
            await asyncio.to_thread(gpu_mode.to_llm)
            await note.delete()
        await channel.send(f"-# 🎨 {why}（{st.get('drawn', 0)} 枚描いたよ）。いつものルナに戻ったよ")
        self.last_chat = time.monotonic()

    @staticmethod
    def help_text(luna: bool) -> str:
        """「!」で出すコマンドの一覧（ルナのチャンネルとトークチャンネルで中身を変える）。"""
        lines = ["📋 **コマンド一覧**"]
        if not luna:
            lines += ["`!ペルソナ` ペルソナの一覧 ／ `!ペルソナ 名前` 切り替え"]
        lines += ["`!記憶` 覚えている好みの一覧 ／ `!記憶 まとめ` 似たものをまとめて減らす（`!記憶 まとめ 25` で件数指定）／ "
                  "`!忘れて 3 5` 番号の項目を消す",
                  "`!作法` 小説の作法の一覧 ／ `!作法 〇〇` 追加 ／ `!作法 削除 3` ／ `!作法 書き換え`＋改行＋全文",
                  "`!` このヘルプ"]
        if luna:
            lines += ["`!comfyui` ComfyUI を起動してお絵描きモードに（名前が「ルナ（お絵描き中）」、おしゃべりは止まる）",
                      "`!gemma` お絵描きモードを抜けて Gemma に戻る（「0」でも抜ける）",
                      "`!消去 50` 古い順に50件消す ／ `!消去 新しい 10` 新しい順に10件（1回500件まで）",
                      "",
                      "**言葉で切り替わるもの**",
                      "・「小説を書いて」など → 執筆モードに入るか確認（はい／いいえ）。「執筆やめて」で抜ける",
                      "・執筆中に「別の小説」「キャラを変える」→ 打ち合わせからやり直し。提案の番号（1〜4）で続きを選べる",
                      "・「〇〇の絵のプロンプト作って」→ 描画セットを作って、お絵描きモードに入るか確認",
                      "・お絵描き中: 番号で候補を選ぶ ／「もう一度」で描き直し ／ 数字で追加の枚数",
                      "",
                      "-# 自動で戻るもの: お絵描きは ComfyUI が30分動かなければ Gemma へ、話さないまま20分で Strata へ。"
                      "定時の配信（5:30〜7:00）の15分前からは Strata に戻す"]
        return "\n".join(lines)

    async def clear_cmd(self, msg: discord.Message) -> None:
        """「!消去 50」古い順に50件 /「!消去 新しい 10」新しい順に10件（このコマンドより前の発言。1回500件まで）。
        執筆モードの記録（state の writing_luna.json）は消さない。会話の履歴は Discord から読むので、消した分は忘れる。"""
        words = msg.content.split()[1:]
        nums = [int(w) for w in words if w.isdigit()]
        if not nums:
            await msg.reply("🧹 「!消去 50」で古い順に50件、「!消去 新しい 10」で新しい順に10件消すよ（1回500件まで）",
                            mention_author=False)
            return
        newest = any(w in ("新しい", "新しい順", "最近", "new") for w in words)
        targets = [m async for m in msg.channel.history(limit=min(nums[0], 500), before=msg, oldest_first=not newest)]
        me = msg.guild.me if msg.guild else None
        if not (me and msg.channel.permissions_for(me).manage_messages):   # 権限が無ければ、自分（ルナ）の発言だけ
            targets = [m for m in targets if m.author.id == me.id] if me else []
            note = "（メッセージの管理の権限が無いので、ルナの発言だけ消したよ）"
        else:
            note = ""
        # 14日以内は100件ずつまとめて、それより古いものは1件ずつ（Discord の決まり）
        cutoff = discord.utils.utcnow() - dt.timedelta(days=13, hours=23)
        recent = [m for m in targets if m.created_at > cutoff] if not note else []   # まとめて消すのにも権限が要る
        cutoff = cutoff if not note else discord.utils.utcnow()
        done = 0
        for i in range(0, len(recent), 100):
            chunk = recent[i:i + 100]
            try:
                if len(chunk) == 1:
                    await chunk[0].delete()
                else:
                    await msg.channel.delete_messages(chunk)
                done += len(chunk)
            except discord.HTTPException as ex:
                log.warning("clear: %s", ex)
        for m in targets:
            if m.created_at <= cutoff:
                try:
                    await m.delete()
                    done += 1
                except discord.HTTPException as ex:
                    log.warning("clear: %s", ex)
        try:
            await msg.delete()
        except discord.HTTPException:
            pass
        await msg.channel.send(f"-# 🧹 {done} 件消したよ{note}", delete_after=15)

    async def craft_cmd(self, msg: discord.Message, ccfg: dict) -> None:
        """小説の作法（.local/persona/<id>_craft.md。ファイルを直接編集してもよい）。
        「!作法」一覧 /「!作法 〇〇」1項目追加 /「!作法 削除 3 5」番号で削除 /「!作法 書き換え」＋改行＋全文で丸ごと置き換え。"""
        persona = strata_chat.load_persona(ccfg)
        if not persona.get("memory"):
            await msg.reply(f"✍️ {persona['name']}は作法を記憶しない設定です（ペルソナの memory: true で有効）。", mention_author=False)
            return
        pid, name = persona["id"], persona["name"]
        rest = msg.content.strip().split(maxsplit=1)[1] if len(msg.content.strip().split(maxsplit=1)) > 1 else ""
        head = rest.split("\n", 1)[0].strip()
        if head.startswith(("書き換え", "かきかえ", "上書き")):
            body = rest.split("\n", 1)[1] if "\n" in rest else ""
            if not body.strip():
                await msg.reply("✍️ 「!作法 書き換え」の次の行から、作法の全文を書いてね（「- 」で始まる行が1項目）", mention_author=False)
                return
            strata_chat.save_craft(pid, body, name)
            await msg.reply(f"✍️ 作法を書き換えたよ（{len(strata_chat.craft_items(pid))}項目）", mention_author=False)
            return
        if head.startswith(("削除", "消して", "けして")):
            gone = strata_chat.remove_craft(pid, [int(x) for x in re.findall(r"\d+", head)])
            await msg.reply("✍️ 消したよ: " + (" / ".join(gone) if gone else "（該当なし。番号は「!作法」で確認）"), mention_author=False)
            return
        if rest:
            strata_chat.add_craft(pid, rest, name)
            await msg.reply(f"✍️ 作法に足したよ: {rest[:120]}", mention_author=False)
            return
        n, lines = 0, []
        for line in strata_chat.load_craft(pid).splitlines():
            if line.startswith("- "):
                n += 1
                lines.append(f"{n}. {line[2:]}")
            elif line.strip() and not line.startswith(("# ", "（")):
                lines.append(line)
        text = (f"✍️ {name}の小説の作法（{n}項目）\n" + ("\n".join(lines) or "（まだありません）")
                + "\n-# 「!作法 〇〇」で追加 /「!作法 削除 番号」/「!作法 書き換え」＋改行＋全文で置き換え。"
                f"ファイル: {strata_chat.craft_path(pid)}")
        for part in chunks(text):
            await msg.channel.send(part)

    async def draw(self, msg: discord.Message, persona: dict, path, arg: str, ccfg: dict) -> None:
        """保存したプロンプトから数枚選んで、ComfyUI（MOSS-Image）で描いてチャットに貼る。描いたあともお絵描きモードのまま。"""
        mcfg = ccfg.get("moss") or {}
        picks = strata_chat.pick_prompts(path, arg, int(mcfg.get("draw_count", 4)))
        doc = json.loads(path.read_text(encoding="utf-8"))
        out_dir = strata_chat.ROOT / ".local" / "luna" / "images"
        out_dir.mkdir(parents=True, exist_ok=True)
        await self.enter_draw(msg.channel, persona, ccfg)
        await msg.channel.send(f"-# 🎨 {len(picks)} 枚描くね（" + " ".join(f"#{p['id']}" for p in picks) + "）")
        for p in picks:
            w, h = strata_chat.SIZES.get(p.get("size"), strata_chat.SIZES["portrait"])
            async with msg.channel.typing():
                png = await asyncio.to_thread(comfy_client.generate, p["prompt"], doc["negative"], w, h, mcfg)
            file = out_dir / f"{path.stem}-{p['id']:03d}.png"
            file.write_bytes(png)
            await self.chat_send(msg, f"{DRAW_MARK}#{p['id']} " + (p.get("caption") or "")[:300], file=file)
        await msg.channel.send("-# 🎨 描き終わったよ。お絵描きモードのままにしておくね（!gemma で戻る）")

    async def idle_loop(self) -> None:
        """GPU を Gemma に貸したまま一定時間（20分）話さなければ、Strata に戻す（ルナは話しているあいだ Gemma のまま）。
        お絵描きモード（ComfyUI）は、ComfyUI が動かず発言もないまま30分で Gemma に戻す（そこから20分で Strata）。
        定時の配信の時間帯に入ったら（始まる15分前から）、話・お絵描きの途中でも Strata に戻す。"""
        await self.wait_until_ready()
        while True:
            await asyncio.sleep(60)
            try:
                ccfg = strata_chat.section_config()
                lcfg = ccfg.get("luna") or {}
                idle = int(lcfg.get("idle_minutes", 20)) * 60
                st = strata_chat.writing_state("luna")
                ds = strata_chat.draw_session("luna")
                why = gpu_mode.blocked_reason(ccfg)
                if ds.get("active"):
                    busy, last_id = await asyncio.to_thread(gpu_mode.comfy_activity)
                    if busy or (last_id and last_id != self.comfy_last):   # ComfyUI の画面から描いた分も数える
                        self.comfy_at, self.comfy_last = time.time(), last_id
                    if self.chat_lock.locked():   # Discord から描いている最中
                        continue
                    quiet = time.time() - max(self.comfy_at, ds.get("last", 0))
                    ch = self.luna_writing_channel()
                    luna = strata_chat.personas(ccfg)["luna"]
                    async with self.chat_lock:
                        if not ch:
                            if why or quiet > int(lcfg.get("draw_idle_minutes", 30)) * 60:
                                strata_chat.save_draw_session("luna", {"active": False})
                                await asyncio.to_thread(gpu_mode.to_llm)
                        elif why:
                            await self.end_draw(luna, ch, f"{why}なので、お絵描きモードを終えたよ", to="llm")
                        elif gpu_mode.current() != "comfy":
                            await self.end_draw(luna, ch, "ComfyUI が止まっていたので、お絵描きモードを終えたよ", to=None)
                        elif quiet > int(lcfg.get("draw_idle_minutes", 30)) * 60:
                            await self.end_draw(luna, ch, f"{int(quiet // 60)}分 ComfyUI が動いていなかったので、お絵描きモードを終えたよ")
                    continue
                if why and gpu_mode.held_by_bot() and not self.chat_lock.locked():
                    # 会話・執筆中なら、ここで Strata に返す
                    ch = self.luna_writing_channel()
                    async with self.chat_lock:
                        if st.get("active") and ch:
                            await self.end_writing(strata_chat.personas(ccfg)["luna"], ch, to_strata=True)
                        else:
                            strata_chat.end_writing("luna")
                            await asyncio.to_thread(gpu_mode.to_llm)
                    if ch:
                        await ch.send(f"-# ⏰ {why}なので、GPU を Strata に返したよ。そのあいだは Strata で答えるね")
                    log.info("idle: 配信の時間帯なので GPU を Strata に戻しました")
                elif st.get("active") and time.time() - st.get("last", 0) > idle and not self.chat_lock.locked():
                    ch = self.luna_writing_channel()
                    async with self.chat_lock:
                        if ch:
                            await self.end_writing(strata_chat.personas(ccfg)["luna"], ch, to_strata=True)
                            await ch.send(f"-# 💤 {idle // 60}分お話がなかったので、執筆モードを終えたよ")
                        else:
                            strata_chat.end_writing("luna")
                            await asyncio.to_thread(gpu_mode.to_llm)
                elif gpu_mode.held_by_bot() and time.monotonic() - self.last_chat > idle and not self.chat_lock.locked():
                    async with self.chat_lock:
                        await asyncio.to_thread(gpu_mode.to_llm)
                    log.info("idle: GPU を Strata に戻しました")
            except Exception:   # noqa: BLE001
                log.error("idle: %s", traceback.format_exc())

    def luna_writing_channel(self):
        return self.luna_bot.get_channel(self.luna_channel_id) if self.luna_bot and self.luna_channel_id else None

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
        words = msg.content.split()[1:]
        if words and words[0] in ("まとめ", "まとめて", "圧縮"):   # 「!記憶 まとめ 25」似たものをまとめて減らす
            target = next((int(w) for w in words[1:] if w.isdigit()), None)
            async with self.chat_lock:
                if not await self.ensure_gpu(msg, persona, ccfg):   # ルナは Gemma でまとめる
                    return
                persona = strata_chat.load_persona(ccfg)
                async with msg.channel.typing():
                    try:
                        before, after = await asyncio.to_thread(strata_chat.compact_memory, persona, ccfg, target)
                    except Exception as ex:   # noqa: BLE001
                        await msg.reply(f"⚠️ まとめられませんでした: {str(ex)[:300]}", mention_author=False)
                        return
            await msg.channel.send(f"🧠 記憶を {before}→{after} 件にまとめたよ（前のファイルは .local/persona/backup/ に残してある）")
        items = strata_chat.load_memory(persona["id"])
        lines = [f"{i}. [{strata_chat.KIND_LABEL.get(m['kind'], m['kind'])}] {m['text']}" for i, m in enumerate(items, 1)]
        text = (f"🧠 {persona['name']}が覚えていること（{len(items)}件。「!忘れて 番号」で消せます）\n" + ("\n".join(lines) or "（まだありません）")
                + f"\n-# ファイルを直接編集してもよい: {strata_chat.memory_path(persona['id'])}")
        for part in chunks(text):
            await msg.channel.send(part)

    async def switch_persona(self, msg: discord.Message, ccfg: dict) -> None:
        """「!ペルソナ」で一覧、「!ペルソナ 名前」で切り替え（切り替えたら、そのペルソナがあいさつする）。"""
        words = msg.content.split(maxsplit=1)
        now = strata_chat.load_persona(ccfg)
        # 専用チャンネル（別アカウントの Bot）があるペルソナは、ここでは一覧にも出さず呼べない（会話・記憶を混ぜない）
        hidden = {"luna"} if self.luna_channel_id else set()
        if len(words) == 1:
            lines = [f"{'▶' if p['id'] == now['id'] else '・'} {p['name']}（{p['id']}）"
                     for p in strata_chat.personas(ccfg).values() if p["id"] not in hidden]
            await msg.reply("ペルソナ一覧（「!ペルソナ 名前」で切り替え）\n" + "\n".join(lines), mention_author=False)
            return
        p = strata_chat.find_persona(ccfg, words[1])
        if p and p["id"] in hidden:
            p = None
        if not p:
            await msg.reply(f"⚠️ 「{words[1][:50]}」というペルソナはありません。「!ペルソナ」で一覧を出せます。",
                            mention_author=False)
            return
        async with self.chat_lock:
            if not await self.ensure_gpu(msg, p, ccfg):   # ルナなら Gemma を起動、しらべなら Strata に戻す
                return
            strata_chat.set_persona(p["id"])
            self.last_chat = time.monotonic()
        await self.chat_send(msg, p.get("greeting") or f"{p['name']}に切り替えました。")

    async def chat_send(self, msg: discord.Message, text: str, file=None) -> None:
        """ペルソナの名前・アイコンで（.env の DISCORD_CHAT_WEBHOOK_URL があれば Webhook、無ければ Bot の返信）。
        小説（NOVEL_MARK で始まる）は、分割した2通目以降にも印を付ける（履歴で読み飛ばすため）。file は最後の1通に添付。"""
        # Webhook はしらべのトークチャンネル用。ルナ専用チャンネルは LunaBot 自身（msg を受けた Bot）の名前で返す
        url = os.environ.get("DISCORD_CHAT_WEBHOOK_URL") if msg.channel.id == self.chat_channel_id else None
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
                writing = strata_chat.writing_state("luna").get("active")
                drawing = strata_chat.draw_session("luna").get("active")
                if gpu_mode.held_by_bot() or writing or drawing:   # ギモン深堀りは Strata を使う。ルナに貸していたら返してもらう
                    async with self.chat_lock:
                        await thread.send("-# ⏳ GPU を Strata に戻しています（2〜3分）…")
                        ch = self.luna_writing_channel() if writing or drawing else None
                        luna = strata_chat.personas(strata_chat.section_config())["luna"]
                        if ch and drawing:   # ルナの側には理由（ギモン深堀り）を書かない
                            await self.end_draw(luna, ch, "GPU が必要になったので、お絵描きモードを終えたよ", to="llm")
                        elif ch:
                            await self.end_writing(luna, ch, to_strata=True)
                            await ch.send("-# GPU が必要になったので、執筆モードを終えたよ")
                        else:
                            if writing:
                                strata_chat.end_writing("luna")
                            if drawing:
                                strata_chat.save_draw_session("luna", {"active": False})
                            await asyncio.to_thread(gpu_mode.to_llm)
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


class LunaBot(discord.Client):
    """ルナ専用チャンネルを受ける別の Bot（名前・アイコン・入力中の表示がルナになる）。処理は main（QABot）の chat に渡す。
    別の Discord アカウントで作った Bot・別のサーバーでもよい。話しかけられるのは、この Bot の持ち主
    （開発者ポータルのアプリの Owner / Team）と config の strata_chat.luna_owner_ids だけ（ギモン深堀の持ち主とは別）。"""

    def __init__(self, main: QABot, channel_id: int, owner_ids=()):
        intents = discord.Intents.default()
        intents.message_content = True
        super().__init__(intents=intents)
        self.main, self.channel_id = main, channel_id
        self.owners: set[int] = {int(x) for x in owner_ids}

    async def on_ready(self) -> None:
        app = await self.application_info()
        if app.owner:
            self.owners.add(app.owner.id)
        if getattr(app, "team", None):
            self.owners |= {m.id for m in app.team.members}
        log.info("luna ready: %s / channel %s / owners %s", self.user, self.channel_id, self.owners)

    async def on_message(self, msg: discord.Message) -> None:
        if msg.author.bot or msg.channel.id != self.channel_id or msg.author.id not in self.owners:
            return
        asyncio.create_task(self.main.chat(msg, persona_id="luna"))


async def run_bots(bot: QABot, token: str, luna: LunaBot | None, luna_token: str | None) -> None:
    async with bot:
        if not luna:
            await bot.start(token)
            return
        async with luna:
            await asyncio.gather(bot.start(token), luna.start(luna_token))


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
    luna_token, luna_channel = os.environ.get("DISCORD_LUNA_BOT_TOKEN"), os.environ.get("DISCORD_LUNA_CHANNEL_ID")
    if not strata_chat.section_config().get("enabled", True):
        chat_channel = luna_channel = None
    if not luna_token:
        luna_channel = None
    bot = QABot(cfg, int(channel), int(chat_channel) if chat_channel else None, int(luna_channel) if luna_channel else None)
    luna = LunaBot(bot, int(luna_channel), strata_chat.section_config().get("luna_owner_ids") or ()) if luna_channel else None
    bot.luna_bot = luna
    if luna and load_state(strata_chat.SECTION).get("persona") == "luna":   # トークチャンネルはルナを使わない
        strata_chat.set_persona(strata_chat.section_config().get("default_persona") or "shirabe")
    asyncio.run(run_bots(bot, token, luna, luna_token))


if __name__ == "__main__":
    sys.exit(main())
