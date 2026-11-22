"""Read a series script aloud with a VOICEVOX-compatible engine (AivisSpeech / VOICEVOX) and save an mp3.

The script lines are `堀田: ...` / `横井: ...` / `司: ...`, with readings written as {表記|よみ}; the よみ is spoken.
Voices per speaker come from style/series_voices.yaml (engine -> speaker -> style id) or --voices.

usage:
  python tts_render.py --engine aivis --list                         # show the engine's voices (style ids)
  python tts_render.py <week_dir> <n> --engine aivis --lines 20      # first 20 lines only (a sample to compare)
  python tts_render.py <week_dir> <n> --engine voicevox --voices 堀田=13,横井=2,司=11
engines: aivis = AivisSpeech (http://127.0.0.1:10101), voicevox = VOICEVOX (http://127.0.0.1:50021)
"""
import argparse
import io
import json
import re
import shutil
import subprocess
import sys
import urllib.parse
import urllib.request
import wave
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
ENGINES = {"aivis": "http://127.0.0.1:10101", "voicevox": "http://127.0.0.1:50021"}
SPEAKERS = ("堀田", "横井", "司")
GAP_LINE, GAP_SECTION = 0.35, 0.9      # seconds of silence between lines / sections


def call(base: str, path: str, params: dict, body: bytes = b"", timeout: int = 120) -> bytes:
    """POST (audio_query / synthesis)."""
    url = f"{base}{path}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(url, data=body, method="POST", headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def list_voices(base: str) -> None:
    data = json.loads(urllib.request.urlopen(f"{base}/speakers", timeout=30).read())
    for sp in data:
        for st in sp.get("styles", []):
            print(f"{st['id']:>12}  {sp['name']} / {st['name']}")


def load_voices(engine: str, arg: str | None) -> dict:
    if arg:
        return {k: int(v) for k, v in (x.split("=") for x in arg.split(","))}
    p = ROOT / "style" / "series_voices.yaml"
    if p.exists():
        import yaml
        cfg = (yaml.safe_load(p.read_text(encoding="utf-8")) or {}).get(engine) or {}
        if all(s in cfg for s in SPEAKERS):
            return {s: int(cfg[s]["id"] if isinstance(cfg[s], dict) else cfg[s]) for s in SPEAKERS}
    sys.exit(f"voices for {engine} are not set: use --voices 堀田=ID,横井=ID,司=ID (see --list)")


def parse(script_path: Path, keep_directions: bool = False) -> list[tuple[str, str, bool]]:
    """(speaker, spoken text, starts a new section). Readings {表記|よみ} -> よみ.
    keep_directions=False (AivisSpeech): drop every （演出） (also mid-line) and [笑]; keep [間] as a marker for silence.
    keep_directions=True (Gemini): keep them; the prompt tells the model they are directions, not words."""
    body = script_path.read_text(encoding="utf-8").split("---", 2)[2]
    out, new_sec = [], False
    for ln in body.splitlines():
        if ln.startswith("## "):
            new_sec = True
            continue
        m = re.match(r"^(堀田|横井|司):\s*(.*)$", ln)
        if m:
            text = re.sub(r"\{([^|}]*)\|([^}]*)\}", r"\2", m.group(2)).strip()
            if not keep_directions:
                text = re.sub(r"（[^）]*）", "", text)   # every direction, not only a leading one
                text = text.replace("[笑]", "").strip()
            if text:
                out.append((m.group(1), text, new_sec))
                new_sec = False
    return out


def synth(base: str, text: str, style: int, speed: float) -> bytes:
    q = json.loads(call(base, "/audio_query", {"text": text, "speaker": style}, body=b""))
    q["speedScale"] = speed
    return call(base, "/synthesis", {"speaker": style}, body=json.dumps(q).encode("utf-8"), timeout=300)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("week_dir", nargs="?")
    ap.add_argument("n", nargs="?")
    ap.add_argument("--engine", choices=ENGINES, default="aivis")
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--voices")
    ap.add_argument("--lines", type=int, default=0, help="only the first N lines (0 = all)")
    ap.add_argument("--speed", type=float, default=1.05)
    a = ap.parse_args()
    base = ENGINES[a.engine]
    try:
        urllib.request.urlopen(f"{base}/version", timeout=5)
    except OSError:
        sys.exit(f"{a.engine} is not running at {base} (start the app first)")
    if a.list:
        return list_voices(base)
    voices = load_voices(a.engine, a.voices)
    d = Path(a.week_dir)
    lines = parse(d / f"ep{a.n}-script.md")
    if a.lines:
        lines = lines[: a.lines]
    params = None
    frames = io.BytesIO()
    for i, (spk, text, new_sec) in enumerate(lines, 1):
        parts = [p.strip() for p in text.split("[間]") if p.strip()]   # [間] = a short pause inside a line
        for j, part in enumerate(parts):
            wav = synth(base, part, voices[spk], a.speed)
            with wave.open(io.BytesIO(wav)) as w:
                if params is None:
                    params = w.getparams()
                data = w.readframes(w.getnframes())
            if i > 1 or j > 0:
                gap = 0.6 if j > 0 else (GAP_SECTION if new_sec else GAP_LINE)
                frames.write(b"\x00" * int(params.framerate * gap) * params.sampwidth * params.nchannels)
            frames.write(data)
        print(f"[{i}/{len(lines)}] {spk}: {text[:30]}", flush=True)
    tag = f"ep{a.n}-{a.engine}" + (f"-{a.lines}lines" if a.lines else "")
    out_wav = d / "audio" / f"{tag}.wav"
    out_wav.parent.mkdir(exist_ok=True)
    with wave.open(str(out_wav), "wb") as w:
        w.setparams(params)
        w.writeframes(frames.getvalue())
    ffmpeg = shutil.which("ffmpeg") or "ffmpeg"
    out_mp3 = out_wav.with_suffix(".mp3")
    subprocess.run([ffmpeg, "-y", "-loglevel", "error", "-i", str(out_wav), "-ac", "1", "-b:a", "96k", str(out_mp3)], check=True)
    out_wav.unlink()
    print("saved:", out_mp3)


if __name__ == "__main__":
    main()
