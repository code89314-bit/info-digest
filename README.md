# info-digest — 必要な情報だけをDiscordに届ける

Xを見なくても済むように、3つのテーマの一次情報を自動でDiscordに流します。
GitHub Actions（無料）で定期実行するので、PCやサーバーを動かしておく必要はありません。

| チャンネル | 情報源 | 頻度 |
|---|---|---|
| #地震 | 気象庁の地震・津波情報（P2P地震情報 API 経由） | 5分ごと |
| #医薬品供給 | 厚労省「医療用医薬品供給状況報告」の新着（New）品目 | 平日 10・13・16・19時台 |
| #ai-llm | ComfyUI・Ollamaのリリース、Hugging Faceの話題モデル、Zenn、HNなど → VRAM16GB以下のローカルAIに絞ってClaudeが選別・日本語要約 | 毎日 7:43・18:43 |

---

## セットアップ（PCで約15分）

### 1. Discordでチャンネルとウェブフックを作る

1. 自分のサーバーにテキストチャンネルを3つ作成（例：`地震`、`医薬品供給`、`ai-llm`）
2. 各チャンネルの ⚙️ 設定 → **連携サービス** → **ウェブフック** → **新しいウェブフック**
3. 「ウェブフックURLをコピー」して、どのチャンネルのURLか分かるようにメモ

> ウェブフックURLは「そのチャンネルに投稿できる鍵」です。他人に見せないでください。

### 2. GitHubにリポジトリを作る

1. GitHubで **New repository** → 名前は自由（例：`info-digest`）
   - **Public を推奨**：GitHub Actionsが無料・無制限になります（コードに秘密情報は含まれません）
   - Privateの場合、5分ごとの地震チェックで無料枠（月2,000分）を超えるため、`.github/workflows/earthquake.yml` の `*/5` を `*/30` などに変更してください
2. このフォルダの中身を全部アップロード（**Add file → Upload files** にドラッグ）
   - `.github` フォルダも必ず含めてください。Macで見えない場合は Finder で `⌘ + Shift + .` を押すと表示されます

### 3. 秘密情報（Secrets）を登録する

リポジトリの **Settings → Secrets and variables → Actions → New repository secret** で以下を登録：

| Name | 値 | 必須 |
|---|---|---|
| `DISCORD_WEBHOOK_EARTHQUAKE` | #地震 のウェブフックURL | ○ |
| `DISCORD_WEBHOOK_DRUG` | #医薬品供給 のウェブフックURL | ○ |
| `DISCORD_WEBHOOK_AI` | #ai-llm のウェブフックURL | ○ |
| `ANTHROPIC_API_KEY` | Claude APIキー（[console.anthropic.com](https://console.anthropic.com/)で発行） | 任意 |

`ANTHROPIC_API_KEY` が無い場合、AIチャンネルは「キーワードに一致した記事の見出し一覧」になります。
登録すると、Claudeが中身のある記事だけを選び、日本語の見出しと要約を付けます（従量課金・少額。コンソールで利用上限を設定しておくと安心です）。

### 4. 動作確認

1. **Actions** タブを開き、案内が出たら有効化
2. 左の一覧から各ワークフローを選び **Run workflow** で手動実行
3. Discordに投稿が来れば完了。以後は自動で動きます

初回の動き：
- **地震**：過去分を「既読」にするだけで投稿しません（次の地震から通知）
- **医薬品**：最新のExcelのNew品目をすぐ投稿します
- **AI**：直近36時間分の記事からダイジェストを作ります

---

## カスタマイズ（`config.yaml` を編集）

GitHub上で `config.yaml` を開き、鉛筆アイコンで直接編集できます。

- **地震の通知ライン**：`min_scale: 30`（震度3以上）→ `40` にすれば震度4以上だけ
- **医薬品ウォッチリスト**：気になる成分名を `watchlist:` に書くと ⭐ で別枠表示
  ```yaml
  watchlist:
    - アモキシシリン
    - カルボシステイン
  ```
- **AIの興味テーマ**：`interests:` に書いた分野をClaudeが優先して選びます
- **AIの情報源**：`sources:` にRSSのURLを追加・削除できます

実行時刻を変えたいときは `.github/workflows/*.yml` の `cron:` を編集します（**UTC表記**なので日本時間から9時間引いてください）。

---

## 知っておいてほしいこと

- **地震は「記録・確認用」です。** GitHub Actionsの定期実行は混雑時に数分〜十数分遅れることがあります。身を守るための速報は、スマホの緊急地震速報に任せてください。
- **Anthropicのフィード**は公式RSSが無いため、有志が作成しているものを使っています。止まった場合は `config.yaml` から削除してください。
- 厚労省ページの構成が変わると医薬品の取得が失敗することがあります。Actionsタブで赤い✗が続いたら確認してください。
- Publicリポジトリで60日間まったく更新がないとGitHubが定期実行を止めますが、このシステムは状態ファイルを自動で更新するので通常は止まりません。止まった場合はActionsタブから再有効化できます。

## ファイル構成

```
config.yaml                 設定
src/earthquake.py           地震・津波
src/drug_supply.py          医薬品供給
src/ai_digest.py            AI・LLMダイジェスト
src/common.py               Discord投稿などの共通処理
state/                      既読管理（自動更新）
.github/workflows/          定期実行の設定
tests/test_offline.py       ネット接続なしの動作テスト
```

## データ元

- 地震・津波：気象庁発表情報（[P2P地震情報](https://www.p2pquake.net/) JSON API v2 経由）
- 医薬品：[厚生労働省 医療用医薬品供給状況報告](https://www.mhlw.go.jp/stf/seisakunitsuite/bunya/kenkou_iryou/iryou/kouhatu-iyaku/04_00003.html)
- AI：各社公式ブログRSS、Hacker News（hnrss.org）、Hugging Face Daily Papers
