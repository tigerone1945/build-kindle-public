"""ドメインの型（CMP-003）：列挙型と Pydantic モデル。

他のコンポーネントの共通言語であり、ロジックは持たない。
値の範囲・文字数の検証（確信度 0.0〜1.0、要約の長さなど）は、LLM へ渡すスキーマへ持たせず、
受け取った後の出力検証（分類器の契約）で行う（Design 第7.2節）。
ID の採番（INQ-/TCK-/REV-）と時刻の付与は、それぞれの生成元コンポーネントが行う。
"""

from datetime import datetime
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict

# --- 列挙型（Design 第7.1節） ---


class Category(StrEnum):
    SALES = "sales"
    SUPPORT = "support"
    BILLING = "billing"
    TECHNICAL = "technical"
    COMPLAINT = "complaint"
    UNCLASSIFIED = "unclassified"


class Priority(StrEnum):
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class Language(StrEnum):
    JA = "ja"
    OTHER = "other"


class Channel(StrEnum):
    EMAIL = "email"
    FORM = "form"


class ReviewReason(StrEnum):
    HIGH_RISK_KEYWORD = "HIGH_RISK_KEYWORD"
    COMPLAINT_OR_LEGAL = "COMPLAINT_OR_LEGAL"
    UNCLASSIFIED = "UNCLASSIFIED"
    NON_JAPANESE = "NON_JAPANESE"
    AMBIGUOUS_CATEGORY = "AMBIGUOUS_CATEGORY"
    LOW_CONFIDENCE = "LOW_CONFIDENCE"
    INPUT_GUARDRAIL = "INPUT_GUARDRAIL"
    LLM_FAILURE = "LLM_FAILURE"
    INVALID_OUTPUT = "INVALID_OUTPUT"


class ResultKind(StrEnum):
    AUTO_REGISTERED = "auto_registered"
    HUMAN_REVIEW = "human_review"
    TICKET_FAILED = "ticket_failed"
    INPUT_ERROR = "input_error"
    PROCESS_ERROR = "process_error"


class ClassifyStatus(StrEnum):
    SUCCESS = "success"
    GUARDRAIL_TRIPPED = "guardrail_tripped"
    LLM_FAILURE = "llm_failure"
    INVALID_OUTPUT = "invalid_output"


# --- 問い合わせ（DATA-001） ---


class Inquiry(BaseModel):
    """問い合わせ1件。`inquiry_id` がなければ、読み込み側（InquiryLoader）が付与する。"""

    inquiry_id: str | None = None
    channel: Channel
    subject: str | None = None
    body: str
    sender: str | None = None
    form_fields: dict[str, str] | None = None
    received_at: datetime | None = None


# --- 分類結果（DATA-002）：LLM の構造化出力 ---


class ClassificationResult(BaseModel):
    """LLM が返す分類結果。全項目が必須（Design 第7.2節）。

    範囲・文字数・副次カテゴリの整合は、出力検証で確認する。
    """

    model_config = ConfigDict(extra="forbid")

    category: Category
    priority: Priority
    confidence: float
    summary: str
    secondary_categories: list[Category]
    detected_language: Language
    is_complaint_or_legal: bool
    is_ambiguous: bool
    rationale: str


class ClassifyOutcome(BaseModel):
    """分類器の結果。失敗も値として返し、例外で業務フローを制御しない。

    `error_detail` は、例外の種類と要約のみ（マスキング前の生の例外文言は保持しない）。
    """

    status: ClassifyStatus
    classification: ClassificationResult | None = None
    attempts: int = 0
    error_detail: str | None = None


# --- 判定（ルールエンジンの結果） ---


class Decision(BaseModel):
    final_priority: Priority
    high_risk_hits: list[str]
    urgent_hits: list[str]
    reasons: list[ReviewReason]
    kind: Literal[ResultKind.AUTO_REGISTERED, ResultKind.HUMAN_REVIEW]
    department: str | None = None


# --- チケット（DATA-003） ---


class TicketRequest(BaseModel):
    """チケットシステムへの登録内容。`note` は副次カテゴリから作る。"""

    category: Category
    priority: Priority
    summary: str
    department: str | None = None
    confidence: float | None = None
    note: str = ""
    inquiry_id: str | None = None
    request_id: str


