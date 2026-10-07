"""TASK-011：チケットシステム（モック）と再試行キュー（REQ-008、ERR-003、SEC-004 / CMP-010、第10章、ADR-012）。

ファイルは `tmp_path` に書く。LLM・ネットワークに依存しない。
"""

import json
import re
import socket
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest

from triage_agent.models import Category, Priority, RetryQueueEntry, TicketRecord, TicketRequest
from triage_agent.storage import JsonlStore, StorageError
from triage_agent.tickets import (
    RETRY_QUEUE_FILE,
    TICKETS_FILE,
    MockTicketSystem,
    RetryQueue,
    TicketRegistrationError,
    TicketSystem,
)

FIXED_TIME = datetime(2026, 9, 20, 12, 0, 0, tzinfo=UTC)


def make_request(**overrides: object) -> TicketRequest:
    values: dict[str, object] = {
        "category": Category.BILLING,
        "priority": Priority.MEDIUM,
        "summary": "請求書の内容についての確認依頼。",
        "department": "請求",
        "confidence": 0.92,
        "note": "副次カテゴリ: サポート",
        "inquiry_id": "INQ-abcd1234",
        "request_id": "req-1",
    }
    values.update(overrides)
    return TicketRequest.model_validate(values)


def read_lines(path: Path) -> list[dict[str, object]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def test_the_file_names_follow_the_design() -> None:
    # 出力先の名前（Design 第7.4節）。実装の定数と同じ値を比べて、名前の誤りを見逃さない。
    assert TICKETS_FILE == "tickets.jsonl"
    assert RETRY_QUEUE_FILE == "retry_queue.jsonl"


class TestCreateTicket:
    """REQ-008 AC-1・AC-2：内容を登録し、チケットIDを返す。"""

    def test_returns_a_record_with_a_ticket_id(self, tmp_path: Path) -> None:
        record = MockTicketSystem(tmp_path).create_ticket(make_request())
        assert isinstance(record, TicketRecord)
        assert re.fullmatch(r"TCK-[0-9a-f]{8}", record.ticket_id)

    def test_the_content_is_recorded_in_tickets_jsonl(self, tmp_path: Path) -> None:
        # REQ-008 AC-1：カテゴリ、優先度、要約、担当部署、確信度、副次カテゴリの備考。
        record = MockTicketSystem(tmp_path).create_ticket(make_request())
        [line] = read_lines(tmp_path / TICKETS_FILE)
        assert line["ticket_id"] == record.ticket_id
        assert line["category"] == "billing"
        assert line["priority"] == "medium"
        assert line["summary"] == "請求書の内容についての確認依頼。"
        assert line["department"] == "請求"
        assert line["confidence"] == 0.92
        assert line["note"] == "副次カテゴリ: サポート"
        assert line["inquiry_id"] == "INQ-abcd1234"
        assert line["request_id"] == "req-1"

    def test_the_record_keeps_every_field_of_the_request(self, tmp_path: Path) -> None:
        request = make_request()
        record = MockTicketSystem(tmp_path).create_ticket(request)
        assert record.model_dump(exclude={"ticket_id", "created_at"}) == request.model_dump()

    def test_the_record_can_be_read_back(self, tmp_path: Path) -> None:
        record = MockTicketSystem(tmp_path).create_ticket(make_request())
        assert JsonlStore(tmp_path / TICKETS_FILE).read_all(TicketRecord) == [record]

    def test_the_creation_time_is_recorded(self, tmp_path: Path) -> None:
        record = MockTicketSystem(tmp_path, clock=lambda: FIXED_TIME).create_ticket(make_request())
        assert record.created_at == FIXED_TIME
        assert read_lines(tmp_path / TICKETS_FILE)[0]["created_at"].startswith("2026-09-20T12:00:00")  # type: ignore[union-attr]

    def test_the_default_creation_time_is_timezone_aware(self, tmp_path: Path) -> None:
        record = MockTicketSystem(tmp_path).create_ticket(make_request())
        assert record.created_at.tzinfo is not None

    def test_a_department_of_none_is_allowed(self, tmp_path: Path) -> None:
        # 「未分類」など、担当部署が未定のチケット（REQ-007 AC-4。人間の確定の後に登録される場合）。
        record = MockTicketSystem(tmp_path).create_ticket(make_request(department=None, confidence=None, note=""))
        assert record.department is None
        assert record.confidence is None
        assert read_lines(tmp_path / TICKETS_FILE)[0]["department"] is None

    def test_tickets_are_appended_in_order_and_existing_lines_are_kept(self, tmp_path: Path) -> None:
        system = MockTicketSystem(tmp_path)
        first = system.create_ticket(make_request(summary="1件目"))
        before = (tmp_path / TICKETS_FILE).read_text(encoding="utf-8")
        second = system.create_ticket(make_request(summary="2件目"))
        after = (tmp_path / TICKETS_FILE).read_text(encoding="utf-8")
        assert after.startswith(before)
        assert [line["ticket_id"] for line in read_lines(tmp_path / TICKETS_FILE)] == [first.ticket_id, second.ticket_id]
        assert first.ticket_id != second.ticket_id

    def test_the_output_directory_is_created(self, tmp_path: Path) -> None:
        MockTicketSystem(tmp_path / "output" / "nested").create_ticket(make_request())
        assert (tmp_path / "output" / "nested" / TICKETS_FILE).is_file()

    def test_satisfies_the_ticket_system_interface(self, tmp_path: Path) -> None:
        assert isinstance(MockTicketSystem(tmp_path), TicketSystem)


class TestInjectedFailure:
    """ERR-003：登録に失敗したら、`TicketRegistrationError`。テストで失敗を再現できる。"""

    def test_a_failure_raises_ticket_registration_error(self, tmp_path: Path) -> None:
        system = MockTicketSystem(tmp_path, fail_if=lambda request: True)
        with pytest.raises(TicketRegistrationError):
            system.create_ticket(make_request())

    def test_a_failed_registration_writes_nothing(self, tmp_path: Path) -> None:
        system = MockTicketSystem(tmp_path, fail_if=lambda request: True)
        with pytest.raises(TicketRegistrationError):
            system.create_ticket(make_request())
        assert not (tmp_path / TICKETS_FILE).exists()

    def test_the_failure_can_depend_on_the_request(self, tmp_path: Path) -> None:
        system = MockTicketSystem(tmp_path, fail_if=lambda request: request.inquiry_id == "INQ-bad")
        ok = system.create_ticket(make_request(inquiry_id="INQ-good"))
        with pytest.raises(TicketRegistrationError):
            system.create_ticket(make_request(inquiry_id="INQ-bad"))
        assert [line["ticket_id"] for line in read_lines(tmp_path / TICKETS_FILE)] == [ok.ticket_id]

    def test_the_failure_can_be_switched_off_afterwards(self, tmp_path: Path) -> None:
        # 登録に失敗した後、同じチケットシステムで成功できる（Human Review の解決の再実行など）。
        system = MockTicketSystem(tmp_path, fail_if=lambda request: True)
        with pytest.raises(TicketRegistrationError):
            system.create_ticket(make_request())
        system.fail_if = None
        assert system.create_ticket(make_request()).ticket_id
        assert len(read_lines(tmp_path / TICKETS_FILE)) == 1

    def test_the_error_message_does_not_contain_the_request_content(self, tmp_path: Path) -> None:
        # エラーは、再試行キューや処理ログに残りうる。要約など、登録内容を含めない。
        secret = "090-1234-5678 taro@example.com"
        system = MockTicketSystem(tmp_path, fail_if=lambda request: True)
        with pytest.raises(TicketRegistrationError) as excinfo:
            system.create_ticket(make_request(summary=secret))
        assert secret not in str(excinfo.value)

    def test_a_write_failure_of_the_ticket_file_is_a_registration_failure(self, tmp_path: Path) -> None:
        # `tickets.jsonl` が書き込めない（ここでは、同名のディレクトリがある）。ID を返さず、失敗として扱う。
        (tmp_path / TICKETS_FILE).mkdir()
        secret = "taro@example.com"
        with pytest.raises(TicketRegistrationError) as excinfo:
            MockTicketSystem(tmp_path).create_ticket(make_request(summary=secret))
        assert secret not in str(excinfo.value)
        assert TICKETS_FILE in str(excinfo.value)


class TestRetryQueue:
    """ERR-003・DATA-007：登録に失敗した内容・エラー・時刻・発生元を、再試行キューへ記録する。"""

    def test_enqueue_records_the_content_error_time_and_source(self, tmp_path: Path) -> None:
        request = make_request()
        entry = RetryQueue(tmp_path, clock=lambda: FIXED_TIME).enqueue(request, "チケットを登録できません", "pipeline")
        assert entry.request == request
        assert entry.error == "チケットを登録できません"
        assert entry.queued_at == FIXED_TIME
        assert entry.source == "pipeline"

    def test_the_entry_is_recorded_in_retry_queue_jsonl(self, tmp_path: Path) -> None:
        request = make_request()
        RetryQueue(tmp_path, clock=lambda: FIXED_TIME).enqueue(request, "失敗の理由", "review_resolution")
        [line] = read_lines(tmp_path / RETRY_QUEUE_FILE)
        assert line["error"] == "失敗の理由"
        assert line["source"] == "review_resolution"
        assert str(line["queued_at"]).startswith("2026-09-20T12:00:00")
        assert line["request"] == json.loads(request.model_dump_json())

    def test_the_entry_can_be_read_back(self, tmp_path: Path) -> None:
        entry = RetryQueue(tmp_path).enqueue(make_request(), "失敗", "pipeline")
        assert JsonlStore(tmp_path / RETRY_QUEUE_FILE).read_all(RetryQueueEntry) == [entry]

    @pytest.mark.parametrize("source", ["pipeline", "review_resolution"])
    def test_both_sources_are_recorded(self, tmp_path: Path, source: str) -> None:
        RetryQueue(tmp_path).enqueue(make_request(), "失敗", source)  # type: ignore[arg-type]
        assert read_lines(tmp_path / RETRY_QUEUE_FILE)[0]["source"] == source

    def test_an_unknown_source_is_rejected(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError):
            RetryQueue(tmp_path).enqueue(make_request(), "失敗", "somewhere")  # type: ignore[arg-type]
        assert not (tmp_path / RETRY_QUEUE_FILE).exists()

    def test_entries_are_appended_and_the_default_time_is_timezone_aware(self, tmp_path: Path) -> None:
        queue = RetryQueue(tmp_path)
        first = queue.enqueue(make_request(summary="1件目"), "失敗1", "pipeline")
        queue.enqueue(make_request(summary="2件目"), "失敗2", "pipeline")
        lines = read_lines(tmp_path / RETRY_QUEUE_FILE)
        assert [line["error"] for line in lines] == ["失敗1", "失敗2"]
        assert first.queued_at.tzinfo is not None

    def test_a_write_failure_of_the_queue_is_not_swallowed(self, tmp_path: Path) -> None:
        # キューに書けない失敗を握りつぶすと、登録に失敗した内容が失われる（Design 第10章）。
        (tmp_path / RETRY_QUEUE_FILE).mkdir()
        with pytest.raises(StorageError):
            RetryQueue(tmp_path).enqueue(make_request(), "失敗", "pipeline")

    def test_a_failed_ticket_can_be_queued_for_retry(self, tmp_path: Path) -> None:
        # 失敗 → 再試行キュー、の流れ（Pipeline がつなぐ。ここでは部品どうしが噛み合うことを確認する）。
        request = make_request()
        system = MockTicketSystem(tmp_path, fail_if=lambda r: True)
        with pytest.raises(TicketRegistrationError) as excinfo:
            system.create_ticket(request)
        RetryQueue(tmp_path).enqueue(request, str(excinfo.value), "pipeline")
        assert not (tmp_path / TICKETS_FILE).exists()
        [entry] = JsonlStore(tmp_path / RETRY_QUEUE_FILE).read_all(RetryQueueEntry)
        assert entry.request == request


class TestNoNetwork:
    """チケットシステムは、ネットワーク通信を行わない（CMP-010）。LLM への手段も持たない（SEC-004）。"""

    def test_registration_and_queueing_work_with_every_socket_connection_forbidden(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        attempts: list[object] = []

        def forbidden(*args: object, **kwargs: object) -> None:
            attempts.append(args)
            raise RuntimeError("ネットワーク通信が試みられた")

        monkeypatch.setattr(socket.socket, "connect", forbidden)
        monkeypatch.setattr(socket.socket, "connect_ex", forbidden)
        monkeypatch.setattr(socket, "create_connection", forbidden)
        request = make_request()
        MockTicketSystem(tmp_path).create_ticket(request)
        RetryQueue(tmp_path).enqueue(request, "失敗", "pipeline")
        assert attempts == []

    def test_the_module_does_not_load_the_llm_or_http_libraries(self) -> None:
        # チケットの登録は、LLM の Tool にしない（ADR-002）。LLM・HTTP のライブラリに依存しない。別プロセスで確認する。
        code = (
            "import sys, triage_agent.tickets; "
            "loaded = [m for m in ('agents', 'openai', 'httpx', 'httpx2', 'requests', 'urllib.request', 'http.client', 'smtplib') "
            "if m in sys.modules]; "
            "print(loaded); sys.exit(1 if loaded else 0)"
        )
        completed = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=False)
        assert completed.returncode == 0, completed.stdout + completed.stderr
