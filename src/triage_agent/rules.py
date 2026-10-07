"""ルールエンジン（CMP-007）：優先度、Human Review 理由、部署の決定。

すべて純粋関数である。I/O・LLM・時刻に依存せず、同じ入力には常に同じ結果を返す（NFR-004）。
Human Review へ回すか、自動登録するかは、LLM ではなく、ここのルールが分類結果の項目値から決める（ADR-002）。
"""

import unicodedata
from collections.abc import Mapping, Sequence

from triage_agent.config import CategoryEntry, Keywords
from triage_agent.models import (
    Category,
    ClassificationResult,
    ClassifyOutcome,
    ClassifyStatus,
    Decision,
    Inquiry,
    Language,
    Priority,
    ResultKind,
    ReviewReason,
)

# 分類に失敗した（分類結果がない）場合の Human Review の理由（REQ-006 の (g)〜(i)）。
_FAILURE_REASONS = {
    ClassifyStatus.GUARDRAIL_TRIPPED: ReviewReason.INPUT_GUARDRAIL,
    ClassifyStatus.LLM_FAILURE: ReviewReason.LLM_FAILURE,
    ClassifyStatus.INVALID_OUTPUT: ReviewReason.INVALID_OUTPUT,
}


def _normalize(text: str) -> str:
    return unicodedata.normalize("NFKC", text).casefold()


def find_keywords(text: str, keywords: Sequence[str]) -> list[str]:
    """`text` に含まれるキーワードを、`keywords` の順に、重複なしで返す（設定に書かれたままの表記）。

    NFKC 正規化と `casefold` の後、部分一致で照合する（ADR-009）。全角・半角と大文字・小文字の違いは同一とみなす。
    「障害者割引」が「障害」に一致する誤検出は、この方式の仕様である（安全側に倒れる）。
    ひらがな・カタカナ表記や、空白・ゼロ幅文字の挿入は、吸収されず、一致しない。
    """
    normalized_text = _normalize(text)
    hits: dict[str, None] = {}
    for keyword in keywords:
        normalized_keyword = _normalize(keyword)
        if not normalized_keyword:
            # 空のキーワードは、あらゆる文に一致してしまう。設定の検証（ERR-006）で防ぐが、ここでも黙って通さない。
            raise ValueError("キーワードが空です")
        if normalized_keyword in normalized_text:
            hits[keyword] = None
    return list(hits)


def decide_priority(llm_priority: Priority | None, urgent_hits: Sequence[str]) -> Priority:
    """最終的な優先度を決める。緊急度キーワードがあれば「高」。分類結果の優先度を下げない（REQ-004）。

    分類結果がなく `llm_priority` が `None` の場合は、緊急度キーワードがなければ「中」（REQ-004 AC-5）。
    """
    if urgent_hits:
        return Priority.HIGH
    if llm_priority is None:
        return Priority.MEDIUM
    return llm_priority


def _require_consistent(outcome: ClassifyOutcome) -> ClassificationResult | None:
    """分類結果を取り出す。成功なら分類結果が必須、失敗なら分類結果がないこと。食い違いは、作り込みの誤りとして検出する。"""
    if outcome.status is ClassifyStatus.SUCCESS:
        if outcome.classification is None:
            raise ValueError("分類が成功したのに、分類結果がありません")
        return outcome.classification
    if outcome.classification is not None:
        raise ValueError(f"分類が失敗（{outcome.status.value}）したのに、分類結果があります")
    return None


def collect_review_reasons(
    *,
    high_risk_hits: Sequence[str],
    outcome: ClassifyOutcome,
    confidence_threshold: float,
) -> list[ReviewReason]:
    """REQ-006 の9条件（a〜i）を評価し、該当した理由を全て、(a) から (i) の順で返す。

    分類結果がない場合（ガードレール該当・LLM 失敗・形式不正）は、キーワード（a）とその失敗の理由（g〜i）だけを評価する。
    確信度は、閾値「未満」だけを低確信度とする（ちょうど等しければ該当しない。REQ-006 AC-3）。
    """
    classification = _require_consistent(outcome)
    reasons: list[ReviewReason] = []

    if high_risk_hits:  # (a)
        reasons.append(ReviewReason.HIGH_RISK_KEYWORD)

    if classification is not None:
        if classification.is_complaint_or_legal or classification.category is Category.COMPLAINT:  # (b)
            reasons.append(ReviewReason.COMPLAINT_OR_LEGAL)
        if classification.category is Category.UNCLASSIFIED:  # (c)
            reasons.append(ReviewReason.UNCLASSIFIED)
        if classification.detected_language is not Language.JA:  # (d)
            reasons.append(ReviewReason.NON_JAPANESE)
        if classification.is_ambiguous:  # (e)
            reasons.append(ReviewReason.AMBIGUOUS_CATEGORY)
        # `<` ではなく `not >=` で比べる。数値でない値（NaN）が、黙って自動登録に通らないようにする。
        if not classification.confidence >= confidence_threshold:  # (f)
            reasons.append(ReviewReason.LOW_CONFIDENCE)
    else:
        reasons.append(_FAILURE_REASONS[outcome.status])  # (g)〜(i)

    return reasons


def resolve_department(category: Category, master: Mapping[Category, CategoryEntry]) -> str | None:
    """マスタから、カテゴリに対応する担当部署を返す。「未分類」は、部署がなく `None`（REQ-007 AC-4）。

    「クレーム対応」は、マスタの部署を返す。人間の承認（REQ-010）の後に使うためで、自動登録での
    アサインはしない（`evaluate` は、クレーム対応を必ず Human Review にする。REQ-007 AC-3）。
    """
    return master[category].department


def evaluate(
    inquiry: Inquiry,
    outcome: ClassifyOutcome,
    *,
    keywords: Keywords,
    master: Mapping[Category, CategoryEntry],
    confidence_threshold: float,
) -> Decision:
    """問い合わせと分類の結果から、最終的な優先度・検出キーワード・理由・区分・部署を決める。

    担当部署は、自動登録の場合だけ、マスタに従って決める（REQ-007 AC-1）。Human Review では、決めない。
    """
    # 件名と本文は、改行で区切った1つのテキストとして照合する。区切りなしで連結すると、
    # 件名の末尾と本文の先頭にまたがる語に一致してしまう（ADR-009）。
    text = "\n".join(part for part in (inquiry.subject, inquiry.body) if part is not None)
    high_risk_hits = find_keywords(text, keywords.high_risk)
    urgent_hits = find_keywords(text, keywords.urgent)

    classification = _require_consistent(outcome)
    reasons = collect_review_reasons(
        high_risk_hits=high_risk_hits, outcome=outcome, confidence_threshold=confidence_threshold
    )
    final_priority = decide_priority(classification.priority if classification else None, urgent_hits)

    # 分類結果がなければ、失敗の理由が必ず加わるため、`reasons` は空でない（`classification` の型の絞り込みも兼ねる）。
    if reasons or classification is None:
        return Decision(
            final_priority=final_priority,
            high_risk_hits=high_risk_hits,
            urgent_hits=urgent_hits,
            reasons=reasons,
            kind=ResultKind.HUMAN_REVIEW,
        )
    return Decision(
        final_priority=final_priority,
        high_risk_hits=high_risk_hits,
        urgent_hits=urgent_hits,
        reasons=reasons,
        kind=ResultKind.AUTO_REGISTERED,
        department=resolve_department(classification.category, master),
    )
