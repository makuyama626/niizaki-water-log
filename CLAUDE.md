# プロジェクト: 新崎川 水位＋雨量ロガー

## 目的
神奈川県「雨量水位情報」から、新崎川の水位と周辺の雨量を10分間隔で自動取得し、
年間ログ（CSV）として残す。マクヤマベースの沢散歩（リバートレッキング）の
催行可否判断・安全管理に使う。

## 構成（このフォルダ）
- `niizaki_logger.py` … 取得・解析・CSV追記。テスト済み（`python niizaki_logger.py --test` で自己テスト）
- `.github/workflows/log.yml` … GitHub Actions。毎時自動実行し、更新があればCSVをコミット
- `data/` … 出力CSV。初期データ投入済み
  - `niizaki_waterlevel.csv`（水位）
  - `rainfall.csv`（雨量・観測所ごと）
  - `combined_10min.csv`（同時刻で水位と各点雨量を横並びにした比較表）
- `README.md` … 観測所・基準水位・出力仕様・差し替え方法など詳細

## 観測所
- 水位: 新崎橋（湯河原町中央／新崎川）。基準水位: 水防団待機0.80 / 氾濫注意1.15 / 避難判断1.20 / 氾濫危険1.65 m
- 雨量: 南郷山（幕山公園直上＝本命）、白銀山（源頭側・高所）、浅間山（幕山隣接・欠測のことあり）

## 残作業（デプロイ）＝今回お願いしたいゴール
1. GitHubに**公開（Public）**リポジトリを作成（データは公開情報。Publicが無料・止まりにくい）
2. このフォルダの全ファイルをpush（`.github/workflows/log.yml` を必ず含める）
3. Actionsの **Workflow permissions を「Read and write」** に設定
   （例: `gh api -X PUT repos/{owner}/{repo}/actions/permissions/workflow -f default_workflow_permissions=write`）
4. ワークフローを**手動で1回実行**し、`data/` のCSVが更新されることを確認

## 進め方の注意
- ユーザーはGitHub初心者。各ステップを**やさしく日本語で説明**しながら進める。
- 最初に **git と gh（GitHub CLI）の有無・ログイン状態を確認**し、未導入なら案内する。
- 取得元ページは直近数時間ぶんしか残らないため、1時間ごとの取得で取りこぼさない設計。
- 回答は日本語。
