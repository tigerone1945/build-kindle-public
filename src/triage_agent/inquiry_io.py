"""問い合わせの読み込み（CMP-004）。

`.json`（1件のオブジェクト、またはその配列）と `.jsonl`（1行に1件）を、1件ずつ `Inquiry` として検証する。
不正な1件は、全体を止めずに位置と理由を持つ `InvalidRecord` として返す（REQ-001 AC-5）。
ファイル自体が読めない場合は `InputFileError` を送出し、処理を開始させない（REQ-001 AC-4）。

不正な入力の本文には個人情報が含まれうるため、`InvalidRecord.reason` や `InputFileError` のメッセージには
入力された値を含めない（エラーの種類と項目名、位置のみ）。
"""

import json
import uuid
from pathlib import Path

from pydantic import ValidationError

from triage_agent.models import InvalidRecord, Inquiry

JSON_SUFFIX = ".json"
JSONL_SUFFIX = ".jsonl"

# `.jsonl` の1行が JSON として解釈できない場合の理由。
JSON_INVALID = "json_invalid"

# 入力の順序を保ったまま、正常な1件と不正な1件を混在して返す。
LoadedRecord = Inquiry | InvalidRecord


class InputFileError(Exception):
    """入力ファイルを処理できない（存在しない・読めない・拡張子が不正・`.json` 全体が JSON でない）。

    メッセージは、原因の種類のみを示し、ファイルの内容を含めない。
    """


def load_inquiries(path: Path) -> list[LoadedRecord]:
    """入力ファイルを読み込み、ファイル内の順序で `Inquiry` または `InvalidRecord` を返す。

    問い合わせが1件もない入力（空・空白のみ・空の配列・空行のみ）は、エラーにせず空のリストを返す。
    """
    suffix = path.suffix.lower()
    if suffix not in (JSON_SUFFIX, JSONL_SUFFIX):
        raise InputFileError(
            f"入力ファイルの拡張子が不正です（{JSON_SUFFIX} または {JSONL_SUFFIX} のみ対応）: {path}"
        )

    text = _read_text(path)
    if suffix == JSONL_SUFFIX:
        return _parse_jsonl(text)
    return _parse_json(text, path)


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError as error:
        raise InputFileError(f"入力ファイルが存在しません: {path}") from error
    except UnicodeDecodeError:
        # 例外の連鎖にも、読めなかったバイト列を残さない。
        raise InputFileError(f"入力ファイルを UTF-8 として読み取れません: {path}") from None
    except OSError as error:
        raise InputFileError(
            f"入力ファイルを読み取れません: {path}（{type(error).__name__}）"
        ) from error


def _parse_jsonl(text: str) -> list[LoadedRecord]:
    records: list[LoadedRecord] = []
    # `splitlines()` は JSON の文字列内にもありうる文字（U+2028 など）でも分割するため、改行だけで分割する。
    for line_number, line in enumerate(text.split("\n"), start=1):
        if not line.strip():
            continue
        try:
            raw = json.loads(line)
        except json.JSONDecodeError:
            records.append(InvalidRecord(position=line_number, reason=JSON_INVALID))
            continue
        records.append(_validate_record(raw, line_number))
    return records


def _parse_json(text: str, path: Path) -> list[LoadedRecord]:
    if not text.strip():
        return []
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as error:
        # `JSONDecodeError` はファイル全体（`doc`）を保持するため、連鎖させず、種類と位置だけを示す。
        raise InputFileError(
            f"入力ファイルを JSON として解釈できません: {JSON_INVALID}"
            f"（{error.lineno} 行 {error.colno} 桁）: {path}"
        ) from None

    if isinstance(parsed, list):
        return [_validate_record(item, position) for position, item in enumerate(parsed, start=1)]
    return [_validate_record(parsed, 1)]


def _validate_record(raw: object, position: int) -> LoadedRecord:
    try:
        inquiry = Inquiry.model_validate(raw)
    except ValidationError as error:
        return InvalidRecord(
            position=position,
            reason=_describe_validation_error(error),
            inquiry_id=_readable_inquiry_id(raw),
        )
    if not inquiry.inquiry_id:
        inquiry.inquiry_id = f"INQ-{uuid.uuid4().hex[:8]}"
    return inquiry


def _describe_validation_error(error: ValidationError) -> str:
    """検証エラーを `<項目名>: <種類>` にまとめる。入力された値は含めない。"""
    reasons: list[str] = []
    for detail in error.errors(include_url=False, include_context=False, include_input=False):
        # `loc` の2番目以降には、`form_fields` のキーなど、入力の値そのものが入りうる。最上位の項目名だけを使う。
        reason = f"{detail['loc'][0]}: {detail['type']}" if detail["loc"] else detail["type"]
        if reason not in reasons:
            reasons.append(reason)
    return ", ".join(reasons)


def _readable_inquiry_id(raw: object) -> str | None:
    """不正な1件でも、問い合わせIDが文字列として読み取れれば、報告に添える。"""
    if isinstance(raw, dict):
        inquiry_id = raw.get("inquiry_id")
        if isinstance(inquiry_id, str) and inquiry_id:
            return inquiry_id
    return None
