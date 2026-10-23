"""Discord Webhook のアイコン（assets/*.png）を作る。python scripts/make_icons.py

公開リポジトリの raw URL を config.yaml の avatar_url に書いて使う。
どちらも「クリーム×ネイビー」配色（暗い Discord の中で目立つ）で、左右に podcast の音波を入れた姉妹デザイン。
  onco.png  : カプセルがマイクになっている（ニュースを podcast で届ける）
  learn.png : 開いた本（学習番組）
  ai.png        : AI チップ（ローカルAIダイジェスト）
  ai_global.png : 地球と AI チップ（海外AIダイジェスト）
  gimon.png     : 虫めがねと「?」（ギモン深堀り Bot。Bot のアイコンは API で設定）
"""
import math
import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont

FONT = "C:/Windows/Fonts/YuGothB.ttc"

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


def chip(d: ImageDraw.ImageDraw, cx: int, cy: int, size: int, text_size: int) -> None:
    """AI チップ（ネイビーの本体・コーラルの足・白い「AI」）。"""
    s = size // 2
    pin, gap, w = size // 6, size // 4, max(10, size // 14)
    for k in (-1, 0, 1):
        o = k * gap
        for (x0, y0, x1, y1) in ((cx + o - w, cy - s - pin, cx + o + w, cy - s), (cx + o - w, cy + s, cx + o + w, cy + s + pin),
                                 (cx - s - pin, cy + o - w, cx - s, cy + o + w), (cx + s, cy + o - w, cx + s + pin, cy + o + w)):
            d.rounded_rectangle([x0, y0, x1, y1], radius=w // 2, fill=rgb(CORAL))
    d.rounded_rectangle([cx - s, cy - s, cx + s, cy + s], radius=size // 8, fill=rgb(NAVY))
    d.rounded_rectangle([cx - s + w * 2, cy - s + w * 2, cx + s - w * 2, cy + s - w * 2], radius=size // 12,
                        outline=rgb(STEEL), width=max(6, w // 2))
    d.text((cx, cy + 4), "AI", font=ImageFont.truetype(FONT, text_size), fill=rgb(WHITE), anchor="mm")


def ai() -> None:
    """ローカルAIダイジェスト: AI チップ。"""
    fg = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    d = ImageDraw.Draw(fg)
    chip(d, 512, 512, 330, 150)
    waves(d, 512, 512)
    finish(fg, "ai.png")


def ai_global() -> None:
    """海外AIダイジェスト: 地球（ティールの海・ネイビーの経緯線）の右下に AI チップ。"""
    fg = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    d = ImageDraw.Draw(fg)
    cx, cy, r = 470, 470, 220
    d.ellipse([cx - r, cy - r, cx + r, cy + r], fill=rgb(TEAL))
    for w in (0.5,):
        d.ellipse([cx - r * w, cy - r, cx + r * w, cy + r], outline=rgb(NAVY), width=18)
    d.line([(cx, cy - r), (cx, cy + r)], fill=rgb(NAVY), width=18)
    for y in (-0.5, 0, 0.5):
        hw = r * math.sqrt(1 - y * y)
        d.line([(cx - hw, cy + r * y), (cx + hw, cy + r * y)], fill=rgb(NAVY), width=18)
    d.ellipse([cx - r, cy - r, cx + r, cy + r], outline=rgb(NAVY), width=26)
    d.ellipse([655 - 150, 655 - 150, 655 + 150, 655 + 150], fill=rgb(BG[0]))   # チップのまわりを抜く
    chip(d, 655, 655, 190, 86)
    finish(fg, "ai_global.png")


def gimon() -> None:
    """ギモン深堀り（Discord Bot）: 虫めがねの中に「？」。"""
    fg = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    d = ImageDraw.Draw(fg)
    cx, cy, r = 450, 450, 200
    hx0, hx1 = cx + int(r * 0.72), cx + int(r * 1.55)
    d.line([(hx0, hx0), (hx1, hx1)], fill=rgb(CORAL), width=80)
    d.ellipse([hx1 - 40, hx1 - 40, hx1 + 40, hx1 + 40], fill=rgb(CORAL))
    d.ellipse([cx - r, cy - r, cx + r, cy + r], fill=rgb(WHITE), outline=rgb(NAVY), width=44)
    d.text((cx, cy + 10), "?", font=ImageFont.truetype(FONT, 260), fill=rgb(NAVY), anchor="mm")
    d.arc([cx - r + 50, cy - r + 50, cx + r - 50, cy + r - 50], 200, 250, fill=rgb(TEAL), width=18)   # つや
    finish(fg, "gimon.png")


def note() -> None:
    """note 下書き: 白い原稿（ネイビーの行）と、斜めの鉛筆（アンバーの軸・コーラルの消しゴム）。"""
    fg = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    d = ImageDraw.Draw(fg)
    x0, y0, x1, y1 = 280, 230, 660, 790
    d.rounded_rectangle([x0, y0, x1, y1], radius=36, fill=rgb(WHITE), outline=rgb(NAVY), width=26)
    for k in range(6):
        y = y0 + 95 + k * 72
        d.rounded_rectangle([x0 + 60, y - 10, x1 - 60 - (k % 2) * 90, y + 10], radius=10, fill=rgb(STEEL))
    pen = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    p = ImageDraw.Draw(pen)
    cx, w = 512, 92
    p.rounded_rectangle([cx - w // 2, 150, cx + w // 2, 230], radius=24, fill=rgb(CORAL))      # 消しゴム
    p.rectangle([cx - w // 2, 222, cx + w // 2, 250], fill=rgb(STEEL))
    p.rectangle([cx - w // 2, 250, cx + w // 2, 700], fill=rgb(AMBER))                          # 軸
    p.polygon([(cx - w // 2, 700), (cx + w // 2, 700), (cx, 830)], fill=rgb("#FBE3C0"))         # 削った木
    p.polygon([(cx - 16, 785), (cx + 16, 785), (cx, 830)], fill=rgb(NAVY))                      # 芯
    pen = pen.rotate(-35, resample=Image.BICUBIC, center=(512, 512), translate=(150, -20))
    fg.alpha_composite(pen)
    finish(fg, "note.png")


if __name__ == "__main__":
    OUT.mkdir(exist_ok=True)
    targets = sys.argv[1:] or ["onco", "learn", "ai", "ai_global", "gimon", "note"]
    for name in targets:
        globals()[name]()
