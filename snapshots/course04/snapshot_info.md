# Snapshot Information

Course: 講座4（BUILD）— 問い合わせトリアージAIエージェント（ローカルCLI）

Tag: `course04-v1.0`（注釈つきタグ）

Branch: `course04-build`

Commit: タグ `course04-v1.0` が指すコミット。`git rev-parse course04-v1.0^{commit}` で確認する（このファイルは、そのコミットに含まれるため、ハッシュ自体は、ここに書けない）

Created Date: 2026-09-21

## Purpose

講座4の完成状態を固定する。Kindle（PoC-Kindle）、Udemy 講座4 の教材制作と、講座5 の実装の開始点（Course 4 Completed Source Snapshot）は、最新のコードではなく、このタグの状態を基準にする。

## Included Directories

手順書 §11.3 の基本対象に、依存の版を再現するための `uv.lock` を加える。

```text
README_JA.md
CLAUDE.md
pyproject.toml
uv.lock
src/
tests/
specs/
config/
data/
```

## Notes

- **状態（タグの時点）：** SDD の仕様は v1.18（レビュー：GO 18/18）。全 23 タスクが完了。`uv run pytest` は 1063 件が通る（LLM・ネットワークなし）。`uv run mypy src` は、問題なし。実 LLM のスモークテスト（`-m llm`、8 件）と、README_JA.md の手順を、実際の API Key で確認済み。
- **取り出し方（基準のソースを、別のディレクトリへ取り出す）：**

  ```bash
  mkdir -p <出力先>
  git archive course04-v1.0 README_JA.md CLAUDE.md pyproject.toml uv.lock src tests specs config data | tar -x -C <出力先>
  ```

  作業ツリーを変えずに確認するだけなら、`git show course04-v1.0:<パス>` も使える。
- **修正が必要になったとき：** 基準のソース（`src/`・`tests/`・`specs/`）を、教材の都合で直接書き換えない。実装のブランチで修正 → テスト → commit → タグの更新 → このファイルの更新（Snapshot の再生成）→ 教材の修正、の順で行う（手順書 §27）。
- **次の段階：** 講座5 は、`course04-build` から作るブランチ `course05-validate` で実装する（タグ `course05-v1.0`）。
