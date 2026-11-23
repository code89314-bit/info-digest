"""Series cover images with MOSS-Image in ComfyUI (http://127.0.0.1:8188). The layout is a JSON file:

  {"name": "a-exonmap", "width": 1280, "height": 672, "seeds": [1, 2], "batch": 1,
   "caption": "whole-image description (NegPip weights like (photo:-2) work)",
   "negative": "...",
   "boxes": [{"x": 0, "y": 0, "w": 100, "h": 100, "type": "obj"|"text", "text": "exact text for text boxes",
              "desc": "what is in this box", "palette": ["#RRGGBB"]}, ...]}   # background first, foreground last

Boxes go through Create Bounding Boxes (PPM) -> MOSS-Image Layout Prompt, so text boxes render their exact text.
Needs the GPU (stop Strata first). Output: <out_dir>/<name>-s<seed>_0000N_.png

usage: python cover_moss.py <layout.json> [<layout.json> ...] --out <dir>
"""
import argparse
import json
import shutil
import time
import urllib.request
from pathlib import Path

URL = "http://127.0.0.1:8188"
OUT = Path("E:/comfyui-data/output")


def graph(lay: dict, seed: int) -> dict:
    W, H = lay.get("width", 1280), lay.get("height", 672)
    boxes = [{"x": b["x"], "y": b["y"], "width": b["w"], "height": b["h"],
              "metadata": {"type": b.get("type", "obj"), "text": b.get("text", ""), "desc": b.get("desc", ""),
                           "palette": b.get("palette", []), "frame": [W, H]}} for b in lay["boxes"]]
    return {
        "1": {"class_type": "UNETLoader", "inputs": {"unet_name": lay.get("unet", "MOSS/moss_image_sft_int8_convrot.safetensors"),
                                                     "weight_dtype": "default"}},
        "2": {"class_type": "CLIPLoader", "inputs": {"clip_name": "qwen3.5_9b_w4a8_gptq.safetensors", "type": "moss_image", "device": "default"}},
        "3": {"class_type": "VAELoader", "inputs": {"vae_name": "flux2-vae_small.safetensors"}},
        "4": {"class_type": "ModelSamplingAuraFlow", "inputs": {"model": ["1", 0], "shift": 5}},
        "5": {"class_type": "SkipLayerGuidanceDiTSimple", "inputs": {"model": ["4", 0], "double_layers": "", "single_layers": "22",
                                                                     "start_percent": 0, "end_percent": 1}},
        "6": {"class_type": "CLIPNegPip", "inputs": {"model": ["5", 0], "clip": ["2", 0]}},
        "7": {"class_type": "CreateBoundingBoxesPPM", "inputs": {"width": W, "height": H, "editor_state": {"__value__": boxes},
                                                                 "last_incoming": {"__value__": []}}},
        "8": {"class_type": "MOSSImageLayoutPrompt", "inputs": {"prompt": lay["caption"], "element": ["7", 2], "width": W, "height": H,
                                                                "character_boxes": ""}},
        "9": {"class_type": "CLIPTextEncode", "inputs": {"clip": ["6", 1], "text": ["8", 0]}},
        "10": {"class_type": "CLIPTextEncode", "inputs": {"clip": ["6", 1], "text": lay.get("negative", "")}},
        "11": {"class_type": "EmptyFlux2LatentImage", "inputs": {"width": W, "height": H, "batch_size": lay.get("batch", 1)}},
        "12": {"class_type": "KSamplerSelect", "inputs": {"sampler_name": "euler"}},
        "13": {"class_type": "BasicScheduler", "inputs": {"model": ["6", 0], "scheduler": "sgm_uniform", "steps": lay.get("steps", 50), "denoise": 1}},
        "14": {"class_type": "SamplerCustom", "inputs": {"model": ["6", 0], "add_noise": True, "noise_seed": seed, "cfg": lay.get("cfg", 5),
                                                         "positive": ["9", 0], "negative": ["10", 0], "sampler": ["12", 0],
                                                         "sigmas": ["13", 0], "latent_image": ["11", 0]}},
        "15": {"class_type": "VAEDecode", "inputs": {"samples": ["14", 0], "vae": ["3", 0]}},
        "16": {"class_type": "SaveImage", "inputs": {"images": ["15", 0], "filename_prefix": f"series/{lay['name']}-s{seed}"}},
    }


def post(path: str, data: dict | None = None) -> dict:
    req = urllib.request.Request(URL + path, json.dumps(data).encode() if data is not None else None,
                                 {"Content-Type": "application/json"})
    return json.loads(urllib.request.urlopen(req, timeout=60).read())


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("layouts", nargs="+")
    ap.add_argument("--out", required=True)
    a = ap.parse_args()
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    ids = []
    for f in a.layouts:
        lay = json.loads(Path(f).read_text(encoding="utf-8"))
        for s in lay.get("seeds", [1]):
            ids.append(post("/prompt", {"prompt": graph(lay, s)})["prompt_id"])
    print("queued", len(ids), flush=True)
    for pid in ids:
        while True:
            h = post(f"/history/{pid}").get(pid)
            if h and h.get("status", {}).get("completed") is not None:
                break
            time.sleep(5)
        for node in h.get("outputs", {}).values():
            for im in node.get("images", []):
                src = OUT / im.get("subfolder", "") / im["filename"]
                shutil.copy(src, out / im["filename"])
                print("saved", out / im["filename"], flush=True)
        if h["status"].get("status_str") != "success":
            print("failed", pid, json.dumps(h["status"].get("messages", []))[-500:], flush=True)


if __name__ == "__main__":
    main()
