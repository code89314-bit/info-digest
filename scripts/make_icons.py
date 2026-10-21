"""Discord Webhook のアイコン（assets/*.png）を作る。python scripts/make_icons.py

公開リポジトリの raw URL を config.yaml の avatar_url に書いて使う。
どちらも「クリーム×ネイビー」配色（暗い Discord の中で目立つ）で、左右に podcast の音波を入れた姉妹デザイン。
  onco.png  : カプセルがマイクになっている（ニュースを podcast で届ける）
  learn.png : 開いた本（学習番組）
"""
import math
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter

OUT = Path(__file__).resolve().parents[1] / "assets"
S = 1024          # 描画サイズ（最後に 512 に縮小してなめらかにする）

BG = ("#FFF7EA", "#F3E3C7")
WHITE, NAVY, CORAL, CORAL_DARK, STEEL, TEAL, AMBER =     "#FFFFFF", "#1E3A5F", "#FF6B5B", "#C2410C", "#5B7896", "#14B8A6", "#F59E0B"


def rgb(h: str) -> tuple[int, int, int]:
    return tuple(int(h[i:i + 2], 16) for i in (1, 3, 5))


def gradient(top: str, bottom: str) -> Image.Image:
    a, b = rgb(top), rgb(bottom)
    img = Image.new("RGB", (S, S))
    d = ImageDraw.Draw(img)
    for i in range(2 * S):
        t = i / (2 * S - 1)
        d.line([(i, 0), (0, i)], fill=tuple(round(a[k] + (b[k] - a[k]) * t) for k in range(3)), width=2)
    return img


def waves(d: ImageDraw.ImageDraw, cx: int, cy: int) -> None:
    """左右の音波（内側ティール、外側アンバー）。"""
    for r, col in ((260, TEAL), (330, AMBER)):
        for a0, a1 in ((-32, 32), (148, 212)):
            d.arc([cx - r, cy - r, cx + r, cy + r], a0, a1, fill=rgb(col), width=30)


def finish(fg: Image.Image, name: str) -> None:
    img = gradient(*BG).convert("RGBA")
    sh = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    sh.putalpha(fg.split()[3].filter(ImageFilter.GaussianBlur(22)).point(lambda v: v * 50 // 255))
    img.alpha_composite(sh, (0, 18))
    img.alpha_composite(fg)
    img.resize((512, 512), Image.LANCZOS).save(OUT / name)
    print("saved", OUT / name)


def onco() -> None:
    fg = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    d = ImageDraw.Draw(fg)
    cx, top, bot, w = 512, 170, 600, 250
    mid = (top + bot) // 2
    box = [cx - w // 2, top, cx + w // 2, bot]
    d.rounded_rectangle(box, radius=w // 2, fill=rgb(NAVY))
    lower = Image.new("RGBA", (S, S), (0, 0, 0, 0))     # カプセルの下半分（コーラル）
    ImageDraw.Draw(lower).rounded_rectangle(box, radius=w // 2, fill=rgb(CORAL))
    mask = Image.new("L", (S, S), 0)
    ImageDraw.Draw(mask).rectangle([0, mid, S, S], fill=255)
    fg.paste(lower, (0, 0), Image.composite(lower.split()[3], mask, mask))
    for row in range(4):                                # 上半分にマイクの網目
        y = top + 70 + row * 42
        half = math.sqrt(max(0, (w / 2) ** 2 - max(0, top + w / 2 - y) ** 2)) - 40 if y < top + w / 2 else w / 2 - 40
        n = int(half * 2 // 42) + 1
        for k in range(n):
            x = cx - (n - 1) * 21 + k * 42
            d.ellipse([x - 9, y - 9, x + 9, y + 9], fill=rgb(STEEL))
    d.rectangle([cx - w // 2, mid - 6, cx + w // 2, mid + 6], fill=rgb(CORAL_DARK))
    d.arc([cx - 185, mid - 150, cx + 185, bot + 70], 0, 180, fill=rgb(NAVY), width=34)   # ホルダーとスタンド
    d.rounded_rectangle([cx - 17, bot + 68, cx + 17, 800], radius=10, fill=rgb(NAVY))
    d.rounded_rectangle([cx - 130, 790, cx + 130, 830], radius=20, fill=rgb(NAVY))
    waves(d, cx, mid)
    finish(fg, "onco.png")


def learn() -> None:
    fg = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    d = ImageDraw.Draw(fg)
    cx, cy = 512, 470
    top, bot, half = cy - 150, cy + 150, 165
    # 表紙（ネイビー）→ 左右のページ（白）→ 文字の行 → のど
    d.polygon([(cx, bot + 40), (cx - half - 30, bot + 10), (cx - half - 30, top + 30), (cx, top + 60),
               (cx + half + 30, top + 30), (cx + half + 30, bot + 10)], fill=rgb(NAVY))
    for s in (-1, 1):
        d.polygon([(cx, top + 30), (cx + s * 60, top - 10), (cx + s * half, top), (cx + s * half, bot - 10),
                   (cx + s * 60, bot - 20), (cx, bot + 10)], fill=rgb(WHITE))
        for k in range(5):
            y = top + 55 + k * 46
            x0, x1 = sorted([cx + s * 45, cx + s * (half - 40)])
            d.rounded_rectangle([x0, y - 9, x1, y + 9], radius=9, fill=rgb(STEEL))
    d.line([(cx, top + 30), (cx, bot + 10)], fill=rgb(NAVY), width=12)
    # しおり（コーラル）
    bx = cx + 110
    d.polygon([(bx - 26, top - 4), (bx + 26, top - 2), (bx + 26, bot + 90), (bx, bot + 62), (bx - 26, bot + 90)],
              fill=rgb(CORAL))
    waves(d, cx, cy)
    finish(fg, "learn.png")


if __name__ == "__main__":
    OUT.mkdir(exist_ok=True)
    onco()
    learn()
