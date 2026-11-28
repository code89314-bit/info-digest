"""Contact sheet of cover candidates for the user to choose from: 2 per row, each with a 200px-wide thumbnail
(how it looks shrunk on a phone) in its top-right corner, and a label "<letter>  <file name>" below.

usage: python cover_sheet.py <image.png> [<image.png> ...] --out <sheet.png>
"""
import argparse
import string
from pathlib import Path

from PIL import Image, ImageDraw


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("images", nargs="+")
    ap.add_argument("--out", required=True)
    ap.add_argument("--first", default="A", help="letter of the first image (e.g. D for a second sheet)")
    a = ap.parse_args()
    W, H, TW = 640, 336, 200
    rows = (len(a.images) + 1) // 2
    sheet = Image.new("RGB", (2 * W + 30, rows * (H + 30) + 10), "white")
    d = ImageDraw.Draw(sheet)
    start = string.ascii_uppercase.index(a.first.upper())
    for i, f in enumerate(a.images):
        im = Image.open(f).convert("RGB")
        r, c = divmod(i, 2)
        x, y = 10 + c * (W + 10), 10 + r * (H + 30)
        sheet.paste(im.resize((W, H)), (x, y))
        th = im.resize((TW, round(TW * im.height / im.width)))
        sheet.paste(th, (x + W - TW - 5, y + 5))
        d.text((x + 4, y + H + 4), f"{string.ascii_uppercase[(start + i) % 26]}  {Path(f).name}", fill="black")
    sheet.save(a.out)
    print("saved", a.out)


if __name__ == "__main__":
    main()
