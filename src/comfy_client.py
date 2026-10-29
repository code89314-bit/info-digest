"""ComfyUI（Docker、http://127.0.0.1:8188）で MOSS-Image の絵を1枚描く。ルナの「描く」スキル用。

ワークフローは API 形式でここで組み立てる（ComfyUI 0.42 以降の標準ノード。テンプレート「MOSS-Image: Text to Image」と同じ構成）:
  UNETLoader → ModelSamplingAuraFlow(shift) → SkipLayerGuidanceDiTSimple(負側のスキップ、単層22) → KSampler
  CLIPLoader(type=moss_image) → CLIPTextEncode（正・負）、EmptyFlux2LatentImage、VAELoader(flux2-vae_small) → VAEDecode → SaveImage
設定は config.yaml の strata_chat.moss。
"""
import random
import time
import uuid

import requests

URL = "http://127.0.0.1:8188"


def workflow(prompt: str, negative: str, width: int, height: int, m: dict, seed: int) -> dict:
    return {
        "unet": {"class_type": "UNETLoader", "inputs": {"unet_name": m.get("unet", "moss_image_sft_int8_convrot.safetensors"),
                                                         "weight_dtype": "default"}},
        "clip": {"class_type": "CLIPLoader", "inputs": {"clip_name": m.get("clip", "qwen3.5_9b_w4a8_gptq.safetensors"),
                                                         "type": "moss_image", "device": "default"}},
        "vae": {"class_type": "VAELoader", "inputs": {"vae_name": m.get("vae", "flux2-vae_small.safetensors")}},
        "shift": {"class_type": "ModelSamplingAuraFlow", "inputs": {"model": ["unet", 0], "shift": float(m.get("shift", 5.0))}},
        "slg": {"class_type": "SkipLayerGuidanceDiTSimple",
                "inputs": {"model": ["shift", 0], "double_layers": "", "single_layers": str(m.get("skip_layer", 22)),
                           "start_percent": 0.0, "end_percent": float(m.get("skip_end", 0.5))}},
        "pos": {"class_type": "CLIPTextEncode", "inputs": {"clip": ["clip", 0], "text": prompt}},
        "neg": {"class_type": "CLIPTextEncode", "inputs": {"clip": ["clip", 0], "text": negative}},
        "latent": {"class_type": m.get("latent_node", "EmptyFlux2LatentImage"),
                   "inputs": {"width": width, "height": height, "batch_size": 1}},
        "sample": {"class_type": "KSampler",
                   "inputs": {"model": ["slg", 0], "positive": ["pos", 0], "negative": ["neg", 0], "latent_image": ["latent", 0],
                              "seed": seed, "steps": int(m.get("steps", 25)), "cfg": float(m.get("cfg", 2.5)),
                              "sampler_name": m.get("sampler", "er_sde"), "scheduler": m.get("scheduler", "simple"),
                              "denoise": 1.0}},
        "decode": {"class_type": "VAEDecode", "inputs": {"samples": ["sample", 0], "vae": ["vae", 0]}},
        "save": {"class_type": "SaveImage", "inputs": {"images": ["decode", 0], "filename_prefix": "luna/moss"}},
    }


def generate(prompt: str, negative: str, width: int, height: int, m: dict, seed: int | None = None,
             timeout: int = 1200) -> bytes:
    """1枚描いて PNG のバイト列を返す。"""
    seed = seed if seed is not None else random.randint(0, 2**31 - 1)
    r = requests.post(f"{URL}/prompt", json={"prompt": workflow(prompt, negative, width, height, m, seed),
                                             "client_id": uuid.uuid4().hex}, timeout=60)
    if r.status_code >= 400:
        raise RuntimeError(f"ComfyUI がワークフローを受け付けませんでした: {r.text[:500]}")
    pid = r.json()["prompt_id"]
    end = time.time() + timeout
    while time.time() < end:
        time.sleep(3)
        h = requests.get(f"{URL}/history/{pid}", timeout=30).json().get(pid)
        if not h:
            continue
        status = h.get("status") or {}
        if status.get("status_str") == "error":
            msgs = [x for x in status.get("messages") or [] if x and x[0] == "execution_error"]
            raise RuntimeError(f"ComfyUI でエラー: {str(msgs[-1][1].get('exception_message') if msgs else status)[:400]}")
        for out in (h.get("outputs") or {}).values():
            for img in out.get("images") or []:
                v = requests.get(f"{URL}/view", params={"filename": img["filename"], "subfolder": img.get("subfolder", ""),
                                                        "type": img.get("type", "output")}, timeout=60)
                v.raise_for_status()
                return v.content
    raise RuntimeError(f"ComfyUI の絵が {timeout // 60} 分で終わりませんでした")
