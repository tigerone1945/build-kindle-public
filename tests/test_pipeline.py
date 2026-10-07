"""TASK-013：Pipeline（REQ-001、REQ-006、REQ-008、REQ-009、REQ-012、ERR-003〜005、ERR-007、NFR-003、NFR-005
/ CMP-012、第5.2〜5.3節、第6章、第10章、ADR-013）。

分類器は Fake（LLM・ネットワークなし）。ファイルは `tmp_path` に書く。設定は、メモリ上で組み立てる。
ストアの書き込み失敗は、出力先のファイル名と同じ名前のディレクトリを作って、再現する（追記できない）。
"""

import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fakes import FakeClassifier, make_result

from triage_agent.classifier import Classifier
from triage_agent.config import (
    AppConfig,
    CategoryEntry,
    DecisionSettings,
    GuardrailSettings,
    Keywords,
    LlmSettings,
    PathSettings,
    Settings,
    TracingSettings,
)
from triage_agent.models import (
    Category,
    Channel,
    ClassifyOutcome,
    ClassifyStatus,
    InvalidRecord,
    Inquiry,
    Language,
    Priority,
    ProcessLogRecord,
    ProcessResult,
    ResultKind,
    ReviewItem,
    ReviewReason,
    RetryQueueEntry,
    TicketRecord,
    TicketRequest,
)
from triage_agent.pipeline import Pipeline
from triage_agent.process_log import PROCESS_LOG_FILE, ProcessLog
from triage_agent.review import REVIEW_QUEUE_FILE, ReviewQueue
from triage_agent.storage import JsonlStore, StorageError
from triage_agent.tickets import (
    RETRY_QUEUE_FILE,
    TICKETS_FILE,
    MockTicketSystem,
    RetryQueue,
    TicketSystem,
    build_ticket_note,
)

FIXED_TIME = datetime(2026, 9, 20, 12, 0, 0, tzinfo=UTC)
SENDER = "taro.yamada@example.com"
SECRET_BODY = "本文の固有の断片ZZZ"
MASTER = {
    Category.SALES: CategoryEntry(label="営業", department="営業", description="営業"),
    Category.SUPPORT: CategoryEntry(label="サポート", department="サポート", description="サポート"),
    Category.BILLING: CategoryEntry(label="請求", department="請求", description="請求"),
    Category.TECHNICAL: CategoryEntry(label="技術", department="技術", description="技術"),
    Category.COMPLAINT: CategoryEntry(label="クレーム対応", department="クレーム対応", description="苦情"),
    Category.UNCLASSIFIED: CategoryEntry(label="未分類", department=None, description="不明"),
}
CONFIG = AppConfig(
    settings=Settings(
        llm=LlmSettings(model="m", timeout_seconds=10.0, max_retries=2, retry_backoff_seconds=1.0),
        decision=DecisionSettings(confidence_threshold=0.7),
        guardrail=GuardrailSettings(min_body_length=5),
        paths=PathSettings(output_dir="output"),
        tracing=TracingSettings(enabled=True, include_sensitive_data=False),
    ),
    categories=MASTER,
    keywords=Keywords(high_risk=["解約", "返金", "訴訟", "法的措置", "個人情報"], urgent=["至急", "使えない", "障害"]),
)


def make_inquiry(**overrides: object) -> Inquiry:
    values: dict[str, object] = {
        "inquiry_id": "INQ-1",
        "channel": Channel.EMAIL,
        "subject": "請求書について",
        "body": "請求書の内容について確認したい",
        "sender": SENDER,
    }
    values.update(overrides)
    return Inquiry.model_validate(values)


def success(**overrides: object) -> ClassifyOutcome:
    return ClassifyOutcome(status=ClassifyStatus.SUCCESS, classification=make_result(**overrides), attempts=1)


def failure(status: ClassifyStatus, error_detail: str | None = None) -> ClassifyOutcome:
    attempts = 0 if status is ClassifyStatus.GUARDRAIL_TRIPPED else 3
    return ClassifyOutcome(status=status, attempts=attempts, error_detail=error_detail)


