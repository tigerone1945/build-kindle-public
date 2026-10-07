"""Pipeline（CMP-012）：1件の問い合わせを、分類 → ルール適用 → 登録 → ログ の順に処理する。

問い合わせは、1件ずつ独立に処理する（Design 第5.3節）。分類器・チケットシステム・キュー・処理ログは、
外から受け取る（テストで差し替えられる。NFR-004）。

失敗の扱い：
- 1件の想定外の例外は、`process_error` として記録し、次の問い合わせへ進む（ERR-005）。
- ログ・キューの書き込み失敗（`StorageError`）は、想定外の例外として扱わず、捕捉せずに送出する（ERR-007、ADR-013）。
  追跡できない処理を続けないため。`PROCESS_ERROR` の記録も、同じファイルへの書き込みなので、失敗する。
  中断した問い合わせを示せるよう、処理中の `request_id` を `current_request_id` として公開する。
- チケット登録の失敗は、再試行キューへ登録し、`ticket_failed` として継続する（ERR-003）。
"""

import uuid

from triage_agent.classifier import Classifier
from triage_agent.clock import Clock, utc_now
from triage_agent.config import AppConfig
from triage_agent.models import (
    Category,
    ClassificationResult,
    ClassifyOutcome,
    Decision,
    InvalidRecord,
    Inquiry,
    MaskedInput,
    ProcessLogRecord,
    ProcessResult,
    ResultKind,
    TicketRequest,
)
from triage_agent.process_log import ProcessLog
from triage_agent.review import ReviewQueue
from triage_agent.rules import evaluate
from triage_agent.storage import StorageError
from triage_agent.tickets import RetryQueue, TicketRegistrationError, TicketSystem, build_ticket_note


