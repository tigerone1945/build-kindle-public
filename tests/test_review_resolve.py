"""TASK-016：Human Review の解決（REQ-010、REQ-012 AC-2、ERR-007 / CMP-011、CMP-001、第5.4節、第8.1節、ADR-013）。

前半は `ReviewResolver` の単体テスト、後半は `main(["review", ...])` の結合テスト。
LLM・ネットワークは使わない（`review` は API Key も要らない）。ファイルは `tmp_path` に書く。
書き込みの失敗は、ファイルと同名のディレクトリを作る（読み込みは成功したまま、追記だけが失敗する場合）か、
出力先のディレクトリへ、新しいファイルを作れないようにして再現する。
"""

import json
import os
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fakes import make_result

from triage_agent.cli import COMMANDS, build_parser, main
from triage_agent.config import AppConfig, load_config
from triage_agent.models import (
    Category,
    Channel,
    ClassificationResult,
    Decision,
    Inquiry,
    Priority,
    ProcessLogRecord,
    ResultKind,
    ReviewItem,
    ReviewReason,
    ReviewResolution,
    ReviewValues,
    RetryQueueEntry,
    TicketRecord,
    TicketRequest,
)
from triage_agent.process_log import PROCESS_LOG_FILE, ProcessLog
from triage_agent.review import (
    REVIEW_QUEUE_FILE,
    REVIEW_RESOLUTIONS_FILE,
    ReviewAlreadyResolvedError,
    ReviewNotFoundError,
    ReviewQueue,
    ReviewResolutionError,
    ReviewResolver,
)
from triage_agent.storage import JsonlStore, StorageError
from triage_agent.tickets import (
    RETRY_QUEUE_FILE,
    TICKETS_FILE,
    MockTicketSystem,
    RetryQueue,
    TicketRegistrationError,
)

FIXED_TIME = datetime(2026, 9, 21, 12, 0, 0, tzinfo=UTC)
SECRET_BODY = "本文の固有の断片ZZZ"
SENDER = "taro.yamada@example.com"
MASTER_DEPARTMENTS = ["営業", "サポート", "請求", "技術", "クレーム対応"]


def make_inquiry(**overrides: object) -> Inquiry:
    values: dict[str, object] = {
        "inquiry_id": "INQ-abcd1234",
        "channel": Channel.EMAIL,
        "subject": "確認したい件",
        "body": f"{SECRET_BODY}。確認してください。",
        "sender": SENDER,
    }
    values.update(overrides)
    return Inquiry.model_validate(values)


def make_decision(**overrides: object) -> Decision:
    values: dict[str, object] = {
        "final_priority": Priority.MEDIUM,
        "high_risk_hits": [],
        "urgent_hits": [],
        "reasons": [ReviewReason.LOW_CONFIDENCE],
        "kind": ResultKind.HUMAN_REVIEW,
    }
    values.update(overrides)
    return Decision.model_validate(values)


def flag(
    queue: ReviewQueue,
    *,
    classification: ClassificationResult | None | str = "default",
    inquiry: Inquiry | None = None,
    decision: Decision | None = None,
    request_id: str = "req-1",
) -> ReviewItem:
    """Human Review 項目を登録する。`classification` を省略すると、billing・中・確信度 0.5（低確信度）の仮分類がつく。"""
    provisional = make_result(confidence=0.5) if classification == "default" else classification
    return queue.flag_for_review(
        inquiry=inquiry if inquiry is not None else make_inquiry(),
        classification=provisional,  # type: ignore[arg-type]
        decision=decision if decision is not None else make_decision(),
        request_id=request_id,
    )


class Env:
    """出力先・解決の部品・書かれたファイルの読み取り。"""

    def __init__(self, output_dir: Path, config: AppConfig, *, fail_if: Callable[[TicketRequest], bool] | None = None) -> None:
        self.output_dir = output_dir
        self.queue = ReviewQueue(output_dir, clock=lambda: FIXED_TIME)
        self.ticket_system = MockTicketSystem(output_dir, fail_if=fail_if, clock=lambda: FIXED_TIME)
        self.resolver = ReviewResolver(
            queue=self.queue,
            tickets=self.ticket_system,
            retry_queue=RetryQueue(output_dir, clock=lambda: FIXED_TIME),
            process_log=ProcessLog(output_dir),
            master=config.categories,
            clock=lambda: FIXED_TIME,
        )

    def tickets(self) -> list[TicketRecord]:
        return JsonlStore(self.output_dir / TICKETS_FILE).read_all(TicketRecord)

    def resolutions(self) -> list[ReviewResolution]:
        return JsonlStore(self.output_dir / REVIEW_RESOLUTIONS_FILE).read_all(ReviewResolution)

    def retries(self) -> list[RetryQueueEntry]:
        return JsonlStore(self.output_dir / RETRY_QUEUE_FILE).read_all(RetryQueueEntry)

    def log(self) -> list[ProcessLogRecord]:
        return JsonlStore(self.output_dir / PROCESS_LOG_FILE).read_all(ProcessLogRecord)

    def pending_ids(self) -> list[str]:
        return [item.review_id for item in self.queue.list_pending()]

    def assert_nothing_recorded(self) -> None:
        """チケット・解決記録・再試行キュー・処理ログのいずれにも、何も書かれていない。"""
        assert self.tickets() == []
        assert self.resolutions() == []
        assert self.retries() == []
        assert self.log() == []


@pytest.fixture
def config(config_dir: Path) -> AppConfig:
    return load_config(config_dir)