class Env:
    """Pipeline と、それが書くファイルの読み取り。"""

    def __init__(
        self,
        output_dir: Path,
        classifier: Classifier,
        *,
        fail_if: Callable[[TicketRequest], bool] | None = None,
        tickets: TicketSystem | None = None,
    ) -> None:
        self.output_dir = output_dir
        self.classifier = classifier
        self.pipeline = Pipeline(
            classifier=classifier,
            config=CONFIG,
            tickets=tickets
            if tickets is not None
            else MockTicketSystem(output_dir, fail_if=fail_if, clock=lambda: FIXED_TIME),
            retry_queue=RetryQueue(output_dir, clock=lambda: FIXED_TIME),
            review_queue=ReviewQueue(output_dir, clock=lambda: FIXED_TIME),
            process_log=ProcessLog(output_dir),
            clock=lambda: FIXED_TIME,
        )

    def tickets_written(self) -> list[TicketRecord]:
        return JsonlStore(self.output_dir / TICKETS_FILE).read_all(TicketRecord)

    def reviews(self) -> list[ReviewItem]:
        return JsonlStore(self.output_dir / REVIEW_QUEUE_FILE).read_all(ReviewItem)

    def retries(self) -> list[RetryQueueEntry]:
        return JsonlStore(self.output_dir / RETRY_QUEUE_FILE).read_all(RetryQueueEntry)

    def log(self) -> list[ProcessLogRecord]:
        return JsonlStore(self.output_dir / PROCESS_LOG_FILE).read_all(ProcessLogRecord)

    def break_store(self, file_name: str) -> None:
        """その名前のディレクトリを作り、ファイルへの追記を失敗させる。"""
        (self.output_dir / file_name).mkdir(parents=True, exist_ok=True)


def make_env(
    tmp_path: Path,
    outcome: ClassifyOutcome | None = None,
    *,
    fail_if: Callable[[TicketRequest], bool] | None = None,
) -> Env:
    return Env(tmp_path, FakeClassifier(outcome if outcome is not None else success()), fail_if=fail_if)


class RaisingClassifier:
    """呼ばれると、決められた例外を送出する。"""

    def __init__(self, error: BaseException) -> None:
        self.error = error
        self.calls = 0

    def classify(self, inquiry: Inquiry, request_id: str) -> ClassifyOutcome:
        self.calls += 1
        raise self.error


class FlakyClassifier:
    """問い合わせIDが `failing_ids` にあるものだけ、例外を送出する。それ以外は、成功の結果を返す。"""

    def __init__(self, failing_ids: set[str], error: BaseException) -> None:
        self.failing_ids = failing_ids
        self.error = error

    def classify(self, inquiry: Inquiry, request_id: str) -> ClassifyOutcome:
        if inquiry.inquiry_id in self.failing_ids:
            raise self.error
        return success()


class TestAutoRegistration:
    """分類が成功し、Human Review の理由がない：チケットを登録する（REQ-008、AC-01）。"""

    def test_a_ticket_is_registered_and_the_result_has_its_id(self, tmp_path: Path) -> None:
        env = make_env(tmp_path)
        result = env.pipeline.process(make_inquiry())
        assert result.kind is ResultKind.AUTO_REGISTERED
        (ticket,) = env.tickets_written()
        assert result.ticket_id == ticket.ticket_id
        assert result.review_id is None
        assert result.reasons == []
        assert result.error is None

    def test_the_result_fields(self, tmp_path: Path) -> None:
        env = make_env(tmp_path, success(category=Category.BILLING, confidence=0.9))
        result = env.pipeline.process(make_inquiry())
        assert result.inquiry_id == "INQ-1"
        assert result.category is Category.BILLING
        assert result.priority is Priority.MEDIUM
        assert result.confidence == 0.9
        assert result.department == "請求"

    def test_the_review_queue_and_the_retry_queue_stay_empty(self, tmp_path: Path) -> None:
        env = make_env(tmp_path)
        env.pipeline.process(make_inquiry())
        assert env.reviews() == []
        assert env.retries() == []

    def test_the_ticket_content(self, tmp_path: Path) -> None:
        env = make_env(
            tmp_path,
            success(
                category=Category.TECHNICAL,
                summary="連携でエラーが出る",
                confidence=0.85,
                secondary_categories=[Category.SUPPORT, Category.SALES],
            ),
        )
        result = env.pipeline.process(make_inquiry())
        (ticket,) = env.tickets_written()
        assert ticket.category is Category.TECHNICAL
        assert ticket.priority is Priority.MEDIUM
        assert ticket.summary == "連携でエラーが出る"
        assert ticket.department == "技術"
        assert ticket.confidence == 0.85
        assert ticket.inquiry_id == "INQ-1"
        assert ticket.request_id == result.request_id
        # 副次カテゴリは、備考に反映される（AC-09）。
        assert ticket.note == "副次カテゴリ: サポート、営業"

    def test_the_note_is_empty_without_secondary_categories(self, tmp_path: Path) -> None:
        env = make_env(tmp_path, success(secondary_categories=[]))
        env.pipeline.process(make_inquiry())
        assert env.tickets_written()[0].note == ""

    def test_an_urgent_keyword_raises_the_priority_of_the_ticket_and_the_result(self, tmp_path: Path) -> None:
        env = make_env(tmp_path, success(priority=Priority.LOW))
        result = env.pipeline.process(make_inquiry(body="至急、請求書の内容を確認したい"))
        assert result.kind is ResultKind.AUTO_REGISTERED
        assert result.priority is Priority.HIGH
        assert env.tickets_written()[0].priority is Priority.HIGH

    def test_the_threshold_itself_is_registered_automatically(self, tmp_path: Path) -> None:
        # AC-07：確信度が閾値ちょうど（0.70）なら、確信度による Human Review の対象にしない。
        env = make_env(tmp_path, success(confidence=0.7))
        assert env.pipeline.process(make_inquiry()).kind is ResultKind.AUTO_REGISTERED


