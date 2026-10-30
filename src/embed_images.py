"""Discord の欄（embed）に画像を付ける。

欄の中で最初に載っている記事のページから代表画像（og:image）を取り、取れなければ欄の絵文字の画像
（Twemoji）を使う。Google ニュースのリンクは転送用のページで記事の画像が取れないので飛ばす。
"""
import re

import requests

from common import UA

OG = [re.compile(r'<meta[^>]+property=["\']og:image["\'][^>]*content=["\']([^"\']+)', re.I),
      re.compile(r'<meta[^>]+content=["\']([^"\']+)["\'][^>]*property=["\']og:image', re.I)]
LINK = re.compile(r"\]\((https?://[^)\s]+)\)")
SKIP = ("news.google.com",)
TWEMOJI = "https://cdn.jsdelivr.net/gh/jdecked/twemoji@15.1.0/assets/72x72/{}.png"
# 絵文字の範囲（記号・絵文字・国旗など）と、続けて1つの絵文字になる文字（異体字セレクタ・ZWJ・肌色）
EMOJI = re.compile(r"[←-⯿㊗㊙\U0001F000-\U0001FAFF][️‍\U0001F3FB-\U0001F3FF"
                   r"←-⯿\U0001F000-\U0001FAFF]*")


def og_image(url: str, timeout: int = 15) -> str | None:
    """ページの代表画像の URL。取れなければ None。"""
    try:
        r = requests.get(url, headers=UA, timeout=timeout)
        r.raise_for_status()
    except requests.RequestException:
        return None
    head = r.text[:200_000]
    for pat in OG:
        m = pat.search(head)
        if m and m.group(1).startswith("http"):
            return m.group(1).replace("&amp;", "&")
    return None


def emoji_image(title: str) -> str | None:
    """欄の見出しにある絵文字（「｜」の後ろの分類の絵文字を優先）の画像 URL。"""
    for part in reversed(title.split("｜")):
        m = EMOJI.search(part)
        if m:
            seq = m.group(0)
            if "‍" not in seq:   # Twemoji のファイル名は、ZWJ を含まなければ FE0F を付けない
                seq = seq.replace("️", "")
            return TWEMOJI.format("-".join(f"{ord(c):x}" for c in seq))
    return None


def add_image(embed: dict, mode: str = "thumbnail", tries: int = 3) -> None:
    """embed に画像を付ける。mode: thumbnail（右上に小さく）／image（下に大きく）。"""
    urls = [u for u in LINK.findall(embed.get("description") or "") if not any(s in u for s in SKIP)]
    img = next((i for i in (og_image(u) for u in urls[:tries]) if i), None) or emoji_image(embed.get("title") or "")
    if img:
        embed[mode] = {"url": img}
