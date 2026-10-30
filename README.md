# info-digest — 必要な情報だけを Discord に届ける

X やニュースアプリを見なくても済むように、必要な一次情報だけを自動で集め、Discord のチャンネルに届けます。
地震・医薬品は GitHub Actions（クラウド）で、それ以外は自宅PCのローカルLLM（Strata）で動きます。

このファイルは、**PCが壊れてもゼロから元に戻せる**ことを目標に書いています。
これまでの経緯や細かい設計判断は [docs/HANDOFF.md](docs/HANDOFF.md) にあります。

---

## 1. 何が届くか

| チャンネル | 中身 | 動く場所 | 時刻（JST） |
|---|---|---|---|
| #地震 | 震度3以上・津波予報（P2P地震情報 API） | GitHub Actions | 5分ごと |
| #医薬品供給 | 厚労省「医療用医薬品供給状況報告」の新着 | GitHub Actions | 平日 10・13・16・19時台 |
| #ai-llm | Zenn・Qiita の日本語記事（ComfyUI・ローカルLLM）を選別・要約 | 自宅PC | 毎日 5:30 |
| 海外AI | Reddit・HN・Hugging Face などを日本語で要約 | 自宅PC | 毎日 5:35 |
| 学習番組 | がん薬物療法の学習テーマを NotebookLM で調べ、要点＋podcast | 自宅PC | 毎日 6:00 |
| がん薬物療法ニュース | 毎日ためて、曜日テーマで要約＋podcast | 自宅PC | 毎日 6:30 |
| 朝のニュース | NHK・ITmedia から関心に合うものだけ（広告なしで読める要約）＋株式（S&P500・米国と日本の個別株の値動きと原因）＋横浜線沿線（町田〜橋本）の申込が要るイベント・イベント・新しいお店 | 自宅PC | 毎日 6:50 |
| note 下書き | 週刊がん薬物療法ニュース（土）・保険の話（水）を note に下書き保存 | 自宅PC | 土・水 7:00 |
| 疑問深堀り・トーク | Discord Bot（しらべ＝NotebookLM で調べもの、ルナ＝小説・画像） | 自宅PC（常駐） | いつでも |

## 2. 全体の構成

```
                ┌──────────── GitHub Actions（PCの電源と無関係）────────────┐
                │  earthquake.py / drug_supply.py → Discord Webhook          │
                └────────────────────────────────────────────────────────────┘

 自宅PC（Windows 11・RTX 4070 Ti SUPER 16GB・RAM 32GB）
 ┌──────────────────────────────────────────────────────────────────────────┐
 │ タスクスケジューラ ─→ scripts/run_ai_digest.ps1 ─→ src/*.py ─→ Discord     │
 │                                         │                                │
 │                                         ├→ Strata :8080（Qwen3.6-35B-A3B）  │ ← いつも動いているLLM
 │                                         ├→ NotebookLM（notebooklm-py）       │ ← 一次資料の読み込み・podcast
 │                                         └→ SearXNG :8888（ウェブ検索）       │
 │ Discord Bot（src/qa_bot.py）─→ Strata / llama.cpp :8090（ルナ用 Gemma 4）     │
 │ Tailscale（外から自宅のLLMを使う：持参薬鑑別アプリなど）                         │
 └──────────────────────────────────────────────────────────────────────────┘
```

