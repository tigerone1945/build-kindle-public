"""TASK-009：分類器の契約（REQ-003、ERR-002、NFR-004 / CMP-006 の契約の部分、第7.2節、ADR-004）。

インターフェース、出力検証、テスト用の Fake。SDK・LLM・ネットワークに依存しない。
"""

import math
import subprocess
import sys
from pathlib import Path

import pytest
from fakes import FakeClassifier, make_result

from triage_agent.classifier import MAX_TEXT_LENGTH, Classifier, Violation, validate_classification
from triage_agent.models import (
    Category,
    Channel,
    ClassifyOutcome,
    ClassifyStatus,
    Inquiry,
    Language,
)


def fields_of(violations: list[Violation]) -> list[str]:
    return [violation.field for violation in violations]


class TestValidClassification:
    def test_valid_result_has_no_violations(self) -> None:
        assert validate_classification(make_result()) == []

    def test_secondary_categories_may_be_empty_or_multiple(self) -> None:
        assert validate_classification(make_result(secondary_categories=[])) == []
        assert (
            validate_classification(make_result(secondary_categories=[Category.SUPPORT, Category.TECHNICAL])) == []
        )

    def test_unclassified_is_a_valid_primary_category(self) -> None:
        assert validate_classification(make_result(category=Category.UNCLASSIFIED)) == []

    def test_all_flags_and_languages_are_accepted_as_they_are(self) -> None:
        # 言語・クレーム性・曖昧さは、値の検証ではなく、ルールエンジンが判断する（ADR-004）。
        result = make_result(detected_language=Language.OTHER, is_complaint_or_legal=True, is_ambiguous=True)
        assert validate_classification(result) == []


class TestConfidenceRange:
    @pytest.mark.parametrize("confidence", [0.0, 0.5, 0.7, 1.0])
    def test_within_range_passes(self, confidence: float) -> None:
        assert validate_classification(make_result(confidence=confidence)) == []

    @pytest.mark.parametrize("confidence", [-0.01, -1.0, 1.01, 2.0, 100.0])
    def test_out_of_range_fails(self, confidence: float) -> None:
        assert fields_of(validate_classification(make_result(confidence=confidence))) == ["confidence"]

    @pytest.mark.parametrize("confidence", [math.nan, math.inf, -math.inf])
    def test_non_finite_fails(self, confidence: float) -> None:
        # NaN は、数値の比較が常に偽になり、範囲内と誤判定されやすい。
        assert fields_of(validate_classification(make_result(confidence=confidence))) == ["confidence"]


@pytest.mark.parametrize("field", ["summary", "rationale"])
class TestTextFields:
    """要約・根拠：空でない、200文字以内（Design 第7.2節）。"""

    def test_empty_fails(self, field: str) -> None:
        assert fields_of(validate_classification(make_result(**{field: ""}))) == [field]

    @pytest.mark.parametrize("blank", [" ", "   ", "\n", "　　", " \t\n "])
    def test_blank_fails(self, field: str, blank: str) -> None:
        assert fields_of(validate_classification(make_result(**{field: blank}))) == [field]

    def test_one_character_passes(self, field: str) -> None:
        assert validate_classification(make_result(**{field: "あ"})) == []

    def test_exactly_max_length_passes(self, field: str) -> None:
        assert validate_classification(make_result(**{field: "あ" * MAX_TEXT_LENGTH})) == []

    def test_over_max_length_fails(self, field: str) -> None:
        text = "あ" * (MAX_TEXT_LENGTH + 1)
        assert fields_of(validate_classification(make_result(**{field: text}))) == [field]

    def test_length_counts_characters_not_bytes(self, field: str) -> None:
        # 200文字の日本語は600バイト（UTF-8）だが、文字数で数える。
        text = "あ" * MAX_TEXT_LENGTH
        assert len(text.encode("utf-8")) > MAX_TEXT_LENGTH
        assert validate_classification(make_result(**{field: text})) == []


def test_max_text_length_is_200() -> None:
    assert MAX_TEXT_LENGTH == 200


class TestSecondaryCategories:
    def test_containing_unclassified_fails(self) -> None:
        result = make_result(secondary_categories=[Category.UNCLASSIFIED])
        violations = validate_classification(result)
        assert fields_of(violations) == ["secondary_categories"]
        assert "未分類" in violations[0].reason

    def test_duplicating_the_primary_category_fails(self) -> None:
        result = make_result(category=Category.BILLING, secondary_categories=[Category.BILLING])
        violations = validate_classification(result)
        assert fields_of(violations) == ["secondary_categories"]
        assert "主カテゴリ" in violations[0].reason

    def test_duplicates_within_the_list_fail(self) -> None:
        result = make_result(secondary_categories=[Category.SUPPORT, Category.SUPPORT])
        violations = validate_classification(result)
        assert fields_of(violations) == ["secondary_categories"]
        assert "重複" in violations[0].reason

    def test_primary_unclassified_with_secondary_unclassified_fails(self) -> None:
        # 主カテゴリが未分類でも、副次カテゴリに未分類は入れられない（主との重複でもある）。
        result = make_result(category=Category.UNCLASSIFIED, secondary_categories=[Category.UNCLASSIFIED])
        assert fields_of(validate_classification(result)) == ["secondary_categories"] * 2

    def test_a_valid_secondary_with_a_different_violation_is_not_flagged(self) -> None:
        result = make_result(secondary_categories=[Category.SUPPORT], confidence=1.5)
        assert fields_of(validate_classification(result)) == ["confidence"]


