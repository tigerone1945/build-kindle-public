"""処理ログ（CMP-013）。

問い合わせの処理結果と、Human Review の解決を、`process_log.jsonl` へ追記する（REQ-012）。
記録は `record_type`（`inquiry` / `review_resolution`）で区別する。

個人情報（メールアドレス・電話番号）のマスキングは、呼び出し側に任せず、ここで必ず適用する（SEC-002）。
対象は、入力内容（件名・本文・送信者）とエラー情報である（REQ-012 AC-4、Assumption A-03）。
"""

from pathlib import Path

from triage_agent.masking import mask_pii
from triage_agent.models import MaskedInput, ProcessLogRecord
from triage_agent.storage import JsonlStore

PROCESS_LOG_FILE = "process_log.jsonl"


class ProcessLog:
    """マスキングつきの、追記専用の処理ログ。"""

    def __init__(self, output_dir: Path) -> None:
        self._store = JsonlStore(output_dir / PROCESS_LOG_FILE)

    def append(self, record: ProcessLogRecord) -> ProcessLogRecord:
        """マスキングしたレコードを追記し、そのレコードを返す。渡されたレコード自体は変更しない。"""
        masked = _mask_record(record)
        self._store.append(masked)
        return masked


def _mask_optional(text: str | None) -> str | None:
    return None if text is None else mask_pii(text)


def _mask_record(record: ProcessLogRecord) -> ProcessLogRecord:
    masked_input = (
        None
        if record.input is None
        else MaskedInput(
            subject=_mask_optional(record.input.subject),
            body=mask_pii(record.input.body),
            sender=_mask_optional(record.input.sender),
        )
    )
    return record.model_copy(update={"input": masked_input, "error": _mask_optional(record.error)})
