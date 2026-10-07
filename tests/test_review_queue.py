"""TASK-012：Human Review キュー（登録と一覧）（REQ-009 / CMP-011、第7.3節、ADR-006、ADR-012）。

ファイルは `tmp_path` に書く。LLM・ネットワークに依存しない。
"""

import json
import re
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fakes import make_result

from triage_agent.models import (
    Category,
    Channel,
    Decision,
    Inquiry,
    Priority,
    ResultKind,
    ReviewItem,
    ReviewReason,
    ReviewResolution,
    ReviewValues,
)
from triage_agent.review import REVIEW_QUEUE_FILE, REVIEW_RESOLUTIONS_FILE, ReviewQueue
from triage_agent.storage import JsonlStore, StorageError

FIXED_TIME = datetime(2026, 9, 20, 12, 0, 0, tzinfo=UTC)
SENDER = "taro.yamada@example.com"


def make_inquiry(**overrides: object) -> Inquiry:
    values: dict[str, object] = {
        "inquiry_id": "INQ-abcd1234",
        "channel": Channel.EMAIL,
        "subject": "解約について",
        "body": "解約したいので、090-1234-5678 に電話をください。",
        "sender": SENDER,
        "form_fields": {"種別": "解約"},
    }
    values.update(overrides)
    return Inquiry.model_validate(values)


def make_decision(**overrides: object) -> Decision:
    values: dict[str, object] = {
        "final_priority": Priority.HIGH,
        "high_risk_hits": ["解約"],
        "urgent_hits": ["至急"],
        "reasons": [ReviewReason.HIGH_RISK_KEYWORD],
        "kind": ResultKind.HUMAN_REVIEW,
    }
    values.update(overrides)
    return Decision.model_validate(values)


def flag(queue: ReviewQueue, **overrides: object) -> ReviewItem:
    arguments: dict[str, object] = {
        "inquiry": make_inquiry(),
        "classification": make_result(),
        "decision": make_decision(),
        "request_id": "req-1",
    }
    arguments.update(overrides)
    return queue.flag_for_review(**arguments)  # type: ignore[arg-type]


def resolve(output_dir: Path, review_id: str) -> None:
    """解決記録を追記する（TASK-016 の `resolve` の代わりに、ファイルへ直接書く）。"""
    JsonlStore(output_dir / REVIEW_RESOLUTIONS_FILE).append(
        ReviewResolution(
            review_id=review_id,
            action="approve",
            before=ReviewValues(category=Category.BILLING, priority=Priority.MEDIUM, department="請求"),
            after=ReviewValues(category=Category.BILLING, priority=Priority.MEDIUM, department="請求"),
            ticket_id="TCK-00000000",
            resolved_at=FIXED_TIME,
        )
    )