@pytest.fixture
def env(tmp_path: Path, config: AppConfig) -> Env:
    return Env(tmp_path / "out", config)


def deny_new_files(directory: Path, request: pytest.FixtureRequest) -> None:
    """ディレクトリに、新しいファイルを作れないようにする（既存のファイルへの追記はできる）。テスト後に戻す。"""
    if os.geteuid() == 0:
        pytest.skip("root では、ファイルの作成を拒否できない（権限の制限が効かない）")
    os.chmod(directory, 0o555)
    request.addfinalizer(lambda: os.chmod(directory, 0o755))


class TestApprove:
    """REQ-010 AC-2・AC-4：承認は、仮分類の内容でチケットを登録し、項目を対応済みにする。"""

    def test_the_ticket_is_registered_with_the_provisional_values(self, env: Env) -> None:
        item = flag(env.queue)
        resolution = env.resolver.resolve(item.review_id)
        (ticket,) = env.tickets()
        assert ticket.ticket_id == resolution.ticket_id
        assert ticket.category is Category.BILLING
        assert ticket.priority is Priority.MEDIUM
        assert ticket.department == "請求"
        assert ticket.summary == "請求書の内容についての確認依頼。"
        assert ticket.confidence == 0.5
        assert ticket.inquiry_id == "INQ-abcd1234"
        assert ticket.request_id == "req-1"

    def test_the_item_leaves_the_pending_list(self, env: Env) -> None:
        first = flag(env.queue)
        second = flag(env.queue)
        env.resolver.resolve(first.review_id)
        assert env.pending_ids() == [second.review_id]

    def test_the_resolution_record_is_an_approval_without_differences(self, env: Env) -> None:
        item = flag(env.queue)
        resolution = env.resolver.resolve(item.review_id, reviewer="佐藤")
        (saved,) = env.resolutions()
        assert saved == resolution
        assert saved.review_id == item.review_id
        assert saved.action == "approve"
        assert saved.before == saved.after
        assert saved.after == ReviewValues(category=Category.BILLING, priority=Priority.MEDIUM, department="請求")
        assert saved.reviewer == "佐藤"
        assert saved.resolved_at == FIXED_TIME

    def test_the_reviewer_is_optional(self, env: Env) -> None:
        assert env.resolver.resolve(flag(env.queue).review_id).reviewer is None

    def test_a_complaint_is_registered_to_the_complaint_department(self, env: Env) -> None:
        item = flag(env.queue, classification=make_result(category=Category.COMPLAINT, confidence=0.95))
        env.resolver.resolve(item.review_id)
        (ticket,) = env.tickets()
        assert ticket.category is Category.COMPLAINT
        assert ticket.department == "クレーム対応"

    def test_the_priority_is_the_recorded_final_priority_including_the_urgent_raise(self, env: Env) -> None:
        """緊急度キーワードによる「高」は、承認しても下がらない（BR-003）。"""
        item = flag(
            env.queue,
            classification=make_result(priority=Priority.LOW),
            decision=make_decision(final_priority=Priority.HIGH, urgent_hits=["至急"]),
        )
        resolution = env.resolver.resolve(item.review_id)
        (ticket,) = env.tickets()
        assert ticket.priority is Priority.HIGH
        assert resolution.before.priority is Priority.HIGH
        assert resolution.action == "approve"

    def test_secondary_categories_are_written_to_the_note(self, env: Env) -> None:
        item = flag(env.queue, classification=make_result(secondary_categories=[Category.TECHNICAL, Category.SALES]))
        env.resolver.resolve(item.review_id)
        (ticket,) = env.tickets()
        assert ticket.note == "副次カテゴリ: 技術、営業"

    def test_no_note_without_secondary_categories(self, env: Env) -> None:
        env.resolver.resolve(flag(env.queue).review_id)
        (ticket,) = env.tickets()
        assert ticket.note == ""

    def test_the_process_log_gets_a_resolution_record(self, env: Env) -> None:
        """REQ-012 AC-2：レビューID・処理時刻・承認か修正か・修正前後の値・チケットIDが、処理ログへ記録される。"""
        item = flag(env.queue)
        resolution = env.resolver.resolve(item.review_id)
        (record,) = env.log()
        assert record.record_type == "review_resolution"
        assert record.review_id == item.review_id
        assert record.ticket_id == resolution.ticket_id
        assert record.processed_at == FIXED_TIME
        assert record.action == "approve"
        assert record.before == resolution.before
        assert record.after == resolution.after
        assert record.request_id == "req-1"
        assert record.inquiry_id == "INQ-abcd1234"

    def test_the_process_log_holds_no_inquiry_content(self, env: Env) -> None:
        env.resolver.resolve(flag(env.queue).review_id)
        text = (env.output_dir / PROCESS_LOG_FILE).read_text(encoding="utf-8")
        assert SECRET_BODY not in text
        assert SENDER not in text
        assert "確認したい件" not in text

    def test_the_resolution_record_holds_no_inquiry_content(self, env: Env) -> None:
        env.resolver.resolve(flag(env.queue).review_id)
        text = (env.output_dir / REVIEW_RESOLUTIONS_FILE).read_text(encoding="utf-8")
        assert SECRET_BODY not in text
        assert SENDER not in text


