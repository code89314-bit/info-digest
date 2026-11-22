"""Read a series script aloud with Gemini TTS (Google AI Studio API) and save an mp3.

Gemini TTS does expressive speech (intonation, laughs, pauses) from natural-language directions, but a request can have
at most 2 speakers. The script is cut into chunks that have at most 2 distinct speakers, each chunk is one request,
and the chunks are joined.

Key: GEMINI_API_KEY in .env (never commit it). Voices: style/series_voices.yaml `gemini:` (voice names), or defaults.

usage:
  python tts_gemini.py --models                                   # list TTS-capable models for this key
  python tts_gemini.py <week_dir> <n> --lines 25                  # first 25 lines only (a sample)
  python tts_gemini.py <week_dir> <n> --model gemini-2.5-pro-preview-tts
"""
import argparse
import base64
import io
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
import array
import wave
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from tts_render import ROOT, SPEAKERS, parse  # noqa: E402

API = "https://generativelanguage.googleapis.com/v1beta"
DEFAULT_VOICES = {"堀田": "Charon", "横井": "Sulafat", "司": "Puck"}
RATE = 24000          # Gemini TTS returns 24 kHz 16-bit mono PCM
PROMPT = """以下は、日本の病院薬剤師向けの学習ポッドキャストの会話です。3人は気心の知れた仲間で、くつろいだ雰囲気で話しています。
自然な日本語の会話として、抑揚・笑い・タメ・相づちをつけて読んでください。
- 行の先頭の（ ）は話し方の指示です。指示どおりに話し、（ ）の中の言葉は読まないでください。
- [笑] は短い自然な笑い、[間] はひと呼吸の間です。文字としては読まないでください。
- 堀田: 落ち着いた男性のがん専門薬剤師。話が乗ると熱が入る。
- 横井: 堀田の部下の、大人の女性薬剤師。落ち着いた声で、さらっとユーモアを効かせる。
- 司: 他の分野のベテラン男性薬剤師で、番組の進行役（タイトルコール・締め）。普通の高さの声（低すぎない）。丁寧だが、ノリが良く明るく反応する。

"""


def api_key() -> str:
    key = os.environ.get("GEMINI_API_KEY")
    if not key and (ROOT / ".env").exists():
        for ln in (ROOT / ".env").read_text(encoding="utf-8").splitlines():
            if ln.strip().startswith("GEMINI_API_KEY") and "=" in ln:
                key = ln.split("=", 1)[1].strip().strip('"').strip("'")
    if not key:
        sys.exit("GEMINI_API_KEY is not in .env")
    return key


def get(path: str, key: str) -> dict:
    req = urllib.request.Request(f"{API}/{path}", headers={"x-goog-api-key": key})
    return json.loads(urllib.request.urlopen(req, timeout=60).read())


def tts_models(key: str) -> list[str]:
    names, token = [], ""
    while True:
        data = get("models?pageSize=200" + (f"&pageToken={token}" if token else ""), key)
        names += [m["name"].split("/", 1)[1] for m in data.get("models", []) if "tts" in m["name"]]
        token = data.get("nextPageToken")
        if not token:
            return names


def voices() -> dict:
    p = ROOT / "style" / "series_voices.yaml"
    if p.exists():
        import yaml
        g = (yaml.safe_load(p.read_text(encoding="utf-8")) or {}).get("gemini") or {}
        if all(s in g for s in SPEAKERS):
            return {s: (g[s]["voice"] if isinstance(g[s], dict) else g[s]) for s in SPEAKERS}
    return DEFAULT_VOICES


TAGS = [("笑", "[laughing]"), ("驚", "[surprised]"), ("考え", "[thoughtful]"), ("不思議", "[curious]"),
        ("楽し", "[cheerful]"), ("うれし", "[happy]"), ("からか", "[teasing]"), ("しみじみ", "[softly]"),
        ("ゆっくり", "[slowly]"), ("言いよど", "[hesitating]"), ("明る", "[cheerful]"), ("さらっと", "[matter-of-fact]")]


