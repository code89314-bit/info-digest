"""note 週刊号のトップ画像（1280×670）を作る。python scripts/make_cover.py 2026-10-15 2026-10-21

「じぶんの薬局」の過去のトップ画像のテイストに合わせる:
くすんだパステルの無地の背景、少し揺れた濃いグレーの手描き風の線、平らな塗り、モチーフは中央に1〜3個で余白を多く、文字は小さなラベルだけ。
モチーフ: 週刊の新聞（日付入り）・点滴バッグ・カプセル。背景色は週ごとに入れ替わる。
"""
import datetime as dt
import math
import random
import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

W, H, K = 1280, 670, 2                 # K 倍で描いて縮小（線をなめらかにする）
INK = (58, 58, 58)
BACKGROUNDS = ["#BCC9A6", "#D6D2A0", "#F6E7E4", "#E9ECF2", "#CFDCE3"]   # セージ・カーキ・ピンク・グレー・ブルーグレー
PAPER, NEWS_HEAD = "#F7F5EE", "#9DB0C4"
BAG, LIQUID = "#F4F6F7", "#C9B8E6"
CAP_A, CAP_B = "#F2F0EA", "#E8A88C"
FONT = "C:/Windows/Fonts/YuGothB.ttc"


def rgb(h: str) -> tuple[int, int, int]:
    return tuple(int(h[i:i + 2], 16) for i in (1, 3, 5))


class Pen:
    """手描き風の線: 点列に低い周波数の揺れを足し、丸い点を並べて太線にする。"""

    def __init__(self, d: ImageDraw.ImageDraw, seed: int):
        self.d, self.rnd = d, random.Random(seed)

    def wobble(self, pts: list[tuple[float, float]], amp: float = 2.2) -> list[tuple[float, float]]:
        ph1, ph2 = self.rnd.uniform(0, 6.28), self.rnd.uniform(0, 6.28)
        out = []
        for i, (x, y) in enumerate(pts):
            t = i / max(1, len(pts) - 1)
            out.append((x + amp * K * math.sin(t * 7 + ph1), y + amp * K * math.sin(t * 5 + ph2)))
        return out

    def line(self, pts, width: float = 6.5, color=INK) -> None:
        r = width * K / 2
        dense = []
        for (x0, y0), (x1, y1) in zip(pts, pts[1:]):
            n = max(2, int(math.hypot(x1 - x0, y1 - y0) / 2))
            dense += [(x0 + (x1 - x0) * k / n, y0 + (y1 - y0) * k / n) for k in range(n)]
        dense.append(pts[-1])
        for x, y in dense:
            self.d.ellipse([x - r, y - r, x + r, y + r], fill=color)

    def shape(self, pts, fill, width: float = 6.5, closed: bool = True) -> None:
        pts = self.wobble(pts)
        if fill:
            self.d.polygon(pts, fill=rgb(fill) if isinstance(fill, str) else fill)
        self.line(pts + ([pts[0]] if closed else []), width)


def rounded(x0, y0, x1, y1, r, n=10):
    """角の丸い四角の輪郭の点列（K 倍の座標で渡す）。"""
    pts = []
    for cx, cy, a0 in ((x1 - r, y0 + r, -90), (x1 - r, y1 - r, 0), (x0 + r, y1 - r, 90), (x0 + r, y0 + r, 180)):
        pts += [(cx + r * math.cos(math.radians(a0 + 90 * k / n)), cy + r * math.sin(math.radians(a0 + 90 * k / n)))
                for k in range(n + 1)]
    return pts


def edge(a, b, n=30):
    return [(a[0] + (b[0] - a[0]) * k / n, a[1] + (b[1] - a[1]) * k / n) for k in range(n + 1)]


def rotate(pts, cx, cy, deg):
    a = math.radians(deg)
    return [(cx + (x - cx) * math.cos(a) - (y - cy) * math.sin(a), cy + (x - cx) * math.sin(a) + (y - cy) * math.cos(a))
            for x, y in pts]


