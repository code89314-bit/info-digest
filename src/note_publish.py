"""note に週刊号を「下書き保存」する（公開はしない。最終確認と公開はユーザーが手で行う）。

note には公式の投稿APIが無いので、Playwright で note 専用のブラウザ（プロファイル）を操作する。
ログインは最初に1回だけ手で行い（--login）、ログイン状態はこのプロファイル（.local/note_profile）に残す。
ふだん使いの Chrome のログイン情報・Cookie には触らない。押すのは「下書き保存」だけ（公開・投稿のボタンは押さない）。

準備（1回だけ）:
    .venv\\Scripts\\python.exe -m playwright install chromium
    .venv\\Scripts\\python.exe src\\note_publish.py --login     ← 開いたブラウザで note にログイン
使い方:
    python src/note_publish.py --check                          ログインが生きているか
    python src/note_publish.py --draft .local/note_drafts/2026-W43.json   保存済みの号を下書き保存
note のエディタは画面なし（headless）だと読み込みが終わらないため、ブラウザの画面を出して操作する。
（ふだんは note_weekly.py が下書きを作ったあとに自動で呼ぶ）
"""
import datetime as dt
import json
import sys
import time
from pathlib import Path

from common import ROOT

PROFILE = ROOT / ".local" / "note_profile"
NEW_URL = "https://note.com/notes/new"
LOGIN_URL = "https://note.com/login"


class NeedLogin(Exception):
    pass


def _context(p, headless: bool):
    PROFILE.mkdir(parents=True, exist_ok=True)
    return p.chromium.launch_persistent_context(str(PROFILE), headless=headless, locale="ja-JP",
                                                viewport={"width": 1280, "height": 900})


def _shot(page, name: str) -> Path:
    """失敗したときの画面を残す（原因を調べるため）。"""
    out = ROOT / ".local" / "logs" / f"note_publish-{name}-{dt.datetime.now():%Y%m%d-%H%M%S}.png"
    out.parent.mkdir(parents=True, exist_ok=True)
    try:
        page.screenshot(path=str(out), full_page=True)
    except Exception:   # noqa: BLE001
        pass
    return out


def login(timeout_min: int = 15) -> None:
    """画面ありのブラウザで note のログイン画面を開き、ユーザーがログインするのを待つ。"""
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        ctx = _context(p, headless=False)
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.goto(LOGIN_URL)
        print(f"開いたブラウザで note にログインしてください（{timeout_min}分待ちます）…")
        end = time.time() + timeout_min * 60
        while time.time() < end:
            time.sleep(3)
            pages = [pg for pg in ctx.pages if not pg.is_closed()]
            if not pages:   # ユーザーがブラウザを閉じた
                break
            if any("note.com" in pg.url and "/login" not in pg.url for pg in pages):
                time.sleep(3)   # ログイン直後の Cookie の保存を待つ
                break
        try:
            ctx.close()
        except Exception:   # noqa: BLE001  閉じられていても構わない
            pass
    print("ログインできたか確認します…")
    print("ログインしました（.local/note_profile に保存）" if check() else
          "ログインを確認できませんでした。もう一度 --login を実行してください")


def check(headless: bool = True) -> bool:
    """ログインが生きているか（ログイン済みなら、ログイン画面を開いてもトップへ戻される）。"""
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        ctx = _context(p, headless)
        page = ctx.new_page()
        try:
            page.goto(LOGIN_URL, wait_until="domcontentloaded")
            page.wait_for_timeout(5000)
            return "/login" not in page.url
        finally:
            ctx.close()


# note のエディタ（ProseMirror）に HTML を「貼り付け」として渡す（見出し・太字・リンク・箇条書きが残る）
PASTE_JS = """(html) => {
  const el = document.querySelector('.ProseMirror[contenteditable="true"]');
  if (!el) return false;
  el.focus();
  const dt = new DataTransfer();
  dt.setData('text/html', html);
  dt.setData('text/plain', html.replace(/<[^>]+>/g, ''));
  el.dispatchEvent(new ClipboardEvent('paste', {clipboardData: dt, bubbles: true, cancelable: true}));
  return true;
}"""


def save_draft(title: str, body_html: str, *, headless: bool = False) -> str:
    """新しい記事にタイトルと本文を入れて「下書き保存」する。編集画面のURLを返す。公開はしない。"""
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        ctx = _context(p, headless)
        page = ctx.new_page()
        try:
            page.goto(NEW_URL, wait_until="domcontentloaded")
            page.wait_for_timeout(5000)
            if "/login" in page.url:
                raise NeedLogin("note のログインが切れています（このPCで `python src/note_publish.py --login`）")
            title_box = page.locator('textarea[placeholder*="タイトル"]').first
            title_box.wait_for(timeout=60000)
            title_box.fill(title)
            body = page.locator('.ProseMirror[contenteditable="true"]').first
            body.wait_for(timeout=30000)
            body.click()
            if not page.evaluate(PASTE_JS, body_html):
                raise RuntimeError("本文の入力欄が見つかりません（note の画面が変わった可能性）")
            page.wait_for_timeout(3000)
            if len(body.inner_text().strip()) < 50:
                raise RuntimeError("本文を貼り付けられませんでした（note の画面が変わった可能性）")
            # 押すのは「下書き保存」だけ（公開・投稿のボタンには触らない）
            save = page.get_by_role("button", name="下書き保存").first
            save.wait_for(timeout=30000)
            save.click()
            page.wait_for_timeout(5000)
            print("note に下書き保存しました:", page.url)
            return page.url
        except NeedLogin:
            raise
        except Exception as ex:
            raise RuntimeError(f"{ex}（画面: {_shot(page, 'error')}）") from ex
        finally:
            ctx.close()


def main(argv: list[str]) -> None:
    if "--login" in argv:
        login()
    elif "--check" in argv:
        print("ログイン中" if check() else "ログインが必要です（--login）")
    elif "--draft" in argv:
        doc = json.loads(Path(argv[argv.index("--draft") + 1]).read_text(encoding="utf-8"))
        save_draft(doc["title"], doc["body_html"], headless="--headless" in argv)
    else:
        print(__doc__)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