class TestHumanReview:
    """Human Review の理由がある：キューへ登録し、チケットは登録しない（REQ-009、AC-02・04・06・08）。"""

    def test_a_high_risk_keyword_goes_to_the_queue_without_a_ticket(self, tmp_path: Path) -> None:
        env = make_env(tmp_path, success(confidence=0.99))
        result = env.pipeline.process(make_inquiry(body="解約したいです"))
        assert result.kind is ResultKind.HUMAN_REVIEW
        (item,) = env.reviews()
        assert result.review_id == item.review_id
        assert result.ticket_id is None
        assert env.tickets_written() == []
        assert item.high_risk_hits == ["解約"]
        assert item.request_id == result.request_id

    def test_all_the_reasons_are_recorded(self, tmp_path: Path) -> None:
        outcome = success(confidence=0.5, detected_language=Language.OTHER, is_ambiguous=True)
        env = make_env(tmp_path, outcome)
        result = env.pipeline.process(make_inquiry(body="解約したい"))
        expected = [
            ReviewReason.HIGH_RISK_KEYWORD,
            ReviewReason.NON_JAPANESE,
            ReviewReason.AMBIGUOUS_CATEGORY,
            ReviewReason.LOW_CONFIDENCE,
        ]
        assert result.reasons == expected
        assert env.reviews()[0].reasons == expected
        assert env.log()[0].reasons == expected

    def test_the_provisional_classification_is_kept(self, tmp_path: Path) -> None:
        env = make_env(tmp_path, success(category=Category.BILLING, confidence=0.5))
        result = env.pipeline.process(make_inquiry())
        assert result.category is Category.BILLING
        assert result.confidence == 0.5
        # 部署は、人間が確認するまで決めない。
        assert result.department is None
        assert env.reviews()[0].classification is not None

    def test_a_complaint_with_a_high_confidence_goes_to_review(self, tmp_path: Path) -> None:
        # AC-08。
        env = make_env(tmp_path, success(category=Category.COMPLAINT, is_complaint_or_legal=True, confidence=0.95))
        result = env.pipeline.process(make_inquiry())
        assert result.kind is ResultKind.HUMAN_REVIEW
        assert ReviewReason.COMPLAINT_OR_LEGAL in result.reasons
        assert env.tickets_written() == []

    def test_a_non_japanese_inquiry_with_a_high_confidence_goes_to_review(self, tmp_path: Path) -> None:
        # AC-04。
        env = make_env(tmp_path, success(detected_language=Language.OTHER, confidence=0.95))
        assert env.pipeline.process(make_inquiry()).reasons == [ReviewReason.NON_JAPANESE]

    def test_below_the_threshold_goes_to_review(self, tmp_path: Path) -> None:
        # AC-06。
        env = make_env(tmp_path, success(confidence=0.69))
        result = env.pipeline.process(make_inquiry())
        assert result.reasons == [ReviewReason.LOW_CONFIDENCE]
        assert env.reviews()[0].classification is not None


