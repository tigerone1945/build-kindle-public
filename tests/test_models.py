"""TASK-002：ドメインモデル（DATA-001〜007, DATA-009 / Design 第7章）の検証。"""

import json
from datetime import UTC, datetime

import pytest
from pydantic import BaseModel, ValidationError

from triage_agent.models import (
    Category,
    Channel,
    ClassificationResult,
    ClassifyOutcome,
    ClassifyStatus,
    Decision,
    ExpectedResult,
    Inquiry,
    InvalidRecord,
    LabeledInquiry,
    Language,
    MaskedInput,
    Priority,
    ProcessLogRecord,
    ProcessResult,
    ResultKind,
    RetryQueueEntry,
    ReviewItem,
    ReviewReason,
    ReviewResolution,
    ReviewValues,
    TicketRecord,
    TicketRequest,
    TriageRunContext,
)

NOW = datetime(2026, 9, 20, 9, 30, tzinfo=UTC)


def make_inquiry() -> Inquiry:
    return Inquiry(
        inquiry_id="INQ-0001",
        channel=Channel.EMAIL,
        subject="請求書について",
        body="先月の請求書の金額を確認したいです。",
        sender="taro@example.com",
        form_fields={"plan": "standard"},
        received_at=NOW,
    )


def make_classification() -> ClassificationResult:
    return ClassificationResult(
        category=Category.BILLING,
        priority=Priority.MEDIUM,
        confidence=0.9,
        summary="請求金額の確認",
        secondary_categories=[Category.SUPPORT],
        detected_language=Language.JA,
        is_complaint_or_legal=False,
        is_ambiguous=False,
        rationale="請求書の金額に関する問い合わせのため。",
    )


def make_ticket_request() -> TicketRequest:
    return TicketRequest(
        category=Category.BILLING,
        priority=Priority.MEDIUM,
        summary="請求金額の確認",
        department="請求",
        confidence=0.9,
        note="副次カテゴリ: サポート",
        inquiry_id="INQ-0001",
        request_id="req-1",
    )


def make_review_item() -> ReviewItem:
    return ReviewItem(
        review_id="REV-0001",
        inquiry=make_inquiry(),
        classification=make_classification(),
        final_priority=Priority.HIGH,
        reasons=[ReviewReason.HIGH_RISK_KEYWORD, ReviewReason.LOW_CONFIDENCE],
        high_risk_hits=["解約"],
        request_id="req-1",
        created_at=NOW,
    )


ALL_MODELS: list[BaseModel] = [
    make_inquiry(),
    make_classification(),
    ClassifyOutcome(
        status=ClassifyStatus.SUCCESS,
        classification=make_classification(),
        attempts=1,
    ),
    ClassifyOutcome(
        status=ClassifyStatus.LLM_FAILURE, attempts=3, error_detail="APITimeoutError"
    ),
    Decision(
        final_priority=Priority.HIGH,
        high_risk_hits=[],
        urgent_hits=["至急"],
        reasons=[],
        kind=ResultKind.AUTO_REGISTERED,
        department="請求",
    ),
    make_ticket_request(),
    TicketRecord(
        **make_ticket_request().model_dump(), ticket_id="TCK-0001", created_at=NOW
    ),
    make_review_item(),
    ReviewResolution(
        review_id="REV-0001",
        action="correct",
        before=ReviewValues(category=Category.BILLING, priority=Priority.MEDIUM),
        after=ReviewValues(
            category=Category.TECHNICAL, priority=Priority.HIGH, department="技術"
        ),
        ticket_id="TCK-0002",
        reviewer="tanaka",
        resolved_at=NOW,
    ),
    RetryQueueEntry(
        request=make_ticket_request(),
        error="TicketRegistrationError",
        queued_at=NOW,
        source="pipeline",
    ),
    ProcessLogRecord(
        record_type="inquiry",
        request_id="req-1",
        inquiry_id="INQ-0001",
        processed_at=NOW,
        input=MaskedInput(subject="請求書", body="連絡先は[MASKED]です", sender="[MASKED]"),
        classification=make_classification(),
        attempts=1,
        high_risk_hits=["解約"],
        urgent_hits=[],
        final_priority=Priority.HIGH,
        kind=ResultKind.HUMAN_REVIEW,
        reasons=[ReviewReason.HIGH_RISK_KEYWORD],
        review_id="REV-0001",
    ),
    ProcessResult(
        request_id="req-1",
        inquiry_id="INQ-0001",
        kind=ResultKind.AUTO_REGISTERED,
        category=Category.BILLING,
        priority=Priority.MEDIUM,
        confidence=0.9,
        department="請求",
        ticket_id="TCK-0001",
    ),
    InvalidRecord(position=3, reason="body: missing", inquiry_id="INQ-9"),
    TriageRunContext(inquiry=make_inquiry(), min_body_length=5),
    LabeledInquiry(
        inquiry=make_inquiry(),
        expected=ExpectedResult(
            category=Category.BILLING,
            priority=Priority.MEDIUM,
            kind=ResultKind.AUTO_REGISTERED,
            department="請求",
        ),
    ),
]


