"""評価（CMP-014）：正解つきのデータセットで、分類精度・Routing 精度・Escalation 妥当性を測る（REQ-015）。

- データセットは JSONL（`.jsonl`）のみ。1行ごとに `LabeledInquiry` として検証し、不正な行があれば、
  すべての行番号と理由（エラーの種類と項目名だけ。入力された値を含めない）を報告して、1件も処理しない（AC-4・AC-5）。
- 各問い合わせは、通常と同じ Pipeline で処理する。出力先は一時ディレクトリで、本番の出力ファイルには書かない（AC-3）。
- 分母が 0 の指標は「対象なし」とする（AC-6）。合格基準は、定めない（測定と表示のみ。要件 第13章 項目7）。
- ログ・キューの書き込み・読み込みの失敗（`StorageError`）は、捕捉せずに送出する（ERR-007）。

不一致の一覧は、データセットの行番号で示す（問い合わせIDは、あれば添える。ID は任意で、重複しうるため）。
"""

import json
import tempfile
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from pydantic import ValidationError

from triage_agent.models import ExpectedResult, LabeledInquiry, ProcessResult, ResultKind
from triage_agent.pipeline import Pipeline

JSONL_SUFFIX = ".jsonl"
NOT_APPLICABLE = "対象なし"

# 検証エラーの項目名は、`loc` の先頭のこの個数までにする（例：`inquiry.body`、`expected.category`）。
# 3要素目以降には、`form_fields` のキーなど、利用者が決める値が入りうるため、示さない。
LOCATION_DEPTH = 2
JSON_INVALID = "json_invalid"


class EvaluationDatasetError(Exception):
    """評価データセットを処理できない。メッセージには、データセットの内容（本文など）を含めない。"""


@dataclass(frozen=True)
class LabeledRow:
    """データセットの1行。`line` は、ファイル内の行番号（1から。空行も数える）。"""

    line: int
    item: LabeledInquiry


class _InvalidLine(Exception):
    """不正な1行。メッセージは、エラーの種類と項目名だけ。"""


def load_dataset(path: Path) -> list[LabeledRow]:
    """評価データセットを読み込み、行の順に返す。空行（空白のみ）は無視する（行番号は数え続ける）。

    ファイルがない・読めない・拡張子が `.jsonl` でない場合と、不正な行がある場合は、`EvaluationDatasetError`。
    不正な行は、最初の1件で止めず、すべて集めて報告する。
    """
    # 拡張子は、`run` の入力ファイルと同じく、大文字小文字を区別しない。
    if path.suffix.lower() != JSONL_SUFFIX:
        raise EvaluationDatasetError(f"評価データセットの拡張子が不正です（{JSONL_SUFFIX} のみ対応）: {path}")
    text = _read_text(path)

    rows: list[LabeledRow] = []
    problems: list[str] = []
    for line_number, line in enumerate(text.split("\n"), start=1):
        if not line.strip():
            continue
        try:
            rows.append(LabeledRow(line=line_number, item=_parse_line(line)))
        except _InvalidLine as invalid:
            problems.append(f"  {line_number}行目: {invalid}")
    if problems:
        raise EvaluationDatasetError("不正な行があります（評価は始めていません）\n" + "\n".join(problems))
    return rows


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError as error:
        raise EvaluationDatasetError(f"評価データセットが存在しません: {path}") from error
    except UnicodeDecodeError:
        # 例外の連鎖にも、読めなかったバイト列を残さない。
        raise EvaluationDatasetError(f"評価データセットを UTF-8 として読み取れません: {path}") from None
    except OSError as error:
        raise EvaluationDatasetError(f"評価データセットを読み取れません: {path}（{type(error).__name__}）") from error


def _parse_line(line: str) -> LabeledInquiry:
    try:
        raw = json.loads(line)
    except json.JSONDecodeError:
        raise _InvalidLine(JSON_INVALID) from None
    try:
        return LabeledInquiry.model_validate(raw)
    except ValidationError as error:
        # `ValidationError` は、入力値を保持するため、連鎖させない。
        raise _InvalidLine(_describe_validation_error(error)) from None


def _describe_validation_error(error: ValidationError) -> str:
    """検証エラーを `<項目名>: <種類>`（複数は、重複なしで `, ` 区切り）にまとめる。入力された値は含めない。"""
    reasons: list[str] = []
    for detail in error.errors(include_url=False, include_context=False, include_input=False):
        location = ".".join(str(part) for part in detail["loc"][:LOCATION_DEPTH])
        reason = f"{location}: {detail['type']}" if location else detail["type"]
        if reason not in reasons:
            reasons.append(reason)
    return ", ".join(reasons)


# --- 指標 ---


@dataclass(frozen=True)
class Ratio:
    """一致した件数 ÷ 対象の件数。対象が 0 件のときは、`rate` が `None`（「対象なし」）。"""

    correct: int
    total: int

    @property
    def rate(self) -> float | None:
        return None if self.total == 0 else self.correct / self.total


@dataclass(frozen=True)
class Mismatch:
    """不一致の1件：カテゴリまたは Routing が、正解と一致しない（優先度のずれは含めない）。"""

    line: int
    inquiry_id: str | None
    expected: ExpectedResult
    actual: ProcessResult


@dataclass(frozen=True)
class EvaluationReport:
    total: int
    classification: Ratio
    routing: Ratio
    escalation: Ratio
    priority: Ratio  # 参考。3つの指標とは別で、不一致の判定に含めない。
    mismatches: list[Mismatch]
    process_errors: int  # 想定外の例外（`process_error`）になった件数。