class TestClassificationFailures:
    """分類結果がない（入力ガードレール・LLM 失敗・形式不正）：未分類として Human Review へ（REQ-002、ERR-001、ERR-002）。"""

    CASES = [
        (ClassifyStatus.GUARDRAIL_TRIPPED, ReviewReason.INPUT_GUARDRAIL, None),
        (ClassifyStatus.LLM_FAILURE, ReviewReason.LLM_FAILURE, "APIConnectionError"),
        (ClassifyStatus.INVALID_OUTPUT, ReviewReason.INVALID_OUTPUT, "ModelRefusalError"),
    ]

    @pytest.mark.parametrize(("status", "reason", "detail"), CASES)
    def test_it_goes_to_review_as_unclassified(
        self, tmp_path: Path, status: ClassifyStatus, reason: ReviewReason, detail: str | None
    ) -> None:
        env = make_env(tmp_path, failure(status, detail))
        result = env.pipeline.process(make_inquiry())
        assert result.kind is ResultKind.HUMAN_REVIEW
        assert result.category is Category.UNCLASSIFIED
        assert result.confidence is None
        assert result.department is None
        assert result.reasons == [reason]
        assert env.tickets_written() == []
        (item,) = env.reviews()
        assert item.classification is None
        assert item.reasons == [reason]

    @pytest.mark.parametrize(("status", "reason", "detail"), CASES)
    def test_the_priority_without_a_classification_is_medium(
        self, tmp_path: Path, status: ClassifyStatus, reason: ReviewReason, detail: str | None
    ) -> None:
        # REQ-004 AC-5：緊急度キーワードがなければ「中」。
        env = make_env(tmp_path, failure(status, detail))
        assert env.pipeline.process(make_inquiry()).priority is Priority.MEDIUM
        assert env.reviews()[0].final_priority is Priority.MEDIUM

    @pytest.mark.parametrize(("status", "reason", "detail"), CASES)
    def test_an_urgent_keyword_makes_the_priority_high(
        self, tmp_path: Path, status: ClassifyStatus, reason: ReviewReason, detail: str | None
    ) -> None:
        env = make_env(tmp_path, failure(status, detail))
        result = env.pipeline.process(make_inquiry(body="至急対応をお願いします"))
        assert result.priority is Priority.HIGH
        assert env.reviews()[0].final_priority is Priority.HIGH

    @pytest.mark.parametrize(("status", "reason", "detail"), CASES)
    def test_the_failure_detail_is_in_the_result_and_the_log(
        self, tmp_path: Path, status: ClassifyStatus, reason: ReviewReason, detail: str | None
    ) -> None:
        env = make_env(tmp_path, failure(status, detail))
        result = env.pipeline.process(make_inquiry())
        assert result.error == detail
        assert env.log()[0].error == detail
        assert env.log()[0].attempts == (0 if status is ClassifyStatus.GUARDRAIL_TRIPPED else 3)

    def test_keywords_are_detected_even_when_the_classification_failed(self, tmp_path: Path) -> None:
        env = make_env(tmp_path, failure(ClassifyStatus.LLM_FAILURE, "APIConnectionError"))
        result = env.pipeline.process(make_inquiry(body="解約したい"))
        assert result.reasons == [ReviewReason.HIGH_RISK_KEYWORD, ReviewReason.LLM_FAILURE]

    def test_when_the_llm_always_fails_every_inquiry_goes_to_review_without_aborting(self, tmp_path: Path) -> None:
        # NFR-003：LLM が使えなくても、全件が Human Review へ回り、処理を途中で異常終了させない。
        env = make_env(tmp_path, failure(ClassifyStatus.LLM_FAILURE, "APITimeoutError"))
        results = [env.pipeline.process(make_inquiry(inquiry_id=f"INQ-{n}")) for n in range(5)]
        assert [r.kind for r in results] == [ResultKind.HUMAN_REVIEW] * 5
        assert len(env.reviews()) == 5
        assert len(env.log()) == 5
        assert env.tickets_written() == []