def read_lines(path: Path) -> list[dict[str, object]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def test_the_file_names_follow_the_design() -> None:
    # 出力先の名前（Design 第7.4節）。実装の定数と同じ値を比べて、名前の誤りを見逃さない。
    assert REVIEW_QUEUE_FILE == "review_queue.jsonl"
    assert REVIEW_RESOLUTIONS_FILE == "review_resolutions.jsonl"


class TestFlagForReview:
    """REQ-009 AC-1：レビューID、問い合わせ内容、仮分類、全ての理由、登録時刻を登録する。"""

    def test_returns_an_item_with_a_review_id(self, tmp_path: Path) -> None:
        item = flag(ReviewQueue(tmp_path))
        assert isinstance(item, ReviewItem)
        assert re.fullmatch(r"REV-[0-9a-f]{8}", item.review_id)

    def test_the_item_holds_the_content_of_the_decision(self, tmp_path: Path) -> None:
        inquiry = make_inquiry()
        classification = make_result(category=Category.COMPLAINT)
        decision = make_decision(final_priority=Priority.HIGH, high_risk_hits=["解約", "返金"])
        item = flag(ReviewQueue(tmp_path), inquiry=inquiry, classification=classification, decision=decision)
        assert item.inquiry == inquiry
        assert item.classification == classification
        assert item.final_priority is Priority.HIGH
        assert item.high_risk_hits == ["解約", "返金"]
        assert item.request_id == "req-1"

    def test_the_original_content_is_kept_unmasked(self, tmp_path: Path) -> None:
        # 担当者が判断するため、元の内容を保存する（A-09、確認済み。マスキングは処理ログの責務）。
        flag(ReviewQueue(tmp_path))
        [line] = read_lines(tmp_path / REVIEW_QUEUE_FILE)
        inquiry = line["inquiry"]
        assert isinstance(inquiry, dict)
        assert inquiry["sender"] == SENDER
        assert "090-1234-5678" in str(inquiry["body"])
        assert inquiry["form_fields"] == {"種別": "解約"}

    def test_every_reason_is_saved(self, tmp_path: Path) -> None:
        # REQ-006 AC-2：最初の1つだけでなく、該当した全ての理由。
        reasons = [
            ReviewReason.HIGH_RISK_KEYWORD,
            ReviewReason.COMPLAINT_OR_LEGAL,
            ReviewReason.LOW_CONFIDENCE,
        ]
        item = flag(ReviewQueue(tmp_path), decision=make_decision(reasons=reasons))
        assert item.reasons == reasons
        [line] = read_lines(tmp_path / REVIEW_QUEUE_FILE)
        assert line["reasons"] == ["HIGH_RISK_KEYWORD", "COMPLAINT_OR_LEGAL", "LOW_CONFIDENCE"]

    def test_the_provisional_classification_is_saved(self, tmp_path: Path) -> None:
        flag(ReviewQueue(tmp_path), classification=make_result(category=Category.BILLING, confidence=0.55))
        [line] = read_lines(tmp_path / REVIEW_QUEUE_FILE)
        classification = line["classification"]
        assert isinstance(classification, dict)
        assert classification["category"] == "billing"
        assert classification["confidence"] == 0.55

    def test_without_a_classification_it_is_null(self, tmp_path: Path) -> None:
        # 分類結果がない場合（入力ガードレール該当・LLM 失敗・形式不正）。仮分類は `null`。
        decision = make_decision(reasons=[ReviewReason.LLM_FAILURE], high_risk_hits=[])
        item = flag(ReviewQueue(tmp_path), classification=None, decision=decision)
        assert item.classification is None
        [line] = read_lines(tmp_path / REVIEW_QUEUE_FILE)
        assert "classification" in line
        assert line["classification"] is None

    def test_the_registration_time_is_recorded(self, tmp_path: Path) -> None:
        item = flag(ReviewQueue(tmp_path, clock=lambda: FIXED_TIME))
        assert item.created_at == FIXED_TIME
        [line] = read_lines(tmp_path / REVIEW_QUEUE_FILE)
        assert str(line["created_at"]).startswith("2026-09-20T12:00:00")

    def test_the_default_registration_time_is_timezone_aware(self, tmp_path: Path) -> None:
        assert flag(ReviewQueue(tmp_path)).created_at.tzinfo is not None

    def test_the_saved_item_can_be_read_back(self, tmp_path: Path) -> None:
        item = flag(ReviewQueue(tmp_path))
        assert JsonlStore(tmp_path / REVIEW_QUEUE_FILE).read_all(ReviewItem) == [item]

    def test_items_are_appended_and_existing_lines_are_kept(self, tmp_path: Path) -> None:
        queue = ReviewQueue(tmp_path)
        first = flag(queue, request_id="req-1")
        before = (tmp_path / REVIEW_QUEUE_FILE).read_text(encoding="utf-8")
        second = flag(queue, request_id="req-2")
        assert (tmp_path / REVIEW_QUEUE_FILE).read_text(encoding="utf-8").startswith(before)
        assert [line["review_id"] for line in read_lines(tmp_path / REVIEW_QUEUE_FILE)] == [
            first.review_id,
            second.review_id,
        ]
        assert first.review_id != second.review_id

    def test_no_ticket_is_registered(self, tmp_path: Path) -> None:
        # REQ-009 AC-3：Human Review キューへ登録した時点では、チケットを登録しない。書き込むのは、キューだけ。
        flag(ReviewQueue(tmp_path))
        assert sorted(path.name for path in tmp_path.iterdir()) == [REVIEW_QUEUE_FILE]

    def test_the_output_directory_is_created(self, tmp_path: Path) -> None:
        flag(ReviewQueue(tmp_path / "output" / "nested"))
        assert (tmp_path / "output" / "nested" / REVIEW_QUEUE_FILE).is_file()

    def test_an_auto_registered_decision_is_rejected(self, tmp_path: Path) -> None:
        decision = make_decision(kind=ResultKind.AUTO_REGISTERED, reasons=[], department="請求")
        with pytest.raises(ValueError, match="auto_registered"):
            flag(ReviewQueue(tmp_path), decision=decision)
        assert not (tmp_path / REVIEW_QUEUE_FILE).exists()

    def test_a_decision_without_reasons_is_rejected(self, tmp_path: Path) -> None:
        # 何を確認すべきかが分からない項目を、キューに並べない（REQ-009 のユーザーストーリー）。
        with pytest.raises(ValueError):
            flag(ReviewQueue(tmp_path), decision=make_decision(reasons=[]))
        assert not (tmp_path / REVIEW_QUEUE_FILE).exists()

    def test_the_caller_s_lists_are_not_shared_with_the_item(self, tmp_path: Path) -> None:
        decision = make_decision(reasons=[ReviewReason.HIGH_RISK_KEYWORD], high_risk_hits=["解約"])
        item = flag(ReviewQueue(tmp_path), decision=decision)
        decision.reasons.append(ReviewReason.UNCLASSIFIED)
        decision.high_risk_hits.append("返金")
        assert item.reasons == [ReviewReason.HIGH_RISK_KEYWORD]
        assert item.high_risk_hits == ["解約"]

    def test_a_write_failure_is_not_swallowed(self, tmp_path: Path) -> None:
        # キューへ書けない失敗を握りつぶすと、人間が確認すべき項目が失われる（ERR-007）。
        (tmp_path / REVIEW_QUEUE_FILE).mkdir()
        with pytest.raises(StorageError):
            flag(ReviewQueue(tmp_path))


class TestListPending:
    """REQ-009 AC-1：解決記録（REQ-010）がない項目は、未対応として扱う。"""

    def test_an_empty_queue_has_no_pending_items(self, tmp_path: Path) -> None:
        assert ReviewQueue(tmp_path).list_pending() == []

    def test_flagged_items_are_pending_in_the_order_of_registration(self, tmp_path: Path) -> None:
        queue = ReviewQueue(tmp_path)
        items = [flag(queue, request_id=f"req-{n}") for n in range(3)]
        assert queue.list_pending() == items

    def test_a_resolved_item_disappears_from_the_list(self, tmp_path: Path) -> None:
        queue = ReviewQueue(tmp_path)
        first, second, third = (flag(queue, request_id=f"req-{n}") for n in range(3))
        resolve(tmp_path, second.review_id)
        assert queue.list_pending() == [first, third]

    def test_when_every_item_is_resolved_nothing_is_pending(self, tmp_path: Path) -> None:
        queue = ReviewQueue(tmp_path)
        for item in [flag(queue), flag(queue)]:
            resolve(tmp_path, item.review_id)
        assert queue.list_pending() == []

    def test_the_state_is_derived_from_the_files_not_from_the_instance(self, tmp_path: Path) -> None:
        # 状態は、解決記録の有無で導出する（ADR-006）。別のインスタンスでも、同じ結果になる。
        item = flag(ReviewQueue(tmp_path))
        other = ReviewQueue(tmp_path)
        assert other.list_pending() == [item]
        resolve(tmp_path, item.review_id)
        assert other.list_pending() == []

    def test_a_resolution_for_an_unknown_review_id_is_ignored(self, tmp_path: Path) -> None:
        queue = ReviewQueue(tmp_path)
        item = flag(queue)
        resolve(tmp_path, "REV-ffffffff")
        assert queue.list_pending() == [item]

    def test_resolutions_without_any_queue_file_are_harmless(self, tmp_path: Path) -> None:
        resolve(tmp_path, "REV-00000000")
        assert ReviewQueue(tmp_path).list_pending() == []

    def test_the_pending_item_keeps_every_field(self, tmp_path: Path) -> None:
        queue = ReviewQueue(tmp_path)
        item = flag(queue, decision=make_decision(reasons=[ReviewReason.NON_JAPANESE, ReviewReason.LOW_CONFIDENCE]))
        [pending] = queue.list_pending()
        assert pending.review_id == item.review_id
        assert pending.reasons == [ReviewReason.NON_JAPANESE, ReviewReason.LOW_CONFIDENCE]
        assert pending.inquiry.sender == SENDER
        assert pending.classification == item.classification

    def test_listing_does_not_modify_the_files(self, tmp_path: Path) -> None:
        queue = ReviewQueue(tmp_path)
        flag(queue)
        before = (tmp_path / REVIEW_QUEUE_FILE).read_bytes()
        queue.list_pending()
        assert (tmp_path / REVIEW_QUEUE_FILE).read_bytes() == before
        assert not (tmp_path / REVIEW_RESOLUTIONS_FILE).exists()

    def test_a_broken_line_is_reported_with_its_line_number(self, tmp_path: Path) -> None:
        queue = ReviewQueue(tmp_path)
        flag(queue)
        with (tmp_path / REVIEW_QUEUE_FILE).open("a", encoding="utf-8") as handle:
            handle.write("{broken\n")
        with pytest.raises(StorageError, match="2行目"):
            queue.list_pending()

    def test_a_broken_resolution_file_is_reported_not_ignored(self, tmp_path: Path) -> None:
        # 解決記録を読めないまま一覧を返すと、対応済みの項目が未対応として表示される。
        queue = ReviewQueue(tmp_path)
        flag(queue)
        (tmp_path / REVIEW_RESOLUTIONS_FILE).write_text("{broken\n", encoding="utf-8")
        with pytest.raises(StorageError, match=REVIEW_RESOLUTIONS_FILE):
            queue.list_pending()
