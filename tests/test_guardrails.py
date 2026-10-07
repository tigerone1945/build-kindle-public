"""TASK-008：入力ガードレール（REQ-002 / CMP-005、ADR-003）。

LLM・ネットワーク・ファイルに依存しない。SDK のガードレールは、Runner を使わず、`InputGuardrail.run` で直接評価する。
"""

import asyncio

import pytest
from agents import Agent, RunContextWrapper
from pydantic import ValidationError

from triage_agent.guardrails import (
    GuardrailFailure,
    check_input,
    triage_input_guardrail,
)
from triage_agent.models import Channel, Inquiry, TriageRunContext

MIN_LENGTH = 5


class TestCheckInputLength:
    """REQ-002 AC-1：空白を除く文字数が最小文字数未満なら不合格。"""

    def test_four_characters_fail(self) -> None:
        check = check_input("解約した", MIN_LENGTH)
        assert not check.passed
        assert check.failure is GuardrailFailure.TOO_SHORT

    def test_exactly_min_length_passes(self) -> None:
        assert check_input("あいうえお", MIN_LENGTH).passed

    def test_min_length_plus_one_passes(self) -> None:
        assert check_input("あいうえおか", MIN_LENGTH).passed

    def test_empty_body_fails(self) -> None:
        assert check_input("", MIN_LENGTH).failure is GuardrailFailure.TOO_SHORT

    @pytest.mark.parametrize(
        "body",
        [
            "     ",  # 半角スペースのみ
            "\u3000\u3000\u3000\u3000\u3000\u3000",  # 全角スペースのみ
            "\t\n\r \u3000\u00a0",  # タブ・改行・全角スペース・改行なしスペース
        ],
    )
    def test_whitespace_only_fails(self, body: str) -> None:
        assert not check_input(body, MIN_LENGTH).passed

    def test_whitespace_is_not_counted(self) -> None:
        # 空白を含めれば5文字以上だが、空白を除くと4文字。
        assert not check_input("あ い う え", MIN_LENGTH).passed
        assert not check_input("あ\u3000い\nう\tえ", MIN_LENGTH).passed
        # 空白を除いてちょうど5文字なら合格。
        assert check_input("あ い う え お", MIN_LENGTH).passed

    def test_length_counts_characters_not_bytes(self) -> None:
        # 日本語は1文字が複数バイトだが、数えるのは文字数。
        assert check_input("解約したい", MIN_LENGTH).passed
        assert not check_input("解約した", MIN_LENGTH).passed

    def test_threshold_follows_the_argument(self) -> None:
        assert check_input("あいう", 3).passed
        assert not check_input("あいう", 4).passed


class TestCheckInputLetterOrDigit:
    """REQ-002 AC-2：文字・数字が1つもなければ不合格。"""

    @pytest.mark.parametrize(
        "body",
        [
            "?????",
            "！！！！！！",
            "...。。。、、、",
            "@#$%^&*()",
            "-----",
            "_____",  # `\w` には含まれるが、記号であり、文字・数字ではない
            "😀😀😀😀😀",  # 絵文字
        ],
    )
    def test_symbols_only_fail_with_no_letter_or_digit(self, body: str) -> None:
        check = check_input(body, MIN_LENGTH)
        assert not check.passed
        assert check.failure is GuardrailFailure.NO_LETTER_OR_DIGIT

    @pytest.mark.parametrize(
        "body",
        [
            "解約したい",  # 漢字・ひらがな
            "ｶﾀｶﾅｶﾅ",  # 半角カタカナ
            "12345",  # 半角数字
            "１２３４５",  # 全角数字
            "hello",  # 英字
            "ＡＢＣＤＥ",  # 全角英字
        ],
    )
    def test_letters_and_digits_pass(self, body: str) -> None:
        assert check_input(body, MIN_LENGTH).passed

    def test_a_single_letter_among_symbols_is_enough(self) -> None:
        assert check_input("????あ????", MIN_LENGTH).passed
        assert check_input("!!!!!7!!!!!", MIN_LENGTH).passed

    def test_whitespace_between_symbols_does_not_count_as_letters(self) -> None:
        check = check_input("? ? ? ? ? ?", MIN_LENGTH)
        assert check.failure is GuardrailFailure.NO_LETTER_OR_DIGIT


