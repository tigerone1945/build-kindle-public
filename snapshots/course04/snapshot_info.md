# Source Snapshot — Course 4

Tag: `course04-v1.0`

## Purpose

このTagは、本書で扱う問い合わせトリアージAIエージェントの **Course 4完成時点** を、読者向けに固定したSource Snapshotです。

本書のBUILD工程で扱う、SDDからSource Code・Testへ変換した完成状態を確認するときは、このTagを参照してください。

## Included Content

主な公開対象は次のとおりです。

```text
config/
data/
specs/triage-agent/
src/triage_agent/
tests/
.env.example
.python-version
pyproject.toml
uv.lock
README.md
```

- `specs/triage-agent/`：Requirements / Design / Tasks
- `src/triage_agent/`：実装ソース
- `tests/`：自動テスト
- `config/`：設定・マスタ
- `data/`：サンプル入力・評価データ
- `pyproject.toml` / `uv.lock`：Python環境と依存関係

## How to Use the Snapshot

現在の読者向け最新版は `main` です。

本書で扱った固定Snapshotを確認する場合は、リポジトリをcloneしたあとに次を実行します。

```bash
git fetch --tags
git switch --detach course04-v1.0
```

元の最新版へ戻る場合：

```bash
git switch main
```

## Notes

このPublicリポジトリは読者向けです。制作原稿、編集・レビュー記録、内部プロンプトなどの制作資料は含みません。

外部API、LLM、Pythonパッケージ等は時間とともに変更される可能性があります。本書の記述と照合するときは、`course04-v1.0` のSource Snapshotを基準にしてください。
