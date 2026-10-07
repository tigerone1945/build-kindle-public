"""TASK-004：問い合わせの読み込み（REQ-001 AC-1〜7、ERR-004 / CMP-004）。"""

import json
import re
from pathlib import Path

import pytest

from triage_agent.inquiry_io import InputFileError, load_inquiries
from triage_agent.models import Channel, InvalidRecord, Inquiry


def _write(tmp_path: Path, name: str, content: str | bytes) -> Path:
    path = tmp_path / name
    if isinstance(content, bytes):
        path.write_bytes(content)
    else:
        path.write_text(content, encoding="utf-8")
    return path


def _inquiry(body: str = "請求書の再発行をお願いします", **extra: object) -> dict[str, object]:
    return {"channel": "email", "body": body, **extra}


def _jsonl(*lines: object) -> str:
    return "\n".join(line if isinstance(line, str) else json.dumps(line, ensure_ascii=False) for line in lines)


def _only_invalid(records: list[Inquiry | InvalidRecord]) -> InvalidRecord:
    assert len(records) == 1
    record = records[0]
    assert isinstance(record, InvalidRecord)
    return record


# --- 3形式の読み込み（AC-1, AC-3, AC-6） ---


def test_single_object_json(tmp_path: Path) -> None:
    path = _write(tmp_path, "one.json", json.dumps(_inquiry(inquiry_id="INQ-1"), ensure_ascii=False))

    records = load_inquiries(path)

    assert len(records) == 1
    assert isinstance(records[0], Inquiry)
    assert records[0].inquiry_id == "INQ-1"
    assert records[0].body == "請求書の再発行をお願いします"


def test_json_array_keeps_file_order(tmp_path: Path) -> None:
    items = [_inquiry(f"本文{i}", inquiry_id=f"INQ-{i}") for i in range(3)]
    path = _write(tmp_path, "many.json", json.dumps(items, ensure_ascii=False))

    records = load_inquiries(path)

    assert [r.inquiry_id for r in records if isinstance(r, Inquiry)] == ["INQ-0", "INQ-1", "INQ-2"]


def test_jsonl_one_inquiry_per_line(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        "many.jsonl",
        _jsonl(_inquiry("一件目", inquiry_id="A"), _inquiry("二件目", inquiry_id="B")) + "\n",
    )

    records = load_inquiries(path)

    assert [r.body for r in records if isinstance(r, Inquiry)] == ["一件目", "二件目"]


@pytest.mark.parametrize("name", ["upper.JSONL", "mixed.JsonL"])
def test_extension_is_case_insensitive_for_jsonl(tmp_path: Path, name: str) -> None:
    path = _write(tmp_path, name, _jsonl(_inquiry(inquiry_id="A"), _inquiry(inquiry_id="B")))

    assert len(load_inquiries(path)) == 2


def test_extension_is_case_insensitive_for_json(tmp_path: Path) -> None:
    path = _write(tmp_path, "upper.JSON", json.dumps(_inquiry()))

    assert len(load_inquiries(path)) == 1


def test_all_inquiry_fields_are_accepted(tmp_path: Path) -> None:
    full = {
        "inquiry_id": "INQ-9",
        "channel": "form",
        "subject": "件名",
        "body": "本文",
        "sender": "taro@example.com",
        "form_fields": {"種別": "請求"},
        "received_at": "2026-09-20T09:00:00+09:00",
    }
    path = _write(tmp_path, "full.json", json.dumps(full, ensure_ascii=False))

    (record,) = load_inquiries(path)

    assert isinstance(record, Inquiry)
    assert record.channel == Channel.FORM
    assert record.subject == "件名"
    assert record.sender == "taro@example.com"
    assert record.form_fields == {"種別": "請求"}
    assert record.received_at is not None


# --- 問い合わせIDの付与（AC-2） ---


