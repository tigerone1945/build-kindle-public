"""TASK-006：処理ログ（REQ-012 AC-3・AC-4、SEC-002 / CMP-013、CMP-008）。"""

from datetime import UTC, datetime
from pathlib import Path

from triage_agent.models import (
    Category,
    ClassificationResult,
    Language,
    MaskedInput,
    Priority,
    ProcessLogRecord,
    ResultKind,
    ReviewReason,
    ReviewValues,
)
from triage_agent.process_log import PROCESS_LOG_FILE, ProcessLog
from triage_agent.storage import JsonlStore

PROCESSED_AT = datetime(2026, 9, 20, 9, 0, tzinfo=UTC)


def _classification() -> ClassificationResult:
    return ClassificationResult(
        category=Category.BILLING,
        priority=Priority.MEDIUM,
        confidence=0.9,
        summary="請求書の再発行の依頼",
        secondary_categories=[],
        detected_language=Language.JA,
        is_complaint_or_legal=False,
        is_ambiguous=False,
        rationale="請求書について尋ねている",
    )


def _inquiry_record(**overrides: object) -> ProcessLogRecord:
    fields: dict[str, object] = {
        "record_type": "inquiry",
        "request_id": "REQ-1",
        "inquiry_id": "INQ-1",
        "processed_at": PROCESSED_AT,
        "input": MaskedInput(subject="請求書の件", body="請求書番号12345を再発行してください", sender="山田 <taro@example.com>"),
        "classification": _classification(),
        "attempts": 1,
        "final_priority": Priority.MEDIUM,
        "kind": ResultKind.AUTO_REGISTERED,
        "ticket_id": "TCK-1",
    }
    fields.update(overrides)
    return ProcessLogRecord.model_validate(fields)


def _read(log_dir: Path) -> list[ProcessLogRecord]:
    return JsonlStore(log_dir / PROCESS_LOG_FILE).read_all(ProcessLogRecord)


def test_log_file_is_process_log_jsonl_in_the_output_directory(tmp_path: Path) -> None:
    ProcessLog(tmp_path).append(_inquiry_record())

    assert PROCESS_LOG_FILE == "process_log.jsonl"
    assert (tmp_path / "process_log.jsonl").is_file()


def test_output_directory_is_created_when_missing(tmp_path: Path) -> None:
    log_dir = tmp_path / "output" / "nested"

    ProcessLog(log_dir).append(_inquiry_record())

    assert len(_read(log_dir)) == 1


# --- マスキング（SEC-002、REQ-012 AC-4） ---


def test_email_and_phone_are_masked_in_subject_body_sender_and_error(tmp_path: Path) -> None:
    record = _inquiry_record(
        input=MaskedInput(
            subject="taro@example.com からの連絡",
            body="折り返しは 090-1234-5678 か +81 90 1234 5678 へ。メールは taro.yamada@example.co.jp まで",
            sender="山田 <taro@example.com>",
        ),
        error="通信エラー（送信先: taro@example.com、電話 03-1234-5678）",
    )

    ProcessLog(tmp_path).append(record)

    raw_log = (tmp_path / PROCESS_LOG_FILE).read_text(encoding="utf-8")
    for personal in ("taro@example.com", "taro.yamada@example.co.jp", "090-1234-5678", "1234 5678", "03-1234-5678"):
        assert personal not in raw_log
    (saved,) = _read(tmp_path)
    assert saved.input == MaskedInput(
        subject="[EMAIL] からの連絡",
        body="折り返しは [PHONE] か [PHONE] へ。メールは [EMAIL] まで",
        sender="山田 <[EMAIL]>",
    )
    assert saved.error == "通信エラー（送信先: [EMAIL]、電話 [PHONE]）"


def test_masking_is_applied_by_the_log_regardless_of_the_caller(tmp_path: Path) -> None:
    # 呼び出し側が、生の入力を渡しても（型名は `MaskedInput` でも、中身は生）、ログには生で残らない。
    raw_body = "連絡先 taro@example.com"

    ProcessLog(tmp_path).append(_inquiry_record(input=MaskedInput(body=raw_body)))

    assert "taro@example.com" not in (tmp_path / PROCESS_LOG_FILE).read_text(encoding="utf-8")


