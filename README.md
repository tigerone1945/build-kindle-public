# build-kindle-public

Kindle技術書 **BUILD編** の読者向けコンパニオンリポジトリです。

本書で扱う「完成したSDDを、Source
Code・Test・Traceabilityへ変換するBUILD工程」を確認・実行するためのソースを公開しています。

## このリポジトリについて

このPublicリポジトリは、制作に使用したPrivateリポジトリから、読者に必要なファイルだけを抽出したものです。

-   制作原稿・レビュー記録・編集資料は含みません
-   秘密情報を含む `.env` は含みません
-   環境変数の例は `.env.example` を使用してください
-   本書で扱った固定Snapshotは `course04-v1.0` Tagで確認します

## 主な構成

``` text
.
├── config/
├── data/
│   ├── eval/
│   └── samples/
├── snapshots/course04/
├── specs/triage-agent/
├── src/triage_agent/
├── tests/
├── .env.example
├── .python-version
├── pyproject.toml
├── uv.lock
└── README.md
```

## 1. Clone

``` bash
git clone https://github.com/tigerone1945/build-kindle-public.git
cd build-kindle-public
```

## 2. 環境構築

本リポジトリは **Python 3.13以上** と `uv` を使用します。

``` bash
uv sync --locked
cp .env.example .env
```

`.env` の `OPENAI_API_KEY` に自分のOpenAI
APIキーを設定してください。`.env`
やAPIキーはGitへcommitしないでください。

## 3. サンプルを実行する

サンプル問い合わせは `data/samples/inquiries.jsonl` にあります。

``` bash
uv run --env-file .env triage-agent run --input data/samples/inquiries.jsonl
```

`python -m triage_agent` からも同じCLIを起動できます。

``` bash
uv run --env-file .env python -m triage_agent run --input data/samples/inquiries.jsonl
```

実行結果やHuman Reviewキューなどは、設定に従って `output/`
以下へ生成されます。

## 4. Test

通常の自動テストは実LLMを呼ばずに実行できます。

``` bash
uv run pytest
uv run mypy src
```

### 実LLMテスト

`tests/test_live_llm.py` は実際のOpenAI
APIを利用するテストです。実行するとAPI利用料が発生します。

``` bash
uv run --env-file .env pytest -m llm -s -rs
```

通常の `uv run pytest` では、`llm`
マーカーが付いた実LLMテストは除外されます。

## 5. SDD

`specs/triage-agent/` にBUILDの入力となる以下の仕様を収録しています。

-   `requirements.md`
-   `design.md`
-   `tasks.md`

本書では、これらのSDDと `src/triage_agent/`、`tests/`
の対応関係を追跡しながらBUILD工程を説明します。

## 6. Source Code

実装本体は `src/triage_agent/` にあります。

問い合わせ分類、Input Guardrail、Structured
Output、決定論的業務ルール、Routing、Human
Review、Persistence、Application Logging、Evaluationなどを確認できます。

## 7. Source Snapshot / Tag

`main` は読者向けに公開している最新状態です。本書で扱った **Course
4完成時点のSource Snapshot** は `course04-v1.0` です。

``` bash
git fetch --tags
git tag --list
git switch --detach course04-v1.0
git describe --tags --exact-match
```

`course04-v1.0` と表示されれば、本書のSource Snapshotを参照しています。

最新版の `main` へ戻る場合：

``` bash
git switch main
```

Snapshotの説明は `snapshots/course04/snapshot_info.md` にあります。

## 8. Evaluation

評価用データは `data/eval/` にあります。

Evaluationは、正解付きデータを通常と同じPipelineで処理し、分類精度・Routing精度・Escalation妥当性などを確認するための機能です。

## 注意事項

-   外部API、LLM、Pythonパッケージ等は時間とともに変更される可能性があります
-   本書の記述と照合するときは `course04-v1.0` のSource
    Snapshotを基準にしてください
-   APIキーや `.env` をGitHubへ公開しないでください
-   制作原稿・編集資料・レビュー記録・内部プロンプトなどはPublicリポジトリに含めていません

## Author

前田 陽造 / Zero-One-Tech