def newspaper(img, pen: Pen, cx, cy, start: dt.date, end: dt.date) -> None:
    w, h, deg = 330 * K, 400 * K, -6
    x0, y0 = cx - w / 2, cy - h / 2
    outline = edge((x0, y0), (x0 + w, y0)) + edge((x0 + w, y0), (x0 + w, y0 + h)) + \
        edge((x0 + w, y0 + h), (x0, y0 + h)) + edge((x0, y0 + h), (x0, y0))
    pen.shape(rotate(outline, cx, cy, deg), PAPER)
    band = edge((x0 + 22 * K, y0 + 22 * K), (x0 + w - 22 * K, y0 + 22 * K)) + \
        edge((x0 + w - 22 * K, y0 + 22 * K), (x0 + w - 22 * K, y0 + 130 * K)) + \
        edge((x0 + w - 22 * K, y0 + 130 * K), (x0 + 22 * K, y0 + 130 * K)) + \
        edge((x0 + 22 * K, y0 + 130 * K), (x0 + 22 * K, y0 + 22 * K))
    pen.shape(rotate(band, cx, cy, deg), NEWS_HEAD, width=5)
    for k in range(5):                                   # 記事の行
        y = y0 + (170 + k * 44) * K
        right = x0 + w - (40 + (k % 3) * 40) * K
        pen.line(pen.wobble(rotate(edge((x0 + 40 * K, y), (right, y), 12), cx, cy, deg), 1.2), width=5, color=(150, 150, 150))
    # 題字と日付（新聞の傾きに合わせて回した文字を貼る）
    label = Image.new("RGBA", (int(w), int(130 * K)), (0, 0, 0, 0))
    ld = ImageDraw.Draw(label)
    ld.text((w / 2, 48 * K), "週刊", font=ImageFont.truetype(FONT, 44 * K), fill=INK, anchor="mm")
    ld.text((w / 2, 96 * K), f"{start.month}/{start.day}–{end.month}/{end.day}",
            font=ImageFont.truetype(FONT, 22 * K), fill=INK, anchor="mm")
    label = label.rotate(-deg, resample=Image.BICUBIC, expand=True)
    lx, ly = rotate([(cx, y0 + 76 * K)], cx, cy, deg)[0]
    img.alpha_composite(label, (int(lx - label.width / 2), int(ly - label.height / 2)))


def iv_bag(pen: Pen, cx, cy) -> None:
    w, h = 190 * K, 270 * K
    x0, y0 = cx - w / 2, cy - h / 2
    pen.shape(rounded(x0, y0, x0 + w, y0 + h, 40 * K), BAG)
    pen.shape(rounded(x0 + 18 * K, y0 + 110 * K, x0 + w - 18 * K, y0 + h - 18 * K, 26 * K), LIQUID, width=0.1)
    pen.shape(rounded(cx - 28 * K, y0 - 40 * K, cx + 28 * K, y0 + 6 * K, 10 * K), BAG, width=5)      # つり下げ穴
    pen.d.ellipse([cx - 9 * K, y0 - 28 * K, cx + 9 * K, y0 - 10 * K], fill=INK)
    for k in range(3):                                                                         # 目盛り
        y = y0 + (60 + k * 30) * K
        pen.line(edge((x0 + 26 * K, y), (x0 + 70 * K, y), 6), width=4)
    pen.shape(rounded(cx - 18 * K, y0 + h - 4 * K, cx + 18 * K, y0 + h + 52 * K, 8 * K), BAG, width=5)   # 出口
    tube = [(cx + 6 * K * math.sin(t / 6), y0 + h + 52 * K + t * 2.6 * K) for t in range(0, 36)]
    tube += [(tube[-1][0] + k * 3 * K, tube[-1][1] + k * 1.2 * K) for k in range(1, 30)]
    pen.line(pen.wobble(tube, 1.5), width=6)