def test_missing_inquiry_id_is_assigned(tmp_path: Path) -> None:
    path = _write(tmp_path, "noid.jsonl", _jsonl(_inquiry(), _inquiry()))

    records = load_inquiries(path)

    ids = [r.inquiry_id for r in records if isinstance(r, Inquiry)]
    assert len(ids) == 2
    assert all(i is not None and re.fullmatch(r"INQ-[0-9a-f]{8}", i) for i in ids)
    assert ids[0] != ids[1]


def test_existing_inquiry_id_is_kept(tmp_path: Path) -> None:
    path = _write(tmp_path, "id.json", json.dumps(_inquiry(inquiry_id="CUSTOM-1")))

    (record,) = load_inquiries(path)

    assert isinstance(record, Inquiry)
    assert record.inquiry_id == "CUSTOM-1"


@pytest.mark.parametrize("empty_id", ["", None])
def test_empty_inquiry_id_is_treated_as_missing(tmp_path: Path, empty_id: str | None) -> None:
    path = _write(tmp_path, "emptyid.json", json.dumps(_inquiry(inquiry_id=empty_id)))

    (record,) = load_inquiries(path)

    assert isinstance(record, Inquiry)
    assert record.inquiry_id is not None
    assert record.inquiry_id.startswith("INQ-")


# --- 不正な1件（AC-5、ERR-004） ---


def test_missing_body_is_invalid_and_other_lines_are_read(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        "mixed.jsonl",
        _jsonl(_inquiry("一件目"), {"channel": "email"}, _inquiry("三件目")),
    )

    first, second, third = load_inquiries(path)

    assert isinstance(first, Inquiry)
    assert isinstance(third, Inquiry)
    assert isinstance(second, InvalidRecord)
    assert second.position == 2
    assert second.reason == "body: missing"


def test_non_string_body_is_invalid(tmp_path: Path) -> None:
    path = _write(tmp_path, "num.jsonl", _jsonl({"channel": "email", "body": 12345}))

    invalid = _only_invalid(load_inquiries(path))

    assert invalid.position == 1
    assert invalid.reason == "body: string_type"


def test_unknown_channel_is_invalid(tmp_path: Path) -> None:
    path = _write(tmp_path, "channel.json", json.dumps({"channel": "fax", "body": "本文"}))

    assert _only_invalid(load_inquiries(path)).reason == "channel: enum"


def test_all_failing_fields_are_listed_once_each(tmp_path: Path) -> None:
    path = _write(tmp_path, "empty_obj.json", "{}")

    assert _only_invalid(load_inquiries(path)).reason == "channel: missing, body: missing"


def test_broken_jsonl_line_is_invalid_and_reading_continues(tmp_path: Path) -> None:
    path = _write(tmp_path, "broken.jsonl", _jsonl(_inquiry("一件目"), '{"body": "壊れた', _inquiry("三件目")))

    first, second, third = load_inquiries(path)

    assert isinstance(first, Inquiry)
    assert isinstance(third, Inquiry)
    assert second == InvalidRecord(position=2, reason="json_invalid")


@pytest.mark.parametrize("element", [123, '"just a string"', ["a", "b"], None, True])
def test_non_object_jsonl_line_is_invalid(tmp_path: Path, element: object) -> None:
    line = element if isinstance(element, str) else json.dumps(element)
    path = _write(tmp_path, "nonobj.jsonl", _jsonl(_inquiry("一件目"), line))

    _, invalid = load_inquiries(path)

    assert invalid == InvalidRecord(position=2, reason="model_type")


def test_non_object_array_elements_are_invalid_with_element_position(tmp_path: Path) -> None:
    path = _write(tmp_path, "arr.json", json.dumps([_inquiry("一件目"), 42, _inquiry("三件目"), "text", ["x"]]))

    records = load_inquiries(path)

    assert [type(r) for r in records] == [Inquiry, InvalidRecord, Inquiry, InvalidRecord, InvalidRecord]
    assert [r.position for r in records if isinstance(r, InvalidRecord)] == [2, 4, 5]
    assert all(r.reason == "model_type" for r in records if isinstance(r, InvalidRecord))


