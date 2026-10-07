"""TASK-005：個人情報のマスキング（SEC-002, REQ-012 AC-4 / CMP-008）。"""

import pytest

from triage_agent.masking import EMAIL_PLACEHOLDER, PHONE_PLACEHOLDER, mask_pii


def test_placeholders_are_fixed_by_the_design() -> None:
    assert EMAIL_PLACEHOLDER == "[EMAIL]"
    assert PHONE_PLACEHOLDER == "[PHONE]"


# --- メールアドレス ---


@pytest.mark.parametrize(
    "email",
    [
        "taro@example.com",
        "taro.yamada@example.co.jp",
        "taro+support@sub.example.com",
        "TARO_YAMADA-01@Example.COM",
        "taro＠example.com",
    ],
)
def test_email_is_masked(email: str) -> None:
    assert mask_pii(email) == "[EMAIL]"
    assert mask_pii(f"連絡先は {email} です") == "連絡先は [EMAIL] です"


def test_email_followed_by_japanese_punctuation() -> None:
    assert mask_pii("taro@example.com。よろしくお願いします") == "[EMAIL]。よろしくお願いします"
    assert mask_pii("メール：taro@example.com.") == "メール：[EMAIL]."


def test_email_with_digits_in_local_part_is_masked_as_one_email() -> None:
    assert mask_pii("09012345678@docomo.ne.jp") == "[EMAIL]"


# --- 電話番号（日本の一般的な表記） ---


@pytest.mark.parametrize(
    "phone",
    [
        # 固定電話
        "03-1234-5678",
        "0312345678",
        "06-6123-4567",
        "011-123-4567",
        "0123-45-6789",
        # 携帯
        "090-1234-5678",
        "09012345678",
        "080-1234-5678",
        "070-1234-5678",
        # フリーダイヤル・IP 電話
        "0120-123-456",
        "050-1234-5678",
        # 区切りが空白、ハイフンの種類違い
        "090 1234 5678",
        "090‐1234‐5678",
        "090−1234−5678",
        # 全角
        "０９０－１２３４－５６７８",
        "０３－１２３４－５６７８",
        "０９０１２３４５６７８",
        # +81 表記
        "+81-90-1234-5678",
        "+81 90 1234 5678",
        "+819012345678",
        "+81 (0)90-1234-5678",
        "+81-3-1234-5678",
    ],
)
def test_phone_number_is_masked(phone: str) -> None:
    assert mask_pii(phone) == "[PHONE]"
    assert mask_pii(f"お電話は{phone}までお願いします") == "お電話は[PHONE]までお願いします"


# --- 過剰にマスクしない ---


@pytest.mark.parametrize(
    "text",
    [
        "請求書番号12345",
        "請求書番号 12345 の件です",
        "10,000円",
        "1,234,567円",
        "1,234,567,890円",
        "注文番号 1234567890",  # 10桁でも、0 で始まらなければ電話番号ではない
        "12345678901",
        "2026-09-20",
        "2026年9月20日 10:30",
        "〒150-0001 東京都",
        "バージョン 1.2.3 で発生します",
        "在庫は100個です",
        "0.5 倍",
        "090-1234",  # 桁が足りない
        "090123456789",  # 12桁（電話番号の桁数ではない）
        "0901234567890",  # 13桁
        "+81-90-1234",  # +81 でも桁が足りない
    ],
)
def test_non_pii_text_is_unchanged(text: str) -> None:
    assert mask_pii(text) == text


@pytest.mark.parametrize(
    "text",
    [
        "",
        "使い方を教えてください。",
        "The invoice for last month looks wrong.",
        "@example と書いてあります",
        "user@ のようなメールは途中で切れています",
    ],
)
def test_text_without_personal_information_is_unchanged(text: str) -> None:
    assert mask_pii(text) == text


# --- 複数箇所・複数種類 ---


def test_multiple_kinds_and_occurrences_are_masked_together() -> None:
    text = (
        "山田です。連絡先は taro@example.com か 090-1234-5678、"
        "会社は 03-1234-5678、予備は hanako@example.co.jp です。請求書番号12345、10,000円。"
    )
    assert mask_pii(text) == (
        "山田です。連絡先は [EMAIL] か [PHONE]、"
        "会社は [PHONE]、予備は [EMAIL] です。請求書番号12345、10,000円。"
    )


def test_adjacent_phone_numbers_are_masked_separately() -> None:
    assert mask_pii("090-1234-5678,080-1234-5678") == "[PHONE],[PHONE]"
    assert mask_pii("090-1234-5678/03-1234-5678") == "[PHONE]/[PHONE]"


def test_multiline_text_is_masked_per_occurrence() -> None:
    assert mask_pii("氏名：山田\nTEL：090-1234-5678\nMail：taro@example.com\n") == (
        "氏名：山田\nTEL：[PHONE]\nMail：[EMAIL]\n"
    )


def test_masking_is_idempotent() -> None:
    once = mask_pii("taro@example.com と 090-1234-5678 と +81-3-1234-5678")
    assert mask_pii(once) == once


def test_no_original_personal_information_remains() -> None:
    masked = mask_pii("taro@example.com 090-1234-5678 +81 90 1234 5678 ０３－１２３４－５６７８")
    for fragment in ("taro", "example", "1234", "5678", "１２３４"):
        assert fragment not in masked
