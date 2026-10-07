"""Human Review キュー（CMP-011）：項目の登録と、未対応の一覧。

Human Review 項目は、追記専用の `review_queue.jsonl` へ登録する。項目に「状態」は持たせず、
`review_resolutions.jsonl` に解決記録があるかどうかで、対応済みかを導出する（ADR-006）。
担当者が判断するため、項目には、問い合わせの元の内容を保存する（Assumption A-09。処理ログとは異なり、
マスキングしない）。元の内容を保持するのは、このキューだけである（Design 第11章）。

解決（承認・修正、チケットの登録、解決記録と処理ログへの追記）は、`ReviewResolver` が行う（REQ-010、Design 第5.4節）。
"""

import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from triage_agent.clock import Clock, utc_now
from triage_agent.config import CategoryEntry
from triage_agent.models import (
    Category,
    ClassificationResult,
    Decision,
    Inquiry,
    Priority,
    ProcessLogRecord,
    ResultKind,
    ReviewAction,
    ReviewItem,
    ReviewResolution,
    ReviewValues,
    TicketRequest,
)
from triage_agent.process_log import ProcessLog
from triage_agent.storage import JsonlStore
from triage_agent.tickets import RetryQueue, TicketRegistrationError, TicketSystem, build_ticket_note

REVIEW_QUEUE_FILE = "review_queue.jsonl"
REVIEW_RESOLUTIONS_FILE = "review_resolutions.jsonl"

# 仮分類がない項目のチケットの要約は、件名（なければ本文の先頭のこの文字数）にする（Design 第5.4節 手順3）。
FALLBACK_SUMMARY_LENGTH = 50


class ReviewError(Exception):
    """Human Review を確定できなかった。CLI は、メッセージを表示して、終了コード 1 で終わる（REQ-010 AC-8）。

    メッセージには、問い合わせの内容を含めない。
    """


class ReviewNotFoundError(ReviewError):
    """指定されたレビューIDの項目がない（REQ-010 AC-6）。"""


class ReviewAlreadyResolvedError(ReviewError):
    """指定されたレビューIDの項目は、対応済みである（REQ-010 AC-6）。"""


class ReviewResolutionError(ReviewError):
    """確定する内容が不正である：カテゴリが未分類のまま（AC-5）、または、担当部署がマスタにない（AC-9）。"""


class ReviewQueue:
    """Human Review 項目の登録と、未対応の一覧。

    ファイルへの書き込み・読み込みの失敗は、`StorageError` として送出する（握りつぶさない。ERR-007）。
    """

    def __init__(self, output_dir: Path, *, clock: Clock = utc_now) -> None:
        self._queue = JsonlStore(output_dir / REVIEW_QUEUE_FILE)
        self._resolutions = JsonlStore(output_dir / REVIEW_RESOLUTIONS_FILE)
        self._clock = clock

    def flag_for_review(
        self,
        *,
        inquiry: Inquiry,
        classification: ClassificationResult | None,
        decision: Decision,
        request_id: str,
    ) -> ReviewItem:
        """Human Review 項目を `review_queue.jsonl` へ追記して返す（REQ-009 AC-1）。

        `classification` は、仮分類（得られていなければ `None`）。`decision` は、ルールエンジンが Human Review と
        判定した結果で、最終優先度・検出キーワード・該当した全ての理由を持つ。チケットは登録しない（AC-3）。
        """
        if decision.kind is not ResultKind.HUMAN_REVIEW:
            raise ValueError(f"Human Review ではない判定は、キューへ登録できません（{decision.kind.value}）")
        if not decision.reasons:
            raise ValueError("Human Review の理由がありません")
        item = ReviewItem(
            # レビューIDは、`REV-` と、ランダムな UUID の先頭8桁（一意性は確率的。ADR-012）。
            review_id=f"REV-{uuid.uuid4().hex[:8]}",
            inquiry=inquiry,
            classification=classification,
            final_priority=decision.final_priority,
            reasons=list(decision.reasons),
            high_risk_hits=list(decision.high_risk_hits),
            request_id=request_id,
            created_at=self._clock(),
        )
        self._queue.append(item)
        return item

    def list_pending(self) -> list[ReviewItem]:
        """未対応の項目を、登録の順に返す（REQ-010 AC-1 の一覧の元）。

        `review_queue.jsonl` の項目のうち、`review_resolutions.jsonl` に解決記録がないものを、未対応とする。
        """
        resolved = {resolution.review_id for resolution in self._resolutions.read_all(ReviewResolution)}
        return [item for item in self._queue.read_all(ReviewItem) if item.review_id not in resolved]

    def get_pending(self, review_id: str) -> ReviewItem:
        """未対応の項目を1件返す。存在しなければ `ReviewNotFoundError`、対応済みなら `ReviewAlreadyResolvedError`。

        どちらのファイルも、対象の項目に関わらず、最初に全体を読む（壊れた行があれば、`StorageError`。ERR-007）。
        """
        items = self._queue.read_all(ReviewItem)
        resolved = {resolution.review_id for resolution in self._resolutions.read_all(ReviewResolution)}
        for item in items:
            if item.review_id == review_id:
                if review_id in resolved:
                    raise ReviewAlreadyResolvedError(f"レビューID {review_id} は、対応済みです")
                return item
        raise ReviewNotFoundError(f"レビューID {review_id} の項目がありません")

    def record_resolution(self, resolution: ReviewResolution) -> None:
        """解決記録を `review_resolutions.jsonl` へ追記する。これで、項目は対応済みになる。"""
        self._resolutions.append(resolution)