def test_invalid_single_object_json_has_position_one(tmp_path: Path) -> None:
    path = _write(tmp_path, "bad_one.json", json.dumps({"channel": "email"}))

    assert _only_invalid(load_inquiries(path)).position == 1


@pytest.mark.parametrize("content", ["5", '"text"', "true", "null"])
def test_json_that_is_neither_array_nor_object_is_one_invalid_record(tmp_path: Path, content: str) -> None:
    path = _write(tmp_path, "scalar.json", content)

    assert _only_invalid(load_inquiries(path)) == InvalidRecord(position=1, reason="model_type")


def test_readable_inquiry_id_is_attached_to_invalid_record(tmp_path: Path) -> None:
    path = _write(tmp_path, "id_invalid.jsonl", _jsonl({"inquiry_id": "INQ-77", "channel": "email"}))

    assert _only_invalid(load_inquiries(path)).inquiry_id == "INQ-77"


@pytest.mark.parametrize("bad_id", [123, ["INQ-1"], {"a": 1}, ""])
def test_unreadable_inquiry_id_is_not_attached(tmp_path: Path, bad_id: object) -> None:
    path = _write(tmp_path, "id_bad.jsonl", _jsonl({"inquiry_id": bad_id, "channel": "email"}))

    assert _only_invalid(load_inquiries(path)).inquiry_id is None


# --- 空行（AC-6） ---


def test_blank_jsonl_lines_are_ignored_without_shifting_line_numbers(tmp_path: Path) -> None:
    content = "\n".join([json.dumps(_inquiry("一件目")), "", "   \t", '{"channel": "email"}', "", json.dumps(_inquiry("六件目"))])
    path = _write(tmp_path, "blank.jsonl", content + "\n\n")

    records = load_inquiries(path)

    assert len(records) == 3
    assert isinstance(records[0], Inquiry)
    assert records[1] == InvalidRecord(position=4, reason="body: missing")
    assert isinstance(records[2], Inquiry)


def test_crlf_line_endings_are_supported(tmp_path: Path) -> None:
    content = "\r\n".join([json.dumps(_inquiry("一件目")), "", json.dumps(_inquiry("三件目"))]) + "\r\n"
    path = _write(tmp_path, "crlf.jsonl", content)

    records = load_inquiries(path)

    assert [r.body for r in records if isinstance(r, Inquiry)] == ["一件目", "三件目"]


def test_unicode_line_separator_inside_a_string_does_not_split_the_line(tmp_path: Path) -> None:
    # U+2028 は JSON の文字列に含められる。行番号は改行（\n）だけで数える。
    path = _write(tmp_path, "u2028.jsonl", _jsonl(_inquiry("前後"), '{"channel": "email"}'))

    first, second = load_inquiries(path)

    assert isinstance(first, Inquiry)
    assert first.body == "前後"
    assert second == InvalidRecord(position=2, reason="body: missing")


# --- 入力値を理由へ含めない（AC-5） ---


@pytest.mark.parametrize(
    ("record", "secret"),
    [
        ({"channel": "email", "body": ["taro@example.com"]}, "taro@example.com"),
        ({"channel": "email", "body": 12345}, "12345"),
        ({"channel": "email", "body": {"phone": "090-1234-5678"}}, "090-1234-5678"),
        ({"channel": "taro@example.com", "body": "本文"}, "taro@example.com"),
        ({"channel": "email", "body": "本文", "sender": ["yamada@example.com"]}, "yamada@example.com"),
        ({"channel": "email", "body": "本文", "received_at": "yamada@example.com"}, "yamada@example.com"),
        # 入力の辞書のキー（項目名ではなく入力値）も、理由に現れない。
        ({"channel": "email", "body": "本文", "form_fields": {"taro@example.com": 1}}, "taro@example.com"),
    ],
)
def test_reason_does_not_contain_input_values(tmp_path: Path, record: dict[str, object], secret: str) -> None:
    for name, content in (("secret.jsonl", _jsonl(record)), ("secret.json", json.dumps(record))):
        invalid = _only_invalid(load_inquiries(_write(tmp_path, name, content)))

        assert secret not in invalid.reason
        assert secret not in invalid.model_dump_json()


