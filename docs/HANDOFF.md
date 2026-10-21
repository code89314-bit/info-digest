# 引き継ぎメモ：info-digest をローカルLLMサーバーで動かす

このファイルは、ローカルLLMサーバー上で作業する Claude Code 向けの引き継ぎ資料です。
これまでの経緯・現在の構成・次にやってほしい作業をまとめています。
リポジトリ: https://github.com/code89314-bit/info-digest （Public）

---

## 1. 背景（ユーザーの目的）

- Xを見ると、インプ稼ぎの定型投稿ばかりで時間を取られる。
- 本当に欲しい情報は次の3つだけ。それをDiscordの専用チャンネルで受け取りたい。
  1. 地震情報
  2. 医薬品の供給情報
  3. AI関連。ただし「VRAM 16GB以下で動く ComfyUI・ローカルLLM」に絞り、**日本語の記事だけ**
- X APIは従量課金（投稿1件の読み取りにつき$0.005）なので、一次情報源（気象庁・厚労省・Zenn/Qiita）から直接取得する方針にした。
- AI記事の選別・要約に使うLLMを、Claude API から**ユーザーが自前で立てたローカルLLMサーバー**に切り替えたい ← **今回の作業**

## 2. 現在の構成（稼働中）

GitHub Actions（無料・Public リポジトリ）で定期実行し、Discord Webhook に投稿している。
既読管理は `state/*.json` に保存し、Actions 自身がコミットしている（`state: xxx` というコミットはこれ）。

| ワークフロー | スクリプト | 情報源 | 実行タイミング（JST） | 状態 |
|---|---|---|---|---|
| 地震・津波情報 | `src/earthquake.py` | P2P地震情報 API v2 `https://api.p2pquake.net/v2/history?codes=551&codes=552` | 5分ごと | 稼働中 |
| 医薬品供給情報 | `src/drug_supply.py` | 厚労省「医療用医薬品供給状況報告」ページの最新Excel（`今回掲載時の更新有無`= New の行） | 平日 10:17 / 13:17 / 16:17 / 19:17 | 稼働中 |
| AI・LLMダイジェスト | `src/ai_digest.py` | Zenn・Qiita のタグ別RSS（11本、`config.yaml` 参照） | 毎日 7:43 / 18:43 | 稼働中 |

```
config.yaml                 全設定（ユーザーが直接編集する前提。日本語コメントあり）
src/common.py               設定・state の読み書き、Discord投稿（2000字・10embed制限の分割、429リトライ、DRY_RUN=1で標準出力のみ）
src/earthquake.py           震度3以上と津波予報を投稿。初回実行は既読化のみ
src/drug_supply.py          Excelのヘッダー行を自動検出。供給停止/限定出荷/通常出荷に分類。watchlist一致を⭐で別枠表示
src/ai_digest.py            RSS取得 → LLMで選別・日本語要約 → タグ別embedで投稿
tests/test_offline.py       ネットワークなしのテスト（`python tests/test_offline.py`）。変更したら必ず通すこと
.github/workflows/*.yml     定期実行の定義（cron はUTC表記）
```

GitHub Secrets（登録済みのもの）:
`DISCORD_WEBHOOK_EARTHQUAKE`, `DISCORD_WEBHOOK_DRUG`, `DISCORD_WEBHOOK_AI`。
`ANTHROPIC_API_KEY` は任意で、未登録の可能性が高い。未登録だと `fallback_keywords` によるキーワード一致になる。

### ai_digest.py の現在のLLM呼び出し

- `select_with_claude(items, cfg)`: Anthropic Messages API（`https://api.anthropic.com/v1/messages`）を `requests` で直接呼んでいる。SDKは使っていない。
- プロンプトは `PROMPT` 定数。`config.yaml` の `focus` / `exclude` / `interests` / `tags` / `max_items` を埋め込む。
- LLMには次の形式の **JSONだけ** を出力させ、正規表現 `\{.*\}` で抜き出して `json.loads` している。
  `{"items":[{"i":候補番号,"title_ja":"...","summary_ja":"...","tag":"..."}]}`
- 失敗時（キー無し・HTTPエラー・JSON解釈失敗）は `None` を返し、`select_by_keywords()` にフォールバックする。
- 候補は最大 `max_candidates`（既定120）件。1件あたり「タイトル＋本文冒頭300字」をプロンプトに入れる。

## 3. 今回やってほしい作業

### 3-1. LLM呼び出しをプロバイダー切り替え式にする

`config.yaml` の `ai_digest` に、次のような設定を追加する。