class TestTicketFailure:
    """チケット登録の失敗：再試行キューへ登録し、`ticket_failed` にする。Human Review へは入れない（ERR-003、AC-12）。"""

    def test_the_request_goes_to_the_retry_queue(self, tmp_path: Path) -> None:
        env = make_env(tmp_path, fail_if=lambda request: True)
        result = env.pipeline.process(make_inquiry())
        assert result.kind is ResultKind.TICKET_FAILED
        assert result.ticket_id is None
        assert result.review_id is None
        (entry,) = env.retries()
        assert entry.source == "pipeline"
        assert entry.request.request_id == result.request_id
        assert entry.request.summary == make_result().summary
        assert entry.error
        assert env.reviews() == []
        assert env.tickets_written() == []

    def test_the_result_says_the_registration_failed_and_was_queued(self, tmp_path: Path) -> None:
        env = make_env(tmp_path, fail_if=lambda request: True)
        result = env.pipeline.process(make_inquiry())
        assert result.error is not None
        assert "登録に失敗" in result.error
        assert "再試行キュー" in result.error
        assert result.category is Category.BILLING
        assert result.department == "請求"
        record = env.log()[0]
        assert record.kind is ResultKind.TICKET_FAILED
        assert record.error == result.error
        assert record.ticket_id is None

    def test_a_write_failure_of_the_ticket_file_is_a_ticket_failure_not_an_abort(self, tmp_path: Path) -> None:
        # チケットの登録先（モック）への書き込みの失敗は、ERR-007 ではなく、チケット登録の失敗（ERR-003）。
        env = make_env(tmp_path)
        env.break_store(TICKETS_FILE)
        result = env.pipeline.process(make_inquiry())
        assert result.kind is ResultKind.TICKET_FAILED
        assert len(env.retries()) == 1

    def test_the_next_inquiry_is_processed_after_a_ticket_failure(self, tmp_path: Path) -> None:
        env = make_env(tmp_path, fail_if=lambda request: request.inquiry_id == "INQ-1")
        first = env.pipeline.process(make_inquiry(inquiry_id="INQ-1"))
        second = env.pipeline.process(make_inquiry(inquiry_id="INQ-2"))
        assert (first.kind, second.kind) == (ResultKind.TICKET_FAILED, ResultKind.AUTO_REGISTERED)


class TestProcessLog:
    """どの区分でも、処理ログが1件追記され、同じ `request_id` が結果とログにある（REQ-012 AC-1、AC-14、NFR-005）。"""

    def scenario(self, tmp_path: Path, name: str) -> tuple[Env, ProcessResult]:
        if name == "auto_registered":
            env = make_env(tmp_path)
            return env, env.pipeline.process(make_inquiry())
        if name == "human_review":
            env = make_env(tmp_path)
            return env, env.pipeline.process(make_inquiry(body="解約したい"))
        if name == "ticket_failed":
            env = make_env(tmp_path, fail_if=lambda request: True)
            return env, env.pipeline.process(make_inquiry())
        if name == "process_error":
            env = Env(tmp_path, RaisingClassifier(RuntimeError("boom")))
            return env, env.pipeline.process(make_inquiry())
        env = make_env(tmp_path)
        return env, env.pipeline.record_invalid_input(InvalidRecord(position=3, reason="body: missing"))

    KINDS = ["auto_registered", "human_review", "ticket_failed", "process_error", "input_error"]

    @pytest.mark.parametrize("name", KINDS)
    def test_one_record_with_the_same_request_id(self, tmp_path: Path, name: str) -> None:
        env, result = self.scenario(tmp_path, name)
        assert result.kind.value == name
        (record,) = env.log()
        assert record.request_id == result.request_id
        assert record.kind is result.kind
        assert record.inquiry_id == result.inquiry_id
        assert record.processed_at == FIXED_TIME
        assert record.record_type == "inquiry"

    def test_the_record_of_an_automatic_registration(self, tmp_path: Path) -> None:
        env = make_env(tmp_path, success(category=Category.BILLING, confidence=0.9))
        result = env.pipeline.process(make_inquiry(body="至急、請求書の内容を確認したい"))
        (record,) = env.log()
        assert record.classification == make_result(category=Category.BILLING, confidence=0.9)
        assert record.attempts == 1
        assert record.urgent_hits == ["至急"]
        assert record.high_risk_hits == []
        assert record.final_priority is Priority.HIGH
        assert record.ticket_id == result.ticket_id
        assert record.review_id is None
        assert record.error is None

    def test_the_record_of_a_human_review(self, tmp_path: Path) -> None:
        env = make_env(tmp_path)
        result = env.pipeline.process(make_inquiry(body="解約したい"))
        (record,) = env.log()
        assert record.review_id == result.review_id
        assert record.ticket_id is None
        assert record.high_risk_hits == ["解約"]
        assert record.reasons == [ReviewReason.HIGH_RISK_KEYWORD]

    def test_the_input_in_the_log_is_masked(self, tmp_path: Path) -> None:
        env = make_env(tmp_path)
        inquiry = make_inquiry(subject="連絡先 taro@example.com", body="090-1234-5678 に電話をください。請求書の確認")
        env.pipeline.process(inquiry)
        (record,) = env.log()
        assert record.input is not None
        assert record.input.sender == "[EMAIL]"
        assert record.input.subject == "連絡先 [EMAIL]"
        assert "[PHONE]" in record.input.body
        raw = (tmp_path / PROCESS_LOG_FILE).read_text(encoding="utf-8")
        assert SENDER not in raw
        assert "taro@example.com" not in raw
        assert "090-1234-5678" not in raw

    def test_records_are_appended_one_per_inquiry(self, tmp_path: Path) -> None:
        env = make_env(tmp_path)
        results = [env.pipeline.process(make_inquiry(inquiry_id=f"INQ-{n}")) for n in range(3)]
        assert [record.request_id for record in env.log()] == [result.request_id for result in results]