# --- 列挙型（Design 第7.1節） ---


@pytest.mark.parametrize(
    ("enum_type", "values"),
    [
        (
            Category,
            {"sales", "support", "billing", "technical", "complaint", "unclassified"},
        ),
        (Priority, {"high", "medium", "low"}),
        (Language, {"ja", "other"}),
        (Channel, {"email", "form"}),
        (
            ReviewReason,
            {
                "HIGH_RISK_KEYWORD",
                "COMPLAINT_OR_LEGAL",
                "UNCLASSIFIED",
                "NON_JAPANESE",
                "AMBIGUOUS_CATEGORY",
                "LOW_CONFIDENCE",
                "INPUT_GUARDRAIL",
                "LLM_FAILURE",
                "INVALID_OUTPUT",
            },
        ),
        (
            ResultKind,
            {
                "auto_registered",
                "human_review",
                "ticket_failed",
                "input_error",
                "process_error",
            },
        ),
        (
            ClassifyStatus,
            {"success", "guardrail_tripped", "llm_failure", "invalid_output"},
        ),
    ],
)
def test_enum_values_match_design(enum_type: type, values: set[str]) -> None:
    assert {member.value for member in enum_type} == values


# --- ClassificationResult（REQ-003 AC-1 / DATA-002） ---


def valid_classification_payload() -> dict[str, object]:
    return {
        "category": "billing",
        "priority": "medium",
        "confidence": 0.9,
        "summary": "請求金額の確認",
        "secondary_categories": [],
        "detected_language": "ja",
        "is_complaint_or_legal": False,
        "is_ambiguous": False,
        "rationale": "請求書に関する問い合わせ。",
    }


def test_classification_accepts_valid_payload() -> None:
    result = ClassificationResult.model_validate(valid_classification_payload())
    assert result.category is Category.BILLING


@pytest.mark.parametrize(
    ("field", "bad_value"),
    [
        ("category", "spam"),
        ("priority", "urgent"),
        ("detected_language", "en"),
        ("secondary_categories", ["spam"]),
        ("confidence", "とても高い"),
        ("is_ambiguous", "たぶん"),
    ],
)
def test_classification_rejects_disallowed_values(field: str, bad_value: object) -> None:
    payload = valid_classification_payload()
    payload[field] = bad_value
    with pytest.raises(ValidationError):
        ClassificationResult.model_validate(payload)


@pytest.mark.parametrize("field", list(valid_classification_payload()))
def test_classification_rejects_missing_field(field: str) -> None:
    payload = valid_classification_payload()
    del payload[field]
    with pytest.raises(ValidationError) as exc_info:
        ClassificationResult.model_validate(payload)
    assert [error["type"] for error in exc_info.value.errors()] == ["missing"]


def test_classification_rejects_unknown_field() -> None:
    payload = valid_classification_payload()
    payload["extra"] = "x"
    with pytest.raises(ValidationError):
        ClassificationResult.model_validate(payload)


# --- Inquiry（REQ-001 AC-3, AC-5 / DATA-001） ---


def test_inquiry_accepts_minimal_fields() -> None:
    inquiry = Inquiry(channel=Channel.FORM, body="使い方を教えてください")
    assert inquiry.inquiry_id is None
    assert inquiry.subject is None
    assert inquiry.sender is None
    assert inquiry.form_fields is None


def test_inquiry_rejects_missing_body() -> None:
    with pytest.raises(ValidationError) as exc_info:
        Inquiry.model_validate({"channel": "email"})
    errors = exc_info.value.errors()
    assert [(error["type"], error["loc"]) for error in errors] == [("missing", ("body",))]


