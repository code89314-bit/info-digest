---
name: series-script
description: がん薬物療法の「週の連載」（公開学習アーカイブ、全6回・各回約20分）の台本を、NotebookLM で一次資料を調べて Claude が書く。週のノートブックの準備（prep）、構成表、各回の台本（話者3人（メイン・サブ・聞き役）・読みがな付き）、note 用の記事、事実チェック表を作る。「連載の台本」「週の連載」「series-script」「第n回の台本」と言われたら使う。
---

# 週の連載の台本づくり

設計の全体は `docs/SERIES_PLAN.md`。題材のリストは `config.yaml` の `series.weeks`。

## なぜ Claude が台本を書くのか
NotebookLM の音声は、専門用語の読み間違いや、話者の入れ替わりがあり、長く残すアーカイブには品質が足りない。
長い台本も NotebookLM では組み立てにくい。そこで **NotebookLM は「資料を調べて出典つきで答える係」だけ**にし、
台本は Claude が書く。音声は、この台本を読み上げソフトで読ませる（読み上げソフトは別に決める）。

## 守ること（必ず）
- **事実は NotebookLM の答え（出典つき）からだけ取る**。薬の名前・一般名・承認の年月・試験の名前・数値・推奨の強さは、
  NotebookLM の答えに書かれたとおりに使う。Claude の記憶で補わない。確かめられないことは台本に入れない。
