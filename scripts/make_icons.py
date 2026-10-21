"""Discord Webhook のアイコン（assets/*.png）を作る。python scripts/make_icons.py

公開リポジトリの raw URL を config.yaml の avatar_url に書いて使う。
"""
import math
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter

OUT = Path(__file__).resolve().parents[1] / "assets"
S = 1024          # 描画サイズ（最後に 512 に縮小してなめらかにする）


def rgb(h: str) -> tuple[int, int, int]:
    return tuple(int(h[i:i + 2], 16) for i in (1, 3, 5))


def gradient(top: str, bottom: str) -> Image.Image:
    a, b = rgb(top), rgb(bottom)
    img = Image.new("RGB", (S, S))
    d = ImageDraw.Draw(img)
    for i in range(2 * S):
        t = i / (2 * S - 1)
        c = tuple(round(a[k] + (b[k] - a[k]) * t) for k in range(3))
        d.line([(i, 0), (0, i)], fill=c, width=2)      # 左上→右下の斜めグラデーション
    return img


def bezier(p0, p1, p2, p3, n=200):
    for i in range(n + 1):
        t = i / n
        u = 1 - t
        yield (u ** 3 * p0[0] + 3 * u * u * t * p1[0] + 3 * u * t * t * p2[0] + t ** 3 * p3[0],
               u ** 3 * p0[1] + 3 * u * u * t * p1[1] + 3 * u * t * t * p2[1] + t ** 3 * p3[1])


def stroke(d: ImageDraw.ImageDraw, pts, width: int, fill) -> None:
    r = width / 2
    for x, y in pts:
        d.ellipse([x - r, y - r, x + r, y + r], fill=fill)


def shadow(layer: Image.Image, radius=18, alpha=90) -> Image.Image:
    a = layer.split()[3].filter(ImageFilter.GaussianBlur(radius)).point(lambda v: v * alpha // 255)
    sh = Image.new("RGBA", layer.size, (0, 0, 0, 0))
    sh.putalpha(a)
    return sh


def finish(bg: Image.Image, fg: Image.Image, name: str) -> None:
    img = bg.convert("RGBA")
    img.alpha_composite(shadow(fg), (0, 14))
    img.alpha_composite(fg)
    img.resize((512, 512), Image.LANCZOS).save(OUT / name)
    print("saved", OUT / name)


def onco() -> None:
    """がん薬物療法ニュース: 紺→ティールの地に、ラベンダーのがん啓発リボンとカプセル。"""
    bg = gradient("#16325C", "#0F8A86")
    fg = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    d = ImageDraw.Draw(fg)
    lav, edge = rgb("#E9DEFF"), rgb("#9F86E0")
    cx = 470
    # リボン: 右下→交差→左上→頂上→右側を下りて交差→左下 の1本のひもを描き、
    # 交差のまわりだけ「下りてくるひも」を上から描き直して重なりを見せる
    down = list(bezier((cx, 185), (cx + 170, 190), (cx + 150, 420), (cx, 575)))
    front = down[90:] + list(bezier((cx, 575), (cx - 40, 640), (cx - 120, 720), (cx - 190, 860)))
    path = list(bezier((cx + 190, 860), (cx + 120, 720), (cx + 40, 640), (cx, 575))) + \
        list(bezier((cx, 575), (cx - 150, 420), (cx - 170, 190), (cx, 185))) + down + front
    stroke(d, path, 132, edge)
    stroke(d, path, 104, lav)
    over = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    o = ImageDraw.Draw(over)
    stroke(o, front, 132, edge)
    stroke(o, front, 104, lav)
    mask = Image.new("L", (S, S), 0)
    ImageDraw.Draw(mask).ellipse([cx - 130, 575 - 130, cx + 130, 575 + 130], fill=255)
    fg.paste(over, (0, 0), Image.composite(over.split()[3], mask, mask))
    # カプセル（右下、斜め）
    cap = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    c = ImageDraw.Draw(cap)
    x0, y0, w, h = 560, 560, 330, 140
    c.rounded_rectangle([x0, y0, x0 + w, y0 + h], radius=h // 2, fill=(255, 255, 255, 255),
                        outline=rgb("#0B4F6C"), width=14)
    c.rounded_rectangle([x0 + w // 2, y0, x0 + w, y0 + h], radius=h // 2, fill=rgb("#FFB547"),
                        outline=rgb("#0B4F6C"), width=14)
    c.rectangle([x0 + w // 2, y0 + 7, x0 + w // 2 + h // 2, y0 + h - 7], fill=rgb("#FFB547"))
    c.line([(x0 + w // 2, y0 + 7), (x0 + w // 2, y0 + h - 7)], fill=rgb("#0B4F6C"), width=10)
    c.ellipse([x0 + 40, y0 + 30, x0 + 80, y0 + 55], fill=(255, 255, 255, 140))
    cap = cap.rotate(38, center=(x0 + w / 2, y0 + h / 2), resample=Image.BICUBIC).transform(
        (S, S), Image.AFFINE, (1, 0, -40, 0, 1, -40), resample=Image.BICUBIC)
    fg.alpha_composite(shadow(cap, 10, 120), (0, 8))
    fg.alpha_composite(cap)
    finish(bg, fg, "onco.png")


def learn() -> None:
    """がん薬物療法 学習: こげ茶→琥珀の地に、開いた本とラベンダーのしおりリボン。"""
    bg = gradient("#4E342E", "#C58B3A")
    fg = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    d = ImageDraw.Draw(fg)
    paper, line, edge = rgb("#FFF8EC"), rgb("#D7C3A3"), rgb("#6D4C41")
    mid, top, bot = S // 2, 300, 760
    left = [(mid, top + 40), (mid - 70, top - 10), (mid - 330, top + 10), (mid - 340, bot - 20),
            (mid - 80, bot - 40), (mid, bot)]
    right = [(S - x, y) for x, y in left]
    for poly in (left, right):
        d.polygon(poly, fill=paper, outline=edge, width=12)
    for k in range(5):                      # 文字の行
        y = top + 90 + k * 70
        d.line([(mid - 280, y + 6), (mid - 70, y - 4 + 10)], fill=line, width=16)
        d.line([(mid + 70, y - 4 + 10), (mid + 280, y + 6)], fill=line, width=16)
    d.line([(mid, top + 40), (mid, bot)], fill=edge, width=12)
    # しおり（ラベンダーのリボン）
    bx = mid + 170
    d.polygon([(bx - 34, top - 2), (bx + 34, top + 2), (bx + 34, bot + 120), (bx, bot + 85), (bx - 34, bot + 120)],
              fill=rgb("#B39DDB"), outline=rgb("#7E57C2"))
    finish(bg, fg, "learn.png")


if __name__ == "__main__":
    OUT.mkdir(exist_ok=True)
    onco()
    learn()
