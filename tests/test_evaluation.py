"""TASK-017：評価データセットと `eval` コマンド（REQ-015、ERR-007 / CMP-014、CMP-001、第8.1節、第8.3節、ADR-013）。

分類器は Fake（LLM・ネットワークなし）。ファイルは `tmp_path` に書く（本番の出力先 `output/` には触れない）。
`main(["eval", ...])` の結合テストと、`Evaluator` の単体テストがある。
不一致の一覧を行番号で示すこと、終了コードを `run` と同じにすること（処理エラーの件があれば 1）は、
ユーザーの指示（2026-09-21。FINDING-039 の推奨案）による。
"""

import json
import tempfile
from collections.abc import Callable
from pathlib import Path

import pytest
from fakes import FakeClassifier, make_result

from triage_agent import cli
from triage_agent.classifier import Classifier
from triage_agent.cli import COMMANDS, main
from triage_agent.config import AppConfig, DecisionSettings, load_config
from triage_agent.evaluation import (
    NOT_APPLICABLE,
    EvaluationDatasetError,
    EvaluationReport,
    Evaluator,
    LabeledRow,
    Ratio,
    build_report,
    format_report,
    load_dataset,
    routing_matches,
)
from triage_agent.guardrails import check_input
from triage_agent.masking import mask_pii
from triage_agent.models import (
    Category,
    ClassifyOutcome,
    ClassifyStatus,
    ExpectedResult,
    Inquiry,
    LabeledInquiry,
    Priority,
    ProcessLogRecord,
    ProcessResult,
    ResultKind,
)
from triage_agent.pipeline import Pipeline
from triage_agent.process_log import PROCESS_LOG_FILE, ProcessLog
from triage_agent.review import ReviewQueue
from triage_agent.storage import StorageError
from triage_agent.tickets import MockTicketSystem, RetryQueue

BUNDLED_DATASET = Path(__file__).parent.parent / "data" / "eval" / "labeled_inquiries.jsonl"
API_KEY = "sk-dummy-SECRET-KEY-FOR-TESTS"
SECRET_BODY = "本文の固有の断片ZZZ"
SECRET_EMAIL = "taro.yamada@example.com"