class TestRequestId:
    def test_the_request_id_is_a_full_uuid_and_differs_per_inquiry(self, tmp_path: Path) -> None:
        env = make_env(tmp_path)
        first = env.pipeline.process(make_inquiry())
        second = env.pipeline.process(make_inquiry())
        assert uuid.UUID(first.request_id).version == 4
        assert len(first.request_id) == 36
        assert first.request_id != second.request_id

    def test_the_classifier_receives_the_same_request_id(self, tmp_path: Path) -> None:
        classifier = FakeClassifier(success())
        env = Env(tmp_path, classifier)
        result = env.pipeline.process(make_inquiry())
        assert [request_id for _, request_id in classifier.calls] == [result.request_id]

    def test_the_request_id_is_in_the_review_item_and_the_ticket(self, tmp_path: Path) -> None:
        env = make_env(tmp_path)
        review = env.pipeline.process(make_inquiry(body="解約したい"))
        ticket = env.pipeline.process(make_inquiry())
        assert env.reviews()[0].request_id == review.request_id
        assert env.tickets_written()[0].request_id == ticket.request_id


class TestUnexpectedExceptions:
    """1件の想定外の例外：`process_error` として記録し、継続する（ERR-005）。結果には例外の種類だけ（Q-10）。"""

    def test_it_becomes_a_process_error_and_the_next_inquiry_is_processed(self, tmp_path: Path) -> None:
        env = Env(tmp_path, FlakyClassifier({"INQ-1"}, RuntimeError("boom")))
        first = env.pipeline.process(make_inquiry(inquiry_id="INQ-1"))
        assert first.kind is ResultKind.PROCESS_ERROR
        assert first.inquiry_id == "INQ-1"
        second = env.pipeline.process(make_inquiry(inquiry_id="INQ-2"))
        assert second.kind is ResultKind.AUTO_REGISTERED
        assert [record.kind for record in env.log()] == [ResultKind.PROCESS_ERROR, ResultKind.AUTO_REGISTERED]

    def test_the_result_has_only_the_exception_type(self, tmp_path: Path) -> None:
        error = RuntimeError(f"失敗 {SECRET_BODY} {SENDER}")
        env = Env(tmp_path, RaisingClassifier(error))
        result = env.pipeline.process(make_inquiry(body=SECRET_BODY))
        assert result.error == "RuntimeError"
        payload = result.model_dump_json()
        assert SECRET_BODY not in payload
        assert SENDER not in payload
        assert "失敗" not in payload

    def test_the_log_has_the_type_and_the_masked_message(self, tmp_path: Path) -> None:
        error = ValueError(f"想定外の値 {SECRET_BODY} 連絡先 {SENDER}")
        env = Env(tmp_path, RaisingClassifier(error))
        result = env.pipeline.process(make_inquiry())
        (record,) = env.log()
        assert record.kind is ResultKind.PROCESS_ERROR
        assert record.request_id == result.request_id
        assert record.error is not None
        assert record.error.startswith("ValueError: 想定外の値")
        assert SECRET_BODY in record.error  # 原因の追跡のため、メッセージの全文は、処理ログに残る。
        assert "[EMAIL]" in record.error
        assert SENDER not in (tmp_path / PROCESS_LOG_FILE).read_text(encoding="utf-8")

    def test_the_input_in_the_log_is_masked_for_a_process_error(self, tmp_path: Path) -> None:
        env = Env(tmp_path, RaisingClassifier(RuntimeError("boom")))
        env.pipeline.process(make_inquiry())
        (record,) = env.log()
        assert record.input is not None
        assert record.input.sender == "[EMAIL]"
        assert SENDER not in (tmp_path / PROCESS_LOG_FILE).read_text(encoding="utf-8")

    def test_nothing_is_registered_for_a_process_error(self, tmp_path: Path) -> None:
        env = Env(tmp_path, RaisingClassifier(RuntimeError("boom")))
        env.pipeline.process(make_inquiry())
        assert env.tickets_written() == []
        assert env.reviews() == []
        assert env.retries() == []

    def test_an_unexpected_exception_from_the_ticket_system_is_a_process_error(self, tmp_path: Path) -> None:
        class BrokenTickets:
            def create_ticket(self, request: TicketRequest) -> TicketRecord:
                raise RuntimeError("チケットシステムの想定外の失敗")

        env = Env(tmp_path, FakeClassifier(success()), tickets=BrokenTickets())
        result = env.pipeline.process(make_inquiry())
        assert result.kind is ResultKind.PROCESS_ERROR
        assert env.retries() == []  # 想定外の例外は、チケット登録の失敗（ERR-003）ではない。

    def test_an_inconsistent_outcome_is_a_process_error(self, tmp_path: Path) -> None:
        # 成功なのに分類結果がない、という作り込みの誤りは、握りつぶさず、処理エラーとして記録する。
        env = make_env(tmp_path, ClassifyOutcome(status=ClassifyStatus.SUCCESS, classification=None, attempts=1))
        result = env.pipeline.process(make_inquiry())
        assert result.kind is ResultKind.PROCESS_ERROR
        assert result.error == "ValueError"

    def test_a_base_exception_is_not_caught(self, tmp_path: Path) -> None:
        env = Env(tmp_path, RaisingClassifier(KeyboardInterrupt()))
        with pytest.raises(KeyboardInterrupt):
            env.pipeline.process(make_inquiry())
        assert env.log() == []