class TicketRecord(TicketRequest):
    ticket_id: str
    created_at: datetime


# --- Human Review（DATA-004, DATA-005） ---


class ReviewItem(BaseModel):
    """Human Review 項目。未対応かどうかは、解決記録の有無で導出する。"""

    review_id: str
    inquiry: Inquiry
    classification: ClassificationResult | None = None
    final_priority: Priority
    reasons: list[ReviewReason]
    high_risk_hits: list[str]
    request_id: str
    created_at: datetime


class ReviewValues(BaseModel):
    """解決記録の `before` / `after`。レビューで修正できる項目（カテゴリ・優先度・部署）の値。"""

    category: Category | None = None
    priority: Priority | None = None
    department: str | None = None


# 解決の種類：承認（修正指定なし）または修正。解決記録と、処理ログの解決の記録が共有する。
ReviewAction = Literal["approve", "correct"]


class ReviewResolution(BaseModel):
    review_id: str
    action: ReviewAction
    before: ReviewValues
    after: ReviewValues
    ticket_id: str | None = None
    reviewer: str | None = None
    resolved_at: datetime


# --- 再試行キュー（DATA-007） ---


class RetryQueueEntry(BaseModel):
    request: TicketRequest
    error: str
    queued_at: datetime
    source: Literal["pipeline", "review_resolution"]


# --- 処理ログ・結果（DATA-006） ---


class MaskedInput(BaseModel):
    """処理ログへ記録する入力。マスキング済みの件名・本文・送信者。"""

    subject: str | None = None
    body: str
    sender: str | None = None


class ProcessLogRecord(BaseModel):
    """処理ログの1件。`position` は入力エラーのときのみ。

    `action`・`before`・`after` は、`record_type` が `review_resolution` のときのみ（REQ-012 AC-2）。
    カテゴリ・優先度・部署の値で、個人情報を含まないため、マスキングの対象外とする。
    これらの項目がない従来の記録も、読み込める（既定値は `None`）。
    """

    record_type: Literal["inquiry", "review_resolution"]
    request_id: str
    inquiry_id: str | None = None
    processed_at: datetime
    input: MaskedInput | None = None
    classification: ClassificationResult | None = None
    attempts: int | None = None
    high_risk_hits: list[str] = []
    urgent_hits: list[str] = []
    final_priority: Priority | None = None
    kind: ResultKind | None = None
    reasons: list[ReviewReason] = []
    ticket_id: str | None = None
    review_id: str | None = None
    error: str | None = None
    position: int | None = None
    action: ReviewAction | None = None
    before: ReviewValues | None = None
    after: ReviewValues | None = None


class ProcessResult(BaseModel):
    """標準出力の1行。入力エラーでは `inquiry_id` がなく、`position` を持つ。"""

    request_id: str
    inquiry_id: str | None = None
    kind: ResultKind
    category: Category | None = None
    priority: Priority | None = None
    confidence: float | None = None
    department: str | None = None
    ticket_id: str | None = None
    review_id: str | None = None
    reasons: list[ReviewReason] = []
    error: str | None = None
    position: int | None = None


class InvalidRecord(BaseModel):
    """入力の不正な1件。`reason` は、エラーの種類と項目名のみ（入力値を含まない）。"""

    position: int
    reason: str
    inquiry_id: str | None = None


# --- 実行コンテキスト ---


class TriageRunContext(BaseModel):
    """1件の分類の実行コンテキスト。入力ガードレールが、本文と最小文字数をここから取得する。"""

    inquiry: Inquiry
    min_body_length: int


# --- 評価データセット（DATA-009） ---


class ExpectedResult(BaseModel):
    category: Category
    priority: Priority
    kind: Literal[ResultKind.AUTO_REGISTERED, ResultKind.HUMAN_REVIEW]
    department: str | None = None


class LabeledInquiry(BaseModel):
    """評価データセットの1行：`{"inquiry": {...}, "expected": {...}}`。"""

    inquiry: Inquiry
    expected: ExpectedResult
