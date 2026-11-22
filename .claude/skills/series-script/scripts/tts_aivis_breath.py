"""AivisSpeech, phrase by phrase, with pauses (and optionally soft in-breaths) at the seams. Writes 1x / 1.5x / 2x mp3s.

- Each line is cut into phrases (tts_irodori4.phrases: after 。！？, after 、 once long enough). Each phrase is made on its own.
- Seams: after 、 a short pause; after 。！？ a slightly longer one; with --breaths DIR, a soft in-breath of that speaker
  (DIR/<hotta|yokoi|tsukasa>_*.wav, made with Irodori v4) goes before a new sentence and before a long line.
- The user's wish (2026-11-22): AivisSpeech sounded best; breathing would make it better. Heard at 1.5x-2x.

usage: python tts_aivis_breath.py <week_dir> <n> [--voices 堀田=ID,横井=ID,司=ID] [--lines 12 (0 = all)] [--breaths DIR] [--keep a,b,...] [--tag x]
"""
import argparse
import io
import random
import re
import shutil
import subprocess
import sys
import wave
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).parent))
from tts_irodori4 import ASCII, phrases  # noqa: E402
from tts_render import ENGINES, load_voices, parse, synth  # noqa: E402

GAP_COMMA, GAP_SENT, GAP_LINE, GAP_SECTION = 0.15, 0.25, 0.4, 0.9
BREATH_DB = -14.0     # breath level against the speech peak
LONG_LINE = 25        # a line at least this long starts with a breath


def pcm(wav_bytes: bytes) -> tuple[np.ndarray, int]:
    with wave.open(io.BytesIO(wav_bytes)) as w:
        return np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16).astype(np.float32) / 32768, w.getframerate()


def load_breaths(d: Path, keep: set[str], sr: int) -> dict[str, list[np.ndarray]]:
    import librosa
    out: dict[str, list[np.ndarray]] = {}
    for f in sorted(d.glob("*.wav")):
        if keep and f.stem not in keep:
            continue
        y, _ = librosa.load(str(f), sr=sr)
        y, _ = librosa.effects.trim(y, top_db=35)
        y = y[: int(sr * 0.45)]
        if len(y) < sr * 0.08:
            continue
        n = min(len(y) // 4, int(sr * 0.05))
        y[:n] *= np.linspace(0, 1, n)
        y[-n:] *= np.linspace(1, 0, n)
        y = y / (np.abs(y).max() + 1e-9) * 10 ** (BREATH_DB / 20)
        out.setdefault(f.stem.rsplit("_", 1)[0], []).append(y.astype(np.float32))
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("week_dir")
    ap.add_argument("n")
    ap.add_argument("--voices", default=None, help="default: style/series_voices.yaml aivis:")
    ap.add_argument("--lines", type=int, default=0, help="0 = all lines")
    ap.add_argument("--start", type=int, default=0)
    ap.add_argument("--speed", type=float, default=1.05)
    ap.add_argument("--breaths", default="")
    ap.add_argument("--keep", default="", help="breath files to use (stems), e.g. hotta_3,yokoi_3")
    ap.add_argument("--tag", default="")
    a = ap.parse_args()
    base = ENGINES["aivis"]
    voices = load_voices("aivis", a.voices)
    d = Path(a.week_dir)
    lines = parse(d / f"ep{a.n}-script.md")[a.start:(a.start + a.lines) if a.lines else None]
    rnd = random.Random(7)
    out, sr, breaths = [], None, None
    for i, (spk, text, new_sec) in enumerate(lines):
        ps = [p.replace("[間]", "、").strip("、 ") for p in phrases(text)]
        ps = [p for p in ps if p]
        for j, p in enumerate(ps):
            y, sr = pcm(synth(base, p, voices[spk], a.speed))
            if breaths is None:
                breaths = load_breaths(Path(a.breaths), set(filter(None, a.keep.split(","))), sr) if a.breaths else {}
            if out:
                gap = (GAP_SECTION if new_sec else GAP_LINE) if j == 0 else (GAP_SENT if ps[j - 1][-1] in "。！？?!" else GAP_COMMA)
                own = breaths.get(ASCII[spk])
                want = own and ((j == 0 and len(text) >= LONG_LINE) or (j > 0 and ps[j - 1][-1] in "。！？?!"))
                if want:
                    b = rnd.choice(own)
                    out.append(np.zeros(int(sr * max(0.05, gap - len(b) / sr - 0.06)), np.float32))
                    out.append(b)
                    out.append(np.zeros(int(sr * 0.06), np.float32))
                else:
                    out.append(np.zeros(int(sr * gap), np.float32))
            out.append(y)
        print(f"[{i + 1}/{len(lines)}] {spk}: {len(ps)} phrases", flush=True)
    tag = a.tag or f"ep{a.n}-aivis" + ("-breath" if breaths else "") + (f"-{a.lines}lines" if a.lines else "")
    wav_path = d / "audio" / f"{tag}.wav"
    audio = np.clip(np.concatenate(out), -1, 1)
    with wave.open(str(wav_path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes((audio * 32767).astype(np.int16).tobytes())
    ff = shutil.which("ffmpeg") or "ffmpeg"
    for sp in ("1.0", "1.5", "2.0"):
        mp3 = wav_path.with_name(f"{tag}-x{sp}.mp3")
        subprocess.run([ff, "-y", "-loglevel", "error", "-i", str(wav_path), "-filter:a",
                        f"loudnorm=I=-16:TP=-1.5,atempo={sp}", "-b:a", "128k", str(mp3)], check=True)
        print("saved:", mp3)
    wav_path.unlink()


if __name__ == "__main__":
    main()