class TestCorrect:
    """REQ-010 AC-3・AC-4：修正は、修正後の内容でチケットを登録し、修正前後の差分を記録する。"""

    def test_a_full_correction_is_registered_and_the_difference_is_recorded(self, env: Env) -> None:
        item = flag(env.queue, classification=make_result(category=Category.SUPPORT, priority=Priority.LOW))
        resolution = env.resolver.resolve(
            item.review_id, ReviewValues(category=Category.TECHNICAL, priority=Priority.HIGH, department="営業")
        )
        (ticket,) = env.tickets()
        assert (ticket.category, ticket.priority, ticket.department) == (Category.TECHNICAL, Priority.HIGH, "営業")
        assert resolution.action == "correct"
        assert resolution.before == ReviewValues(category=Category.SUPPORT, priority=Priority.MEDIUM, department="サポート")
        assert resolution.after == ReviewValues(category=Category.TECHNICAL, priority=Priority.HIGH, department="営業")
        (saved,) = env.resolutions()
        assert saved == resolution

    def test_without_a_department_the_department_of_the_corrected_category_is_used(self, env: Env) -> None:
        item = flag(env.queue)
        resolution = env.resolver.resolve(item.review_id, ReviewValues(category=Category.TECHNICAL))
        assert resolution.after.department == "技術"
        (ticket,) = env.tickets()
        assert ticket.department == "技術"

    def test_a_partial_correction_keeps_the_other_provisional_values(self, env: Env) -> None:
        item = flag(env.queue)
        resolution = env.resolver.resolve(item.review_id, ReviewValues(priority=Priority.HIGH))
        assert resolution.action == "correct"
        assert resolution.after == ReviewValues(category=Category.BILLING, priority=Priority.HIGH, department="請求")
        assert resolution.before == ReviewValues(category=Category.BILLING, priority=Priority.MEDIUM, department="請求")

    def test_a_department_only_correction_may_differ_from_the_category_department(self, env: Env) -> None:
        """担当部署は、確定したカテゴリの部署でなくても、マスタの部署なら指定できる（REQ-010 AC-9）。"""
        item = flag(env.queue)
        resolution = env.resolver.resolve(item.review_id, ReviewValues(department="営業"))
        assert resolution.after == ReviewValues(category=Category.BILLING, priority=Priority.MEDIUM, department="営業")
        assert resolution.before.department == "請求"
        (ticket,) = env.tickets()
        assert (ticket.category, ticket.department) == (Category.BILLING, "営業")

    def test_a_correction_equal_to_the_provisional_values_is_still_a_correction(self, env: Env) -> None:
        """修正の指定が1つでもあれば「修正」（承認ではない）。"""
        item = flag(env.queue)
        resolution = env.resolver.resolve(item.review_id, ReviewValues(category=Category.BILLING))
        assert resolution.action == "correct"
        assert resolution.before == resolution.after

    def test_the_corrected_category_is_not_repeated_in_the_note(self, env: Env) -> None:
        item = flag(env.queue, classification=make_result(secondary_categories=[Category.TECHNICAL]))
        env.resolver.resolve(item.review_id, ReviewValues(category=Category.TECHNICAL))
        (ticket,) = env.tickets()
        assert ticket.note == ""

    def test_the_process_log_records_the_correction(self, env: Env) -> None:
        item = flag(env.queue)
        resolution = env.resolver.resolve(item.review_id, ReviewValues(category=Category.SALES))
        (record,) = env.log()
        assert record.action == "correct"
        assert record.before == resolution.before
        assert record.after == resolution.after
        assert record.after == ReviewValues(category=Category.SALES, priority=Priority.MEDIUM, department="営業")


class TestWithoutAUsableProvisionalClassification:
    """REQ-010 AC-5：仮分類がない、またはカテゴリが「未分類」の項目は、カテゴリの指定なしでは確定できない。"""

    def test_an_item_without_a_classification_cannot_be_approved(self, env: Env) -> None:
        item = flag(env.queue, classification=None, decision=make_decision(reasons=[ReviewReason.LLM_FAILURE]))
        with pytest.raises(ReviewResolutionError):
            env.resolver.resolve(item.review_id)
        env.assert_nothing_recorded()
        assert env.pending_ids() == [item.review_id]

    def test_an_unclassified_provisional_category_cannot_be_approved(self, env: Env) -> None:
        item = flag(env.queue, classification=make_result(category=Category.UNCLASSIFIED, confidence=0.2))
        with pytest.raises(ReviewResolutionError):
            env.resolver.resolve(item.review_id)
        env.assert_nothing_recorded()

    def test_a_priority_only_correction_does_not_supply_the_category(self, env: Env) -> None:
        item = flag(env.queue, classification=None, decision=make_decision(reasons=[ReviewReason.INPUT_GUARDRAIL]))
        with pytest.raises(ReviewResolutionError):
            env.resolver.resolve(item.review_id, ReviewValues(priority=Priority.HIGH))
        env.assert_nothing_recorded()

    def test_specifying_unclassified_is_still_an_error(self, env: Env) -> None:
        item = flag(env.queue)
        with pytest.raises(ReviewResolutionError):
            env.resolver.resolve(item.review_id, ReviewValues(category=Category.UNCLASSIFIED))
        env.assert_nothing_recorded()

    def test_specifying_the_category_makes_it_resolvable(self, env: Env) -> None:
        item = flag(
            env.queue,
            classification=None,
            inquiry=make_inquiry(subject="ログインについて"),
            decision=make_decision(final_priority=Priority.HIGH, reasons=[ReviewReason.LLM_FAILURE]),
        )
        resolution = env.resolver.resolve(item.review_id, ReviewValues(category=Category.SUPPORT))
        (ticket,) = env.tickets()
        assert ticket.category is Category.SUPPORT
        assert ticket.department == "サポート"
        # 優先度は、項目に記録済みの最終優先度。要約は件名。確信度と備考は、仮分類がないので、ない。
        assert ticket.priority is Priority.HIGH
        assert ticket.summary == "ログインについて"
        assert ticket.confidence is None
        assert ticket.note == ""
        assert resolution.action == "correct"
        # 仮分類がないので、修正前の値は、すべて null。
        assert resolution.before == ReviewValues()

    def test_without_a_subject_the_summary_is_the_first_50_characters_of_the_body(self, env: Env) -> None:
        body = "あ" * 30 + "い" * 30
        item = flag(
            env.queue,
            classification=None,
            inquiry=make_inquiry(subject=None, body=body),
            decision=make_decision(reasons=[ReviewReason.INVALID_OUTPUT]),
        )
        env.resolver.resolve(item.review_id, ReviewValues(category=Category.SUPPORT))
        (ticket,) = env.tickets()
        assert ticket.summary == body[:50]
        assert len(ticket.summary) == 50

    def test_an_empty_subject_falls_back_to_the_body(self, env: Env) -> None:
        item = flag(
            env.queue,
            classification=None,
            inquiry=make_inquiry(subject="", body="本文だけの問い合わせです"),
            decision=make_decision(reasons=[ReviewReason.INPUT_GUARDRAIL]),
        )
        env.resolver.resolve(item.review_id, ReviewValues(category=Category.SUPPORT))
        (ticket,) = env.tickets()
        assert ticket.summary == "本文だけの問い合わせです"