GPU は1枚なので、使うものを切り替えます（`D:\Strata\scripts\`）。

| モード | 動くもの | 切り替え |
|---|---|---|
| LLM（ふだん） | Strata（Qwen3.6） | `llm-mode.ps1`。PC起動時は keepalive が自動で起動 |
| ルナ | llama.cpp（Gemma 4 26B heretic） | Bot がルナを選ぶと `luna-mode.ps1` を自動で実行 |
| ComfyUI | Docker の comfyui コンテナ | `comfy-mode.ps1` |

朝の定期実行は、ルナや ComfyUI に GPU を貸していても自動で Strata に戻してから動きます。

---

## 3. ゼロから再構築する

PCが壊れたときは、この順番で進めます。目安は半日（大きなファイルのダウンロード待ちを含む）。

### 3-0. 先にバックアップしておくもの（GitHub には入っていない）

Public リポジトリなので、秘密情報と個人のデータは GitHub に**入れていません**。次の2つを、USBメモリや Google ドライブなど別の場所に定期的にコピーしておいてください。

| 場所 | 中身 | 無いとどうなるか |
|---|---|---|
| `D:\info-digest\.env` | Webhook URL・Bot トークン・APIキー | 作り直せる（下の 3-6）が手間がかかる |
| `D:\info-digest\.local\` | 既読管理、ペルソナの記憶、小説、note のログイン、下書き、ログ | 既読が消える（3-10 で対処）。記憶・小説は戻らない |
| `%USERPROFILE%\.notebooklm\` | NotebookLM の長期ログイン（パスワード相当。取り扱い注意） | 3-7 でログインし直せる |

### 3-1. 必要なアカウント

- GitHub（このリポジトリ）／ Discord（自分のサーバー）／ Google（NotebookLM 用）／ note ／ Tailscale

### 3-2. ソフトを入れる

PowerShell で（winget は Windows 11 に最初から入っています）:

```powershell
winget install Git.Git Python.Python.3.14 Gyan.FFmpeg Tailscale.Tailscale
# ComfyUI を使うなら
winget install Docker.DockerDesktop
```

NotebookLM の操作ツール（notebooklm-py）は、ユーザー領域の Python に入れます:

```powershell
python -m pip install --user notebooklm-py
```

### 3-3. このリポジトリを置く

```powershell
cd D:\
git clone https://github.com/code89314-bit/info-digest.git
cd D:\info-digest
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt -r requirements-home.txt
.venv\Scripts\python -m playwright install chromium
```

### 3-4. Strata（ふだんのLLM）

1. [Strata](https://github.com/Niko1221/Strata) を入手して `D:\Strata` に置き、案内に従ってエンジンを入れる（`D:\Strata\terminal\current\bin\strata.cmd` が使える状態にする）
2. モデル Qwen3.6-35B-A3B（UD-Q4_K_XL、22.9GB）と画像用の部品をダウンロード:
   ```powershell
   $S = 'D:\Strata\terminal\current\bin\strata.cmd'
   & $S install -- --setup --family qwen36 --model UD-Q4_K_XL --vision yes --no-start --no-browser --yes
   ```
3. APIキーを決めて（例: `sk-strata-` に続けて英数字30文字ほど）、`.env` の `LOCAL_LLM_API_KEY` に書く（3-6）
4. プリセット「Qwen36 32K」を保存しながら起動（Tailscale 公開・APIキー必須・画像は CPU で読む）:
   ```powershell
   $env:LOCAL_LLM_API_KEY = '（3で決めたキー）'
   & $S launch --family qwen36 --model ud-q4_k_xl --context 32k --vision cpu --access tailscale --port 8080 `
       --api-key-env LOCAL_LLM_API_KEY --save-preset 'Qwen36 32K' --no-tui --yes
   ```
   **持参薬鑑別アプリ（ブラウザ）から使うなら必須**: モデルの設定ファイル
   `%LOCALAPPDATA%\Strata\install\strata-qwen36-ud-q4_k_xl.json` の最後に
   `"cors_origins": ["https://code89314-bit.github.io"]` を足して、Strata を起動し直す
   （`& $S launch --stop --preset 'Qwen36 32K'` → keepalive が1分以内に起動）。
   これが無いとブラウザが応答を捨て、アプリは「サーバにつながりません」になる（モデルを入れ替えるたびに必要。2026-10-30）
5. 切り替えスクリプトを置く:
   ```powershell
   New-Item -ItemType Directory -Force D:\Strata\scripts
   Copy-Item D:\info-digest\server\strata\*.ps1 D:\Strata\scripts\
   ```

> メモリ 32GB ではぎりぎりです（インストーラーが警告を出しますが動きます）。ブラウザを大量に開くと遅くなります。

### 3-5. ルナ用のモデル（llama.cpp ＋ Gemma 4）と SearXNG

