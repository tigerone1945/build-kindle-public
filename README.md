# build-kindle-public

Kindle技術書 **BUILD編** の読者向けコンパニオンリポジトリです。

本書で扱う「完成したSDDを、Source Code・Test・Traceabilityへ変換するBUILD工程」を確認・実行するためのソースを公開しています。

## このリポジトリについて

このPublicリポジトリは、制作に使用したPrivateリポジトリから、読者に必要なファイルだけを抽出したものです。

- 制作原稿・レビュー記録・編集資料は含みません
- 秘密情報を含む `.env` は含みません
- 環境変数の例は `.env.example` を使用してください

## 主な構成

```text
.
├── config/
├── data/eval/
├── snapshots/course04/
├── specs/triage-agent/
├── src/triage_agent/
├── tests/
├── .env.example
├── .python-version
├── pyproject.toml
└── uv.lock
```

## SDD

`specs/triage-agent/` に、BUILDの入力となる次の仕様を収録しています。

- `requirements.md`
- `design.md`
- `tasks.md`

## Source Code

実装本体は `src/triage_agent/` にあります。

問い合わせ分類、Guardrail、決定論的ルール、Routing、Human Review、Persistence、Logging、Evaluationなど、本書で扱うCourse 4完成時点の実装を確認できます。

## Test

テストは `tests/` にあります。

通常のテストを実行する場合：

```bash
uv sync
uv run pytest
```

`tests/test_live_llm.py` は実LLM/APIを利用するテストです。実行する場合は `.env.example` を参考に必要な環境変数を設定してください。APIキーをGitへcommitしないでください。

## Source Snapshot

`snapshots/course04/snapshot_info.md` に、この公開ソースの基準となったCourse 4 Source Snapshotの情報を収録しています。

このリポジトリの `main` は、読者向けに公開している最新状態です。

## 注意

外部API、LLM、Pythonパッケージ等は時間とともに変更される可能性があります。本リポジトリは、本書で扱った実装時点のSource Snapshotを理解・確認するためのコンパニオンコードです。

## Author

前田 陽造 / Zero-One-Tech