class TestUnknownOrResolvedIds:
    """REQ-010 AC-6：存在しないレビューID、対応済みのレビューIDは、エラー（チケットを登録しない）。"""

    def test_an_unknown_id_is_an_error(self, env: Env) -> None:
        flag(env.queue)
        with pytest.raises(ReviewNotFoundError):
            env.resolver.resolve("REV-00000000")
        env.assert_nothing_recorded()

    def test_an_unknown_id_on_an_empty_queue_is_an_error(self, env: Env) -> None:
        with pytest.raises(ReviewNotFoundError):
            env.resolver.resolve("REV-00000000")

    def test_a_resolved_id_is_an_error_and_no_second_ticket_is_registered(self, env: Env) -> None:
        item = flag(env.queue)
        env.resolver.resolve(item.review_id)
        with pytest.raises(ReviewAlreadyResolvedError):
            env.resolver.resolve(item.review_id, ReviewValues(priority=Priority.HIGH))
        assert len(env.tickets()) == 1
        assert len(env.resolutions()) == 1
        assert len(env.log()) == 1

    def test_the_other_items_are_not_affected(self, env: Env) -> None:
        first = flag(env.queue)
        second = flag(env.queue)
        env.resolver.resolve(first.review_id)
        env.resolver.resolve(second.review_id)
        assert len(env.tickets()) == 2

    def test_the_messages_hold_no_inquiry_content(self, env: Env) -> None:
        item = flag(env.queue)
        with pytest.raises(ReviewNotFoundError) as not_found:
            env.resolver.resolve("REV-00000000")
        env.resolver.resolve(item.review_id)
        with pytest.raises(ReviewAlreadyResolvedError) as resolved:
            env.resolver.resolve(item.review_id)
        for error in (not_found.value, resolved.value):
            assert SECRET_BODY not in str(error)
            assert SENDER not in str(error)


class TestDepartmentMustBeInTheMaster:
    """REQ-010 AC-9（Q-11）：指定できる担当部署は、マスタの部署（`null` を除く）に限る。"""

    def test_the_allowed_departments_are_the_master_departments(self, env: Env) -> None:
        assert env.resolver.departments == MASTER_DEPARTMENTS

    @pytest.mark.parametrize("department", MASTER_DEPARTMENTS)
    def test_every_master_department_is_accepted_whatever_the_category(self, env: Env, department: str) -> None:
        item = flag(env.queue)
        resolution = env.resolver.resolve(item.review_id, ReviewValues(category=Category.BILLING, department=department))
        assert resolution.after.department == department
        (ticket,) = env.tickets()
        assert ticket.department == department

    @pytest.mark.parametrize(
        "department",
        [
            "存在しない部署",
            "",
            " ",
            "請求 ",
            " 請求",
            "請求部",
            "営業部",
            "ｻﾎﾟｰﾄ",
            "未分類",
            "None",
            "山田太郎 taro@example.com",
        ],
    )
    def test_anything_else_is_an_error_and_nothing_is_recorded(self, env: Env, department: str) -> None:
        item = flag(env.queue)
        with pytest.raises(ReviewResolutionError):
            env.resolver.resolve(item.review_id, ReviewValues(department=department))
        env.assert_nothing_recorded()
        assert env.pending_ids() == [item.review_id]

    def test_the_error_names_the_allowed_departments(self, env: Env) -> None:
        item = flag(env.queue)
        with pytest.raises(ReviewResolutionError) as error:
            env.resolver.resolve(item.review_id, ReviewValues(department="存在しない部署"))
        for department in MASTER_DEPARTMENTS:
            assert department in str(error.value)
        assert SECRET_BODY not in str(error.value)

    def test_the_department_is_checked_before_the_ticket_is_registered(self, tmp_path: Path, config: AppConfig) -> None:
        """チケットの登録に失敗する状況でも、部署の不正が先に検出され、再試行キューへ入らない。"""
        failing = Env(tmp_path / "out", config, fail_if=lambda request: True)
        item = flag(failing.queue)
        with pytest.raises(ReviewResolutionError):
            failing.resolver.resolve(item.review_id, ReviewValues(department="存在しない部署"))
        assert failing.retries() == []

    def test_the_department_of_a_corrected_unclassified_category_does_not_exist(self, env: Env) -> None:
        """`unclassified` のマスタの部署は `null` で、指定できる部署に含まれない。"""
        assert None not in env.resolver.departments