class TestStorageFailures:
    """ログ・キューの書き込み失敗（`StorageError`）：`PROCESS_ERROR` にせず、送出する（ERR-007、ADR-013）。"""

    @pytest.mark.parametrize(
        ("file_name", "inquiry_body", "fail_ticket"),
        [
            (PROCESS_LOG_FILE, "請求書の内容について確認したい", False),
            (REVIEW_QUEUE_FILE, "解約したい", False),
            (RETRY_QUEUE_FILE, "請求書の内容について確認したい", True),
        ],
        ids=["process_log", "review_queue", "retry_queue"],
    )
    def test_process_raises_the_storage_error(
        self, tmp_path: Path, file_name: str, inquiry_body: str, fail_ticket: bool
    ) -> None:
        classifier = FakeClassifier(success())
        env = Env(tmp_path, classifier, fail_if=(lambda request: True) if fail_ticket else None)
        env.break_store(file_name)
        with pytest.raises(StorageError):
            env.pipeline.process(make_inquiry(body=inquiry_body))
        # 送出のとき、`current_request_id` が、失敗した件の `request_id` である（CLI が、中断した件を示す）。
        (_, failed_request_id) = classifier.calls[0]
        assert env.pipeline.current_request_id == failed_request_id

    def test_it_is_not_recorded_as_a_process_error(self, tmp_path: Path) -> None:
        env = make_env(tmp_path)
        env.break_store(REVIEW_QUEUE_FILE)
        with pytest.raises(StorageError):
            env.pipeline.process(make_inquiry(body="解約したい"))
        # 処理ログは書き込める状態だが、`PROCESS_ERROR` は記録されていない（隔離せずに、送出した）。
        assert env.log() == []

    def test_a_failure_while_recording_a_process_error_is_raised_too(self, tmp_path: Path) -> None:
        # `PROCESS_ERROR` の記録も、処理ログへの書き込みのため、失敗する。握りつぶさない。
        env = Env(tmp_path, RaisingClassifier(RuntimeError("boom")))
        env.break_store(PROCESS_LOG_FILE)
        with pytest.raises(StorageError):
            env.pipeline.process(make_inquiry())
        assert env.pipeline.current_request_id is not None

    def test_record_invalid_input_raises_the_storage_error_too(self, tmp_path: Path) -> None:
        env = make_env(tmp_path)
        env.break_store(PROCESS_LOG_FILE)
        with pytest.raises(StorageError):
            env.pipeline.record_invalid_input(InvalidRecord(position=2, reason="body: missing"))
        request_id = env.pipeline.current_request_id
        assert request_id is not None
        assert uuid.UUID(request_id).version == 4

    @pytest.mark.parametrize("file_name", [PROCESS_LOG_FILE, REVIEW_QUEUE_FILE])
    def test_the_error_message_has_no_inquiry_content(self, tmp_path: Path, file_name: str) -> None:
        outcome = success(summary="要約の固有の断片YYY")
        env = make_env(tmp_path, outcome)
        env.break_store(file_name)
        inquiry = make_inquiry(subject="件名の固有の断片XXX", body=f"解約したい {SECRET_BODY}")
        with pytest.raises(StorageError) as raised:
            env.pipeline.process(inquiry)
        text = str(raised.value) + repr(raised.value)
        for content in ("件名の固有の断片XXX", SECRET_BODY, SENDER, "要約の固有の断片YYY"):
            assert content not in text

    def test_a_ticket_registered_before_the_log_failure_stays_registered(self, tmp_path: Path) -> None:
        # 中断の時点で、チケットは登録済みで、処理ログが未記録の問い合わせが残りうる（R-10。許容する）。
        env = make_env(tmp_path)
        env.break_store(PROCESS_LOG_FILE)
        with pytest.raises(StorageError):
            env.pipeline.process(make_inquiry())
        assert len(env.tickets_written()) == 1


