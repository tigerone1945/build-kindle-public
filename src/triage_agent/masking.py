"""個人情報のマスキング（CMP-008）：文字列内のメールアドレスと電話番号を置換する純粋関数。

処理ログへ書く文字列（入力内容・エラー情報）に適用する（SEC-002, REQ-012 AC-4）。
電話番号は、日本の一般的な表記（ハイフンあり・なし、`+81` 表記、携帯・固定）を対象とする。
注文番号や金額を誤ってマスクしないよう、桁数で確認する（国内表記は10〜11桁、先頭は0）。
"""

import re

EMAIL_PLACEHOLDER = "[EMAIL]"
PHONE_PLACEHOLDER = "[PHONE]"

_DIGIT = "[0-9０-９]"
# 区切り：ハイフン類（ASCII・全角・各種ダッシュ）と、半角・全角の空白。1文字だけ許す。
_SEP = "[-‐‑−－ 　]?"
_NOT_AFTER_DIGIT = f"(?<!{_DIGIT})"
_NOT_BEFORE_DIGIT = f"(?!{_DIGIT})"

_EMAIL = re.compile(r"[A-Za-z0-9._%+\-]+[@＠][A-Za-z0-9\-]+(?:\.[A-Za-z0-9\-]+)+")

# 国内表記：0 から始まる「市外局番・局番・番号」の3つの数字の並び。区切りなしも同じ形で受ける。
_DOMESTIC_PHONE = re.compile(
    f"{_NOT_AFTER_DIGIT}[0０]{_DIGIT}{{1,4}}{_SEP}{_DIGIT}{{1,4}}{_SEP}{_DIGIT}{{3,4}}{_NOT_BEFORE_DIGIT}"
)
# 国際表記：+81 に続き、先頭の 0 を省く（`+81 (0)90-...` のように 0 を括弧で残す書き方も許す）。
_INTERNATIONAL_PHONE = re.compile(
    rf"(?<![\d０-９])\+81{_SEP}(?:\(0\))?{_SEP}{_DIGIT}{{1,4}}{_SEP}{_DIGIT}{{1,4}}{_SEP}{_DIGIT}{{3,4}}{_NOT_BEFORE_DIGIT}"
)

_DOMESTIC_DIGIT_COUNTS = {10, 11}
_INTERNATIONAL_DIGIT_COUNTS = {9, 10}  # +81 と、括弧つきの 0 を除いた桁数


def _digit_count(text: str) -> int:
    return sum(character.isdigit() for character in text)


def _mask_domestic(match: re.Match[str]) -> str:
    if _digit_count(match.group()) in _DOMESTIC_DIGIT_COUNTS:
        return PHONE_PLACEHOLDER
    return match.group()


def _mask_international(match: re.Match[str]) -> str:
    number = match.group().removeprefix("+81").replace("(0)", "")
    if _digit_count(number) in _INTERNATIONAL_DIGIT_COUNTS:
        return PHONE_PLACEHOLDER
    return match.group()


def mask_pii(text: str) -> str:
    """メールアドレスを `[EMAIL]`、電話番号を `[PHONE]` へ置換した文字列を返す。

    メールアドレスを先に置換する（`09012345678@example.com` のように、ローカル部が
    電話番号に見える場合に、メールアドレスとして1つにまとめるため）。
    """
    masked = _EMAIL.sub(EMAIL_PLACEHOLDER, text)
    masked = _INTERNATIONAL_PHONE.sub(_mask_international, masked)
    return _DOMESTIC_PHONE.sub(_mask_domestic, masked)