def category_matches(expected: ExpectedResult, actual: ProcessResult) -> bool:
    return actual.category is expected.category


def routing_matches(expected: ExpectedResult, actual: ProcessResult) -> bool:
    """正解が自動登録なら、実際も自動登録で、部署が一致。正解が Human Review なら、実際も Human Review（Design 第8.3節）。"""
    if expected.kind is ResultKind.HUMAN_REVIEW:
        return actual.kind is ResultKind.HUMAN_REVIEW
    return actual.kind is ResultKind.AUTO_REGISTERED and actual.department == expected.department


def build_report(judged: Sequence[tuple[LabeledRow, ProcessResult]]) -> EvaluationReport:
    """行と、その処理結果から、指標と不一致の一覧を作る。空でも、ゼロ除算をしない。"""
    category_correct = routing_correct = priority_correct = 0
    expected_review = review_caught = 0
    mismatches: list[Mismatch] = []
    for row, actual in judged:
        expected = row.item.expected
        category_ok = category_matches(expected, actual)
        routing_ok = routing_matches(expected, actual)
        category_correct += category_ok
        routing_correct += routing_ok
        priority_correct += actual.priority is expected.priority
        if expected.kind is ResultKind.HUMAN_REVIEW:
            expected_review += 1
            review_caught += actual.kind is ResultKind.HUMAN_REVIEW
        if not (category_ok and routing_ok):
            mismatches.append(
                Mismatch(line=row.line, inquiry_id=row.item.inquiry.inquiry_id, expected=expected, actual=actual)
            )
    total = len(judged)
    return EvaluationReport(
        total=total,
        classification=Ratio(category_correct, total),
        routing=Ratio(routing_correct, total),
        escalation=Ratio(review_caught, expected_review),
        priority=Ratio(priority_correct, total),
        mismatches=mismatches,
        process_errors=sum(1 for _, actual in judged if actual.kind is ResultKind.PROCESS_ERROR),
    )


# --- 評価の実行 ---


class EvaluationProgress(Protocol):
    """評価の進捗の書き込み先。中断したときの表示（処理中の `request_id`、処理を完了した件数）に使う（ERR-007）。"""

    pipeline: Pipeline | None
    processed: int | None


class Evaluator:
    """一時ディレクトリを出力先とした Pipeline で、全件を処理する。

    `make_pipeline` は、出力先のディレクトリを受け取って、Pipeline を作る関数（テストが、書き込みに失敗する
    Pipeline を返せるよう、外から受け取る）。一時ディレクトリは、評価の終了後（失敗のときも）に削除する。
    """

    def __init__(self, make_pipeline: Callable[[Path], Pipeline]) -> None:
        self._make_pipeline = make_pipeline

    def evaluate(self, rows: Sequence[LabeledRow], progress: EvaluationProgress | None = None) -> EvaluationReport:
        """全件を、データセットの順に処理して、報告を返す。`StorageError` は、捕捉せずに送出する（ERR-007）。"""
        judged: list[tuple[LabeledRow, ProcessResult]] = []
        with tempfile.TemporaryDirectory(prefix="triage-eval-") as directory:
            pipeline = self._make_pipeline(Path(directory))
            if progress is not None:
                progress.pipeline = pipeline
                progress.processed = 0
            for row in rows:
                judged.append((row, pipeline.process(row.item.inquiry)))
                if progress is not None:
                    progress.processed = len(judged)
        return build_report(judged)


# --- 表示 ---


def _percent(ratio: Ratio) -> str:
    rate = ratio.rate
    return NOT_APPLICABLE if rate is None else f"{rate * 100:.1f}%（{ratio.correct}/{ratio.total}）"


def _describe_expected(expected: ExpectedResult) -> str:
    return (
        f"カテゴリ={expected.category.value}、優先度={expected.priority.value}、"
        f"区分={expected.kind.value}、部署={expected.department or 'なし'}"
    )


def _describe_actual(actual: ProcessResult) -> str:
    category = actual.category.value if actual.category is not None else "なし"
    priority = actual.priority.value if actual.priority is not None else "なし"
    text = f"カテゴリ={category}、優先度={priority}、区分={actual.kind.value}、部署={actual.department or 'なし'}"
    if actual.reasons:
        text += "、理由=" + ",".join(reason.value for reason in actual.reasons)
    if actual.error:
        # 例外の種類・失敗の種類だけ（問い合わせの内容を含まない。ERR-002、ERR-005）。
        text += f"、エラー={actual.error}"
    return text


def format_report(report: EvaluationReport) -> str:
    """指標と不一致の一覧を、人が読める形式にする（Design 第8.1節）。優先度の一致率は、参考として別の1行。"""
    lines = [
        f"評価結果: {report.total}件",
        f"  分類精度: {_percent(report.classification)}",
        f"  Routing 精度: {_percent(report.routing)}",
        f"  Escalation 妥当性: {_percent(report.escalation)}",
        f"  （参考）優先度の一致率: {_percent(report.priority)}",
        "",
        f"不一致: {len(report.mismatches)}件",
    ]
    for mismatch in report.mismatches:
        identifier = f"（問い合わせID: {mismatch.inquiry_id}）" if mismatch.inquiry_id else ""
        lines.append(f"  {mismatch.line}行目{identifier}")
        lines.append(f"    期待: {_describe_expected(mismatch.expected)}")
        lines.append(f"    実際: {_describe_actual(mismatch.actual)}")
    return "\n".join(lines)