class TestTicketRegistrationFailure:
    """REQ-010 AC-7・ERR-003：チケット登録の失敗は、再試行キューへ登録し、解決記録を書かず、項目を未対応のまま残す。"""

    def make_failing_env(self, tmp_path: Path, config: AppConfig) -> Env:
        return Env(tmp_path / "out", config, fail_if=lambda request: True)

    def test_the_request_goes_to_the_retry_queue_and_the_item_stays_pending(self, tmp_path: Path, config: AppConfig) -> None:
        failing = self.make_failing_env(tmp_path, config)
        item = flag(failing.queue)
        with pytest.raises(TicketRegistrationError):
            failing.resolver.resolve(item.review_id, ReviewValues(priority=Priority.HIGH))
        (entry,) = failing.retries()
        assert entry.source == "review_resolution"
        assert entry.request.category is Category.BILLING
        assert entry.request.priority is Priority.HIGH
        assert entry.request.department == "請求"
        assert entry.request.inquiry_id == "INQ-abcd1234"
        assert entry.error
        assert failing.resolutions() == []
        assert failing.log() == []
        assert failing.pending_ids() == [item.review_id]

    def test_the_retry_entry_error_holds_no_inquiry_content(self, tmp_path: Path, config: AppConfig) -> None:
        failing = self.make_failing_env(tmp_path, config)
        item = flag(failing.queue)
        with pytest.raises(TicketRegistrationError):
            failing.resolver.resolve(item.review_id)
        (entry,) = failing.retries()
        assert SECRET_BODY not in entry.error
        assert SENDER not in entry.error

    def test_resolving_again_after_the_failure_registers_exactly_one_ticket(self, tmp_path: Path, config: AppConfig) -> None:
        failing = self.make_failing_env(tmp_path, config)
        item = flag(failing.queue)
        with pytest.raises(TicketRegistrationError):
            failing.resolver.resolve(item.review_id)
        failing.ticket_system.fail_if = None
        resolution = failing.resolver.resolve(item.review_id)
        assert [ticket.ticket_id for ticket in failing.tickets()] == [resolution.ticket_id]
        assert failing.pending_ids() == []
        assert len(failing.retries()) == 1


class TestStorageFailures:
    """ERR-007：ログ・キュー・解決記録の書き込み・読み込みの失敗は、握りつぶさず、`StorageError` として送出する。"""

    def test_a_broken_line_in_the_queue_aborts_before_any_ticket(self, env: Env) -> None:
        item = flag(env.queue)
        with (env.output_dir / REVIEW_QUEUE_FILE).open("a", encoding="utf-8") as handle:
            handle.write(f"{SECRET_BODY} これは JSON ではない\n")
        with pytest.raises(StorageError) as error:
            env.resolver.resolve(item.review_id)
        assert REVIEW_QUEUE_FILE in str(error.value)
        assert "2行目" in str(error.value)
        assert SECRET_BODY not in str(error.value)
        assert env.tickets() == []

    def test_a_broken_line_in_the_resolutions_aborts_before_any_ticket(self, env: Env) -> None:
        item = flag(env.queue)
        (env.output_dir / REVIEW_RESOLUTIONS_FILE).write_text(f"{SECRET_BODY}\n", encoding="utf-8")
        with pytest.raises(StorageError) as error:
            env.resolver.resolve(item.review_id)
        assert REVIEW_RESOLUTIONS_FILE in str(error.value)
        assert SECRET_BODY not in str(error.value)
        assert env.tickets() == []

    def test_an_unknown_id_is_not_reported_when_the_resolutions_are_unreadable(self, env: Env) -> None:
        """壊れた解決記録は、対象の項目に関わらず、検出される（読めないまま「存在しない」と誤報しない）。"""
        flag(env.queue)
        (env.output_dir / REVIEW_RESOLUTIONS_FILE).write_text("壊れた行\n", encoding="utf-8")
        with pytest.raises(StorageError):
            env.resolver.resolve("REV-00000000")

    def test_a_resolution_write_failure_is_raised_and_the_item_stays_pending(
        self, env: Env, request: pytest.FixtureRequest
    ) -> None:
        item = flag(env.queue)
        (env.output_dir / TICKETS_FILE).touch()  # チケットへの追記は成功する（既存のファイル）
        deny_new_files(env.output_dir, request)  # 解決記録のファイルを、新しく作れない
        with pytest.raises(StorageError) as error:
            env.resolver.resolve(item.review_id)
        assert REVIEW_RESOLUTIONS_FILE in str(error.value)
        assert SECRET_BODY not in str(error.value)
        os.chmod(env.output_dir, 0o755)
        assert env.pending_ids() == [item.review_id]
        assert len(env.tickets()) == 1  # チケットは登録済み（R-08・R-10：再実行で重複しうる）
        assert env.log() == []

    def test_a_process_log_write_failure_is_raised_after_the_resolution_is_recorded(self, env: Env) -> None:
        item = flag(env.queue)
        (env.output_dir / PROCESS_LOG_FILE).mkdir()  # 追記できない
        with pytest.raises(StorageError) as error:
            env.resolver.resolve(item.review_id)
        assert PROCESS_LOG_FILE in str(error.value)
        assert len(env.tickets()) == 1
        assert len(env.resolutions()) == 1

    def test_a_retry_queue_write_failure_is_not_a_ticket_registration_error(self, tmp_path: Path, config: AppConfig) -> None:
        failing = Env(tmp_path / "out", config, fail_if=lambda request: True)
        item = flag(failing.queue)
        (failing.output_dir / RETRY_QUEUE_FILE).mkdir()
        with pytest.raises(StorageError) as error:
            failing.resolver.resolve(item.review_id)
        assert RETRY_QUEUE_FILE in str(error.value)
        assert not isinstance(error.value, TicketRegistrationError)
        assert failing.pending_ids() == [item.review_id]