```yaml
  llm:
    provider: openai_compat        # openai_compat | anthropic | none
    base_url: http://localhost:11434/v1   # Ollama の例。LM Studio は :1234/v1、llama.cpp server は :8080/v1
    model: qwen3:14b               # サーバーで実際に動いているモデル名に合わせる
    api_key_env: LOCAL_LLM_API_KEY # 不要なら空でよい
    timeout: 600
    temperature: 0.2
```

- `openai_compat` は `POST {base_url}/chat/completions` を呼ぶ。Ollama / LM Studio / llama.cpp / vLLM で共通に使える。
- 既存の `anthropic` 経路は残す（ユーザーが品質比較のために戻せるように）。
- 失敗したらキーワード一致に落ちる、という現在の挙動は維持する。

### 3-2. ローカルLLM向けの堅牢化（16GB VRAMを想定）

- **JSON出力の安定化**: サーバーが対応していれば `response_format: {"type":"json_object"}` を付ける（Ollama は `format: "json"` でも可）。抜き出しに失敗したら1回だけ再試行する。
- **思考タグの除去**: Qwen系などが出力する `<think>...</think>` を、JSONを抜き出す前に削除する。
- **文脈長対策**: 候補が多いとVRAMを圧迫する。`batch_size`（例: 25件）ごとに選別し、最後に重複除去と件数上限をかける2段階方式にする。1件あたりの本文は200字程度に絞ってよい。
- 候補番号 `i` が範囲外・非整数のときは捨てる（既存コードは対応済み）。

### 3-3. 実行場所を分ける（重要）

GitHub Actions のランナーはクラウド上にあるため、自宅のLLMサーバーには届かない。
LLMサーバーを外部公開するのはセキュリティ上すすめない（ユーザーにも同じ説明をしている）。

- **地震・医薬品**: 今までどおり GitHub Actions（PCの電源状態に関係なく動く必要があるため）
- **AIダイジェスト**: LLMサーバー上で定期実行する（Linux なら cron または systemd timer、Windows ならタスクスケジューラ）
  - `.github/workflows/ai_digest.yml` の `schedule:` は削除するかコメントアウトする（二重投稿防止）。`workflow_dispatch` は残してよい。
  - Discord Webhook URL はサーバー側の `.env` などに置き、**リポジトリにコミットしない**（Public リポジトリのため）。`.gitignore` に `.env` を追加すること。
  - 既読管理 `state/ai_digest.json` の扱い: サーバー上のローカルファイルとして持つ方式を推奨（git push は不要になる）。その場合 `state/ai_digest.json` も `.gitignore` 対象にするか、サーバー専用の state ディレクトリを環境変数で指定できるようにする。
  - 実行時刻にサーバーが落ちていた場合に備え、`lookback_hours`（現在36）と既読管理で取りこぼしがないことを確認する。

### 3-4. 動作確認

1. `python tests/test_offline.py` が通ること（`openai_compat` 経路のモックテストを追加する）
2. `DRY_RUN=1 python src/ai_digest.py` で、実際のRSSとローカルLLMを使い、Discordに投稿せず出力を確認する
3. 問題なければ本番実行して、#ai-llm チャンネルに届くことをユーザーに確認してもらう

### 3-5. 実施結果（2026-10-21、LLMサーバー上の Claude Code）

- LLMサーバー = このリポジトリを置いたWindows PC（`D:\info-digest`）。LLM は同じPCの **Strata**（Qwen3.8-Flash-Next Q2_0、`http://127.0.0.1:8080/v1`、APIキー必須）。
- `ai_digest.llm`（`config.yaml`）で `openai_compat` / `anthropic` / `none` を切り替え。`extra_body` はサーバー独自の項目をそのまま渡す（Strata は `reasoning_budget_tokens`）。
- バッチ選別（既定25件）→ 重複除去 → `score` 順に `max_items` 件。`<think>` 除去・JSON解釈失敗時1回再試行・`response_format` 非対応なら外して再送。
- 秘密情報は `.env`（`common.load_dotenv()` が読む）。既読管理は `INFO_DIGEST_STATE_DIR=D:\info-digest\.local\state`（`state/ai_digest.json` の内容で初期化済み）。`.env` `.venv/` `.local/` は `.gitignore` 済み。
- 定期実行: タスクスケジューラ「info-digest AI」が毎日 7:43 / 18:43 に `scripts/run_ai_digest.ps1` を実行（PC停止中に過ぎた分は起動後に実行）。LLMサーバーが応答しなければ最大180分待ち（ComfyUI使用中など）、それでもだめならキーワード一致で投稿。ログは `.local/logs/`。
- `.github/workflows/ai_digest.yml` の `schedule:` はコメントアウト（`workflow_dispatch` は残した）。
- 追加: **海外の話題** `ai_global`（`config.yaml` の別区画）。Reddit（r/LocalLLaMA・r/comfyui・r/StableDiffusion の日間上位）・Hacker News・HFトレンド・ComfyUI/Ollama のリリースを、LLMが日本語で要約して**別チャンネル**（`DISCORD_WEBHOOK_AI_GLOBAL`）に投稿。`python src/ai_digest.py ai_global` で実行、既読管理は `state/ai_global.json`、`llm:` を省略すると `ai_digest` の設定を使う。タスク「info-digest AI global」が毎日 8:13 / 19:13。候補110件で約10分。#ai-llm の「日本語記事のみ」の方針はそのまま。