class TestRecordInvalidInput:
    """入力エラーの1件：`request_id` を採番し、処理ログへ記録する。分類・ルール・登録は行わない（ERR-004）。"""

    INVALID = InvalidRecord(position=3, reason="channel: missing, body: missing", inquiry_id="INQ-9")

    def test_the_result(self, tmp_path: Path) -> None:
        env = make_env(tmp_path)
        result = env.pipeline.record_invalid_input(self.INVALID)
        assert result.kind is ResultKind.INPUT_ERROR
        assert result.position == 3
        assert result.error == "channel: missing, body: missing"
        assert result.inquiry_id == "INQ-9"
        assert uuid.UUID(result.request_id).version == 4
        assert result.category is None
        assert result.reasons == []

    def test_the_log_has_the_position_and_the_reason_with_the_same_request_id(self, tmp_path: Path) -> None:
        env = make_env(tmp_path)
        result = env.pipeline.record_invalid_input(self.INVALID)
        (record,) = env.log()
        assert record.request_id == result.request_id
        assert record.kind is ResultKind.INPUT_ERROR
        assert record.position == 3
        assert record.error == "channel: missing, body: missing"
        assert record.inquiry_id == "INQ-9"

    def test_the_raw_input_is_not_recorded(self, tmp_path: Path) -> None:
        env = make_env(tmp_path)
        env.pipeline.record_invalid_input(InvalidRecord(position=1, reason="body: string_type"))
        (record,) = env.log()
        assert record.input is None
        assert record.classification is None

    def test_nothing_else_is_called_or_written(self, tmp_path: Path) -> None:
        classifier = FakeClassifier(success())
        env = Env(tmp_path, classifier)
        env.pipeline.record_invalid_input(self.INVALID)
        assert classifier.calls == []
        for file_name in (TICKETS_FILE, REVIEW_QUEUE_FILE, RETRY_QUEUE_FILE):
            assert not (tmp_path / file_name).exists()


class TestCurrentRequestId:
    def test_it_is_none_before_and_after_processing(self, tmp_path: Path) -> None:
        env = make_env(tmp_path)
        assert env.pipeline.current_request_id is None
        env.pipeline.process(make_inquiry())
        assert env.pipeline.current_request_id is None
        env.pipeline.record_invalid_input(InvalidRecord(position=1, reason="json_invalid"))
        assert env.pipeline.current_request_id is None

    def test_it_is_set_from_the_numbering_while_the_inquiry_is_processed(self, tmp_path: Path) -> None:
        seen: list[tuple[str | None, str]] = []

        class SpyClassifier:
            def classify(self, inquiry: Inquiry, request_id: str) -> ClassifyOutcome:
                seen.append((holder["pipeline"].current_request_id, request_id))
                return success()

        holder: dict[str, Pipeline] = {}
        env = Env(tmp_path, SpyClassifier())
        holder["pipeline"] = env.pipeline
        env.pipeline.process(make_inquiry())
        assert len(seen) == 1
        assert seen[0][0] == seen[0][1]

    def test_it_is_none_after_a_handled_process_error(self, tmp_path: Path) -> None:
        env = Env(tmp_path, RaisingClassifier(RuntimeError("boom")))
        env.pipeline.process(make_inquiry())
        assert env.pipeline.current_request_id is None


class TestBuildTicketNote:
    def test_labels_of_the_secondary_categories(self) -> None:
        assert build_ticket_note([Category.BILLING], MASTER) == "副次カテゴリ: 請求"
        assert build_ticket_note([Category.BILLING, Category.TECHNICAL], MASTER) == "副次カテゴリ: 請求、技術"

    def test_it_is_empty_without_secondary_categories(self) -> None:
        assert build_ticket_note([], MASTER) == ""