@dataclass(frozen=True)
class _Confirmed:
    """確定する値（検証済み）。カテゴリと優先度は、必ず決まっている。"""

    category: Category
    priority: Priority
    department: str | None

    def as_values(self) -> ReviewValues:
        return ReviewValues(category=self.category, priority=self.priority, department=self.department)


class ReviewResolver:
    """Human Review 項目を、承認または修正して確定する（REQ-010、Design 第5.4節）。

    確定の手順は、項目の取得 → 最終値の決定 → 検証 → チケット登録 → 解決記録と処理ログへの追記。
    検証（カテゴリ・担当部署）は、チケットを登録する前に行い、エラーなら何も登録・記録しない。
    ログ・キュー・解決記録への書き込み・読み込みの失敗（`StorageError`）は、握りつぶさない（ERR-007）。
    """

    def __init__(
        self,
        *,
        queue: ReviewQueue,
        tickets: TicketSystem,
        retry_queue: RetryQueue,
        process_log: ProcessLog,
        master: Mapping[Category, CategoryEntry],
        clock: Clock = utc_now,
    ) -> None:
        self._queue = queue
        self._tickets = tickets
        self._retry_queue = retry_queue
        self._process_log = process_log
        self._master = master
        self._clock = clock

    @property
    def departments(self) -> list[str]:
        """修正で指定できる担当部署：マスタの部署（`null` を除く）。重複は除き、マスタの順に返す。"""
        return list(dict.fromkeys(entry.department for entry in self._master.values() if entry.department is not None))

    def resolve(
        self, review_id: str, corrections: ReviewValues | None = None, reviewer: str | None = None
    ) -> ReviewResolution:
        """項目を確定し、解決記録を返す。

        `corrections` の項目のうち、`None` でないものが修正の指定である。指定が1つもなければ「承認」、あれば「修正」。
        チケットの登録に失敗したときは、再試行キューへ登録し、解決記録は書かず（項目は未対応のまま）、
        `TicketRegistrationError` を送出する（REQ-010 AC-7）。
        """
        corrections = corrections if corrections is not None else ReviewValues()
        item = self._queue.get_pending(review_id)
        before = self._values_of_approval(item)
        confirmed = self._decide(item, corrections)
        request = TicketRequest(
            category=confirmed.category,
            priority=confirmed.priority,
            summary=self._summary_of(item),
            department=confirmed.department,
            confidence=item.classification.confidence if item.classification is not None else None,
            note=self._note_of(item, confirmed.category),
            inquiry_id=item.inquiry.inquiry_id,
            request_id=item.request_id,
        )
        try:
            ticket = self._tickets.create_ticket(request)
        except TicketRegistrationError as error:
            self._retry_queue.enqueue(request, str(error), "review_resolution")
            raise

        action: ReviewAction = "correct" if corrections != ReviewValues() else "approve"
        resolved_at = self._clock()
        after = confirmed.as_values()
        resolution = ReviewResolution(
            review_id=review_id,
            action=action,
            before=before,
            after=after,
            ticket_id=ticket.ticket_id,
            reviewer=reviewer,
            resolved_at=resolved_at,
        )
        self._queue.record_resolution(resolution)
        self._process_log.append(
            ProcessLogRecord(
                record_type="review_resolution",
                request_id=item.request_id,
                inquiry_id=item.inquiry.inquiry_id,
                processed_at=resolved_at,
                review_id=review_id,
                ticket_id=ticket.ticket_id,
                action=action,
                before=before,
                after=after,
            )
        )
        return resolution

    def _values_of_approval(self, item: ReviewItem) -> ReviewValues:
        """修正前の値：承認したときに確定する値（仮分類のカテゴリ、項目の最終優先度、そのカテゴリのマスタの部署）。

        仮分類がなければ、すべて `null`（Design 第7.3節）。
        """
        if item.classification is None:
            return ReviewValues()
        category = item.classification.category
        return ReviewValues(
            category=category, priority=item.final_priority, department=self._master[category].department
        )

    def _decide(self, item: ReviewItem, corrections: ReviewValues) -> _Confirmed:
        """最終値を決めて、検証する（Design 第5.4節 手順3・4）。エラーなら `ReviewResolutionError`。"""
        default = self._values_of_approval(item)
        category = corrections.category if corrections.category is not None else default.category
        if category is None or category is Category.UNCLASSIFIED:
            raise ReviewResolutionError("カテゴリが未分類のままです。--category で、確定するカテゴリを指定してください")
        if corrections.department is not None and corrections.department not in self.departments:
            raise ReviewResolutionError(
                f"担当部署 {corrections.department!r} は、マスタにありません。指定できる担当部署：{'、'.join(self.departments)}"
            )
        # 優先度は、項目に記録済みの最終優先度（緊急度キーワードによる「高」への引き上げを含む。BR-003）。
        priority = corrections.priority if corrections.priority is not None else item.final_priority
        department = (
            corrections.department if corrections.department is not None else self._master[category].department
        )
        return _Confirmed(category=category, priority=priority, department=department)

    @staticmethod
    def _summary_of(item: ReviewItem) -> str:
        if item.classification is not None:
            return item.classification.summary
        return item.inquiry.subject or item.inquiry.body[:FALLBACK_SUMMARY_LENGTH]

    def _note_of(self, item: ReviewItem, category: Category) -> str:
        """副次カテゴリの備考（BR-005）。確定したカテゴリと同じものは、副次として書かない。"""
        if item.classification is None:
            return ""
        secondary = [other for other in item.classification.secondary_categories if other is not category]
        return build_ticket_note(secondary, self._master)
