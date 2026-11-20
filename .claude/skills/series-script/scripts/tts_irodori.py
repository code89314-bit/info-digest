"""Read a series script aloud with Irodori-TTS v3 VoiceDesign (local, Docker, GPU) and save an mp3.

- Voices: style/series_voices.yaml `irodori:` (caption per speaker). The first run designs each voice from its caption and
  keeps it as a reference wav (.local/series/voices/irodori/<name>.wav) so all episodes use the same voices.
- Directions in the script become Irodori's controls: leading （…） -> caption (emotion/style) + emoji, [笑] -> 🤭,
  [間] -> ⏸. Readings {表記|よみ} -> よみ (Irodori reads kanji weakly).
- GPU: Strata must be stopped first (VRAM). This script does not switch modes; the caller does.

usage: python tts_irodori.py <week_dir> <n> [--lines 30]
"""
import argparse
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from tts_render import ROOT, parse  # noqa: E402

COMPOSE = r"D:\Docker\irodori\docker-compose.yml"
SERVICE = "irodori-v3-voicedesign"
VOICE_DIR = ROOT / ".local" / "series" / "voices" / "irodori"
DEFAULT_VOICES = {
    "照井": {"caption": "落ち着いた大人の男性の声。病院のベテラン薬剤師が、ラジオ番組で同僚と話している。穏やかで知的、話が乗ると少し熱が入る。", "seed": 11},
    "しらべ": {"caption": "明るくハキハキした、20代後半の女性の声。ラジオ番組のアシスタントのように、軽やかで親しみやすく話す。", "seed": 22},
    "わた": {"caption": "低めで柔らかい声の、40代の男性。丁寧で控えめに、相手を立てながら質問する。", "seed": 33},
}
DESIGN_TEXT = "こんにちは。今日もよろしくお願いします。それでは、さっそく本題に入っていきましょう。"
# direction keyword -> emoji added to the text
EMOJI = [("笑", "🤭"), ("驚", "😲"), ("考え", "🤔"), ("不思議", "🤔"), ("楽し", "😊"), ("うれし", "😊"), ("嬉し", "😊"),
         ("しみじみ", "😌"), ("からか", "😏"), ("照れ", "🫣"), ("ゆっくり", "🐢"), ("言いよど", "😰")]


def voices() -> dict:
    v = {k: dict(x) for k, x in DEFAULT_VOICES.items()}
    p = ROOT / "style" / "series_voices.yaml"
    if p.exists():
        import yaml
        for k, x in ((yaml.safe_load(p.read_text(encoding="utf-8")) or {}).get("irodori") or {}).items():
            if isinstance(x, dict):
                v.setdefault(k, {}).update(x)
    return v


def convert(text: str) -> tuple[str, str]:
    """script text (with directions) -> (Irodori text with emoji, style caption)"""
    style = ""
    m = re.match(r"^（([^）]*)）", text)
    if m:
        style = m.group(1)
        text = text[m.end():]
        emo = next((e for k, e in EMOJI if k in style), "")
        text = emo + text
    text = text.replace("[笑]", "🤭").replace("[間]", "⏸")
    style = (f"この台詞は「{style}」話している。" if style else "")
    return text.strip(), style


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("week_dir")
    ap.add_argument("n")
    ap.add_argument("--lines", type=int, default=0)
    a = ap.parse_args()
    d = Path(a.week_dir).resolve()
    lines = parse(d / f"ep{a.n}-script.md", keep_directions=True)
    if a.lines:
        lines = lines[: a.lines]
    tag = f"ep{a.n}-irodori" + (f"-{a.lines}lines" if a.lines else "")
    (d / "audio").mkdir(exist_ok=True)
    VOICE_DIR.mkdir(parents=True, exist_ok=True)
    vs = voices()
    ascii_name = {"照井": "teru", "しらべ": "shirabe", "わた": "wata"}
    job = {"voices": {k: {"caption": x["caption"], "seed": x.get("seed", 1), "ref": f"/voices/{ascii_name.get(k, k)}.wav"}
                      for k, x in vs.items()},
           "design_text": DESIGN_TEXT, "out": f"/data/audio/{tag}.wav", "lines": []}
    for i, (spk, text, new_sec) in enumerate(lines):
        t, style = convert(text)
        job["lines"].append({"speaker": spk, "text": t, "style": style, "gap": 0.9 if new_sec and i else 0.35})
    jp = d / "audio" / f"{tag}.job.json"
    jp.write_text(json.dumps(job, ensure_ascii=False, indent=1), encoding="utf-8")
    scripts = Path(__file__).parent
    cmd = ["docker", "compose", "-f", COMPOSE, "--profile", "v3", "run", "--rm",
           "-v", f"{d}:/data", "-v", f"{VOICE_DIR}:/voices", "-v", f"{scripts}:/work",
           SERVICE, "uv", "run", "--no-sync", "python", "/work/irodori_batch.py", f"/data/audio/{tag}.job.json"]
    print(" ".join(cmd[:8]), "...", flush=True)
    subprocess.run(cmd, check=True)
    wav = d / "audio" / f"{tag}.wav"
    mp3 = wav.with_suffix(".mp3")
    subprocess.run([shutil.which("ffmpeg") or "ffmpeg", "-y", "-loglevel", "error", "-i", str(wav), "-ac", "1", "-b:a", "96k",
                    str(mp3)], check=True)
    wav.unlink()
    print("saved:", mp3)


if __name__ == "__main__":
    main()