def test_inquiry_rejects_non_string_body() -> None:
    with pytest.raises(ValidationError) as exc_info:
        Inquiry.model_validate({"channel": "email", "body": 12345})
    errors = exc_info.value.errors()
    assert [(error["type"], error["loc"]) for error in errors] == [
        ("string_type", ("body",))
    ]


def test_inquiry_rejects_unknown_channel() -> None:
    with pytest.raises(ValidationError):
        Inquiry.model_validate({"channel": "fax", "body": "本文です"})


def test_inquiry_keeps_short_body_for_the_guardrail() -> None:
    """短い本文は入力ガードレール（REQ-002）が扱うため、モデルでは拒否しない。"""
    assert Inquiry(channel=Channel.EMAIL, body="").body == ""


# --- JSON シリアライズ・復元（永続化形式 DATA-003〜007, DATA-009） ---


@pytest.mark.parametrize("model", ALL_MODELS, ids=lambda model: type(model).__name__)
def test_model_round_trips_through_json(model: BaseModel) -> None:
    dumped = model.model_dump_json()
    restored = type(model).model_validate_json(dumped)
    assert restored == model


@pytest.mark.parametrize("model", ALL_MODELS, ids=lambda model: type(model).__name__)
def test_model_serializes_to_a_single_json_line(model: BaseModel) -> None:
    """JSONL（1行1件）へ追記できる：改行を含まない。"""
    dumped = model.model_dump_json()
    assert "\n" not in dumped
    assert isinstance(json.loads(dumped), dict)


def test_enums_serialize_as_their_values() -> None:
    payload = json.loads(make_classification().model_dump_json())
    assert payload["category"] == "billing"
    assert payload["secondary_categories"] == ["support"]
    assert payload["detected_language"] == "ja"


# --- ProcessResult / ProcessLogRecord / InvalidRecord（REQ-001 AC-5, REQ-012） ---


def test_process_result_can_be_created_without_inquiry_id() -> None:
    """入力エラー（問い合わせIDを読み取れない）でも生成できる。"""
    result = ProcessResult(
        request_id="req-9",
        kind=ResultKind.INPUT_ERROR,
        position=4,
        error="body: missing",
    )
    assert result.inquiry_id is None
    assert result.position == 4
    assert result.category is None


def test_process_log_record_carries_position_for_input_errors() -> None:
    record = ProcessLogRecord(
        record_type="inquiry",
        request_id="req-9",
        processed_at=NOW,
        kind=ResultKind.INPUT_ERROR,
        error="body: missing",
        position=4,
    )
    assert record.position == 4
    assert record.input is None
    assert record.inquiry_id is None


# --- 処理ログの解決記録の項目（REQ-012 AC-2、TASK-023） ---


def _resolution_log_record(**overrides: object) -> ProcessLogRecord:
    fields: dict[str, object] = {
        "record_type": "review_resolution",
        "request_id": "req-10",
        "processed_at": NOW,
        "review_id": "REV-1",
        "ticket_id": "TCK-1",
        "action": "correct",
        "before": ReviewValues(category=Category.SUPPORT, priority=Priority.LOW, department=None),
        "after": ReviewValues(category=Category.BILLING, priority=Priority.MEDIUM, department="請求"),
    }
    fields.update(overrides)
    return ProcessLogRecord.model_validate(fields)


def test_process_log_record_has_no_resolution_fields_unless_given() -> None:
    record = ProcessLogRecord(record_type="inquiry", request_id="req-1", processed_at=NOW)
    assert record.action is None
    assert record.before is None
    assert record.after is None


def test_resolution_log_record_round_trips_through_json_with_the_three_fields() -> None:
    record = _resolution_log_record()
    restored = ProcessLogRecord.model_validate_json(record.model_dump_json())
    assert restored == record
    assert restored.action == "correct"
    assert restored.before == ReviewValues(category=Category.SUPPORT, priority=Priority.LOW, department=None)
    assert restored.after == ReviewValues(category=Category.BILLING, priority=Priority.MEDIUM, department="請求")


@pytest.mark.parametrize("action", ["approve", "correct"])
def test_resolution_log_record_accepts_both_actions(action: str) -> None:
    assert _resolution_log_record(action=action).action == action


@pytest.mark.parametrize("action", ["reject", "APPROVE", "", "修正"])
def test_resolution_log_record_rejects_other_actions(action: str) -> None:
    with pytest.raises(ValidationError):
        _resolution_log_record(action=action)