def test_reason_names_the_field_and_kind_only_for_array_body(tmp_path: Path) -> None:
    path = _write(tmp_path, "arrbody.jsonl", _jsonl({"channel": "email", "body": ["taro@example.com"]}))

    assert _only_invalid(load_inquiries(path)).reason == "body: string_type"


def test_reason_for_nested_field_uses_the_top_level_field_name(tmp_path: Path) -> None:
    path = _write(tmp_path, "nested.json", json.dumps({"channel": "email", "body": "本文", "form_fields": {"k": 1}}))

    assert _only_invalid(load_inquiries(path)).reason == "form_fields: string_type"


# --- 入力ファイルのエラー（AC-4） ---


def test_missing_file_raises_input_file_error(tmp_path: Path) -> None:
    with pytest.raises(InputFileError, match="存在しません"):
        load_inquiries(tmp_path / "nothing.jsonl")


def test_directory_raises_input_file_error(tmp_path: Path) -> None:
    directory = tmp_path / "dir.json"
    directory.mkdir()

    with pytest.raises(InputFileError, match="読み取れません"):
        load_inquiries(directory)


@pytest.mark.parametrize("name", ["inquiries.txt", "inquiries.csv", "inquiries", "inquiries.json.bak", "inquiries.ndjson"])
def test_unsupported_extension_raises_input_file_error(tmp_path: Path, name: str) -> None:
    path = _write(tmp_path, name, json.dumps(_inquiry()))

    with pytest.raises(InputFileError, match="拡張子"):
        load_inquiries(path)


@pytest.mark.parametrize("name", ["latin.json", "latin.jsonl"])
def test_file_that_is_not_utf8_raises_input_file_error(tmp_path: Path, name: str) -> None:
    path = _write(tmp_path, name, b'{"channel": "email", "body": "\xff\xfe"}')

    with pytest.raises(InputFileError, match="UTF-8") as excinfo:
        load_inquiries(path)

    assert excinfo.value.__cause__ is None


def test_json_syntax_error_reports_kind_and_position_without_content(tmp_path: Path) -> None:
    path = _write(tmp_path, "syntax.json", '{\n  "channel": "email",\n  "body": "taro@example.com" "x"\n}')

    with pytest.raises(InputFileError) as excinfo:
        load_inquiries(path)

    message = str(excinfo.value)
    assert "json_invalid" in message
    assert "3 行" in message
    assert "taro@example.com" not in message
    assert excinfo.value.__cause__ is None


def test_truncated_json_syntax_error(tmp_path: Path) -> None:
    path = _write(tmp_path, "truncated.json", '{"a": ')

    with pytest.raises(InputFileError, match="json_invalid"):
        load_inquiries(path)


# --- 0件の入力（AC-7） ---


@pytest.mark.parametrize(
    ("name", "content"),
    [
        ("zero.json", b""),
        ("zero.jsonl", b""),
        ("blank.json", "  \n\t\n"),
        ("blank.jsonl", "  \n\t\n"),
        ("empty_array.json", "[]"),
        ("empty_array_spaced.json", " [ ]\n"),
        ("only_blank_lines.jsonl", "\n\n   \n\n"),
    ],
)
def test_input_without_inquiries_returns_no_records(tmp_path: Path, name: str, content: str | bytes) -> None:
    assert load_inquiries(_write(tmp_path, name, content)) == []