def test_numbers_that_are_not_phone_numbers_are_kept(tmp_path: Path) -> None:
    ProcessLog(tmp_path).append(_inquiry_record(input=MaskedInput(subject="請求書番号12345", body="金額は10,000円です")))

    (saved,) = _read(tmp_path)
    assert saved.input == MaskedInput(subject="請求書番号12345", body="金額は10,000円です")


def test_text_without_personal_information_is_unchanged(tmp_path: Path) -> None:
    original = _inquiry_record(input=MaskedInput(subject="件名", body="本文です", sender=None), error="タイムアウト")

    ProcessLog(tmp_path).append(original)

    (saved,) = _read(tmp_path)
    assert saved == original


def test_append_returns_the_masked_record_and_does_not_change_the_given_one(tmp_path: Path) -> None:
    record = _inquiry_record(input=MaskedInput(body="連絡先 taro@example.com"), error="失敗 090-1234-5678")

    returned = ProcessLog(tmp_path).append(record)

    assert returned.input == MaskedInput(body="連絡先 [EMAIL]")
    assert returned.error == "失敗 [PHONE]"
    assert record.input == MaskedInput(body="連絡先 taro@example.com")  # 呼び出し側の値は、そのまま
    assert record.error == "失敗 090-1234-5678"
    assert _read(tmp_path) == [returned]


def test_fields_other_than_input_and_error_are_recorded_as_given(tmp_path: Path) -> None:
    record = _inquiry_record(
        input=MaskedInput(subject="件名", body="解約したい"),  # 個人情報を含まない入力（マスキングで変わらない）
        high_risk_hits=["解約"],
        urgent_hits=["至急"],
        final_priority=Priority.HIGH,
        kind=ResultKind.HUMAN_REVIEW,
        reasons=[ReviewReason.HIGH_RISK_KEYWORD, ReviewReason.LOW_CONFIDENCE],
        ticket_id=None,
        review_id="REV-1",
    )

    ProcessLog(tmp_path).append(record)

    (saved,) = _read(tmp_path)
    assert saved == record


# --- 記録の種類（inquiry / review_resolution / 入力エラー） ---


def test_review_resolution_record_is_appended_to_the_same_log(tmp_path: Path) -> None:
    log = ProcessLog(tmp_path)
    resolution = ProcessLogRecord(
        record_type="review_resolution",
        request_id="REQ-2",
        processed_at=PROCESSED_AT,
        review_id="REV-1",
        ticket_id="TCK-9",
        error="失敗 taro@example.com",
    )

    log.append(_inquiry_record())
    log.append(resolution)

    inquiry_saved, resolution_saved = _read(tmp_path)
    assert inquiry_saved.record_type == "inquiry"
    assert resolution_saved.record_type == "review_resolution"
    assert resolution_saved.input is None
    assert resolution_saved.review_id == "REV-1"
    assert resolution_saved.ticket_id == "TCK-9"
    assert resolution_saved.error == "失敗 [EMAIL]"


def _resolution_record(**overrides: object) -> ProcessLogRecord:
    fields: dict[str, object] = {
        "record_type": "review_resolution",
        "request_id": "REQ-2",
        "processed_at": PROCESSED_AT,
        "review_id": "REV-1",
        "ticket_id": "TCK-9",
        "action": "correct",
        "before": ReviewValues(category=Category.SUPPORT, priority=Priority.LOW),
        "after": ReviewValues(category=Category.BILLING, priority=Priority.MEDIUM, department="請求"),
    }
    fields.update(overrides)
    return ProcessLogRecord.model_validate(fields)


def test_resolution_fields_are_appended_unchanged_and_read_back(tmp_path: Path) -> None:
    """REQ-012 AC-2：承認か修正か、修正前後の値が、そのまま記録され、読み戻せる。"""
    record = _resolution_record()

    returned = ProcessLog(tmp_path).append(record)

    (saved,) = _read(tmp_path)
    assert saved == record
    assert returned == record
    assert saved.action == "correct"
    assert saved.before == ReviewValues(category=Category.SUPPORT, priority=Priority.LOW)
    assert saved.after == ReviewValues(category=Category.BILLING, priority=Priority.MEDIUM, department="請求")


