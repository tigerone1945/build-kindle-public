"""入力ガードレール（CMP-005）：極端に短い・意味不明な入力を、LLM を呼ぶ前に検出する。

判定はルールベースの純粋関数（`check_input`）で行い、SDK の `input_guardrail` で包む（ADR-003）。
Agents SDK に触れるコードは、この `guardrails.py` と `classifier.py` に閉じ込める（Design R-05）。
"""

from enum import StrEnum
from typing import Any

from agents import Agent, GuardrailFunctionOutput, RunContextWrapper, TResponseInputItem, input_guardrail
from pydantic import BaseModel, ConfigDict

from triage_agent.models import TriageRunContext


class GuardrailFailure(StrEnum):
    """不合格の理由。トレースにも残るため、問い合わせの内容を含まない固定の値にする（SEC-003）。"""

    TOO_SHORT = "too_short"
    NO_LETTER_OR_DIGIT = "no_letter_or_digit"


class GuardrailCheck(BaseModel):
    """`check_input` の結果。合格なら `failure` は `None`。"""

    model_config = ConfigDict(frozen=True)

    failure: GuardrailFailure | None = None

    @property
    def passed(self) -> bool:
        return self.failure is None


def check_input(body: str, min_length: int) -> GuardrailCheck:
    """本文が分類に回せるかを判定する（REQ-002 AC-1・AC-2）。

    - 空白を除いた文字数が `min_length` 未満なら不合格（ちょうど `min_length` なら合格）。
    - 文字・数字が1つもなければ不合格。
    両方に該当するときは、文字数の不合格を返す。
    """
    if sum(1 for char in body if not char.isspace()) < min_length:
        return GuardrailCheck(failure=GuardrailFailure.TOO_SHORT)
    # 文字・数字は `str.isalnum()` で判定する。正規表現の `\w` は、記号の `_` を含むため使わない
    # （「_____」は、REQ-002 AC-2 の「記号のみ」として不合格にする）。
    if not any(char.isalnum() for char in body):
        return GuardrailCheck(failure=GuardrailFailure.NO_LETTER_OR_DIGIT)
    return GuardrailCheck()


@input_guardrail(run_in_parallel=False)
def triage_input_guardrail(
    context: RunContextWrapper[TriageRunContext],
    agent: Agent[Any],
    input: str | list[TResponseInputItem],
) -> GuardrailFunctionOutput:
    """SDK の入力ガードレール。Agent の実行前に評価され、不合格なら LLM を呼ばずに打ち切る（REQ-002 AC-3）。

    本文と最小文字数は、Agent へ渡す整形済みの入力ではなく、実行コンテキストから取得する。
    """
    run_context = context.context
    check = check_input(run_context.inquiry.body, run_context.min_body_length)
    return GuardrailFunctionOutput(
        output_info={"passed": check.passed, "failure": check.failure},
        tripwire_triggered=not check.passed,
    )