- ローカルのモデル（Qwen3.6 など）は使わない（薬の名前を取り違える。CLAUDE.md）。
- 別々の試験の数値を並べて優劣を言わない（試験間比較）。並べるのは適応・用法・副作用の傾向・試験のデザインまで。
- 物語を面白くするための作り話（逸話・人物の発言・場面の描写）を入れない。
- 個別の患者への判断はしない。ガイドラインの図表は写さない（要約と出典の記載にする）。
- 秘密（Webhook・APIキー・メールアドレス）を出力ファイルに書かない。出力は `.local\series\`（Git に入らない）。
- 公開はしない。台本と記事を作るところまで（公開は1週遅れで本人が確かめてから）。

## 使い方
- `/series-script prep` … 次の週の準備: ノートブックを作って資料を入れ、構成表（plan.md）を書く。
- `/series-script ep <n>` … 第n回の台本・記事・事実チェック表を書く（prep が済んでいること）。
- `/series-script all` … prep がまだなら prep をしてから、第1〜6回を順に書く。
- 週を指定するとき: `/series-script prep EGFR阻害薬`（`series.weeks` の title の一部）。指定が無ければ、
  `.local\series\state.json` でまだ終わっていない、`series.weeks` のいちばん上の週。

## 準備（毎回最初に）
PowerShell で:
```powershell
$env:Path += ";$env:APPDATA\Python\Python310\Scripts"
notebooklm auth refresh        # Cookie を取り直す（失敗したら本人に `notebooklm login` を頼む）
notebooklm list --json         # 認証の確認
```
- 専門ノートのID: `.local\nlm_notebooks.json`（キー: lung, colorectal, onco, onco_support, hoken, hoken_history）。
- 週の状態: `.local\series\state.json`（無ければ作る）:
  `{"weeks": {"<週ID>": {"title": ..., "type": ..., "notebook_id": ..., "done": [1,2], "dir": ...}}}`
  週ID は `YYYYMMDD-<slugの英数字>`（準備した日付）。出力フォルダは `.local\series\<週ID>\`。
- NotebookLM への質問は必ずノートブックを明示する: `notebooklm ask "<質問>" --notebook <ID> --json`。
  `--json` の `references`（`cited_text` と `source_id`）を保存する。資料の名前は `notebooklm source list --notebook <ID> --json`。
- 質問はまとめてファイルに書き（1問ずつ `---` の行で区切る）、`scripts/nlm_ask.py` で順に聞いて JSON に保存する:
  `.venv\Scripts\python.exe -I .claude\skills\series-script\scripts\nlm_ask.py <ノートブックID> <質問.txt> <出力.json>`
  （途中で止まっても、出力にある質問は飛ばして続きから聞く）。答えは長いので、読むときは JSON から answer だけ取り出す。
- **バックアップ**: prep・各回が終わるたびに、週のフォルダと state.json を `.local\series\_backup\<週ID>-<日時>.zip` に固める
  （`.local` は Git に入らないため）。スキル自体を直したときは、本人に断ってブランチにコミット・プッシュする。

## prep（週の準備）
1. `config.yaml` の `series` を読み、週（title・type・notebook・drugs・scope）を決める。type は
   `structure`（構造系）/ `class`（クラス追跡）/ `drug`（ドキュメンタリー）。流れは `reference/arcs.md`。
2. ノートブックを作る: `notebooklm create "連載：<title>" --json`。
3. 資料を入れる（type ごと。`reference/arcs.md` の「資料」）:
   - ウェブ調査（deep を1回）: `notebooklm source add-research "<調査の指示>" --mode deep --import-all --notebook <ID>`
     （30分ほどかかることがある。タイムアウトしたら `notebooklm research wait -n <ID> --import-all`）。
     調査の指示には、題材の名前を config のまま入れ、日本の一次資料（PMDA の添付文書・インタビューフォーム・
     審査報告書・適正使用ガイド・RMP、学会の診療ガイドライン、根拠の試験の論文）を優先と書く。
   - `drugs` があれば、薬ごとに fast の調査を足す（「<薬> 審査報告書 インタビューフォーム 適正使用ガイド PMDA」）。
   - 本人が置いた PDF（`.local\series\pdf\<slug>\` または `.local\guidelines\<notebook>\`）があれば `source add` で入れる。
   - deep の調査は「this write could not be confirmed（IMPORT_RESEARCH のタイムアウト）」と出ても、実際には入っていることが多い
     （2026-11-20 のパイロット版では24件入っていた）。やり直す前に必ず `source list` で確かめる（やり直すと重複する）。
4. 資料を確かめる: `source list --json` で題名を見て、題材と関係ない資料（別の薬・別のがん種・個人ブログ・広告・患者向けの一般記事・
   出版社の販売ページ）は `source delete <ID> -n <ノートブックID> -y` で消す。deep の調査が作る**まとめのレポート**
   （調査の指示と同じような題の資料。AI が書いた要約で一次資料ではない）も消す。残りが 10件未満なら、調査を足すか、本人に知らせて止める。1冊の資料は50件まで。
5. **構成表** `plan.md` を書く（形は `reference/arcs.md` の最後）。各回の範囲・聞くこと・冒頭の振り返りの問い・次回への橋渡し。
   構成表を作るための質問（全体像・年表・主な試験・ガイドラインの版）も NotebookLM に聞き、答えは `research\plan.json` に保存。
   `structure` で `notebook` があれば、その専門ノートにも聞く（ガイドラインの PDF が入っている）。保険の話は `hoken` に聞く。
   **割れた意見の宝庫**: ガイドラインの解説にある「投票の内訳」（例: 強く推奨52%／弱く推奨48%）と、前の版から変わった点。
   構成表を作るときに必ず聞き、しらべの見せ場として各回に割り当てる。
6. `state.json` に週を記録する。本人への報告: 資料の数と主な資料、構成表の要約、気になった点（資料が薄い回など）。

## ep <n>（第n回）
1. `plan.md` の第n回の「聞くこと」を、NotebookLM（週のノートブック。必要なら専門ノート・hoken）に1問ずつ聞く。
   答えと references を `research\ep<n>.json` に保存する。答えが「資料では確認できない」なら、その論点は台本から外す。
   サブ（別の視点・割れた意見）の材料として、回ごとに次も聞く:
   「この回の範囲で、見解が分かれている点・ガイドライン間（日本と海外など）で違う点・推奨が弱い/エビデンスが乏しいとされる点は？
   それぞれ資料のどこに書かれているか」。資料に無ければ、サブは同意や問い返しで緩急をつける。
2. **事実カード** `ep<n>-facts.md` を作る: 台本で使う事実を1行ずつ、出典（資料の題名）と NotebookLM の答えの該当部分つきで。
   薬の名前（一般名と商品名）・承認の年月・試験の名前・数値・推奨の強さは、必ずここを通す。
3. **台本** `ep<n>-script.md` を書く。先に `reference/persona.md`（話者とリスナーのレベル）を読む。
   形・掛け合い・読みがなは `reference/script-format.md`。約6,000字（約20分）。
   リスナーは基礎の固い薬剤師。腎機能の計算式や相互作用の基本などの説明は挟まない。
4. **記事** `ep<n>-article.md`（note 用、2,500〜3,000字）: 台本と同じ事実だけで、小見出し・箇条書き・最後に出典の一覧。
   読みがなの記号は入れない（記事は普通の表記）。
5. **事実チェック** `ep<n>-check.md`: 台本と記事にある 薬の名前・数値・年月・試験の名前・推奨 を全部拾い、
   1つずつ事実カードにあるか確かめる。無いものは直すか消す。最後に「カードに無い事実: 0件」になるまで繰り返す。
   読みがなを付けた語は `style/series_yomi.yaml` に足す（まだ無いものだけ。読みは資料ではなく一般的な読み。迷う語は「要確認」と書く）。
6. `state.json` の done に n を足す。本人への報告: 台本の字数（≒分数）、使った資料の数、外した論点、要確認の読み。

## 第6回（まとめ）
第1〜5回の事実カードと台本を読み、年表・選び方のフロー（または比較表）・各回のポイント・
「何月何日時点の最新の動向」（NotebookLM に新しい順で聞く）・目次（各回の題）をまとめる。新しい事実を足すときは事実カードを通す。

## うまくいかないとき
- 認証エラー: `notebooklm auth refresh` → だめなら本人に `notebooklm login --master-token --account <本人のアカウント>` を頼む（メールアドレスを書かない）。
- 「No result found for RPC ID」・混んでいる: 5〜10分おいて1回だけやり直す。だめなら報告して止める。
- 音声の生成（`generate audio`）はこのスキルでは使わない（音声の枠を使わない）。
