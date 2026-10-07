"""チケットシステム（モック）と再試行キュー（CMP-010）。

チケットシステムは、講座4では実物を使わず、`tickets.jsonl` への追記で代替する（ネットワーク通信は行わない）。
チケットの登録は、この `create_ticket` だけが行う。LLM には、登録の手段を与えない（SEC-004、ADR-002）。
登録に失敗した内容は、再試行キューへ記録する（ERR-003）。キューの消化（再登録）は、講座4の対象外である。
"""

import uuid
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Literal, Protocol, runtime_checkable

from triage_agent.clock import Clock, utc_now
from triage_agent.config import CategoryEntry
from triage_agent.models import Category, RetryQueueEntry, TicketRecord, TicketRequest
from triage_agent.storage import JsonlStore, StorageError

TICKETS_FILE = "tickets.jsonl"
RETRY_QUEUE_FILE = "retry_queue.jsonl"


def build_ticket_note(secondary_categories: Sequence[Category], master: Mapping[Category, CategoryEntry]) -> str:
    """チケットの備考を、副次カテゴリから作る（REQ-008 AC-1、BR-005、Assumption A-08）。なければ空文字。

    自動登録（Pipeline）と、Human Review の解決（ReviewResolver）が、同じ書式で作る。
    """
    if not secondary_categories:
        return ""
    labels = "、".join(master[category].label for category in secondary_categories)
    return f"副次カテゴリ: {labels}"


class TicketRegistrationError(Exception):
    """チケットを登録できなかった。メッセージには、登録内容（問い合わせの要約など）を含めない。"""


@runtime_checkable
class TicketSystem(Protocol):
    """チケットシステム。登録に失敗したときは、`TicketRegistrationError` を送出する（Design 第8.2節）。"""

    def create_ticket(self, request: TicketRequest) -> TicketRecord: ...


class MockTicketSystem:
    """`tickets.jsonl` へ追記するだけの、チケットシステムの代替（`TicketSystem` を満たす）。

    テストで失敗を再現できるよう、`fail_if` に述語を渡すと、その述語が真になる登録が失敗する
    （`TicketRegistrationError`）。属性なので、テストの途中で差し替えたり、`None` に戻したりできる。
    `tickets.jsonl` への書き込みの失敗も、チケットシステム側の失敗として、`TicketRegistrationError` にする。
    """

    def __init__(
        self,
        output_dir: Path,
        *,
        fail_if: Callable[[TicketRequest], bool] | None = None,
        clock: Clock = utc_now,
    ) -> None:
        self._store = JsonlStore(output_dir / TICKETS_FILE)
        self.fail_if = fail_if
        self._clock = clock

    def create_ticket(self, request: TicketRequest) -> TicketRecord:
        if self.fail_if is not None and self.fail_if(request):
            raise TicketRegistrationError("チケットを登録できません（注入された失敗）")
        # チケットIDは、`TCK-` と、ランダムな UUID の先頭8桁（一意性は確率的。ADR-012）。
        record = TicketRecord.model_validate(
            {
                **request.model_dump(),
                "ticket_id": f"TCK-{uuid.uuid4().hex[:8]}",
                "created_at": self._clock(),
            }
        )
        try:
            self._store.append(record)
        except StorageError as error:
            # 追記できなければ、チケットは登録されていない。ID を返さず、失敗として扱う（メッセージに登録内容を含まない）。
            raise TicketRegistrationError(f"チケットを登録できません（{error}）") from error
        return record


class RetryQueue:
    """チケットの登録に失敗した内容と理由を、`retry_queue.jsonl` へ追記する（DATA-007、ERR-003）。

    キューへの書き込みの失敗は、`StorageError` として送出する（握りつぶさない。Design 第10章）。
    """

    def __init__(self, output_dir: Path, *, clock: Clock = utc_now) -> None:
        self._store = JsonlStore(output_dir / RETRY_QUEUE_FILE)
        self._clock = clock

    def enqueue(
        self,
        request: TicketRequest,
        error: str,
        source: Literal["pipeline", "review_resolution"],
    ) -> RetryQueueEntry:
        entry = RetryQueueEntry(request=request, error=error, queued_at=self._clock(), source=source)
        self._store.append(entry)
        return entry