- 追加: **がん薬物療法** `onco`（`config.yaml` の別区画、`DISCORD_WEBHOOK_ONCO`）。国内（PMDA 承認審査・安全対策、厚労省 報道発表、Minds）＋海外（ESMO・NCI・OncLive・Targeted Oncology・CancerNetwork。FDA公式はこのPCから403/404のため報道で代替）。
  - 投稿の先頭に LLM が書く「📋 今日の動向まとめ」（`overview`、がん種別）。冒頭に注意書き（`notice`）。
  - `llm.dedupe: true` で、出典違いの同じ出来事を最後にLLMで1件にまとめる（全区画共通）。
  - **podcast**（`src/podcast_nlm.py`）: NotebookLM（`E:\onco_tekisei` と同じ notebooklm-py・認証・1日上限台帳 `audio_usage.json`）で音声概要を作り、ffmpeg で10MB以下に圧縮して Discord に添付。失敗時は理由を1行投稿。
  - タスク「info-digest onco」毎日 7:50（NotebookLM の認証延命 7:30 の後）。
  - 既知の限界: Strata の Q2_0 量子化モデルは、要約に中国語の字体や英語が混ざることがある。

## 4. 将来の拡張案（ユーザーと相談済み・未着手）

### 4-1. 英語の話題記事の「音声ダイジェスト」（NotebookLM風）

- ユーザーの発案は「英語のホットな記事をNotebookLMに読ませて、要約と音声を作り、Discordに流す」。
- 個人向けNotebookLMには公式APIが無い。Enterprise向けAPIはプレビュー段階で、音声のダウンロードもAPIでは不可。そのため**自前で同等のものを作る**方針を提案済み。
  1. 英語圏の話題記事を収集（Hacker News の高得点記事 `https://hnrss.org/newest?q=...&points=100`、Hugging Face のトレンドモデル `https://huggingface.co/api/models?sort=trendingScore` など。`fetch_hf_models()` は実装済み）
  2. ローカルLLMで記事ごとに日本語要約 → 2人の掛け合い台本を生成（16GBなら2段階方式にする）
  3. VOICEVOX（同じサーバーで動かせる。HTTP API `:50021`）で音声化し、mp3にまとめる
  4. Discord Webhook にファイルとして添付（multipart。Webhookの添付上限に注意。5分程度の音声なら128kbps以下で収まる）
- 頻度は1日1本、注目3〜5本で5分程度が目安。
- ※ 現在の #ai-llm は「日本語記事のみ」の方針なので、英語記事はこの音声機能でだけ扱う想定。

### 4-2. 特定のXアカウント（@jun1228904）の投稿を追う

- ユーザーが有用だと感じているアカウント。
- 案: X API の pay-per-use でユーザータイムラインを `since_id` 付きで取得する。1日10〜30投稿なら月$1.5〜4.5程度。開発者登録とクレジット購入が必要で、**ユーザーの判断待ち**。
- 取得した投稿は ai_digest と同じ方法でLLMに選別させる（RT・定型文・宣伝を除外）。

## 5. 守ってほしいこと

- `config.yaml` はユーザーが手で編集する。日本語コメントと構造を保ち、設定項目を追加するときは説明コメントを付ける。
- 設定のリストが空（YAMLで `None` になる）でも落ちないこと。過去に `watchlist:` が空で落ちたため、`cfg.get("x") or []` の形にしてある。
- 秘密情報（Webhook URL・APIキー）をコードやコミットに含めない。
- ユーザーは非エンジニア寄り。説明は日本語で、手順は具体的に書くこと。
