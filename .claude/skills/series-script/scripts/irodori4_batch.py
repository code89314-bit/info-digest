"""Runs INSIDE the Irodori-TTS v4 container (image irodori-tts:v4). Started by tts_irodori4.py.

job.json:
  {"checkpoint": "Aratako/Irodori-TTS-v4.1-Small", "caption_too": false,
   "voices": {"堀田": {"caption": "...", "seed": 111, "ref": "/voices/hotta.wav"}, ...},
   "design_texts": ["...", "...", "..."],     # sentences for designing a voice when its ref does not exist
   "pieces": [{"speaker": "堀田", "text": "...", "style": "...", "gap": 0.12}, ...],   # one piece = one phrase
   "out": "/data/audio/x.wav"}
1) For each speaker whose ref wav is missing: caption only (no reference), one clip per design text, joined (~30 s)
   and saved as the ref.
2) Each phrase: the speaker's ref (same voice). The caption is added only with caption_too (v4: a reference and a caption
   that disagree can make the audio unstable) or when the phrase has a style (emotion) of its own.
3) Joins the phrases with their gaps into one wav.
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, "/app")   # the Irodori-TTS code lives in /app in the image; this script is mounted at /work

import torch  # noqa: E402
from huggingface_hub import hf_hub_download  # noqa: E402

from irodori_tts.inference_runtime import (InferenceRuntime, RuntimeKey, SamplingRequest,  # noqa: E402
                                           default_runtime_device, save_wav)

job = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
ckpt = hf_hub_download(job.get("checkpoint") or "Aratako/Irodori-TTS-v4.1-Small", "model.safetensors")
dev = default_runtime_device()
rt = InferenceRuntime.from_key(RuntimeKey(checkpoint=ckpt, model_device=dev, codec_device=dev))
print("device:", dev, "checkpoint:", job.get("checkpoint"), flush=True)


def mono(r):
    a = r.audio.detach().float().cpu()
    return a.unsqueeze(0) if a.dim() == 1 else a


for name, v in job["voices"].items():
    ref = Path(v["ref"])
    if ref.exists():
        continue
    ref.parent.mkdir(parents=True, exist_ok=True)
    clips, sr = [], None
    for k, t in enumerate(job["design_texts"]):
        r = rt.synthesize(SamplingRequest(text=t, caption=v["caption"], no_ref=True, seed=v.get("seed", 1) + k),
                          log_fn=None)
        sr = r.sample_rate
        if clips:
            clips.append(torch.zeros(1, int(sr * 0.5)))
        clips.append(mono(r))
    save_wav(ref, torch.cat(clips, dim=-1), sr)
    print("designed voice:", name, ref, flush=True)

pieces, sr = [], None
total = len(job["pieces"])
for i, p in enumerate(job["pieces"], 1):
    v = job["voices"][p["speaker"]]
    caption = None
    if job.get("caption_too"):
        caption = v["caption"] + (" " + p["style"] if p.get("style") else "")
    elif p.get("style"):
        caption = p["style"]
    r = rt.synthesize(SamplingRequest(text=p["text"], caption=caption, ref_wav=v["ref"], seed=v.get("seed", 1)),
                      log_fn=None)
    sr = r.sample_rate
    if pieces and p.get("gap"):
        pieces.append(torch.zeros(1, int(sr * float(p["gap"]))))
    pieces.append(mono(r))
    print(f"[{i}/{total}] {p['speaker']}: {p['text'][:30]}", flush=True)

save_wav(job["out"], torch.cat(pieces, dim=-1), sr)
print("saved:", job["out"], flush=True)