class Pipeline:
    """1件の問い合わせを処理する。逐次処理のため、処理中の問い合わせは常に1件である。"""

    def __init__(
        self,
        *,
        classifier: Classifier,
        config: AppConfig,
        tickets: TicketSystem,
        retry_queue: RetryQueue,
        review_queue: ReviewQueue,
        process_log: ProcessLog,
        clock: Clock = utc_now,
    ) -> None:
        self._classifier = classifier
        self._config = config
        self._tickets = tickets
        self._retry_queue = retry_queue
        self._review_queue = review_queue
        self._process_log = process_log
        self._clock = clock
        self._current_request_id: str | None = None

    @property
    def current_request_id(self) -> str | None:
        """処理中の（書き込みの失敗で、処理を中断した）問い合わせの `request_id`。処理中でなければ `None`。

        採番の直後から、その件の処理が完了するまで、設定される。CLI が、中断した問い合わせを示すために使う（ERR-007）。
        """
        return self._current_request_id

    def process(self, inquiry: Inquiry) -> ProcessResult:
        """1件の問い合わせを処理する。`StorageError` は、送出する（ERR-007）。"""
        request_id = self._begin()
        try:
            result = self._process(inquiry, request_id)
        except StorageError:
            raise
        except Exception as error:
            result = self._record_process_error(inquiry, request_id, error)
        self._current_request_id = None
        return result

    def record_invalid_input(self, invalid: InvalidRecord) -> ProcessResult:
        """入力エラーの1件を、処理ログへ記録し、結果を返す。分類・ルール・登録は行わない（REQ-001 AC-5、ERR-004）。

        入力の生の内容は、記録しない（位置と理由だけ。REQ-012 AC-1）。`StorageError` は、送出する（ERR-007）。
        """
        request_id = self._begin()
        self._process_log.append(
            ProcessLogRecord(
                record_type="inquiry",
                request_id=request_id,
                inquiry_id=invalid.inquiry_id,
                processed_at=self._clock(),
                kind=ResultKind.INPUT_ERROR,
                error=invalid.reason,
                position=invalid.position,
            )
        )
        self._current_request_id = None
        return ProcessResult(
            request_id=request_id,
            inquiry_id=invalid.inquiry_id,
            kind=ResultKind.INPUT_ERROR,
            error=invalid.reason,
            position=invalid.position,
        )

    def _begin(self) -> str:
        # リクエストIDは、UUID の全体を用いる（一意性は、ID の短縮（uuid8）とは異なり、実質的に保証される。ADR-012）。
        request_id = str(uuid.uuid4())
        self._current_request_id = request_id
        return request_id

    def _process(self, inquiry: Inquiry, request_id: str) -> ProcessResult:
        outcome = self._classifier.classify(inquiry, request_id)
        decision = evaluate(
            inquiry,
            outcome,
            keywords=self._config.keywords,
            master=self._config.categories,
            confidence_threshold=self._config.settings.decision.confidence_threshold,
        )
        if decision.kind is ResultKind.AUTO_REGISTERED:
            return self._register_ticket(inquiry, request_id, outcome, decision)
        return self._flag_for_review(inquiry, request_id, outcome, decision)

    def _register_ticket(
        self, inquiry: Inquiry, request_id: str, outcome: ClassifyOutcome, decision: Decision
    ) -> ProcessResult:
        """自動登録：チケットを登録する。失敗したら、再試行キューへ登録し、`ticket_failed` にする（ERR-003）。"""
        classification = outcome.classification
        if classification is None:
            raise ValueError("自動登録の判定なのに、分類結果がありません")
        request = TicketRequest(
            category=classification.category,
            priority=decision.final_priority,
            summary=classification.summary,
            department=decision.department,
            confidence=classification.confidence,
            note=build_ticket_note(classification.secondary_categories, self._config.categories),
            inquiry_id=inquiry.inquiry_id,
            request_id=request_id,
        )
        try:
            ticket = self._tickets.create_ticket(request)
        except TicketRegistrationError as error:
            self._retry_queue.enqueue(request, str(error), "pipeline")
            message = f"チケットの登録に失敗し、再試行キューへ登録しました（{error}）"
            self._append_log(
                inquiry, request_id, outcome, decision, kind=ResultKind.TICKET_FAILED, error=message
            )
            return self._result(
                inquiry, request_id, outcome, decision, kind=ResultKind.TICKET_FAILED, error=message
            )
        self._append_log(
            inquiry, request_id, outcome, decision, kind=ResultKind.AUTO_REGISTERED, ticket_id=ticket.ticket_id
        )
        return self._result(
            inquiry, request_id, outcome, decision, kind=ResultKind.AUTO_REGISTERED, ticket_id=ticket.ticket_id
        )

    def _flag_for_review(
        self, inquiry: Inquiry, request_id: str, outcome: ClassifyOutcome, decision: Decision
    ) -> ProcessResult:
        """Human Review：キューへ登録する。チケットは登録しない（REQ-009 AC-3）。"""
        item = self._review_queue.flag_for_review(
            inquiry=inquiry, classification=outcome.classification, decision=decision, request_id=request_id
        )
        # 分類の失敗（LLM 失敗・形式不正）の理由は、種類と要約だけ（内容を含まない）。結果と処理ログに残す（ERR-001、ERR-002）。
        error = outcome.error_detail
        self._append_log(
            inquiry, request_id, outcome, decision, kind=ResultKind.HUMAN_REVIEW, review_id=item.review_id, error=error
        )
        return self._result(
            inquiry, request_id, outcome, decision, kind=ResultKind.HUMAN_REVIEW, review_id=item.review_id, error=error
        )

    def _record_process_error(self, inquiry: Inquiry, request_id: str, error: Exception) -> ProcessResult:
        """想定外の例外を、処理ログへ記録する（ERR-005）。

        処理ログには、例外の種類とメッセージを残す（マスキングは、処理ログが行う）。標準出力の結果には、
        例外の種類だけを含める。メッセージには、問い合わせの内容が入りうるため（Q-10）。
        """
        kind_name = type(error).__name__
        self._process_log.append(
            ProcessLogRecord(
                record_type="inquiry",
                request_id=request_id,
                inquiry_id=inquiry.inquiry_id,
                processed_at=self._clock(),
                input=_input_of(inquiry),
                kind=ResultKind.PROCESS_ERROR,
                error=f"{kind_name}: {error}",
            )
        )
        return ProcessResult(
            request_id=request_id, inquiry_id=inquiry.inquiry_id, kind=ResultKind.PROCESS_ERROR, error=kind_name
        )

    def _append_log(
        self,
        inquiry: Inquiry,
        request_id: str,
        outcome: ClassifyOutcome,
        decision: Decision,
        *,
        kind: ResultKind,
        ticket_id: str | None = None,
        review_id: str | None = None,
        error: str | None = None,
    ) -> None:
        """処理ログへ、1件を追記する（REQ-012 AC-1）。マスキングは、処理ログが行う（SEC-002）。"""
        self._process_log.append(
            ProcessLogRecord(
                record_type="inquiry",
                request_id=request_id,
                inquiry_id=inquiry.inquiry_id,
                processed_at=self._clock(),
                input=_input_of(inquiry),
                classification=outcome.classification,
                attempts=outcome.attempts,
                high_risk_hits=decision.high_risk_hits,
                urgent_hits=decision.urgent_hits,
                final_priority=decision.final_priority,
                kind=kind,
                reasons=decision.reasons,
                ticket_id=ticket_id,
                review_id=review_id,
                error=error,
            )
        )

    @staticmethod
    def _result(
        inquiry: Inquiry,
        request_id: str,
        outcome: ClassifyOutcome,
        decision: Decision,
        *,
        kind: ResultKind,
        ticket_id: str | None = None,
        review_id: str | None = None,
        error: str | None = None,
    ) -> ProcessResult:
        classification: ClassificationResult | None = outcome.classification
        # 分類結果がない（入力ガードレール該当・LLM 失敗・形式不正）問い合わせは、「未分類」として扱う（REQ-002 AC-1）。
        category = classification.category if classification is not None else Category.UNCLASSIFIED
        return ProcessResult(
            request_id=request_id,
            inquiry_id=inquiry.inquiry_id,
            kind=kind,
            category=category,
            priority=decision.final_priority,
            confidence=classification.confidence if classification is not None else None,
            department=decision.department,
            ticket_id=ticket_id,
            review_id=review_id,
            reasons=decision.reasons,
            error=error,
        )


def _input_of(inquiry: Inquiry) -> MaskedInput:
    """処理ログへ渡す入力（件名・本文・送信者）。マスキングは、処理ログが適用する（SEC-002）。"""
    return MaskedInput(subject=inquiry.subject, body=inquiry.body, sender=inquiry.sender)