def test_resolution_log_record_keeps_the_same_action_values_as_the_resolution_record() -> None:
    """処理ログの `action` は、解決記録（`ReviewResolution.action`）と同じ値だけを受け付ける。"""
    for action in ("approve", "correct"):
        resolution = ReviewResolution(
            review_id="REV-1",
            action=action,  # type: ignore[arg-type]
            before=ReviewValues(),
            after=ReviewValues(),
            resolved_at=NOW,
        )
        assert _resolution_log_record(action=resolution.action).action == resolution.action
    with pytest.raises(ValidationError):
        ReviewResolution(
            review_id="REV-1",
            action="reject",  # type: ignore[arg-type]
            before=ReviewValues(),
            after=ReviewValues(),
            resolved_at=NOW,
        )


def test_before_may_be_empty_when_there_was_no_provisional_classification() -> None:
    """仮分類がない項目の解決では、`before` の項目は `null` になる。"""
    record = _resolution_log_record(before=ReviewValues())
    assert record.before == ReviewValues(category=None, priority=None, department=None)


def test_a_legacy_record_without_the_new_fields_is_still_readable() -> None:
    """後方互換：`action`・`before`・`after` がない従来の記録（JSON）を読み込める。"""
    legacy = (
        '{"record_type": "inquiry", "request_id": "req-1", "inquiry_id": "INQ-1", '
        '"processed_at": "2026-09-20T09:00:00Z", "kind": "auto_registered", "ticket_id": "TCK-1"}'
    )
    record = ProcessLogRecord.model_validate_json(legacy)
    assert record.kind is ResultKind.AUTO_REGISTERED
    assert record.action is None
    assert record.before is None
    assert record.after is None


def test_resolution_fields_do_not_change_the_existing_fields_and_record_types() -> None:
    record = _resolution_log_record()
    assert record.record_type == "review_resolution"
    assert record.review_id == "REV-1"
    assert record.ticket_id == "TCK-1"
    assert record.input is None
    assert record.classification is None


def test_invalid_record_does_not_require_inquiry_id() -> None:
    record = InvalidRecord(position=2, reason="body: missing")
    assert record.inquiry_id is None


def test_default_lists_are_not_shared_between_instances() -> None:
    first = ProcessResult(request_id="a", kind=ResultKind.INPUT_ERROR)
    second = ProcessResult(request_id="b", kind=ResultKind.INPUT_ERROR)
    first.reasons.append(ReviewReason.INPUT_GUARDRAIL)
    assert second.reasons == []


# --- 型の制約 ---


def test_decision_kind_only_allows_auto_registered_or_human_review() -> None:
    with pytest.raises(ValidationError):
        Decision.model_validate(
            {
                "final_priority": "low",
                "high_risk_hits": [],
                "urgent_hits": [],
                "reasons": [],
                "kind": "ticket_failed",
            }
        )


def test_review_resolution_action_is_approve_or_correct() -> None:
    with pytest.raises(ValidationError):
        ReviewResolution.model_validate(
            {
                "review_id": "REV-1",
                "action": "reject",
                "before": {},
                "after": {},
                "resolved_at": NOW.isoformat(),
            }
        )


def test_review_item_allows_missing_classification() -> None:
    """ガードレール該当・LLM 失敗では仮分類がない（null）。"""
    item = make_review_item().model_copy(update={"classification": None})
    restored = ReviewItem.model_validate_json(item.model_dump_json())
    assert restored.classification is None


def test_labeled_inquiry_parses_the_dataset_line_format() -> None:
    line = json.dumps(
        {
            "inquiry": {"channel": "email", "body": "請求書の再発行をお願いします。"},
            "expected": {
                "category": "billing",
                "priority": "low",
                "kind": "auto_registered",
                "department": "請求",
            },
        },
        ensure_ascii=False,
    )
    labeled = LabeledInquiry.model_validate_json(line)
    assert labeled.expected.kind is ResultKind.AUTO_REGISTERED
    assert labeled.inquiry.channel is Channel.EMAIL


def test_triage_run_context_holds_inquiry_and_min_length() -> None:
    context = TriageRunContext(inquiry=make_inquiry(), min_body_length=5)
    assert context.min_body_length == 5
    assert context.inquiry.body.startswith("先月")
