"""テスト用の分類器（Fake）。LLM・ネットワークに接続せず、決められた分類の結果を返す（NFR-004、Design 第13.1節）。

`from fakes import FakeClassifier` で使う（`tests/` は、パッケージではなく、pytest が import パスへ加えるディレクトリ）。
"""

from collections.abc import Mapping

from triage_agent.models import (
    Category,
    ClassificationResult,
    ClassifyOutcome,
    Inquiry,
    Language,
    Priority,
)


class FakeClassifier:
    """`Classifier`（`triage_agent.classifier`）を満たす。

    - `ClassifyOutcome` を1つ渡すと、どの問い合わせにも同じ結果を返す。
    - 問い合わせIDから `ClassifyOutcome` への辞書を渡すと、問い合わせIDごとの結果を返す。
      辞書にない問い合わせIDは、テストの誤りとして `KeyError` にする（黙って成功にしない）。
    呼ばれた内容は `calls` に記録する（「分類器が呼ばれなかった」ことの確認に使う）。
    """

    def __init__(self, outcomes: ClassifyOutcome | Mapping[str, ClassifyOutcome]) -> None:
        self._outcomes = outcomes
        self.calls: list[tuple[Inquiry, str]] = []

    def classify(self, inquiry: Inquiry, request_id: str) -> ClassifyOutcome:
        self.calls.append((inquiry, request_id))
        if isinstance(self._outcomes, ClassifyOutcome):
            return self._outcomes
        if inquiry.inquiry_id is None or inquiry.inquiry_id not in self._outcomes:
            raise KeyError(f"FakeClassifier に、問い合わせID {inquiry.inquiry_id!r} の結果が設定されていません")
        return self._outcomes[inquiry.inquiry_id]


def make_result(**overrides: object) -> ClassificationResult:
    """検証に合格する分類結果を基本とし、指定した項目だけを差し替える。

    値の範囲などの検証は、モデルではなく `validate_classification` で行うため（Design 第7.2節）、
    範囲外の値でもモデルは作れる。
    """
    values: dict[str, object] = {
        "category": Category.BILLING,
        "priority": Priority.MEDIUM,
        "confidence": 0.9,
        "summary": "請求書の内容についての確認依頼。",
        "secondary_categories": [],
        "detected_language": Language.JA,
        "is_complaint_or_legal": False,
        "is_ambiguous": False,
        "rationale": "請求書の確認を求めているため、請求に分類した。",
    }
    values.update(overrides)
    return ClassificationResult.model_validate(values)