def tags(text: str, keep: bool = False) -> str:
    """（演出）/[笑]/[間] for the newer Gemini TTS models.

    keep=False (default since 2026-11-22): every （…） is removed, wherever it is (Gemini read them aloud, even as
    inline tags), [笑] is dropped and [間] becomes "……". keep=True: the old way (a leading （…） -> an inline tag).
    """
    import re
    if not keep:
        text = re.sub(r"（[^）]*）", "", text)
        return re.sub(r"\s+", " ", text.replace("[笑]", "").replace("🤭", "").replace("[間]", "……")).strip()
    m = re.match(r"^（([^）]*)）", text)
    if m:
        tag = next((t for k, t in TAGS if k in m.group(1)), "")
        text = (tag + " " if tag else "") + text[m.end():]
    return text.replace("[笑]", "[laughs]").replace("[間]", "[pause]").replace("🤭", "[laughs]").strip()


def chunks(lines, max_lines: int = 10):
    """Cut into chunks with at most 2 distinct speakers (and at most max_lines lines)."""
    out, cur = [], []
    for ln in lines:
        spk = {s for s, _, _ in cur} | {ln[0]}
        if cur and (len(spk) > 2 or len(cur) >= max_lines or ln[2]):
            out.append(cur)
            cur = []
        cur.append(ln)
    if cur:
        out.append(cur)
    return out


def synth(chunk, model: str, key: str, vmap: dict, keep: bool = False) -> bytes:
    spk = sorted({s for s, _, _ in chunk}, key=SPEAKERS.index)
    if len(spk) == 2:
        speech = {"multiSpeakerVoiceConfig": {"speakerVoiceConfigs": [
            {"speaker": s, "voiceConfig": {"prebuiltVoiceConfig": {"voiceName": vmap[s]}}} for s in spk]}}
    else:
        speech = {"voiceConfig": {"prebuiltVoiceConfig": {"voiceName": vmap[spk[0]]}}}
    if "3." in model:
        # newer models: no developer instruction; one text part per line with speech_metadata.speaker.
        # The script's directions become short inline tags (whether they work is checked by ear).
        parts = [{"text": tags(t, keep)} for _, t, _ in chunk]
        if len(spk) == 2:
            parts = [{"text": tags(t, keep), "speechMetadata": {"speaker": s}} for s, t, _ in chunk]
        body = {"contents": [{"parts": parts}]}
    else:
        body = {"contents": [{"parts": [{"text": PROMPT + "\n".join(f"{s}: {t}" for s, t, _ in chunk)}]}]}
    body["generationConfig"] = {"responseModalities": ["AUDIO"], "speechConfig": speech}
    req = urllib.request.Request(f"{API}/models/{model}:generateContent", data=json.dumps(body).encode("utf-8"),
                                 headers={"x-goog-api-key": key, "Content-Type": "application/json"}, method="POST")
    for attempt in range(4):
        try:
            data = json.loads(urllib.request.urlopen(req, timeout=600).read())
            cand = (data.get("candidates") or [{}])[0]
            if "content" not in cand:      # sometimes an empty answer comes back; try again once
                print(f"  empty answer ({cand.get('finishReason')}), retry", flush=True)
                if attempt < 1:
                    continue
                sys.exit(f"Gemini returned no audio: {json.dumps(cand, ensure_ascii=False)[:200]}")
            parts = cand["content"]["parts"]
            return b"".join(base64.b64decode(p["inlineData"]["data"]) for p in parts if "inlineData" in p)
        except urllib.error.HTTPError as ex:
            msg = ex.read().decode("utf-8", "replace")[:300]
            if ex.code in (429, 500, 503) and attempt < 3:
                print(f"  retry ({ex.code}): {msg[:120]}", flush=True)
                time.sleep(30 * (attempt + 1))
                continue
            sys.exit(f"Gemini error {ex.code}: {msg}")