def test_an_approval_record_keeps_action_approve(tmp_path: Path) -> None:
    values = ReviewValues(category=Category.BILLING, priority=Priority.MEDIUM, department="請求")
    ProcessLog(tmp_path).append(_resolution_record(action="approve", before=values, after=values))

    (saved,) = _read(tmp_path)
    assert saved.action == "approve"
    assert saved.before == saved.after == values


def test_the_error_of_a_resolution_record_is_still_masked_and_its_fields_are_not(tmp_path: Path) -> None:
    """同じ記録の `error` は従来どおりマスキングされ、追加の3項目（値のみ）は、変わらない。"""
    record = _resolution_record(error="失敗 taro@example.com")

    ProcessLog(tmp_path).append(record)

    (saved,) = _read(tmp_path)
    assert saved.error == "失敗 [EMAIL]"
    assert saved.action == record.action
    assert saved.before == record.before
    assert saved.after == record.after


def test_an_inquiry_record_has_no_resolution_fields(tmp_path: Path) -> None:
    ProcessLog(tmp_path).append(_inquiry_record())

    (saved,) = _read(tmp_path)
    assert saved.action is None
    assert saved.before is None
    assert saved.after is None


def test_records_written_before_the_fields_existed_are_still_read_next_to_new_ones(tmp_path: Path) -> None:
    """後方互換：追加項目のない従来の行と、新しい行が、同じファイルにあっても、読み込める。"""
    legacy_line = (
        '{"record_type": "inquiry", "request_id": "REQ-0", "inquiry_id": "INQ-0", '
        '"processed_at": "2026-09-20T08:00:00Z", "kind": "auto_registered", "ticket_id": "TCK-0"}'
    )
    (tmp_path / PROCESS_LOG_FILE).write_text(legacy_line + "\n", encoding="utf-8")

    ProcessLog(tmp_path).append(_resolution_record())

    legacy, new = _read(tmp_path)
    assert legacy.request_id == "REQ-0"
    assert legacy.action is None
    assert new.action == "correct"


def test_input_error_record_has_a_position_and_no_input(tmp_path: Path) -> None:
    record = ProcessLogRecord(
        record_type="inquiry",
        request_id="REQ-3",
        processed_at=PROCESSED_AT,
        kind=ResultKind.INPUT_ERROR,
        error="body: missing",
        position=4,
    )

    ProcessLog(tmp_path).append(record)

    (saved,) = _read(tmp_path)
    assert saved.kind == ResultKind.INPUT_ERROR
    assert saved.position == 4
    assert saved.input is None
    assert saved.inquiry_id is None
    assert saved.error == "body: missing"


# --- 追記のみ（REQ-012 AC-3） ---


def test_records_are_appended_in_order_and_existing_lines_are_not_changed(tmp_path: Path) -> None:
    log = ProcessLog(tmp_path)
    log_file = tmp_path / PROCESS_LOG_FILE

    log.append(_inquiry_record(request_id="REQ-1"))
    first = log_file.read_bytes()
    log.append(_inquiry_record(request_id="REQ-2"))
    second = log_file.read_bytes()
    log.append(_inquiry_record(request_id="REQ-3"))
    third = log_file.read_bytes()

    assert second.startswith(first)
    assert third.startswith(second)
    assert [r.request_id for r in _read(tmp_path)] == ["REQ-1", "REQ-2", "REQ-3"]
    assert len(third.decode("utf-8").splitlines()) == 3


def test_a_new_process_log_on_the_same_directory_keeps_appending(tmp_path: Path) -> None:
    ProcessLog(tmp_path).append(_inquiry_record(request_id="REQ-1"))
    ProcessLog(tmp_path).append(_inquiry_record(request_id="REQ-2"))

    assert [r.request_id for r in _read(tmp_path)] == ["REQ-1", "REQ-2"]
