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
    python src/note_publish.py --draft <同上> --finish <編集画面のURL>    保存済みの下書きに目次・音声だけ入れる
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


TOC_MARK = "ここに note の目次を挿入"
AUDIO_MARK = "ここに音声ファイル"


def _empty_line_at(page, mark: str, after_heading: str | None = None) -> bool:
    """mark を含む段落の文字を消して空行にし、カーソルをそこに置く（「＋」メニューは空行にだけ出る）。

    mark が無ければ、after_heading を含む大見出しの後ろの空行を使う。
    """
    para = page.locator(".ProseMirror p", has_text=mark).first
    if para.count() == 0 and after_heading:
        para = page.locator(f"xpath=//div[contains(@class,'ProseMirror')]/h2[contains(., '{after_heading}')]"
                            "/following-sibling::p[normalize-space(.)=''][1]").first
        if para.count() == 0:
            return False
        para.scroll_into_view_if_needed()
        para.click()
        page.wait_for_timeout(800)
        return True
    if para.count() == 0:
        return False
    para.scroll_into_view_if_needed()
    para.click()
    page.keyboard.press("End")
    page.keyboard.press("Shift+Home")
    page.keyboard.press("Delete")
    page.wait_for_timeout(800)
    return True


def _open_menu_item(page, label: str) -> None:
    """空行の「＋」（メニューを開く）から項目を選ぶ。サイドバーの同名ボタンと区別するため、メニューの項目の class で探す。"""
    page.get_by_role("button", name="メニューを開く").first.click()
    page.wait_for_timeout(800)
    cls = page.get_by_text("有料エリア指定", exact=True).last.get_attribute("class")
    page.locator(f'div[class="{cls}"]', has_text=label).filter(has_text=label).last.click()
    page.wait_for_timeout(1500)


EMPTY_QUOTES = """() => Array.from(document.querySelectorAll('.ProseMirror > figure'))
  .filter(f => f.querySelector('blockquote') && !f.innerText.trim()).length"""


def _remove_empty_quotes(page) -> int:
    """貼り付けで残った空の引用ブロックを消す（中の空行で Backspace を1回。2回押すと前の段落の文字が消える）。"""
    n = 0
    for _ in range(5):
        before = page.evaluate(EMPTY_QUOTES)
        if not before:
            break
        page.locator(".ProseMirror > figure:not(:has(img)) blockquote p").first.click()
        page.wait_for_timeout(500)
        page.keyboard.press("Backspace")
        page.wait_for_timeout(500)
        if page.evaluate(EMPTY_QUOTES) >= before:
            raise RuntimeError("空の引用ブロックが消えませんでした")
        n += 1
    return n


def finish(page, audio: dict | None) -> list[str]:
    """本文を入れたあとの仕上げ: 空の引用を消す・目次を入れる・音声を埋め込む。できなかったことを返す（下書きは残す）。"""
    todo = []
    try:
        _remove_empty_quotes(page)
    except Exception as ex:   # noqa: BLE001
        todo.append(f"空の引用ブロックを消せませんでした（{str(ex)[:80]}）")
    try:
        if _empty_line_at(page, TOC_MARK):
            _open_menu_item(page, "目次")
    except Exception as ex:   # noqa: BLE001
        todo.append(f"目次を入れられませんでした（{str(ex)[:80]}）")
    if audio and audio.get("path"):
        try:
            if not _empty_line_at(page, AUDIO_MARK, audio.get("heading") or "音声版"):
                raise RuntimeError("音声の位置メモが見つかりません")
            _open_menu_item(page, "音声")
            page.locator('input[type=file][accept*="audio"]').last.set_input_files(audio["path"])
            dialog = page.get_by_text("音声の投稿", exact=True)
            dialog.wait_for(timeout=120000)
            if audio.get("cover"):
                page.locator('input[type=file][accept*="image"]').last.set_input_files(audio["cover"])
                page.wait_for_timeout(3000)
            page.locator("label", has_text="タイトル").locator("xpath=following::input[1]").fill(audio.get("title", ""))
            page.locator("label", has_text="作者").locator("xpath=following::input[1]").fill(audio.get("author", ""))
            page.get_by_role("button", name="保存", exact=True).click()
            dialog.wait_for(state="detached", timeout=600000)   # アップロードが終わるとダイアログが閉じる
            page.wait_for_timeout(3000)
        except Exception as ex:   # noqa: BLE001
            todo.append(f"音声を埋め込めませんでした（手で貼ってください: {audio['path']}）（{str(ex)[:80]}）")
    return todo


def save_draft(title: str, body_html: str, *, audio: dict | None = None, headless: bool = False,
               edit_url: str | None = None) -> tuple[str, list[str]]:
    """新しい記事にタイトルと本文を入れ、仕上げ（目次・音声）をして「下書き保存」する。公開はしない。

    edit_url を渡すと、その下書きを開いて仕上げだけ行う。戻り値は（編集画面のURL, できなかったこと）。
    """
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        ctx = _context(p, headless)
        page = ctx.new_page()
        try:
            page.goto(edit_url or NEW_URL, wait_until="domcontentloaded")
            page.wait_for_timeout(8000)
            if "/login" in page.url:
                raise NeedLogin("note のログインが切れています（このPCで `python src/note_publish.py --login`）")
            body = page.locator('.ProseMirror[contenteditable="true"]').first
            body.wait_for(timeout=60000)
            if not edit_url:
                title_box = page.locator('textarea[placeholder*="タイトル"]').first
                title_box.wait_for(timeout=60000)
                title_box.fill(title)
                body.click()
                if not page.evaluate(PASTE_JS, body_html):
                    raise RuntimeError("本文の入力欄が見つかりません（note の画面が変わった可能性）")
                page.wait_for_timeout(3000)
                if len(body.inner_text().strip()) < 50:
                    raise RuntimeError("本文を貼り付けられませんでした（note の画面が変わった可能性）")
            todo = finish(page, audio)
            # 押すのは「下書き保存」だけ（公開・投稿のボタンには触らない）
            save = page.get_by_role("button", name="下書き保存").first
            save.wait_for(timeout=30000)
            save.click()
            page.wait_for_timeout(5000)
            _shot(page, "saved")
            print("note に下書き保存しました:", page.url, *(f"\n  未完了: {t}" for t in todo))
            return page.url, todo
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
        edit = argv[argv.index("--finish") + 1] if "--finish" in argv else None
        save_draft(doc["title"], doc["body_html"], audio=doc.get("audio"), headless="--headless" in argv,
                   edit_url=edit)
    else:
        print(__doc__)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