def smooth(pcm: bytes, ms: int = 15) -> bytes:
    """Fade the edges of a chunk so the joins between chunks (speaker changes) do not click."""
    a = array.array("h", pcm[: len(pcm) // 2 * 2])
    n = min(len(a) // 2, RATE * ms // 1000)
    for i in range(n):
        a[i] = int(a[i] * i / n)
        a[-1 - i] = int(a[-1 - i] * i / n)
    return a.tobytes()


def trim(pcm: bytes, level: int = 300, keep_ms: int = 40) -> bytes:
    """Cut the silence (and any noise below `level`) at both ends of one line, keeping keep_ms of it."""
    a = array.array("h", pcm[: len(pcm) // 2 * 2])
    f = RATE // 100   # 10 ms frames
    loud = [i for i in range(0, len(a), f) if max((abs(x) for x in a[i:i + f]), default=0) > level]
    if not loud:
        return pcm
    # Gemini adds a loud noise burst (~0.2 s, "zaza") after the speech has gone quiet: speech -> >= 80 ms quiet -> burst
    # (2026-11-22, measured at the end of every line). A short loud island after such a quiet run at either end is cut.
    def islands(idx):
        out, cur = [], [idx[0]]
        for i in idx[1:]:
            if i - cur[-1] > f * 8:          # a quiet run of more than 80 ms splits islands
                out.append(cur)
                cur = []
            cur.append(i)
        return out + [cur]
    def noisy(island) -> bool:
        """Short and hissy: zero-crossing rate > 0.25 (speech ~0.1) or clipped. A real last word ("か。") is kept."""
        s = a[island[0]: island[-1] + f]
        zcr = sum(1 for p, q in zip(s, s[1:]) if (p < 0) != (q < 0)) / max(1, len(s))
        return len(s) < RATE * 0.35 and (zcr > 0.25 or max(abs(x) for x in s) >= 32000)

    isl = islands(loud)
    while len(isl) > 1 and noisy(isl[-1]):   # the burst at the very end
        isl.pop()
    while len(isl) > 1 and noisy(isl[0]):    # the same at the start
        isl.pop(0)
    k = RATE * keep_ms // 1000
    return a[max(0, isl[0][0] - k): min(len(a), isl[-1][-1] + f + k)].tobytes()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("week_dir", nargs="?")
    ap.add_argument("n", nargs="?")
    ap.add_argument("--models", action="store_true")
    ap.add_argument("--model")
    ap.add_argument("--lines", type=int, default=0)
    ap.add_argument("--start", type=int, default=0)
    # 2026-11-22: two speakers in one request gave a "zaza" noise at the speaker change -> one line per request
    # (one voice each), the ends trimmed and faded, joined with our own gaps. --multi = the old 2-speaker chunks.
    ap.add_argument("--multi", action="store_true")
    ap.add_argument("--keep-directions", action="store_true", help="old way: leading （…） -> inline tag")
    a = ap.parse_args()
    key = api_key()
    models = tts_models(key)
    if a.models or not models:
        print("\n".join(models) or "no TTS model is available for this key")
        return
    model = a.model or next((m for m in models if "pro" in m), models[0])
    print("model:", model)
    vmap = voices()
    lines = parse(Path(a.week_dir) / f"ep{a.n}-script.md", keep_directions=True)
    lines = lines[a.start:(a.start + a.lines) if a.lines else None]
    pcm = io.BytesIO()
    cs = chunks(lines) if a.multi else [[ln] for ln in lines]
    for i, c in enumerate(cs, 1):
        if i > 1:
            pcm.write(b"\x00\x00" * int(RATE * (0.9 if c[0][2] else (0.3 if a.multi else 0.35))))
        audio = synth(c, model, key, vmap, a.keep_directions)
        pcm.write(smooth(audio if a.multi else trim(audio), 15 if a.multi else 25))
        print(f"[{i}/{len(cs)}] {', '.join(sorted({s for s, _, _ in c}))}: {c[0][1][:25]}", flush=True)
    d = Path(a.week_dir) / "audio"
    d.mkdir(exist_ok=True)
    out_wav = d / (f"ep{a.n}-gemini" + ("" if a.multi else "-line") + (f"-{a.lines}lines" if a.lines else "") + ".wav")
    with wave.open(str(out_wav), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(RATE)
        w.writeframes(pcm.getvalue())
    out_mp3 = out_wav.with_suffix(".mp3")
    subprocess.run([shutil.which("ffmpeg") or "ffmpeg", "-y", "-loglevel", "error", "-i", str(out_wav), "-b:a", "96k",
                    str(out_mp3)], check=True)
    out_wav.unlink()
    print("saved:", out_mp3)


if __name__ == "__main__":
    main()
