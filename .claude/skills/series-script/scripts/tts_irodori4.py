"""Read a series script aloud with Irodori-TTS v4 (local, Docker image irodori-tts:v4, GPU) and save mp3s at 1x / 1.5x / 2x.

What is different from tts_irodori.py (v3):
- One v4 checkpoint does both voice design (caption) and cloning (reference). Default Aratako/Irodori-TTS-v4.1-Small (MIT).
- Each line is cut into short phrases at 。！？ (and at 、 once a phrase is long enough). Each phrase is made separately
  and the phrases are joined with a short gap, so the breath falls at the seams (user's tip, 2026-11-22: much better).
- Reference voices: <voice_dir>/<name>.wav. If missing, a voice is designed from the caption only (no reference) from
  three sentences (~30 s: v4 clones better from about 30 s than from one short clip) and kept there.
  To use your own reference, put hotta.wav / yokoi.wav / tsukasa.wav in the voice dir (any length up to 120 s).
- GPU: Strata must be stopped first (VRAM). This script does not switch modes; the caller does.

usage: python tts_irodori4.py <week_dir> <n> [--start 0] [--lines 12] [--voice-dir DIR] [--checkpoint HF_ID] [--caption-too]
"""
import argparse
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from tts_irodori import convert  # noqa: E402
from tts_render import ROOT, parse  # noqa: E402

COMPOSE = r"D:\Docker\irodori\docker-compose.yml"
IMAGE = "irodori-tts:v4"
VOICE_DIR = ROOT / ".local" / "series" / "voices" / "irodori4"
ASCII = {"堀田": "hotta", "横井": "yokoi", "司": "tsukasa"}
CLEAR = "一語一語をはっきり発音し、落ち着いた速さで話す。雑音や響きのない、クリアな録音。"
VOICES = {   # pitch: 堀田 low, 司 clearly higher, 横井 adult woman (2026-11-22: the two men must be easy to tell apart)
    "堀田": {"caption": "低く太い声の、落ち着いた40代の男性。深みのある低音で、ラジオ番組で丁寧に解説している。" + CLEAR, "seed": 111},
    "横井": {"caption": "落ち着いた、ユーモアのある30代後半の女性。柔らかく知的な声で、ラジオ番組で話している。" + CLEAR, "seed": 222},
    "司": {"caption": "明るく高めの声の、若々しい30代の男性。丁寧で、はきはきとしたラジオ番組の進行役。" + CLEAR, "seed": 333},
}
DESIGN_TEXTS = ["こんにちは。今日もよろしくお願いします。今日のテーマは、肺がんの遺伝子検査です。",
                "ひとつずつ、順番に見ていきましょう。最初は、検査のタイミングについてです。",
                "なるほど、そういうことだったんですね。数字で見ると、違いがよく分かります。"]
GAP_PHRASE, GAP_LINE, GAP_SECTION = 0.12, 0.35, 0.9


def phrases(text: str, soft: int = 18, hard: int = 40) -> list[str]:
    """Cut a line into phrases: always after 。！？, after 、 once the phrase has `soft` chars (never at an ASCII comma: 12万3,989), never longer than `hard`."""
    out, cur = [], ""
    for ch in text:
        cur += ch
        n = len(re.sub(r"（[^）]*）", "", cur))
        if ch in "。！？!?" or (ch in "、，" and n >= soft) or (n >= hard and ch in "、，　"):
            out.append(cur.strip())
            cur = ""
    if cur.strip():
        if out and len(cur.strip()) < 6:   # a tiny tail goes with the phrase before it
            out[-1] += cur.strip()
        else:
            out.append(cur.strip())
    return [p for p in out if re.sub(r"[（）\[\]間笑\s、。]", "", p)]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("week_dir")
    ap.add_argument("n")
    ap.add_argument("--start", type=int, default=0)
    ap.add_argument("--lines", type=int, default=12)
    ap.add_argument("--voice-dir", default=str(VOICE_DIR))
    ap.add_argument("--checkpoint", default="Aratako/Irodori-TTS-v4.1-Small")
    ap.add_argument("--caption-too", action="store_true", help="also pass the voice caption when a reference is used")
    ap.add_argument("--tag", default="")
    a = ap.parse_args()
    d = Path(a.week_dir).resolve()
    vdir = Path(a.voice_dir).resolve()
    vdir.mkdir(parents=True, exist_ok=True)
    lines = parse(d / f"ep{a.n}-script.md", keep_directions=True)[a.start:a.start + a.lines]
    tag = a.tag or f"ep{a.n}-irodori4-l{a.start + 1}-{a.start + len(lines)}"
    (d / "audio").mkdir(exist_ok=True)
    job = {"checkpoint": a.checkpoint, "caption_too": a.caption_too, "design_texts": DESIGN_TEXTS,
           "voices": {k: {**v, "ref": f"/voices/{ASCII[k]}.wav"} for k, v in VOICES.items()},
           "out": f"/data/audio/{tag}.wav", "pieces": []}
    for i, (spk, text, new_sec) in enumerate(lines):
        ps = phrases(text)
        for j, p in enumerate(ps):
            t, style = convert(p)
            if not re.sub(r"[⏸🤭\s]", "", t):
                continue
            gap = GAP_PHRASE if j else (GAP_SECTION if new_sec and i else GAP_LINE)
            job["pieces"].append({"speaker": spk, "text": t, "style": style, "gap": 0 if not job["pieces"] else gap})
    jp = d / "audio" / f"{tag}.job.json"
    jp.write_text(json.dumps(job, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"{len(lines)} lines -> {len(job['pieces'])} phrases", flush=True)
    scripts = Path(__file__).parent
    cmd = ["docker", "run", "--rm", "--gpus", "all", "-e", "HF_HUB_DISABLE_XET=1",
           "-v", "irodori_hf_cache:/root/.cache/huggingface",
           "-v", f"{d}:/data", "-v", f"{vdir}:/voices", "-v", f"{scripts}:/work",
           IMAGE, "uv", "run", "--no-sync", "python", "/work/irodori4_batch.py", f"/data/audio/{tag}.job.json"]
    subprocess.run(cmd, check=True)
    wav = d / "audio" / f"{tag}.wav"
    ff = shutil.which("ffmpeg") or "ffmpeg"
    for sp in ("1.0", "1.5", "2.0"):
        mp3 = wav.with_name(f"{tag}-x{sp}.mp3")
        subprocess.run([ff, "-y", "-loglevel", "error", "-i", str(wav), "-ac", "1",
                        "-filter:a", f"loudnorm=I=-16:TP=-1.5,atempo={sp}", "-b:a", "128k", str(mp3)], check=True)
        print("saved:", mp3)
    wav.unlink()


if __name__ == "__main__":
    main()