# --- CLI（結合） ---


@pytest.fixture(autouse=True)
def _isolated_environment(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """API Key は、設定しない（`review` は、LLM を使わないので要らない）。作業ディレクトリは `tmp_path`。"""
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.chdir(tmp_path)


class Cli:
    def __init__(self, capsys: pytest.CaptureFixture[str], config_dir: Path, output_dir: Path, config: AppConfig) -> None:
        self._capsys = capsys
        self.config_dir = config_dir
        self.output_dir = output_dir
        self.queue = ReviewQueue(output_dir)
        self.config = config

    def run(self, *args: str, with_config: bool = True) -> tuple[int, str, str]:
        argv = list(args)
        if with_config:
            argv += ["--config-dir", str(self.config_dir)]
        code = main(argv)
        captured = self._capsys.readouterr()
        return code, captured.out, captured.err

    def flag(self, **overrides: object) -> ReviewItem:
        return flag(self.queue, **overrides)  # type: ignore[arg-type]

    def tickets(self) -> list[TicketRecord]:
        return JsonlStore(self.output_dir / TICKETS_FILE).read_all(TicketRecord)

    def retries(self) -> list[RetryQueueEntry]:
        return JsonlStore(self.output_dir / RETRY_QUEUE_FILE).read_all(RetryQueueEntry)

    def log(self) -> list[ProcessLogRecord]:
        return JsonlStore(self.output_dir / PROCESS_LOG_FILE).read_all(ProcessLogRecord)

    def pending(self) -> list[str]:
        return [item.review_id for item in self.queue.list_pending()]


@pytest.fixture
def cli(capsys: pytest.CaptureFixture[str], config_dir: Path, tmp_path: Path, config: AppConfig) -> Cli:
    return Cli(capsys, config_dir, tmp_path / "out", config)


def lines_of(text: str) -> list[dict[str, object]]:
    return [json.loads(line) for line in text.splitlines() if line.strip()]


class TestReviewList:
    """REQ-010 AC-1：未対応の項目を、レビューID・理由・仮分類・要約とともに、1件1行の JSON で表示する。"""

    def test_it_lists_the_pending_items_as_jsonl(self, cli: Cli) -> None:
        first = cli.flag(classification=make_result(summary="請求の確認"), decision=make_decision(reasons=[ReviewReason.LOW_CONFIDENCE, ReviewReason.NON_JAPANESE]))
        second = cli.flag(classification=None, decision=make_decision(reasons=[ReviewReason.LLM_FAILURE]))
        code, out, err = cli.run("review", "list")
        assert code == 0
        items = lines_of(out)
        assert [item["review_id"] for item in items] == [first.review_id, second.review_id]
        assert items[0]["reasons"] == ["LOW_CONFIDENCE", "NON_JAPANESE"]
        assert items[0]["classification"]["summary"] == "請求の確認"  # type: ignore[index]
        assert items[1]["classification"] is None
        assert err == ""

    def test_resolved_items_are_not_listed(self, cli: Cli) -> None:
        first = cli.flag()
        second = cli.flag()
        cli.run("review", "resolve", first.review_id)
        code, out, _ = cli.run("review", "list")
        assert code == 0
        assert [item["review_id"] for item in lines_of(out)] == [second.review_id]

    def test_an_empty_queue_prints_nothing_and_succeeds(self, cli: Cli) -> None:
        code, out, err = cli.run("review", "list")
        assert (code, out, err) == (0, "", "")

    def test_it_does_not_need_an_api_key(self, cli: Cli) -> None:
        assert "OPENAI_API_KEY" not in os.environ
        code, _, err = cli.run("review", "list")
        assert code == 0
        assert "OPENAI_API_KEY" not in err

    def test_japanese_is_not_escaped(self, cli: Cli) -> None:
        cli.flag(inquiry=make_inquiry(subject="日本語の件名"))
        _, out, _ = cli.run("review", "list")
        assert "日本語の件名" in out
        assert "\\u" not in out


class TestReviewResolveCommand:
    def test_an_approval_registers_the_ticket_and_prints_the_resolution(self, cli: Cli) -> None:
        item = cli.flag()
        code, out, err = cli.run("review", "resolve", item.review_id)
        assert code == 0
        (line,) = lines_of(out)
        (ticket,) = cli.tickets()
        assert line["review_id"] == item.review_id
        assert line["action"] == "approve"
        assert line["ticket_id"] == ticket.ticket_id
        assert cli.pending() == []
        # 標準出力にも標準エラー出力にも、問い合わせの内容は出ない。
        assert SECRET_BODY not in out + err
        assert SENDER not in out + err

    def test_the_options_make_it_a_correction(self, cli: Cli) -> None:
        item = cli.flag()
        code, out, _ = cli.run(
            "review", "resolve", item.review_id,
            "--category", "technical", "--priority", "high", "--department", "営業", "--reviewer", "佐藤",
        )  # fmt: skip
        assert code == 0
        (line,) = lines_of(out)
        assert line["action"] == "correct"
        assert line["reviewer"] == "佐藤"
        (ticket,) = cli.tickets()
        assert (ticket.category, ticket.priority, ticket.department) == (Category.TECHNICAL, Priority.HIGH, "営業")

    def test_a_category_only_correction_uses_the_department_of_the_category(self, cli: Cli) -> None:
        item = cli.flag()
        code, _, _ = cli.run("review", "resolve", item.review_id, "--category", "complaint")
        assert code == 0
        (ticket,) = cli.tickets()
        assert (ticket.category, ticket.department) == (Category.COMPLAINT, "クレーム対応")

    def test_the_process_log_gets_the_resolution_record(self, cli: Cli) -> None:
        item = cli.flag()
        cli.run("review", "resolve", item.review_id, "--priority", "high")
        (record,) = cli.log()
        assert record.record_type == "review_resolution"
        assert record.action == "correct"
        assert record.review_id == item.review_id
        assert record.before is not None and record.before.priority is Priority.MEDIUM
        assert record.after is not None and record.after.priority is Priority.HIGH

    def test_it_does_not_need_an_api_key(self, cli: Cli) -> None:
        item = cli.flag()
        assert "OPENAI_API_KEY" not in os.environ
        assert cli.run("review", "resolve", item.review_id)[0] == 0


class TestResolveErrorsExitWithOne:
    """REQ-010 AC-8：確定できなかったときは、エラーを標準エラー出力へ表示し、終了コード 1。内容は表示しない。"""

    def assert_error(self, result: tuple[int, str, str]) -> str:
        code, out, err = result
        assert code == 1
        assert out == ""
        assert err.startswith("エラー:")
        assert SECRET_BODY not in err
        assert SENDER not in err
        return err

    def test_an_unknown_id(self, cli: Cli) -> None:
        cli.flag()
        err = self.assert_error(cli.run("review", "resolve", "REV-00000000"))
        assert "REV-00000000" in err
        assert cli.tickets() == []

    def test_a_resolved_id_registers_no_second_ticket(self, cli: Cli) -> None:
        item = cli.flag()
        assert cli.run("review", "resolve", item.review_id)[0] == 0
        self.assert_error(cli.run("review", "resolve", item.review_id))
        assert len(cli.tickets()) == 1

    def test_an_item_without_a_classification_needs_a_category(self, cli: Cli) -> None:
        item = cli.flag(classification=None, decision=make_decision(reasons=[ReviewReason.INPUT_GUARDRAIL]))
        err = self.assert_error(cli.run("review", "resolve", item.review_id))
        assert "--category" in err
        assert cli.tickets() == []
        assert cli.pending() == [item.review_id]

    def test_an_unclassified_item_can_be_resolved_when_a_category_is_given(self, cli: Cli) -> None:
        item = cli.flag(classification=make_result(category=Category.UNCLASSIFIED, confidence=0.2))
        self.assert_error(cli.run("review", "resolve", item.review_id))
        code, _, _ = cli.run("review", "resolve", item.review_id, "--category", "support")
        assert code == 0
        (ticket,) = cli.tickets()
        assert ticket.category is Category.SUPPORT

    def test_specifying_unclassified_is_an_error(self, cli: Cli) -> None:
        item = cli.flag()
        self.assert_error(cli.run("review", "resolve", item.review_id, "--category", "unclassified"))
        assert cli.tickets() == []

    @pytest.mark.parametrize("department", ["存在しない部署", "", "請求部"])
    def test_a_department_outside_the_master(self, cli: Cli, department: str) -> None:
        item = cli.flag()
        err = self.assert_error(cli.run("review", "resolve", item.review_id, "--department", department))
        for allowed in MASTER_DEPARTMENTS:
            assert allowed in err
        assert cli.tickets() == []
        assert cli.pending() == [item.review_id]
        assert cli.log() == []

    def test_a_master_department_is_accepted(self, cli: Cli) -> None:
        item = cli.flag()
        assert cli.run("review", "resolve", item.review_id, "--department", "営業")[0] == 0

    def test_a_ticket_registration_failure(self, cli: Cli) -> None:
        """チケットの登録先に書けない状況：再試行キューへ登録し、項目は未対応のまま。"""
        item = cli.flag()
        (cli.output_dir / TICKETS_FILE).mkdir()
        err = self.assert_error(cli.run("review", "resolve", item.review_id))
        assert "再試行キュー" in err
        (entry,) = cli.retries()
        assert entry.source == "review_resolution"
        assert cli.pending() == [item.review_id]
        assert cli.log() == []


class TestReviewReadFailuresAbort:
    """ERR-007：ストアの読み込みの失敗（壊れた行）は、ファイル名・行番号・原因を表示して、終了コード 1 で中断する。"""

    def corrupt(self, path: Path) -> None:
        with path.open("a", encoding="utf-8") as handle:
            handle.write(f"{SECRET_BODY} {SENDER}\n")

    def assert_aborted(self, result: tuple[int, str, str], file_name: str, line: int) -> None:
        code, out, err = result
        assert code == 1
        assert out == ""
        assert "中断" in err
        assert file_name in err
        assert f"{line}行目" in err
        assert SECRET_BODY not in err
        assert SENDER not in err

    def test_list_with_a_broken_queue_line(self, cli: Cli) -> None:
        cli.flag()
        self.corrupt(cli.output_dir / REVIEW_QUEUE_FILE)
        self.assert_aborted(cli.run("review", "list"), REVIEW_QUEUE_FILE, 2)

    def test_list_with_a_broken_resolutions_line(self, cli: Cli) -> None:
        cli.flag()
        self.corrupt(cli.output_dir / REVIEW_RESOLUTIONS_FILE)
        self.assert_aborted(cli.run("review", "list"), REVIEW_RESOLUTIONS_FILE, 1)

    def test_resolve_with_a_broken_queue_line_registers_no_ticket(self, cli: Cli) -> None:
        item = cli.flag()
        self.corrupt(cli.output_dir / REVIEW_QUEUE_FILE)
        result = cli.run("review", "resolve", item.review_id)
        self.assert_aborted(result, REVIEW_QUEUE_FILE, 2)
        assert item.review_id in result[2]
        assert cli.tickets() == []

    def test_resolve_with_a_broken_resolutions_line_registers_no_ticket(self, cli: Cli) -> None:
        item = cli.flag()
        self.corrupt(cli.output_dir / REVIEW_RESOLUTIONS_FILE)
        result = cli.run("review", "resolve", item.review_id)
        self.assert_aborted(result, REVIEW_RESOLUTIONS_FILE, 1)
        assert cli.tickets() == []

    def test_the_abort_of_list_has_no_review_id_or_count(self, cli: Cli) -> None:
        cli.flag()
        self.corrupt(cli.output_dir / REVIEW_QUEUE_FILE)
        _, _, err = cli.run("review", "list")
        assert "レビューID" not in err
        assert "件数" not in err


class TestReviewWriteFailuresAbort:
    """ERR-007：解決記録・処理ログへの書き込みの失敗は、レビューIDと失敗の情報を表示して、終了コード 1 で中断する。"""

    def test_a_resolution_write_failure(self, cli: Cli, request: pytest.FixtureRequest) -> None:
        item = cli.flag()
        (cli.output_dir / TICKETS_FILE).touch()
        deny_new_files(cli.output_dir, request)
        code, out, err = cli.run("review", "resolve", item.review_id)
        os.chmod(cli.output_dir, 0o755)
        assert code == 1
        assert out == ""
        assert "中断" in err
        assert item.review_id in err
        assert REVIEW_RESOLUTIONS_FILE in err
        assert SECRET_BODY not in err
        assert SENDER not in err
        assert cli.pending() == [item.review_id]

    def test_a_process_log_write_failure(self, cli: Cli) -> None:
        item = cli.flag()
        (cli.output_dir / PROCESS_LOG_FILE).mkdir()
        code, out, err = cli.run("review", "resolve", item.review_id)
        assert code == 1
        assert out == ""
        assert item.review_id in err
        assert PROCESS_LOG_FILE in err
        assert SECRET_BODY not in err


class TestArguments:
    def test_the_config_dir_may_come_before_or_after_the_subcommand(self, cli: Cli) -> None:
        item = cli.flag()
        config = str(cli.config_dir)
        assert cli.run("--config-dir", config, "review", "list", with_config=False)[0] == 0
        assert cli.run("review", "--config-dir", config, "list", with_config=False)[0] == 0
        assert cli.run("review", "list", "--config-dir", config, with_config=False)[0] == 0
        code, _, _ = cli.run("review", "resolve", item.review_id, "--config-dir", config, with_config=False)
        assert code == 0
        assert len(cli.tickets()) == 1

    @pytest.mark.parametrize("position", ["before_command", "between_command_and_action"])
    def test_the_config_dir_before_resolve_is_not_overridden_by_the_default(self, cli: Cli, position: str) -> None:
        """`resolve` の前に指定した `--config-dir` が、`resolve` 側の既定値（`config`）で上書きされない。"""
        item = cli.flag()
        config = str(cli.config_dir)
        if position == "before_command":
            argv = ["--config-dir", config, "review", "resolve", item.review_id]
        else:
            argv = ["review", "--config-dir", config, "resolve", item.review_id]
        code, _, err = cli.run(*argv, with_config=False)
        assert code == 0, err
        assert len(cli.tickets()) == 1

    def test_the_review_id_is_required(self) -> None:
        with pytest.raises(SystemExit) as exit_info:
            build_parser().parse_args(["review", "resolve"])
        assert exit_info.value.code == 2

    def test_an_action_is_required(self) -> None:
        with pytest.raises(SystemExit) as exit_info:
            build_parser().parse_args(["review"])
        assert exit_info.value.code == 2

    @pytest.mark.parametrize(
        "option",
        [["--category", "billing2"], ["--priority", "urgent"], ["--priority", "HIGH"], ["--category", ""]],
    )
    def test_invalid_option_values_are_argument_errors(self, option: list[str]) -> None:
        with pytest.raises(SystemExit) as exit_info:
            build_parser().parse_args(["review", "resolve", "REV-1", *option])
        assert exit_info.value.code == 2

    def test_review_needs_no_api_key_in_the_registry(self) -> None:
        assert COMMANDS["review"].needs_api_key is False
        assert COMMANDS["run"].needs_api_key is True
