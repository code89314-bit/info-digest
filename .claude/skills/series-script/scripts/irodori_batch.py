"""Runs INSIDE the Irodori-TTS v3 container (D:\\Docker\\irodori, image irodori-tts:v3). Started by tts_irodori.py.

job.json:
  {"voices": {"堀田": {"caption": "...", "ref": "/data/voices/堀田.wav", "seed": 11}, ...},
   "design_text": "...",                      # sentence used to create a voice reference when ref does not exist
   "lines": [{"speaker": "堀田", "text": "...", "style": "...", "gap": 0.35}, ...],
   "out": "/data/out.wav"}
1) For each speaker whose ref wav is missing: VoiceDesign from the caption only (no reference) -> save as the ref.
2) Each line: text (+emoji) with the speaker's ref (same voice) and caption = voice caption + line style (emotion).
3) Joins the lines with the given gaps into one wav.
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, "/app")   # the Irodori-TTS code lives in /app in the image; this script is mounted at /work

import torch  # noqa: E402
from huggingface_hub import hf_hub_download

from irodori_tts.inference_runtime import (InferenceRuntime, RuntimeKey, SamplingRequest, default_runtime_device,
                                           save_wav)

job = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
ckpt = hf_hub_download("Aratako/Irodori-TTS-600M-v3-VoiceDesign", "model.safetensors")
dev = default_runtime_device()
rt = InferenceRuntime.from_key(RuntimeKey(checkpoint=ckpt, model_device=dev, codec_device=dev))
print("device:", dev, flush=True)

for name, v in job["voices"].items():
    ref = Path(v["ref"])
    if ref.exists():
        continue
    ref.parent.mkdir(parents=True, exist_ok=True)
    r = rt.synthesize(SamplingRequest(text=job["design_text"], caption=v["caption"], no_ref=True, seed=v.get("seed", 1)),
                      log_fn=None)
    save_wav(ref, r.audio, r.sample_rate)
    print("designed voice:", name, ref, flush=True)

pieces, sr = [], None
for i, ln in enumerate(job["lines"], 1):
    v = job["voices"][ln["speaker"]]
    caption = v["caption"] + (" " + ln["style"] if ln.get("style") else "")
    r = rt.synthesize(SamplingRequest(text=ln["text"], caption=caption, ref_wav=v["ref"], seed=v.get("seed", 1)),
                      log_fn=None)
    sr = r.sample_rate
    audio = r.audio.detach().float().cpu()
    if audio.dim() == 1:
        audio = audio.unsqueeze(0)
    if pieces:
        pieces.append(torch.zeros(audio.shape[0], int(sr * float(ln.get("gap", 0.35)))))
    pieces.append(audio)
    print(f"[{i}/{len(job['lines'])}] {ln['speaker']}: {ln['text'][:30]}", flush=True)

save_wav(job["out"], torch.cat(pieces, dim=-1), sr)
print("saved:", job["out"], flush=True)