def capsule(img, pen: Pen, cx, cy, deg) -> None:
    """カプセル: 全体を片色で塗り、右半分だけもう一色、最後に輪郭と継ぎ目。"""
    w, h = 150 * K, 64 * K
    x0, y0 = cx - w / 2, cy - h / 2
    outline = pen.wobble(rotate(rounded(x0, y0, x0 + w, y0 + h, h / 2 - 1), cx, cy, deg), 1.2)
    pen.d.polygon(outline, fill=rgb(CAP_A))
    half = Image.new("L", img.size, 0)
    ImageDraw.Draw(half).polygon(rotate([(cx, y0 - 20), (x0 + w + 20, y0 - 20), (x0 + w + 20, y0 + h + 20),
                                         (cx, y0 + h + 20)], cx, cy, deg), fill=255)
    shape = Image.new("L", img.size, 0)
    ImageDraw.Draw(shape).polygon(outline, fill=255)
    from PIL import ImageChops
    img.paste(rgb(CAP_B) + (255,), (0, 0), ImageChops.multiply(half, shape))
    pen.d = ImageDraw.Draw(img)
    pen.line(outline + [outline[0]], width=6.5)
    pen.line(pen.wobble(rotate(edge((cx, y0 + 4 * K), (cx, y0 + h - 4 * K), 8), cx, cy, deg), 0.8), width=5)


def make_cover(start: dt.date, end: dt.date, out: Path, seed: int | None = None) -> Path:
    week = end.isocalendar()[1]
    bg = BACKGROUNDS[week % len(BACKGROUNDS)]
    img = Image.new("RGBA", (W * K, H * K), rgb(bg) + (255,))
    pen = Pen(ImageDraw.Draw(img), seed if seed is not None else week)
    newspaper(img, pen, 480 * K, 310 * K, start, end)
    pen.d = ImageDraw.Draw(img)
    iv_bag(pen, 800 * K, 270 * K)
    capsule(img, pen, 285 * K, 545 * K, -18)
    capsule(img, pen, 990 * K, 500 * K, 24)
    out.parent.mkdir(parents=True, exist_ok=True)
    img.convert("RGB").resize((W, H), Image.LANCZOS).save(out)
    return out


# ---- 水曜号「保険と公費のギモン」：保険証・レセプト・吹き出しの「？」 ----
CARD, CARD_BAND, SHEET, BUBBLE = "#F7F5EE", "#E8A88C", "#FBFAF6", "#F2F0EA"


def label(img, text: str, size: int, cx, cy, deg: float = 0, color=INK) -> None:
    """回した文字を (cx, cy) を中心に貼る（K 倍の座標）。"""
    font = ImageFont.truetype(FONT, int(size * K))
    box = ImageDraw.Draw(img).textbbox((0, 0), text, font=font)
    tile = Image.new("RGBA", (box[2] - box[0] + 20 * K, box[3] - box[1] + 20 * K), (0, 0, 0, 0))
    ImageDraw.Draw(tile).text((tile.width / 2, tile.height / 2), text, font=font, fill=color, anchor="mm")
    tile = tile.rotate(-deg, resample=Image.BICUBIC, expand=True)
    img.alpha_composite(tile, (int(cx - tile.width / 2), int(cy - tile.height / 2)))


def insurance_card(img, pen: Pen, cx, cy) -> None:
    w, h, deg = 440 * K, 270 * K, -7
    x0, y0 = cx - w / 2, cy - h / 2
    pen.shape(rotate(rounded(x0, y0, x0 + w, y0 + h, 26 * K), cx, cy, deg), CARD)
    pen.shape(rotate(edge((x0, y0 + 26 * K), (x0 + w, y0 + 26 * K), 20) + edge((x0 + w, y0 + 26 * K), (x0 + w, y0 + 78 * K), 6)
                     + edge((x0 + w, y0 + 78 * K), (x0, y0 + 78 * K), 20) + edge((x0, y0 + 78 * K), (x0, y0 + 26 * K), 6),
                     cx, cy, deg), CARD_BAND, width=4)
    pen.shape(rotate(rounded(x0 + 30 * K, y0 + 110 * K, x0 + 130 * K, y0 + 230 * K, 10 * K), cx, cy, deg), "#D9DDE3", width=5)
    for k in range(4):                                   # 記載欄の行
        y = y0 + (118 + k * 34) * K
        pen.line(pen.wobble(rotate(edge((x0 + 160 * K, y), (x0 + w - (40 + (k % 2) * 70) * K, y), 12), cx, cy, deg), 1.0),
                 width=4.5, color=(150, 150, 150))
    lx, ly = rotate([(cx, y0 + 52 * K)], cx, cy, deg)[0]
    label(img, "保険と公費", 30, lx, ly, deg)


