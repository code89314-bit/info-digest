# info-digest — Claude Code への引き継ぎ

このフォルダで Claude Code を開いたら、まずこのファイルを読む。詳しい経緯は `docs/HANDOFF.md`、使い方・復旧手順は `README.md`。

## 使う人
- 病院薬剤師（がん薬物療法・保険請求に関わる）。日本語で話す。エンジニアではないので、手順は具体的に、専門用語は短く言い換える。
- 「マージして」「どうぞ」などの短い返事で進めることが多い。

## 守ること（必ず）
- **このリポジトリは Public**。秘密情報（Webhook URL・APIキー・トークン・SearXNG の secret_key・Strata の API キー）をコード・コミット・PR に入れない。
  秘密は `.env`（Git に入らない）。Strata のキーは `%APPDATA%\Strata\launch-presets\*.json` と `%LOCALAPPDATA%\Strata\install\*.json` にある。
- Git に入れないもの（本人が別にバックアップ）: `.env`、`.local\`（ログ・状態・ガイドライン PDF・NotebookLM のノートID）、`%USERPROFILE%\.notebooklm\`（パスワード相当）。
  `.local\logs\` のログには Webhook URL が出ていることがある。外に出さない。
- 本人のメールアドレスをどこにも送らない。
- コミット・プッシュは頼まれたときだけ。流れは **ブランチ → PR → 「マージして」と言われたらマージ**。頼まれていないマージはしない。
- 元に戻しにくい操作・外に出る操作（タスク登録、削除、外部への送信）は先に確認する。`Register-ScheduledTask` はこちらで実行せず、コマンドを渡して本人に実行してもらう。
- `.ps1` は ASCII だけで書く（Windows PowerShell 5.1 が ANSI で読む）。
- `config.yaml` は本人も編集する。日本語のコメントを残し、新しい項目には説明のコメントを付ける。空のリストは `cfg.get("x") or []` で受ける。
- テストは `.venv\Scripts\python.exe tests\test_offline.py`（`$env:PYTHONIOENCODING='utf-8'`）。全部 ✅ を確かめてから PR。

## 構成（自宅 PC で動く。Windows 11、RTX 4070 Ti SUPER 16GB、RAM 32GB）
- **定時の配信**（Windows のタスク「info-digest …」）: AI ダイジェスト 5:30/5:35、学習 6:00、がん薬物療法ニュース 6:30、朝のニュース＋株式の欄（`src/market.py`）＋横浜線沿線の欄（`src/local_events.py`）6:50、note 下書き（水・土）など。
- **Discord Bot**（タスク「info-digest qa bot」、`scripts/run_qa_bot.ps1` のループで落ちても60秒で再起動）: `src/qa_bot.py`
  - ギモン深堀り（`src/qa_deep.py`）: 疑問 → NotebookLM で一次資料を調べて note の下書き。
  - しらべ（トーク、`src/strata_chat.py`、`style/personas/shirabe.yaml`）: 雑談は Qwen3.6。調べものは NotebookLM（下記）。
  - ルナ（専用チャンネル）: 会話・小説は Gemma、絵は ComfyUI。
  - Bot のコードを変えたら、`qa_bot.py` の python を止めれば60秒後に新しいコードで立ち上がる。
- **ローカル LLM**: Strata（`D:\Strata`、プリセット「Qwen36 32K」、Qwen3.6-35B-A3B UD-Q4_K_XL、:8080、Tailscale で外からも）。
  ルナのときだけ Gemma 4 26B heretic（llama.cpp :8090、`D:\Strata\scripts\luna-mode.ps1`）。同時には動かない（VRAM）。
  - 持参薬鑑別アプリ（GitHub Pages）が Strata を呼ぶので、Strata のモデル設定に `cors_origins` が要る（README の手順）。
  - RAM がほぼ満杯。長い処理をバックグラウンドで流すと、Claude Code がメモリ不足で止めることがある。止められたら勝手にやり直さず報告する。
- **SearXNG**（`D:\searxng`、:8888）: NotebookLM が使えないときのしらべの検索。

## モデルの役割（2026-10-30 に本人が決定）
- 定時のタスク・しらべの雑談 = Qwen3.6、ルナ = Gemma、**正確さが要る調べもの = NotebookLM**。
- 理由: 商品名→一般名の30問で、ローカルのモデルは薬の名前を取り違えた（Qwen3.6 正解8、Gemma 16、Flash-Next 5以下）。Flash-Next は削除済み。
  薬の名前をローカルのモデルの記憶に任せない（言い換えさせない・検索語から消させない）。

## しらべの専門ノート（NotebookLM）
- 設定 `style/nlm_notebooks.yaml`、処理 `src/nlm_notebooks.py`、ノートID `.local/nlm_notebooks.json`。
- 流れ: Strata が患者情報を確認 → 分野だけ選ぶ → 合うノートに聞く → 資料に答えが無ければ、その場のウェブ調査。答えは書き直さずに出す。
- ノート: 保険点数（いまの告示・通知・疑義解釈）＋保険点数の経緯（同時に聞いて並べる）、がん薬物療法（汎用）、がん支持療法、肺がん、大腸がん。
  - 本人の調べる順: 保険は ①最新の行政文書 ②疑義解釈 ③いつから・なぜ。がんは がん種ごとに 周術期／再発・切除不能 → 1〜3次のレジメン → 選ぶ条件とコンパニオン診断。
  - 肉腫・膵胆肝・泌尿器・婦人科は YAML に定義だけ（未作成）。優先は 肺・大腸 → その4つ。
- 資料の追加: 本人が PDF を `.local\guidelines\<キー>\` に置く → `.venv\Scripts\python.exe src\nlm_notebooks.py build <キー> --no-research`。
  大腸癌治療ガイドラインは書籍のみなので、PDF が来るまで大腸がんのノートは弱い。1冊の資料は50件まで。

## 残っている話（2026-10-30 時点）
- 大腸がんほかのガイドライン PDF（本人が「いつか足す」）。
- 持参薬鑑別アプリの OCR で、Qwen3.6 が薬の名前を作ってしまうことがある（アプリ側で照合する案を出した）。
- Strata のメモリ不足でプロンプトの読み込みが遅くなることがある（温めておく ping のタスク案、RAM 64GB 化の案）。
- Bot のタスクはログオン時だけ起動。止まったら `Start-ScheduledTask 'info-digest qa bot'`。