@pytest.fixture(autouse=True)
def _isolated_environment(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", API_KEY)
    monkeypatch.chdir(tmp_path)


@pytest.fixture
def config(config_dir: Path) -> AppConfig:
    return load_config(config_dir)


def success(**overrides: object) -> ClassifyOutcome:
    return ClassifyOutcome(status=ClassifyStatus.SUCCESS, classification=make_result(**overrides), attempts=1)


def labeled(
    inquiry_id: str | None,
    *,
    category: str = "billing",
    priority: str = "medium",
    kind: str = "auto_registered",
    department: str | None = "請求",
    subject: str | None = "件名",
    body: str = "請求書の内容について確認したい",
) -> dict[str, object]:
    inquiry: dict[str, object] = {"channel": "email", "subject": subject, "body": body}
    if inquiry_id is not None:
        inquiry["inquiry_id"] = inquiry_id
    return {
        "inquiry": inquiry,
        "expected": {"category": category, "priority": priority, "kind": kind, "department": department},
    }


def write_dataset(path: Path, rows: list[dict[str, object] | str], *, name: str = "dataset.jsonl") -> Path:
    """行を書く。文字列は、そのまま（壊れた行を作るため）、辞書は JSON として。"""
    target = path / name
    lines = [row if isinstance(row, str) else json.dumps(row, ensure_ascii=False) for row in rows]
    target.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return target


def make_pipeline_factory(
    classifier: Classifier,
    config: AppConfig,
    *,
    process_log: Callable[[Path], ProcessLog] = ProcessLog,
    ticket_fails: bool = False,
) -> Callable[[Path], Pipeline]:
    def make(output_dir: Path) -> Pipeline:
        return Pipeline(
            classifier=classifier,
            config=config,
            tickets=MockTicketSystem(output_dir, fail_if=(lambda request: True) if ticket_fails else None),
            retry_queue=RetryQueue(output_dir),
            review_queue=ReviewQueue(output_dir),
            process_log=process_log(output_dir),
        )

    return make


def rows_of(items: list[dict[str, object]], *, start: int = 1) -> list[LabeledRow]:
    return [
        LabeledRow(line=start + index, item=LabeledInquiry.model_validate(item)) for index, item in enumerate(items)
    ]


class FlakyProcessLog(ProcessLog):
    """`fail_on` 回目の追記で、書き込みの失敗（`StorageError`）になる処理ログ。"""

    fail_on = 2

    def __init__(self, output_dir: Path) -> None:
        super().__init__(output_dir)
        self._appends = 0

    def append(self, record: ProcessLogRecord) -> ProcessLogRecord:
        self._appends += 1
        if self._appends == self.fail_on:
            raise StorageError(f"{PROCESS_LOG_FILE}: 書き込めません（OSError）")
        return super().append(record)


# --- 読み込み（REQ-015 AC-4・AC-5） ---


class TestLoadDataset:
    def test_it_returns_the_rows_in_order_with_their_line_numbers(self, tmp_path: Path) -> None:
        path = write_dataset(tmp_path, [labeled("A"), "", labeled("B"), "   ", labeled("C")])
        rows = load_dataset(path)
        assert [row.item.inquiry.inquiry_id for row in rows] == ["A", "B", "C"]
        # 空行は、無視するが、行番号には数える。
        assert [row.line for row in rows] == [1, 3, 5]

    def test_the_labels_are_read(self, tmp_path: Path) -> None:
        (row,) = load_dataset(write_dataset(tmp_path, [labeled("A", category="support", priority="low", kind="human_review", department=None)]))
        assert row.item.expected == ExpectedResult(
            category=Category.SUPPORT, priority=Priority.LOW, kind=ResultKind.HUMAN_REVIEW, department=None
        )

    def test_a_row_without_an_inquiry_id_is_accepted_and_gets_no_id(self, tmp_path: Path) -> None:
        (row,) = load_dataset(write_dataset(tmp_path, [labeled(None)]))
        assert row.item.inquiry.inquiry_id is None

    def test_unknown_keys_are_ignored(self, tmp_path: Path) -> None:
        item = labeled("A")
        item["memo"] = "ラベル付けのメモ"
        assert len(load_dataset(write_dataset(tmp_path, [item]))) == 1

    @pytest.mark.parametrize("content", ["", "\n", "  \n\n \n"])
    def test_an_empty_dataset_is_zero_rows_not_an_error(self, tmp_path: Path, content: str) -> None:
        path = tmp_path / "empty.jsonl"
        path.write_text(content, encoding="utf-8")
        assert load_dataset(path) == []

    @pytest.mark.parametrize("name", ["data.csv", "data.json", "data.txt", "data", "data.jsonl.bak", "data.jsonl.csv"])
    def test_only_jsonl_is_accepted(self, tmp_path: Path, name: str) -> None:
        path = write_dataset(tmp_path, [labeled("A")], name=name)
        with pytest.raises(EvaluationDatasetError, match="拡張子"):
            load_dataset(path)

    def test_the_extension_is_checked_before_the_file_is_read(self, tmp_path: Path) -> None:
        with pytest.raises(EvaluationDatasetError, match="拡張子"):
            load_dataset(tmp_path / "does-not-exist.csv")

    @pytest.mark.parametrize("name", ["data.JSONL", "data.Jsonl"])
    def test_the_extension_is_case_insensitive_like_the_run_input(self, tmp_path: Path, name: str) -> None:
        assert len(load_dataset(write_dataset(tmp_path, [labeled("A")], name=name))) == 1

    def test_a_missing_file_is_an_error(self, tmp_path: Path) -> None:
        with pytest.raises(EvaluationDatasetError, match="存在しません"):
            load_dataset(tmp_path / "missing.jsonl")

    def test_a_file_that_is_not_utf8_is_an_error_without_its_bytes(self, tmp_path: Path) -> None:
        path = tmp_path / "bad.jsonl"
        path.write_bytes(b'{"inquiry": "\xff\xfe' + SECRET_BODY.encode() + b'"}\n')
        with pytest.raises(EvaluationDatasetError, match="UTF-8") as error:
            load_dataset(path)
        assert SECRET_BODY not in str(error.value)
        assert error.value.__cause__ is None

    def test_a_directory_is_an_error(self, tmp_path: Path) -> None:
        path = tmp_path / "dir.jsonl"
        path.mkdir()
        with pytest.raises(EvaluationDatasetError, match="読み取れません"):
            load_dataset(path)

    def test_every_invalid_row_is_reported_with_its_line_number_and_no_row_is_returned(self, tmp_path: Path) -> None:
        path = write_dataset(
            tmp_path,
            [labeled("A"), "{ここは壊れている", labeled("B"), "[1, 2]", "", {"inquiry": labeled("C")["inquiry"]}, labeled("D")],
        )
        with pytest.raises(EvaluationDatasetError) as error:
            load_dataset(path)
        message = str(error.value)
        lines = [line for line in message.splitlines() if "行目" in line]
        assert [line.strip() for line in lines] == [
            "2行目: json_invalid",
            "4行目: model_type",
            "6行目: expected: missing",
        ]
        assert "評価は始めていません" in message

    @pytest.mark.parametrize(
        ("row", "reason"),
        [
            ('"文字列だけ"', "model_type"),
            ("null", "model_type"),
            ("12", "model_type"),
            ('{"expected": {"category": "billing", "priority": "medium", "kind": "human_review"}}', "inquiry: missing"),
            ('{"inquiry": {"channel": "email", "body": "本文"}}', "expected: missing"),
            (
                '{"inquiry": {"channel": "email"}, "expected": {"category": "billing", "priority": "medium", "kind": "human_review"}}',
                "inquiry.body: missing",
            ),
            (
                '{"inquiry": {"channel": "email", "body": 123}, "expected": {"category": "billing", "priority": "medium", "kind": "human_review"}}',
                "inquiry.body: string_type",
            ),
            (
                '{"inquiry": {"channel": "fax", "body": "本文"}, "expected": {"category": "billing", "priority": "medium", "kind": "human_review"}}',
                "inquiry.channel: enum",
            ),
            (
                '{"inquiry": {"channel": "email", "body": "本文"}, "expected": {"category": "unknown", "priority": "medium", "kind": "human_review"}}',
                "expected.category: enum",
            ),
            (
                '{"inquiry": {"channel": "email", "body": "本文"}, "expected": {"category": "billing", "priority": "urgent", "kind": "human_review"}}',
                "expected.priority: enum",
            ),
            (
                '{"inquiry": {"channel": "email", "body": "本文"}, "expected": {"category": "billing", "priority": "medium", "kind": "process_error"}}',
                "expected.kind: literal_error",
            ),
            (
                '{"inquiry": {"channel": "email", "body": "本文"}, "expected": {"category": "billing", "priority": "medium"}}',
                "expected.kind: missing",
            ),
        ],
    )
    def test_the_reason_is_the_kind_of_error_and_the_item_name(self, tmp_path: Path, row: str, reason: str) -> None:
        with pytest.raises(EvaluationDatasetError) as error:
            load_dataset(write_dataset(tmp_path, [row]))
        assert f"1行目: {reason}" in str(error.value)

    def test_several_problems_in_one_row_are_listed_once_each(self, tmp_path: Path) -> None:
        row = '{"inquiry": {"channel": "email"}, "expected": {"category": "unknown", "priority": "unknown", "kind": "human_review"}}'
        with pytest.raises(EvaluationDatasetError) as error:
            load_dataset(write_dataset(tmp_path, [row]))
        assert "1行目: inquiry.body: missing, expected.category: enum, expected.priority: enum" in str(error.value)

    def test_the_message_never_holds_the_values_of_the_row(self, tmp_path: Path) -> None:
        """入力された値（本文、送信者、`form_fields` のキー）を、理由に含めない（REQ-015 AC-5）。"""
        row = {
            "inquiry": {
                "channel": "email",
                "subject": SECRET_BODY,
                "body": SECRET_BODY,
                "sender": SECRET_EMAIL,
                "form_fields": {SECRET_EMAIL: 1},
            },
            "expected": {"category": SECRET_BODY, "priority": SECRET_EMAIL, "kind": "human_review"},
        }
        with pytest.raises(EvaluationDatasetError) as error:
            load_dataset(write_dataset(tmp_path, [row]))
        message = str(error.value)
        assert "inquiry.form_fields: string_type" in message
        assert "expected.category: enum" in message
        assert SECRET_BODY not in message
        assert SECRET_EMAIL not in message
        assert error.value.__cause__ is None

    def test_a_broken_json_row_does_not_leak_its_content_either(self, tmp_path: Path) -> None:
        with pytest.raises(EvaluationDatasetError) as error:
            load_dataset(write_dataset(tmp_path, [f'{{"inquiry": "{SECRET_BODY}']))
        assert SECRET_BODY not in str(error.value)
        assert "json_invalid" in str(error.value)


# --- 指標（REQ-015 AC-1・AC-2・AC-6、Design 第8.3節） ---


def evaluate(
    config: AppConfig, items: list[dict[str, object]], outcomes: dict[str, ClassifyOutcome]
) -> EvaluationReport:
    factory = make_pipeline_factory(FakeClassifier(outcomes), config)
    return Evaluator(factory).evaluate(rows_of(items))


class TestMetrics:
    def test_the_metrics_match_a_hand_calculation(self, config: AppConfig) -> None:
        items = [
            labeled("T-1"),  # billing・請求：正解と一致
            labeled("T-2", category="support", priority="low", department="サポート"),  # カテゴリも部署も違う
            labeled("T-3", kind="human_review", department=None),  # Human Review：一致
            labeled("T-4", category="sales", kind="human_review", department=None),  # 自動登録されてしまう
        ]
        report = evaluate(
            config,
            items,
            {
                "T-1": success(category=Category.BILLING, confidence=0.9),
                "T-2": success(category=Category.BILLING, confidence=0.9),
                "T-3": success(category=Category.BILLING, confidence=0.5),
                "T-4": success(category=Category.SALES, confidence=0.95),
            },
        )
        assert report.total == 4
        assert report.classification == Ratio(3, 4)  # T-2 だけ違う
        assert report.routing == Ratio(2, 4)  # T-1・T-3。T-2 は部署違い、T-4 は自動登録
        assert report.escalation == Ratio(1, 2)  # 正解が Human Review の T-3・T-4 のうち、T-3 だけ
        assert report.priority == Ratio(3, 4)  # T-2 だけ違う（medium と low）
        assert [mismatch.line for mismatch in report.mismatches] == [2, 4]
        assert [mismatch.inquiry_id for mismatch in report.mismatches] == ["T-2", "T-4"]

    def test_a_mismatch_holds_the_expected_and_the_actual_result(self, config: AppConfig) -> None:
        report = evaluate(
            config,
            [labeled("T-1", category="support", department="サポート")],
            {"T-1": success(category=Category.BILLING, confidence=0.9)},
        )
        (mismatch,) = report.mismatches
        assert mismatch.expected.category is Category.SUPPORT
        assert mismatch.actual.category is Category.BILLING
        assert mismatch.actual.kind is ResultKind.AUTO_REGISTERED
        assert mismatch.actual.department == "請求"

    def test_a_category_difference_alone_is_a_mismatch(self, config: AppConfig) -> None:
        """Routing が一致していても（どちらも Human Review）、カテゴリが違えば、不一致（REQ-015 AC-2）。"""
        report = evaluate(
            config,
            [labeled("T-1", category="billing", kind="human_review", department=None)],
            {"T-1": success(category=Category.SUPPORT, confidence=0.5)},
        )
        assert report.routing == Ratio(1, 1)
        assert report.classification == Ratio(0, 1)
        assert [mismatch.inquiry_id for mismatch in report.mismatches] == ["T-1"]

    def test_a_routing_difference_alone_is_a_mismatch(self, config: AppConfig) -> None:
        """カテゴリが一致していても、Routing が違えば（正解は Human Review、実際は自動登録）、不一致。"""
        report = evaluate(
            config,
            [labeled("T-1", category="billing", kind="human_review", department=None)],
            {"T-1": success(category=Category.BILLING, confidence=0.95)},
        )
        assert report.classification == Ratio(1, 1)
        assert report.routing == Ratio(0, 1)
        assert [mismatch.inquiry_id for mismatch in report.mismatches] == ["T-1"]

    def test_the_rates_are_fractions(self) -> None:
        assert Ratio(3, 4).rate == 0.75
        assert Ratio(0, 5).rate == 0.0
        assert Ratio(0, 0).rate is None

    def test_a_priority_only_difference_lowers_only_the_reference_rate(self, config: AppConfig) -> None:
        """優先度だけがずれても、3指標は 100% のまま、不一致に載らない（REQ-015 AC-1・AC-2）。"""
        items = [labeled("T-1", priority="high"), labeled("T-2")]
        report = evaluate(
            config,
            items,
            {
                "T-1": success(category=Category.BILLING, priority=Priority.MEDIUM, confidence=0.9),
                "T-2": success(category=Category.BILLING, priority=Priority.MEDIUM, confidence=0.9),
            },
        )
        assert report.classification == Ratio(2, 2)
        assert report.routing == Ratio(2, 2)
        assert report.escalation == Ratio(0, 0)
        assert report.mismatches == []
        assert report.priority == Ratio(1, 2)

    def test_an_urgent_keyword_raises_the_actual_priority(self, config: AppConfig) -> None:
        report = evaluate(
            config,
            [labeled("T-1", priority="high", body="至急、請求書の内容を確認したい")],
            {"T-1": success(category=Category.BILLING, priority=Priority.LOW, confidence=0.9)},
        )
        assert report.priority == Ratio(1, 1)

    def test_an_actual_ticket_failure_is_not_a_match_for_an_expected_auto_registration(self, config: AppConfig) -> None:
        factory = make_pipeline_factory(
            FakeClassifier(success(category=Category.BILLING, confidence=0.9)), config, ticket_fails=True
        )
        report = Evaluator(factory).evaluate(rows_of([labeled("T-1")]))
        assert report.routing == Ratio(0, 1)
        assert report.classification == Ratio(1, 1)
        assert report.process_errors == 0
        assert len(report.mismatches) == 1
        assert report.mismatches[0].actual.kind is ResultKind.TICKET_FAILED

    def test_an_unexpected_exception_is_a_mismatch_and_is_counted(self, config: AppConfig) -> None:
        class Exploding:
            def classify(self, inquiry: Inquiry, request_id: str) -> ClassifyOutcome:
                raise RuntimeError(f"想定外 {SECRET_BODY}")

        report = Evaluator(make_pipeline_factory(Exploding(), config)).evaluate(rows_of([labeled("T-1")]))
        assert report.process_errors == 1
        assert report.classification == Ratio(0, 1)
        assert report.routing == Ratio(0, 1)
        assert report.priority == Ratio(0, 1)
        (mismatch,) = report.mismatches
        assert mismatch.actual.kind is ResultKind.PROCESS_ERROR
        assert mismatch.actual.error == "RuntimeError"  # 例外の種類だけ（Q-10）

    def test_a_failure_to_classify_is_a_human_review_and_matches_an_expected_human_review(self, config: AppConfig) -> None:
        outcome = ClassifyOutcome(status=ClassifyStatus.LLM_FAILURE, attempts=3, error_detail="APIConnectionError")
        report = evaluate(
            config,
            [labeled("T-1", category="unclassified", kind="human_review", department=None)],
            {"T-1": outcome},
        )
        assert report.routing == Ratio(1, 1)
        assert report.escalation == Ratio(1, 1)
        assert report.classification == Ratio(1, 1)  # 分類結果がなければ、未分類

    @pytest.mark.parametrize(
        ("expected_kind", "expected_department", "actual_kind", "actual_department", "matches"),
        [
            (ResultKind.AUTO_REGISTERED, "請求", ResultKind.AUTO_REGISTERED, "請求", True),
            (ResultKind.AUTO_REGISTERED, "請求", ResultKind.AUTO_REGISTERED, "営業", False),
            (ResultKind.AUTO_REGISTERED, "請求", ResultKind.AUTO_REGISTERED, None, False),
            (ResultKind.AUTO_REGISTERED, "請求", ResultKind.HUMAN_REVIEW, None, False),
            (ResultKind.AUTO_REGISTERED, "請求", ResultKind.TICKET_FAILED, "請求", False),
            (ResultKind.AUTO_REGISTERED, "請求", ResultKind.PROCESS_ERROR, None, False),
            (ResultKind.HUMAN_REVIEW, None, ResultKind.HUMAN_REVIEW, None, True),
            (ResultKind.HUMAN_REVIEW, "請求", ResultKind.HUMAN_REVIEW, None, True),  # 正解の部署は、Human Review では見ない
            (ResultKind.HUMAN_REVIEW, None, ResultKind.AUTO_REGISTERED, "請求", False),
            (ResultKind.HUMAN_REVIEW, None, ResultKind.TICKET_FAILED, "請求", False),
        ],
    )
    def test_routing_match_rules(
        self,
        expected_kind: ResultKind,
        expected_department: str | None,
        actual_kind: ResultKind,
        actual_department: str | None,
        matches: bool,
    ) -> None:
        expected = ExpectedResult.model_validate(
            {"category": "billing", "priority": "medium", "kind": expected_kind, "department": expected_department}
        )
        actual = ProcessResult(request_id="r", kind=actual_kind, department=actual_department)
        assert routing_matches(expected, actual) is matches


class TestZeroDenominators:
    """REQ-015 AC-6：分母が 0 の指標は「対象なし」（0% や 100% としない）。ゼロ除算をしない。"""

    def test_an_empty_dataset_has_no_applicable_metric(self, config: AppConfig) -> None:
        report = Evaluator(make_pipeline_factory(FakeClassifier({}), config)).evaluate([])
        assert report.total == 0
        for ratio in (report.classification, report.routing, report.escalation, report.priority):
            assert ratio.total == 0
            assert ratio.rate is None
        assert report.mismatches == []
        assert report.process_errors == 0

    def test_without_an_expected_human_review_only_the_escalation_metric_is_not_applicable(self, config: AppConfig) -> None:
        report = evaluate(config, [labeled("T-1")], {"T-1": success(category=Category.BILLING, confidence=0.9)})
        assert report.escalation.rate is None
        assert report.classification.rate == 1.0
        assert report.routing.rate == 1.0
        assert report.priority.rate == 1.0

    def test_build_report_of_nothing(self) -> None:
        report = build_report([])
        assert (report.total, report.escalation.rate, report.classification.rate) == (0, None, None)


# --- 実行（一時ディレクトリ、書き込みの失敗） ---


class TestEvaluatorRun:
    def test_the_rows_are_processed_in_order_by_the_real_pipeline(self, config: AppConfig) -> None:
        classifier = FakeClassifier(success(category=Category.BILLING, confidence=0.9))
        items = [labeled("A"), labeled("B"), labeled("C")]
        Evaluator(make_pipeline_factory(classifier, config)).evaluate(rows_of(items))
        assert [inquiry.inquiry_id for inquiry, _ in classifier.calls] == ["A", "B", "C"]

    def test_the_output_directory_is_a_temporary_one_removed_afterwards(self, config: AppConfig, tmp_path: Path) -> None:
        seen: list[Path] = []
        during: list[list[str]] = []

        class Spy:
            def __init__(self) -> None:
                self.calls = 0

            def classify(self, inquiry: Inquiry, request_id: str) -> ClassifyOutcome:
                self.calls += 1
                if self.calls == 2:  # 2件目の分類の時点で、1件目の記録が、一時ディレクトリにある
                    during.append(sorted(path.name for path in seen[0].iterdir()))
                return success(category=Category.BILLING, confidence=0.9)

        def factory(output_dir: Path) -> Pipeline:
            seen.append(output_dir)
            return make_pipeline_factory(Spy(), config)(output_dir)

        Evaluator(factory).evaluate(rows_of([labeled("A"), labeled("B")]))
        (directory,) = seen
        assert directory != Path(config.settings.paths.output_dir)
        assert not str(directory).startswith(str(tmp_path / "out"))
        assert "process_log.jsonl" in during[0]
        assert "tickets.jsonl" in during[0]
        assert not directory.exists()

    def test_the_production_output_directory_is_untouched(self, config: AppConfig, tmp_path: Path) -> None:
        production = Path(config.settings.paths.output_dir)
        production.mkdir(parents=True)
        (production / "tickets.jsonl").write_text('{"kept": true}\n', encoding="utf-8")
        before = {path.name: path.read_bytes() for path in production.iterdir()}
        evaluate(config, [labeled("A")], {"A": success(category=Category.BILLING, confidence=0.9)})
        assert {path.name: path.read_bytes() for path in production.iterdir()} == before


class TestWriteFailureAborts:
    """ERR-007：評価の実行中の書き込みの失敗は、捕捉せずに送出する。指標は返らない。"""

    def make_classifier(self) -> FakeClassifier:
        return FakeClassifier(success(category=Category.BILLING, confidence=0.9))

    def test_it_stops_at_the_failing_row_and_reports_the_progress(self, config: AppConfig) -> None:
        classifier = self.make_classifier()
        evaluator = Evaluator(make_pipeline_factory(classifier, config, process_log=FlakyProcessLog))

        class Progress:
            pipeline: Pipeline | None = None
            processed: int | None = None

        progress = Progress()
        with pytest.raises(StorageError) as error:
            evaluator.evaluate(rows_of([labeled("A"), labeled("B"), labeled("C")]), progress)
        assert PROCESS_LOG_FILE in str(error.value)
        assert len(classifier.calls) == 2  # 3件目は、処理されない
        assert progress.processed == 1
        assert progress.pipeline is not None
        assert progress.pipeline.current_request_id == classifier.calls[1][1]

    def test_the_temporary_directory_is_removed_after_a_failure(self, config: AppConfig) -> None:
        seen: list[Path] = []

        def factory(output_dir: Path) -> Pipeline:
            seen.append(output_dir)
            return make_pipeline_factory(self.make_classifier(), config, process_log=FlakyProcessLog)(output_dir)

        with pytest.raises(StorageError):
            Evaluator(factory).evaluate(rows_of([labeled("A"), labeled("B")]))
        assert not seen[0].exists()


# --- 表示 ---


class TestFormatReport:
    def make_report(self, config: AppConfig) -> EvaluationReport:
        items = [
            labeled("T-1"),
            labeled("T-2", category="support", department="サポート", body=SECRET_BODY),
            labeled(None, category="sales", department="営業", body=SECRET_BODY),
        ]
        classifier = FakeClassifier(success(category=Category.BILLING, confidence=0.9))
        return Evaluator(make_pipeline_factory(classifier, config)).evaluate(rows_of(items))

    def test_the_metrics_are_shown_with_percentages_and_counts(self, config: AppConfig) -> None:
        text = format_report(self.make_report(config))
        assert "評価結果: 3件" in text
        assert "分類精度: 33.3%（1/3）" in text
        assert "Routing 精度: 33.3%（1/3）" in text
        assert f"Escalation 妥当性: {NOT_APPLICABLE}" in text

    def test_the_priority_rate_is_a_separate_reference_line(self, config: AppConfig) -> None:
        lines = format_report(self.make_report(config)).splitlines()
        assert lines[1].strip().startswith("分類精度")
        assert lines[2].strip().startswith("Routing 精度")
        assert lines[3].strip().startswith("Escalation 妥当性")
        assert lines[4].strip().startswith("（参考）優先度の一致率")

    def test_each_mismatch_shows_the_line_number_and_the_id_when_there_is_one(self, config: AppConfig) -> None:
        text = format_report(self.make_report(config))
        assert "不一致: 2件" in text
        assert "  2行目（問い合わせID: T-2）" in text
        assert "  3行目\n" in text  # ID のない行は、行番号だけ
        assert "期待: カテゴリ=support、優先度=medium、区分=auto_registered、部署=サポート" in text
        assert "実際: カテゴリ=billing、優先度=medium、区分=auto_registered、部署=請求" in text

    def test_no_inquiry_content_is_shown(self, config: AppConfig) -> None:
        assert SECRET_BODY not in format_report(self.make_report(config))

    def test_everything_is_not_applicable_for_an_empty_dataset(self) -> None:
        text = format_report(build_report([]))
        assert text.count(NOT_APPLICABLE) == 4
        assert "評価結果: 0件" in text
        assert "不一致: 0件" in text
        assert "0.0%" not in text and "100.0%" not in text

    def test_a_perfect_run_shows_100_percent_and_no_mismatch(self, config: AppConfig) -> None:
        report = evaluate(config, [labeled("A")], {"A": success(category=Category.BILLING, confidence=0.9)})
        text = format_report(report)
        assert "分類精度: 100.0%（1/1）" in text
        assert "不一致: 0件" in text

    def test_a_process_error_shows_only_the_type_of_the_exception(self, config: AppConfig) -> None:
        class Exploding:
            def classify(self, inquiry: Inquiry, request_id: str) -> ClassifyOutcome:
                raise RuntimeError(f"想定外 {SECRET_BODY}")

        report = Evaluator(make_pipeline_factory(Exploding(), config)).evaluate(rows_of([labeled("A")]))
        text = format_report(report)
        assert "エラー=RuntimeError" in text
        assert SECRET_BODY not in text


# --- 同梱の評価データセット（data/eval/labeled_inquiries.jsonl） ---

# LLM が返す想定の分類結果（問い合わせIDごと）。データセットの正解に、ルールが至ることを確かめる（ルールの回帰テスト）。
LLM_OUTPUTS: dict[str, ClassifyOutcome] = {
    "EVL-001": success(category=Category.BILLING, priority=Priority.MEDIUM, confidence=0.9),
    "EVL-002": success(category=Category.SUPPORT, priority=Priority.LOW, confidence=0.9),
    "EVL-003": success(category=Category.SALES, priority=Priority.MEDIUM, confidence=0.9),
    "EVL-004": success(category=Category.TECHNICAL, priority=Priority.MEDIUM, confidence=0.9),
    "EVL-005": success(category=Category.SUPPORT, priority=Priority.LOW, confidence=0.9),  # 「至急」で、高へ
    "EVL-006": success(category=Category.TECHNICAL, priority=Priority.MEDIUM, confidence=0.9),  # 「障害」で、高へ
    "EVL-007": success(category=Category.BILLING, priority=Priority.MEDIUM, confidence=0.9),  # 「解約」
    "EVL-008": success(category=Category.BILLING, priority=Priority.MEDIUM, confidence=0.9),  # 「返金」
    "EVL-009": success(category=Category.COMPLAINT, priority=Priority.MEDIUM, confidence=0.95, is_complaint_or_legal=True),
    "EVL-010": success(category=Category.SALES, priority=Priority.MEDIUM, confidence=0.95, is_complaint_or_legal=True),
    "EVL-011": success(category=Category.SALES, priority=Priority.MEDIUM, confidence=0.95, detected_language="other"),
    "EVL-012": success(category=Category.SUPPORT, priority=Priority.LOW, confidence=0.69),
    "EVL-013": success(category=Category.SUPPORT, priority=Priority.LOW, confidence=0.70),
    "EVL-014": success(category=Category.UNCLASSIFIED, priority=Priority.MEDIUM, confidence=0.2),
    "EVL-015": success(category=Category.BILLING, priority=Priority.MEDIUM, confidence=0.85, is_ambiguous=True),
    "EVL-016": ClassifyOutcome(status=ClassifyStatus.GUARDRAIL_TRIPPED, attempts=0),
}


class TestBundledDataset:
    def rows(self) -> list[LabeledRow]:
        return load_dataset(BUNDLED_DATASET)

    def test_it_loads_and_the_ids_are_unique(self) -> None:
        rows = self.rows()
        ids = [row.item.inquiry.inquiry_id for row in rows]
        assert len(rows) == 16
        assert all(ids)
        assert len(set(ids)) == len(ids)

    def test_it_is_fictional_and_holds_no_personal_information(self) -> None:
        for row in self.rows():
            inquiry = row.item.inquiry
            assert inquiry.sender is None
            for text in (inquiry.subject, inquiry.body):
                if text is not None:
                    assert mask_pii(text) == text

    def test_it_covers_every_category_both_kinds_and_the_required_cases(self) -> None:
        rows = {row.item.inquiry.inquiry_id: row.item for row in self.rows()}
        expected = [item.expected for item in rows.values()]
        assert {e.category for e in expected} == set(Category)
        assert {e.kind for e in expected} == {ResultKind.AUTO_REGISTERED, ResultKind.HUMAN_REVIEW}
        assert {e.priority for e in expected} == set(Priority)
        gibberish = rows["EVL-014"]
        # 意味不明だが十分に長い入力：入力ガードレールを通り、正解は、未分類の Human Review。
        assert gibberish.inquiry.body == "asdfghjkl"
        assert check_input(gibberish.inquiry.body, 5).passed
        assert gibberish.expected.category is Category.UNCLASSIFIED
        assert gibberish.expected.kind is ResultKind.HUMAN_REVIEW
        # 極端に短い入力：入力ガードレールに該当する。
        assert not check_input(rows["EVL-016"].inquiry.body, 5).passed
        assert any(item.inquiry.channel.value == "form" for item in rows.values())

    def test_the_department_is_labeled_only_for_automatic_registrations(self) -> None:
        for row in self.rows():
            expected = row.item.expected
            assert (expected.department is not None) == (expected.kind is ResultKind.AUTO_REGISTERED)

    def test_every_row_has_an_assumed_llm_output_and_no_more(self) -> None:
        assert set(LLM_OUTPUTS) == {row.item.inquiry.inquiry_id for row in self.rows()}

    def test_the_rules_reach_the_labels_from_the_assumed_llm_outputs(self, config: AppConfig) -> None:
        """全件が期待どおりの Fake で、3指標も、参考の優先度も、100% になる（ルールの回帰テスト）。"""
        report = Evaluator(make_pipeline_factory(FakeClassifier(LLM_OUTPUTS), config)).evaluate(self.rows())
        assert report.total == 16
        assert report.mismatches == []
        assert report.classification == Ratio(16, 16)
        assert report.routing == Ratio(16, 16)
        assert report.escalation.total == 9
        assert report.escalation == Ratio(9, 9)
        assert report.priority == Ratio(16, 16)
        assert report.process_errors == 0

    def test_a_change_of_the_rules_shows_up_as_a_mismatch(self, config: AppConfig) -> None:
        """回帰テストとして働く：閾値を上げると、確信度 0.70 の自動登録が、Human Review に変わり、不一致になる。"""
        stricter_settings = config.settings.model_copy(update={"decision": DecisionSettings(confidence_threshold=0.71)})
        stricter = config.model_copy(update={"settings": stricter_settings})
        report = Evaluator(make_pipeline_factory(FakeClassifier(LLM_OUTPUTS), stricter)).evaluate(self.rows())
        mismatches = {mismatch.inquiry_id for mismatch in report.mismatches}
        assert "EVL-013" in mismatches
        assert report.routing.correct < 16


# --- CLI（結合） ---


class Cli:
    def __init__(self, capsys: pytest.CaptureFixture[str], config_dir: Path) -> None:
        self._capsys = capsys
        self.config_dir = config_dir

    def run(self, *args: str, classifier: object | None = None, with_config: bool = True) -> tuple[int, str, str]:
        argv = list(args)
        if with_config:
            argv += ["--config-dir", str(self.config_dir)]
        code = main(argv, classifier=classifier)  # type: ignore[arg-type]
        captured = self._capsys.readouterr()
        return code, captured.out, captured.err


@pytest.fixture
def cli_eval(capsys: pytest.CaptureFixture[str], config_dir: Path) -> Cli:
    return Cli(capsys, config_dir)


class TestEvalCommand:
    def dataset(self, tmp_path: Path) -> Path:
        return write_dataset(
            tmp_path,
            [
                labeled("T-1"),
                labeled("T-2", category="support", department="サポート"),
                labeled("T-3", kind="human_review", department=None),
            ],
        )

    def outcomes(self) -> FakeClassifier:
        return FakeClassifier(
            {
                "T-1": success(category=Category.BILLING, confidence=0.9),
                "T-2": success(category=Category.BILLING, confidence=0.9),
                "T-3": success(category=Category.BILLING, confidence=0.5),
            }
        )

    def test_it_prints_the_metrics_and_the_mismatches_to_standard_output(self, cli_eval: Cli, tmp_path: Path) -> None:
        code, out, err = cli_eval.run("eval", "--dataset", str(self.dataset(tmp_path)), classifier=self.outcomes())
        assert code == 0  # 不一致だけでは、エラーにしない
        assert "評価結果: 3件" in out
        assert "分類精度: 66.7%（2/3）" in out
        assert "Routing 精度: 66.7%（2/3）" in out
        assert "Escalation 妥当性: 100.0%（1/1）" in out
        assert "  2行目（問い合わせID: T-2）" in out
        assert err == ""

    def test_a_perfect_dataset_has_no_mismatch(self, cli_eval: Cli, tmp_path: Path) -> None:
        path = write_dataset(tmp_path, [labeled("T-1")])
        code, out, _ = cli_eval.run(
            "eval", "--dataset", str(path), classifier=FakeClassifier(success(category=Category.BILLING, confidence=0.9))
        )
        assert code == 0
        assert "分類精度: 100.0%（1/1）" in out
        assert "不一致: 0件" in out

    def test_an_unexpected_exception_makes_the_exit_code_one_like_run(self, cli_eval: Cli, tmp_path: Path) -> None:
        class Exploding:
            def classify(self, inquiry: Inquiry, request_id: str) -> ClassifyOutcome:
                raise RuntimeError(f"想定外 {SECRET_BODY}")

        path = write_dataset(tmp_path, [labeled("T-1", body=SECRET_BODY)])
        code, out, err = cli_eval.run("eval", "--dataset", str(path), classifier=Exploding())
        assert code == 1
        assert "評価結果: 1件" in out  # 指標と不一致は、表示する
        assert "エラー=RuntimeError" in out
        assert "処理エラー: 1件" in err
        assert SECRET_BODY not in out + err

    def test_a_ticket_failure_alone_does_not_make_the_exit_code_one(
        self, cli_eval: Cli, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """`run` と同じ：チケット登録の失敗（`ticket_failed`）は、終了コードを 1 にしない。"""
        monkeypatch.setattr(cli, "MockTicketSystem", lambda output_dir: MockTicketSystem(output_dir, fail_if=lambda request: True))
        path = write_dataset(tmp_path, [labeled("T-1")])
        code, out, _ = cli_eval.run(
            "eval", "--dataset", str(path), classifier=FakeClassifier(success(category=Category.BILLING, confidence=0.9))
        )
        assert code == 0
        assert "区分=ticket_failed" in out

    def test_an_empty_dataset_succeeds_and_every_metric_is_not_applicable(self, cli_eval: Cli, tmp_path: Path) -> None:
        path = tmp_path / "empty.jsonl"
        path.write_text("\n \n", encoding="utf-8")
        classifier = FakeClassifier({})
        code, out, err = cli_eval.run("eval", "--dataset", str(path), classifier=classifier)
        assert code == 0
        assert out.count(NOT_APPLICABLE) == 4
        assert "評価結果: 0件" in out
        assert classifier.calls == []
        assert err == ""

    def test_without_an_expected_human_review_only_escalation_is_not_applicable(self, cli_eval: Cli, tmp_path: Path) -> None:
        path = write_dataset(tmp_path, [labeled("T-1")])
        _, out, _ = cli_eval.run(
            "eval", "--dataset", str(path), classifier=FakeClassifier(success(category=Category.BILLING, confidence=0.9))
        )
        assert out.count(NOT_APPLICABLE) == 1
        assert f"Escalation 妥当性: {NOT_APPLICABLE}" in out
        assert "分類精度: 100.0%" in out

    def test_the_uppercase_extension_is_accepted(self, cli_eval: Cli, tmp_path: Path) -> None:
        path = write_dataset(tmp_path, [labeled("T-1")], name="data.JSONL")
        code, _, _ = cli_eval.run(
            "eval", "--dataset", str(path), classifier=FakeClassifier(success(category=Category.BILLING, confidence=0.9))
        )
        assert code == 0

    def test_the_config_dir_may_come_before_or_after_the_subcommand(self, cli_eval: Cli, tmp_path: Path) -> None:
        path = write_dataset(tmp_path, [labeled("T-1")])
        classifier = FakeClassifier(success(category=Category.BILLING, confidence=0.9))
        config = str(cli_eval.config_dir)
        assert cli_eval.run("--config-dir", config, "eval", "--dataset", str(path), classifier=classifier, with_config=False)[0] == 0
        assert cli_eval.run("eval", "--config-dir", config, "--dataset", str(path), classifier=classifier, with_config=False)[0] == 0
        assert cli_eval.run("eval", "--dataset", str(path), "--config-dir", config, classifier=classifier, with_config=False)[0] == 0

    def test_the_dataset_option_is_required(self) -> None:
        with pytest.raises(SystemExit) as exit_info:
            cli.build_parser().parse_args(["eval"])
        assert exit_info.value.code == 2


class TestEvalDatasetErrors:
    """REQ-015 AC-4・AC-5：データセットが不正なときは、評価を始めず（分類器を呼ばず）、終了コード 2。"""

    def assert_refused(self, result: tuple[int, str, str], classifier: FakeClassifier) -> str:
        code, out, err = result
        assert code == 2
        assert out == ""
        assert classifier.calls == []
        assert err.startswith("評価データセットを処理できません:")
        assert SECRET_BODY not in err
        assert SECRET_EMAIL not in err
        return err

    @pytest.mark.parametrize("name", ["data.csv", "data.json", "data.txt"])
    def test_an_extension_other_than_jsonl(self, cli_eval: Cli, tmp_path: Path, name: str) -> None:
        classifier = FakeClassifier({})
        path = write_dataset(tmp_path, [labeled("T-1")], name=name)
        err = self.assert_refused(cli_eval.run("eval", "--dataset", str(path), classifier=classifier), classifier)
        assert ".jsonl" in err

    def test_a_missing_file(self, cli_eval: Cli, tmp_path: Path) -> None:
        classifier = FakeClassifier({})
        err = self.assert_refused(
            cli_eval.run("eval", "--dataset", str(tmp_path / "missing.jsonl"), classifier=classifier), classifier
        )
        assert "存在しません" in err

    def test_a_file_that_is_not_utf8(self, cli_eval: Cli, tmp_path: Path) -> None:
        classifier = FakeClassifier({})
        path = tmp_path / "bad.jsonl"
        path.write_bytes(b"\xff\xfe\x00")
        err = self.assert_refused(cli_eval.run("eval", "--dataset", str(path), classifier=classifier), classifier)
        assert "UTF-8" in err

    def test_every_invalid_row_is_listed_and_nothing_is_evaluated(self, cli_eval: Cli, tmp_path: Path) -> None:
        classifier = FakeClassifier(success(category=Category.BILLING, confidence=0.9))
        path = write_dataset(
            tmp_path,
            [
                labeled("T-1", body=SECRET_BODY),
                f'{{"inquiry": "{SECRET_BODY} {SECRET_EMAIL}',
                labeled("T-2"),
                {"inquiry": {"channel": "email", "body": SECRET_BODY}, "expected": {"category": SECRET_EMAIL}},
            ],
        )
        err = self.assert_refused(cli_eval.run("eval", "--dataset", str(path), classifier=classifier), classifier)
        assert "2行目: json_invalid" in err
        assert "4行目: expected.category: enum, expected.priority: missing, expected.kind: missing" in err
        assert "1行目" not in err and "3行目" not in err

    def test_the_api_key_is_checked_first(self, cli_eval: Cli, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("OPENAI_API_KEY")
        code, out, err = cli_eval.run("eval", "--dataset", str(tmp_path / "missing.jsonl"), classifier=FakeClassifier({}))
        assert code == 2
        assert out == ""
        assert "OPENAI_API_KEY" in err

    def test_eval_needs_an_api_key_in_the_registry(self) -> None:
        assert COMMANDS["eval"].needs_api_key is True


class TestEvalDoesNotTouchTheProductionOutput:
    """REQ-015 AC-3：評価は、本番用のチケットと Human Review キューの記録を変更しない。"""

    def test_the_configured_output_directory_is_not_created_or_changed(
        self, cli_eval: Cli, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        temp_root = tmp_path / "tmp"
        temp_root.mkdir()
        monkeypatch.setattr(tempfile, "tempdir", str(temp_root))
        production = tmp_path / "out"
        assert not production.exists()
        path = write_dataset(tmp_path, [labeled("T-1"), labeled("T-2", kind="human_review", department=None)])
        classifier = FakeClassifier(success(category=Category.BILLING, confidence=0.5))
        assert cli_eval.run("eval", "--dataset", str(path), classifier=classifier)[0] == 0
        assert not production.exists()  # 一度も、作られていない
        assert list(temp_root.iterdir()) == []  # 一時ディレクトリは、片づいている

    def test_existing_production_files_are_unchanged(self, cli_eval: Cli, tmp_path: Path) -> None:
        production = tmp_path / "out"
        production.mkdir()
        for name in ("tickets.jsonl", "review_queue.jsonl", "review_resolutions.jsonl", "retry_queue.jsonl", "process_log.jsonl"):
            (production / name).write_text(f'{{"file": "{name}"}}\n', encoding="utf-8")
        before = {path.name: (path.read_bytes(), path.stat().st_mtime_ns) for path in production.iterdir()}
        path = write_dataset(tmp_path, [labeled("T-1"), labeled("T-2", kind="human_review", department=None)])
        classifier = FakeClassifier(success(category=Category.BILLING, confidence=0.5))
        cli_eval.run("eval", "--dataset", str(path), classifier=classifier)
        after = {path.name: (path.read_bytes(), path.stat().st_mtime_ns) for path in production.iterdir()}
        assert after == before


class TestEvalWriteFailureAborts:
    """ERR-007：評価の実行中の書き込みの失敗は、中断（終了コード 1）。指標も不一致も表示しない。"""

    def test_it_aborts_at_the_second_row(self, cli_eval: Cli, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(cli, "ProcessLog", FlakyProcessLog)
        classifier = FakeClassifier(success(category=Category.BILLING, confidence=0.9))
        path = write_dataset(
            tmp_path,
            [
                labeled("A", body=f"{SECRET_BODY} 1"),
                labeled("B", body=f"{SECRET_BODY} 2", subject=SECRET_EMAIL),
                labeled("C", body=f"{SECRET_BODY} 3"),
            ],
        )
        code, out, err = cli_eval.run("eval", "--dataset", str(path), classifier=classifier)
        assert code == 1
        assert out == ""  # 指標も、不一致の一覧も、表示しない
        assert "中断" in err
        assert PROCESS_LOG_FILE in err
        assert "処理を完了した件数: 1件" in err
        second_request_id = classifier.calls[1][1]
        assert second_request_id in err
        assert len(classifier.calls) == 2  # 3件目は、処理されない
        assert SECRET_BODY not in err
        assert SECRET_EMAIL not in err

    def test_the_temporary_directory_is_removed(self, cli_eval: Cli, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        temp_root = tmp_path / "tmp"
        temp_root.mkdir()
        monkeypatch.setattr(tempfile, "tempdir", str(temp_root))
        monkeypatch.setattr(cli, "ProcessLog", FlakyProcessLog)
        path = write_dataset(tmp_path, [labeled("A"), labeled("B")])
        cli_eval.run("eval", "--dataset", str(path), classifier=FakeClassifier(success(category=Category.BILLING, confidence=0.9)))
        assert list(temp_root.iterdir()) == []


class TestEvalWiring:
    """`eval` を、分類器の注入なしで実行すると、本物の分類器を1つだけ作り、全件で使う。"""

    def test_one_classifier_is_built_from_the_settings_and_used_for_every_row(
        self, cli_eval: Cli, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        built: list[dict[str, object]] = []

        class StubClassifier:
            def __init__(self, llm: object, categories: object, min_body_length: int, *, tracing: object) -> None:
                built.append({"llm": llm, "min_body_length": min_body_length, "tracing": tracing, "calls": 0})
                self.record = built[-1]

            def classify(self, inquiry: Inquiry, request_id: str) -> ClassifyOutcome:
                self.record["calls"] = int(self.record["calls"]) + 1  # type: ignore[call-overload]
                return success(category=Category.BILLING, confidence=0.9)

        monkeypatch.setattr(cli, "AgentsSdkClassifier", StubClassifier)
        path = write_dataset(tmp_path, [labeled("A"), labeled("B"), labeled("C")])
        code, out, _ = cli_eval.run("eval", "--dataset", str(path))
        assert code == 0
        assert "評価結果: 3件" in out
        (record,) = built
        assert record["calls"] == 3
        assert record["min_body_length"] == 5