class TestViolationReporting:
    def test_all_violations_are_returned_in_field_order(self) -> None:
        result = make_result(
            confidence=1.5,
            summary="",
            rationale="あ" * (MAX_TEXT_LENGTH + 1),
            secondary_categories=[Category.UNCLASSIFIED],
        )
        assert fields_of(validate_classification(result)) == [
            "confidence",
            "summary",
            "rationale",
            "secondary_categories",
        ]

    def test_each_violation_states_its_reason(self) -> None:
        violations = validate_classification(make_result(confidence=1.5, summary=""))
        assert all(violation.reason for violation in violations)
        assert [str(violation) for violation in violations] == [
            f"{violation.field}: {violation.reason}" for violation in violations
        ]

    def test_violations_do_not_contain_the_classification_content(self) -> None:
        # 違反は、処理ログの error_detail に残りうる。要約・根拠の本文（個人情報を含みうる）を含めない。
        secret = "090-1234-5678"
        too_long = make_result(summary=secret + "あ" * MAX_TEXT_LENGTH, rationale=secret + "い" * MAX_TEXT_LENGTH)
        blank = make_result(summary=" ", rationale="\n")
        for result in (too_long, blank):
            violations = validate_classification(result)
            assert fields_of(violations) == ["summary", "rationale"]
            assert secret not in "".join(str(violation) for violation in violations)
        # 空白のみの本文も、違反の文言へ含めない。
        assert all(" " not in v.reason and "\n" not in v.reason for v in validate_classification(blank))

    def test_validation_does_not_raise_or_modify_the_result(self) -> None:
        result = make_result(confidence=-1.0, summary="")
        before = result.model_copy(deep=True)
        validate_classification(result)
        assert result == before


class TestFakeClassifier:
    INQUIRY = Inquiry(inquiry_id="INQ-1", channel=Channel.EMAIL, body="請求書の内容について確認したい")

    def test_satisfies_the_classifier_interface(self) -> None:
        assert isinstance(FakeClassifier(ClassifyOutcome(status=ClassifyStatus.LLM_FAILURE)), Classifier)

    def test_returns_the_given_outcome(self) -> None:
        outcome = ClassifyOutcome(status=ClassifyStatus.SUCCESS, classification=make_result(), attempts=1)
        assert FakeClassifier(outcome).classify(self.INQUIRY, "req-1") is outcome

    def test_returns_the_same_outcome_for_every_inquiry(self) -> None:
        outcome = ClassifyOutcome(status=ClassifyStatus.GUARDRAIL_TRIPPED)
        fake = FakeClassifier(outcome)
        other = Inquiry(inquiry_id="INQ-2", channel=Channel.FORM, body="別の問い合わせです")
        assert fake.classify(self.INQUIRY, "req-1") is outcome
        assert fake.classify(other, "req-2") is outcome

    def test_returns_failure_outcomes_as_values(self) -> None:
        for status in (ClassifyStatus.GUARDRAIL_TRIPPED, ClassifyStatus.LLM_FAILURE, ClassifyStatus.INVALID_OUTPUT):
            outcome = ClassifyOutcome(status=status, attempts=3, error_detail="TimeoutError")
            assert FakeClassifier(outcome).classify(self.INQUIRY, "req-1") == outcome

    def test_returns_the_outcome_for_each_inquiry_id(self) -> None:
        success = ClassifyOutcome(status=ClassifyStatus.SUCCESS, classification=make_result(), attempts=1)
        failure = ClassifyOutcome(status=ClassifyStatus.LLM_FAILURE, attempts=3)
        fake = FakeClassifier({"INQ-1": success, "INQ-2": failure})
        other = Inquiry(inquiry_id="INQ-2", channel=Channel.FORM, body="別の問い合わせです")
        assert fake.classify(self.INQUIRY, "req-1") is success
        assert fake.classify(other, "req-2") is failure

    def test_unknown_inquiry_id_is_a_test_error(self) -> None:
        fake = FakeClassifier({"INQ-9": ClassifyOutcome(status=ClassifyStatus.LLM_FAILURE)})
        with pytest.raises(KeyError, match="INQ-1.*設定されていません"):
            fake.classify(self.INQUIRY, "req-1")

    def test_inquiry_without_id_is_a_test_error_in_per_id_mode(self) -> None:
        fake = FakeClassifier({"INQ-1": ClassifyOutcome(status=ClassifyStatus.LLM_FAILURE)})
        with pytest.raises(KeyError, match="設定されていません"):
            fake.classify(Inquiry(channel=Channel.EMAIL, body="IDのない問い合わせ"), "req-1")

    def test_records_the_calls(self) -> None:
        fake = FakeClassifier(ClassifyOutcome(status=ClassifyStatus.LLM_FAILURE))
        assert fake.calls == []
        fake.classify(self.INQUIRY, "req-1")
        assert fake.calls == [(self.INQUIRY, "req-1")]


def test_pure_logic_and_fakes_do_not_import_the_agents_sdk() -> None:
    """ルール・モデル・テスト用の Fake は、SDK に依存しない（LLM なしで進められる。NFR-004）。別プロセスで確認する。

    `triage_agent.classifier` は、`AgentsSdkClassifier`（TASK-021）が SDK を使うため、対象に含めない。
    """
    tests_dir = Path(__file__).parent
    code = (
        f"import sys; sys.path.insert(0, {str(tests_dir)!r}); "
        "import triage_agent.models, triage_agent.rules, fakes; "
        "sys.exit(1 if 'agents' in sys.modules else 0)"
    )
    completed = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=False)
    assert completed.returncode == 0, completed.stderr