def receipt(img, pen: Pen, cx, cy, day: dt.date) -> None:
    w, h, deg = 300 * K, 390 * K, 6
    x0, y0 = cx - w / 2, cy - h / 2
    outline = edge((x0, y0), (x0 + w, y0)) + edge((x0 + w, y0), (x0 + w, y0 + h)) + \
        edge((x0 + w, y0 + h), (x0, y0 + h)) + edge((x0, y0 + h), (x0, y0))
    pen.shape(rotate(outline, cx, cy, deg), SHEET)
    gx0, gx1, gy0 = x0 + 28 * K, x0 + w - 28 * K, y0 + 110 * K
    for k in range(6):                                   # 表の横線
        pen.line(pen.wobble(rotate(edge((gx0, gy0 + k * 42 * K), (gx1, gy0 + k * 42 * K), 12), cx, cy, deg), 0.8),
                 width=3.5, color=(120, 120, 120))
    pen.line(pen.wobble(rotate(edge((gx1 - 80 * K, gy0), (gx1 - 80 * K, gy0 + 210 * K), 10), cx, cy, deg), 0.8),
             width=3.5, color=(120, 120, 120))
    lx, ly = rotate([(cx, y0 + 50 * K)], cx, cy, deg)[0]
    label(img, f"{day.month}/{day.day}", 30, lx, ly, deg)
    for k, pts in enumerate(("20", "45", "100", "8")):  # 点数の欄
        px, py = rotate([(gx1 - 40 * K, gy0 + (21 + k * 42) * K)], cx, cy, deg)[0]
        label(img, pts, 18, px, py, deg, color=(90, 90, 90))
    px, py = rotate([(cx - 30 * K, y0 + h - 50 * K)], cx, cy, deg)[0]
    label(img, "点", 26, px, py, deg)


def question_bubble(img, pen: Pen, cx, cy) -> None:
    r = 92 * K
    pts = [(cx + r * math.cos(math.radians(a)), cy + r * 0.86 * math.sin(math.radians(a))) for a in range(120, 470, 6)]
    tail = [(cx - 40 * K, cy + 70 * K), (cx - 92 * K, cy + 128 * K), (cx - 6 * K, cy + 82 * K)]
    pen.shape(pts[:-6] + tail + pts[-6:], BUBBLE)
    label(img, "？", 92, cx + 4 * K, cy - 2 * K)


def make_cover_hoken(day: dt.date, out: Path, seed: int | None = None) -> Path:
    week = day.isocalendar()[1]
    bg = BACKGROUNDS[(week + 2) % len(BACKGROUNDS)]      # 土曜号と同じ週でも背景色をずらす
    img = Image.new("RGBA", (W * K, H * K), rgb(bg) + (255,))
    pen = Pen(ImageDraw.Draw(img), seed if seed is not None else week + 100)
    receipt(img, pen, 820 * K, 350 * K, day)
    pen.d = ImageDraw.Draw(img)
    insurance_card(img, pen, 450 * K, 330 * K)
    pen.d = ImageDraw.Draw(img)
    question_bubble(img, pen, 1080 * K, 150 * K)
    pen.d = ImageDraw.Draw(img)
    capsule(img, pen, 250 * K, 560 * K, -18)
    out.parent.mkdir(parents=True, exist_ok=True)
    img.convert("RGB").resize((W, H), Image.LANCZOS).save(out)
    return out


if __name__ == "__main__":
    if sys.argv[1] == "hoken":     # python scripts/make_cover.py hoken 2026-10-28 [out.png]
        print(make_cover_hoken(dt.date.fromisoformat(sys.argv[2]),
                               Path(sys.argv[3]) if len(sys.argv) > 3 else Path(".local/cover_hoken.png")))
        sys.exit()
    s, e = (dt.date.fromisoformat(a) for a in sys.argv[1:3])
    print(make_cover(s, e, Path(sys.argv[3]) if len(sys.argv) > 3 else Path(".local/cover.png")))