class TestCheckInputBothRules:
    def test_short_symbols_report_the_length_failure(self) -> None:
        # 短く、かつ記号のみ。文字数の不合格を先に返す。
        assert check_input("???", MIN_LENGTH).failure is GuardrailFailure.TOO_SHORT

    def test_failure_value_carries_no_input_content(self) -> None:
        # トレースに残る値。問い合わせの内容を含まない固定の値であること（SEC-003）。
        assert {failure.value for failure in GuardrailFailure} == {"too_short", "no_letter_or_digit"}

    def test_result_is_immutable(self) -> None:
        check = check_input("解約したい", MIN_LENGTH)
        with pytest.raises(ValidationError):
            check.failure = GuardrailFailure.TOO_SHORT  # type: ignore[misc]


def _run_guardrail(body: str, min_length: int = MIN_LENGTH) -> tuple[bool, object]:
    """SDK のガードレールを、実行コンテキストだけを渡して評価する。`(tripwire_triggered, output_info)` を返す。"""
    context = RunContextWrapper(
        context=TriageRunContext(
            inquiry=Inquiry(channel=Channel.EMAIL, body=body),
            min_body_length=min_length,
        )
    )
    agent: Agent[TriageRunContext] = Agent(name="test")
    # Agent へ渡す整形済みの入力は、本文とは無関係な文字列にする（本文は実行コンテキストから取得する）。
    result = asyncio.run(triage_input_guardrail.run(agent, "<inquiry>formatted input</inquiry>", context))
    return result.output.tripwire_triggered, result.output.output_info


class TestSdkGuardrail:
    def test_is_registered_as_sequential_input_guardrail(self) -> None:
        # 並列実行にすると、不合格でも LLM が呼ばれる。実行前に評価する（REQ-002 AC-3）。
        assert triage_input_guardrail.run_in_parallel is False

    def test_failing_body_triggers_tripwire(self) -> None:
        tripped, info = _run_guardrail("あいう")
        assert tripped is True
        assert info == {"passed": False, "failure": GuardrailFailure.TOO_SHORT}

    def test_symbols_only_body_triggers_tripwire(self) -> None:
        tripped, info = _run_guardrail("??????")
        assert tripped is True
        assert info == {"passed": False, "failure": GuardrailFailure.NO_LETTER_OR_DIGIT}

    def test_whitespace_only_body_triggers_tripwire(self) -> None:
        tripped, _ = _run_guardrail("\u3000\u3000\u3000\u3000\u3000\u3000\u3000")
        assert tripped is True

    def test_passing_body_does_not_trigger_tripwire(self) -> None:
        tripped, info = _run_guardrail("解約したい")
        assert tripped is False
        assert info == {"passed": True, "failure": None}

    def test_min_length_is_taken_from_the_run_context(self) -> None:
        assert _run_guardrail("あいう", min_length=3)[0] is False
        assert _run_guardrail("あいう", min_length=4)[0] is True

    def test_body_is_taken_from_the_run_context_not_from_the_agent_input(self) -> None:
        # Agent へ渡す入力が十分に長くても、実行コンテキストの本文が不合格なら不合格。
        context = RunContextWrapper(
            context=TriageRunContext(inquiry=Inquiry(channel=Channel.FORM, body="あ"), min_body_length=MIN_LENGTH)
        )
        agent: Agent[TriageRunContext] = Agent(name="test")
        long_input = "<inquiry>" + "とても長い整形済みの入力です。" * 10 + "</inquiry>"
        result = asyncio.run(triage_input_guardrail.run(agent, long_input, context))
        assert result.output.tripwire_triggered is True

    def test_output_info_does_not_contain_the_body(self) -> None:
        # output_info はトレースに残る。本文（個人情報を含みうる）を含めない（SEC-003）。
        body = "a@b"  # 短くて不合格になるが、メールアドレスの形
        _, info = _run_guardrail(body)
        assert body not in repr(info)