**llama.cpp**: [llama.cpp のリリース](https://github.com/ggml-org/llama.cpp/releases)から Windows・CUDA 版を入手し、`D:\llama.cpp\bin\llama-server.exe` になるように置く。

**Gemma 4 26B heretic**: Hugging Face から `D:\models\gemma4-26b-heretic\` にダウンロード:
- `gemma-4-26B-A4B-it-uncensored-heretic.i1-IQ4_XS.gguf`（[mradermacher/gemma-4-26B-A4B-it-uncensored-heretic-i1-GGUF](https://huggingface.co/mradermacher/gemma-4-26B-A4B-it-uncensored-heretic-i1-GGUF)）
- `gemma-4-26B-A4B-it-uncensored-heretic.mmproj-f16.gguf`（[mradermacher/gemma-4-26B-A4B-it-uncensored-heretic-GGUF](https://huggingface.co/mradermacher/gemma-4-26B-A4B-it-uncensored-heretic-GGUF)）

**SearXNG**（しらべのウェブ検索。NotebookLM が使えないときの代わり。`127.0.0.1:8888`、外には公開しない）:

```powershell
git clone https://github.com/searxng/searxng.git D:\searxng
cd D:\searxng
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt
New-Item -ItemType Directory -Force D:\searxng\local
Copy-Item -Recurse D:\info-digest\server\searxng\* D:\searxng\local\
# settings.yml の secret_key: "CHANGE-ME" を、次の出力に書き換える
python -c "import secrets;print(secrets.token_hex(32))"
```

### 3-6. .env を作る

`.env.example` をコピーして `.env` にし、値を入れます。

- **Webhook**: Discord の各チャンネル → ⚙️ 編集 → 連携サービス → ウェブフック → 新しいウェブフック → URL をコピー
  （#ai-llm＝`DISCORD_WEBHOOK_AI`、海外AI＝`_AI_GLOBAL`、がん＝`_ONCO`、note 下書き＝`_NOTE`、朝のニュース＝`_NEWS`）
- **Bot**: [Discord Developer Portal](https://discord.com/developers/applications) → New Application → Bot → Reset Token でトークン。
  **Message Content Intent を ON**。OAuth2 → URL Generator（`bot`、送信・スレッド・添付の権限）で自分のサーバーに招待。
  疑問深堀り・しらべ用と、ルナ用（別アカウント）の2つを作る
- **チャンネルID**: Discord の設定 → 詳細設定 → 開発者モード ON → チャンネルを右クリック → 「チャンネルIDをコピー」
- `DISCORD_CHAT_WEBHOOK_URL`: トークチャンネルのウェブフック（ペルソナごとの名前・アイコンで話すため）

### 3-7. ログインが必要なもの

```powershell
# NotebookLM：長期のマスタートークンを作る（~/.notebooklm/ に保存。パスワード相当）
notebooklm login --master-token --account <Googleのメールアドレス>

# note：専用のブラウザが開くので、一度だけ手でログインする（.local\note_profile に保存）
D:\info-digest\.venv\Scripts\python D:\info-digest\src\note_publish.py --login
```

### 3-8. Tailscale

Tailscale にログインしたあと、外（スマホの持参薬鑑別アプリなど）から Strata を HTTPS で使えるようにします:

```powershell
tailscale serve --bg --https=8445 http://127.0.0.1:8080
```

### 3-9. 定期実行を登録する

12個のタスク（朝のダイジェスト・Bot・Strata の keepalive・SearXNG・モード切り替え）をまとめて登録します:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File D:\info-digest\scripts\register_tasks.ps1
```

登録後に一度サインアウト→サインインすると、常駐するもの（Strata・SearXNG・Bot）が起動します。

### 3-10. 既読の作り直しと動作確認

`.local\state` が無いと、最初の実行で過去の記事がまとめて投稿されます。先に「今ある記事を既読にするだけ」を実行しておきます:

```powershell
cd D:\info-digest
.venv\Scripts\python src\ai_digest.py ai_digest --seed
.venv\Scripts\python src\ai_digest.py ai_global --seed
.venv\Scripts\python src\ai_digest.py news --seed
```

動作確認:

```powershell
.venv\Scripts\python tests\test_offline.py                    # ネット無しのテスト（全部 ✅ ならOK）
$env:DRY_RUN = '1'; .venv\Scripts\python src\ai_digest.py news # Discord に投稿せず画面に表示
Start-ScheduledTask -TaskName 'info-digest news'              # 本番を1回（ログは .local\logs\）
```

### 3-11. GitHub Actions（地震・医薬品）

リポジトリの **Settings → Secrets and variables → Actions** に登録します（PCの再構築では何もしなくてよい。リポジトリを作り直すときだけ）:

| Name | 値 |
|---|---|
| `DISCORD_WEBHOOK_EARTHQUAKE` | #地震 のウェブフック |
| `DISCORD_WEBHOOK_DRUG` | #医薬品供給 のウェブフック |

---

## 4. ふだんの調整（`config.yaml`）

`config.yaml` に日本語の説明付きで全部の設定があります。よく触るもの:

- **朝のニュースの関心**: `news:` の `focus`（拾うもの）・`exclude`（要らないもの）・`interests`（優先）
- **要約の書き方**: 各区画の `summary_rule`、動向まとめの `overview.rules` / `overview.layout`
- **地震の通知ライン**: `earthquake.min_scale`（30＝震度3以上）
- **医薬品ウォッチリスト**: `drug_supply.watchlist` に成分名を書くと ⭐ で別枠
- **使うLLM**: `ai_digest.llm`（他の区画は `llm:` を書かなければこれを使う）

実行時刻は「タスクスケジューラ」（自宅PC）か `.github/workflows/*.yml`（Actions。UTC 表記なので9時間引く）で変えます。

## 5. 困ったとき

| 症状 | 見るところ・対処 |
|---|---|
| 朝のダイジェストが来ない | `.local\logs\<区画>-日付.log`。`llm server still down` なら Strata が止まっている |
| 持参薬アプリが「サーバにつながりません」 | Strata は動いているのにブラウザだけつながらない → モデル設定の `cors_origins`（3-4 の注記）。確認: `curl -X OPTIONS -H "Origin: https://code89314-bit.github.io" -H "Access-Control-Request-Method: POST" -D - https://<PC名>.<tailnet>.ts.net:8445/v1/chat/completions` に `Access-Control-Allow-Origin` があるか |
| Strata が止まっている | `strata.cmd launch --status --preset "Qwen36 32K"`。`D:\Strata\comfy-mode.flag` があると keepalive が止まったまま → `llm-mode.ps1` |
| しらべの検索ができない | 調べものは NotebookLM。分野が合えば専門ノート（`python src\nlm_notebooks.py list`、ノートのIDは `.local\nlm_notebooks.json`）、合わなければその場のウェブ調査（下の「NotebookLM が使えない」）。使えないときの代わりは SearXNG（`http://127.0.0.1:8888`）。タスク「SearXNG」を実行 |
| NotebookLM が使えない | `notebooklm auth refresh`。だめなら 3-7 のログインをやり直す |
| note の下書きが保存されない | Discord に「ログイン切れ」が来ていたら `note_publish.py --login` |
| 以前のモデル（Q2_0）に戻したい | docs/HANDOFF.md「Strata のモデルを Qwen3.6 に切り替え」の「戻すとき」 |

地震情報は記録・確認用です。Actions は混雑時に数分〜十数分遅れることがあるので、身を守るための速報はスマホの緊急地震速報に任せてください。

## 6. ファイル構成

```
config.yaml               全部の設定（日本語の説明付き。手で編集してよい）
.env.example              秘密情報の見本（本物の .env はコミットしない）
requirements*.txt         Python パッケージ（Actions 用 / 自宅PC 用）
src/
  common.py               設定・既読管理・Discord 投稿（分割・429 リトライ・DRY_RUN）
  earthquake.py           地震・津波                         ← Actions
  drug_supply.py          医薬品供給                         ← Actions
  ai_digest.py            RSS → LLM で選別・要約 → 投稿（ai_digest / ai_global / news / onco の共通処理）
  onco_news.py            がん薬物療法ニュース（毎日ためて曜日テーマで回す）
  learn.py                学習番組
  podcast_nlm.py          NotebookLM（資料の読み込み・要約・podcast）
  nlm_notebooks.py        しらべの専門ノート（保険点数・経緯・がん薬物療法・がん支持療法。設定は style/nlm_notebooks.yaml）
  note_weekly.py ほか     note の下書き（note_hoken.py・note_publish.py）
  qa_bot.py / qa_deep.py  Discord Bot（疑問深堀り）
  strata_chat.py          トーク（ペルソナ・検索・小説・記憶）
  gpu_mode.py             ルナ・ComfyUI と GPU を貸し借りする決まり
  comfy_client.py         ComfyUI で画像を作る
style/                    文体・ペルソナの設定（note の書き方、しらべ・ルナ）
scripts/
  run_ai_digest.ps1       定期実行の入口（LLM の起動待ち・GPU の取り戻し・ログ）
  run_qa_bot.ps1          Bot の常駐
  register_tasks.ps1      タスクスケジューラへの一括登録
  make_cover.py ほか      note のトップ画像・アイコン
server/
  strata/                 D:\Strata\scripts に置くもの（keepalive・モード切り替え）
  searxng/                D:\searxng\local に置くもの（secret_key は空にしてある）
state/                    Actions の既読管理（自動でコミットされる）
tests/test_offline.py     ネット無しのテスト（変更したら必ず通す）
docs/HANDOFF.md           経緯・設計判断・作業の記録
```

## 7. データ元

- 地震・津波：気象庁発表情報（[P2P地震情報](https://www.p2pquake.net/) JSON API v2 経由）
- 医薬品：[厚生労働省 医療用医薬品供給状況報告](https://www.mhlw.go.jp/stf/seisakunitsuite/bunya/kenkou_iryou/iryou/kouhatu-iyaku/04_00003.html)
- AI：Zenn・Qiita のタグ別フィード、Reddit・Hacker News・Hugging Face・GitHub リリース
- ニュース：NHK ニュースの RSS、ITmedia NEWS
- がん薬物療法：PMDA・厚労省・Minds・ESMO・NCI ほか（`config.yaml` の `onco.sources`）
