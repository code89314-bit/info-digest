"""Create a new note magazine for a series week (free, PRIVATE), with the series cover as the magazine image.

Without --go it only fills in the form and saves a screenshot (<out>/magazine-form.png) to check; with --go it presses 作成.
Uses the logged-in browser profile of src/note_publish.py (a browser window opens).
Ask the user for the title first (e.g. 「がん種: 【EGFR遺伝子変異陽性 非小細胞肺がん】」「薬: 【ベバシズマブ】」).
series_publish.py later finds the magazine by the week title (state.json), so keep that title inside the name.

usage: python .claude/skills/series-script/scripts/note_magazine.py --title "<name>" --desc "<description>" --cover <series_cover.png> --out <dir> [--go]
"""
import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT / "src"))
import note_publish as npb  # noqa: E402
from playwright.sync_api import sync_playwright  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--title", required=True)
    ap.add_argument("--desc", required=True)
    ap.add_argument("--cover", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--go", action="store_true", help="press 作成 (otherwise only fill in and take a screenshot)")
    a = ap.parse_args()
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    with npb._profile_lock(), sync_playwright() as p:
        ctx = npb._context(p, headless=False)
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.goto("https://note.com/magazines/new", wait_until="domcontentloaded")
        page.wait_for_timeout(5000)
        page.locator("input[type=file]").first.set_input_files(a.cover)
        page.wait_for_timeout(4000)
        for name in ["保存", "完了", "適用", "決定"]:          # the crop dialog after choosing the image
            b = page.get_by_role("button", name=name, exact=True)
            if b.count() and b.first.is_visible():
                b.first.click()
                page.wait_for_timeout(3000)
                break
        page.locator("input#name").fill(a.title)
        page.locator("textarea").first.fill(a.desc)
        cb = page.locator("input[type=checkbox]").first      # 公開設定: checked = public
        if cb.is_checked():
            page.get_by_text("公開", exact=True).last.click()
            page.wait_for_timeout(800)
        print("public:", cb.is_checked())
        page.screenshot(path=str(out / "magazine-form.png"), full_page=True)
        print("screenshot", out / "magazine-form.png")
        if a.go:
            if cb.is_checked():
                sys.exit("still public - not creating")
            page.get_by_role("button", name="作成", exact=True).click()
            page.wait_for_timeout(6000)
            print("magazine:", page.url)
        ctx.close()


if __name__ == "__main__":
    main()
