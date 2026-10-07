"""TASK-006：追記専用の JSONL ストア（REQ-012 AC-3 / CMP-009、ADR-006）。"""

from pathlib import Path

import pytest
from pydantic import BaseModel

from triage_agent.storage import JsonlStore, StorageError


class Note(BaseModel):
    number: int
    text: str


def _store(tmp_path: Path, name: str = "notes.jsonl") -> JsonlStore:
    return JsonlStore(tmp_path / name)


# --- 追記 ---


def test_append_writes_one_json_object_per_line(tmp_path: Path) -> None:
    store = _store(tmp_path)

    store.append(Note(number=1, text="一件目"))

    assert store.path.read_text(encoding="utf-8") == '{"number":1,"text":"一件目"}\n'


def test_two_appends_make_two_lines(tmp_path: Path) -> None:
    store = _store(tmp_path)

    store.append(Note(number=1, text="a"))
    store.append(Note(number=2, text="b"))

    assert len(store.path.read_text(encoding="utf-8").splitlines()) == 2
    assert store.read_all(Note) == [Note(number=1, text="a"), Note(number=2, text="b")]


def test_existing_lines_are_not_changed_by_appending(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.append(Note(number=1, text="a"))
    store.append(Note(number=2, text="b"))
    before = store.path.read_bytes()

    store.append(Note(number=3, text="c"))

    after = store.path.read_bytes()
    assert after.startswith(before)
    assert after[len(before) :] == b'{"number":3,"text":"c"}\n'


def test_records_are_visible_right_after_each_append(tmp_path: Path) -> None:
    # 書き込みのたびにフラッシュする：ストアを閉じなくても、別の読み手がすぐに読める。
    store = _store(tmp_path)

    store.append(Note(number=1, text="a"))
    assert JsonlStore(store.path).read_all(Note) == [Note(number=1, text="a")]

    store.append(Note(number=2, text="b"))
    assert JsonlStore(store.path).read_all(Note) == [Note(number=1, text="a"), Note(number=2, text="b")]


def test_missing_output_directory_is_created(tmp_path: Path) -> None:
    store = JsonlStore(tmp_path / "output" / "nested" / "notes.jsonl")

    store.append(Note(number=1, text="a"))

    assert store.read_all(Note) == [Note(number=1, text="a")]


def test_multiline_and_special_text_stays_on_one_line_and_round_trips(tmp_path: Path) -> None:
    store = _store(tmp_path)
    tricky = Note(number=1, text='一行目\n二行目\r\n"引用"\t前 後')

    store.append(tricky)
    store.append(Note(number=2, text="次"))

    assert len(store.path.read_bytes().split(b"\n")) == 3  # 2行 + 末尾の改行の後ろの空
    assert store.read_all(Note) == [tricky, Note(number=2, text="次")]


def test_appending_after_a_torn_last_line_does_not_merge_it_with_the_new_record(tmp_path: Path) -> None:
    # 書き込み中の中断で、末尾に改行のない壊れた行が残った状態（Design R-08）。
    store = _store(tmp_path)
    store.append(Note(number=1, text="a"))
    with store.path.open("ab") as handle:
        handle.write(b'{"number":2,"te')

    store.append(Note(number=3, text="c"))

    lines = store.path.read_bytes().split(b"\n")
    assert lines[1] == b'{"number":2,"te'  # 壊れた行は、そのまま残る
    assert lines[2] == b'{"number":3,"text":"c"}'  # 新しいレコードは、無傷
    with pytest.raises(StorageError, match="2行目"):
        store.read_all(Note)


# --- 読み込み ---


def test_read_all_on_a_missing_file_returns_nothing(tmp_path: Path) -> None:
    assert _store(tmp_path).read_all(Note) == []


def test_read_all_ignores_blank_lines(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.path.write_text('{"number":1,"text":"a"}\n\n   \n{"number":2,"text":"b"}\n\n', encoding="utf-8")

    assert store.read_all(Note) == [Note(number=1, text="a"), Note(number=2, text="b")]


def test_broken_json_line_raises_an_error_with_the_line_number(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.path.write_text('{"number":1,"text":"a"}\n{"number":2,"text":"b"}\n{"number":3,"te', encoding="utf-8")

    with pytest.raises(StorageError) as exc_info:
        store.read_all(Note)

    assert "3行目" in str(exc_info.value)
    assert "json_invalid" in str(exc_info.value)
    assert "notes.jsonl" in str(exc_info.value)


def test_line_number_keeps_counting_blank_lines(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.path.write_text('{"number":1,"text":"a"}\n\n\nbroken\n', encoding="utf-8")

    with pytest.raises(StorageError, match="4行目"):
        store.read_all(Note)


def test_line_that_is_valid_json_but_not_a_valid_record_raises_an_error_with_the_line_number(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.path.write_text('{"number":1,"text":"a"}\n{"number":"x","text":"b"}\n', encoding="utf-8")

    with pytest.raises(StorageError) as exc_info:
        store.read_all(Note)

    assert "2行目" in str(exc_info.value)
    assert "validation_error" in str(exc_info.value)


@pytest.mark.parametrize(
    "broken_line",
    [
        '{"number":1,"text":"taro@example.com 090-1234-5678',  # JSON として壊れている
        '{"number":"taro@example.com","text":"090-1234-5678"}',  # 型が合わない
        '["taro@example.com"]',  # オブジェクトでない
    ],
)
def test_error_message_does_not_contain_the_record(tmp_path: Path, broken_line: str) -> None:
    # 記録には個人情報が含まれうる。エラーの文言にも、例外の連鎖にも、内容を残さない。
    store = _store(tmp_path)
    store.path.write_text(broken_line + "\n", encoding="utf-8")

    with pytest.raises(StorageError) as exc_info:
        store.read_all(Note)

    assert "taro@example.com" not in str(exc_info.value)
    assert "090-1234-5678" not in str(exc_info.value)
    assert exc_info.value.__cause__ is None


def test_file_that_is_not_utf8_raises_a_storage_error(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.path.write_bytes(b'{"number":1,"text":"\xff\xfe"}\n')

    with pytest.raises(StorageError, match="UTF-8") as exc_info:
        store.read_all(Note)

    assert exc_info.value.__cause__ is None


def test_unreadable_path_raises_a_storage_error(tmp_path: Path) -> None:
    directory = tmp_path / "notes.jsonl"
    directory.mkdir()

    with pytest.raises(StorageError, match="読み取れません"):
        JsonlStore(directory).read_all(Note)


# --- 書き込みの失敗は握りつぶさない（Design 第10章） ---


def test_write_failure_is_reported_not_swallowed(tmp_path: Path) -> None:
    blocker = tmp_path / "output"
    blocker.write_text("ファイルなので、ディレクトリを作れない", encoding="utf-8")
    store = JsonlStore(blocker / "notes.jsonl")

    with pytest.raises(StorageError, match="書き込めません") as exc_info:
        store.append(Note(number=1, text="a"))

    assert isinstance(exc_info.value.__cause__, OSError)
    assert blocker.read_text(encoding="utf-8") == "ファイルなので、ディレクトリを作れない"


def test_append_to_a_directory_path_is_reported(tmp_path: Path) -> None:
    directory = tmp_path / "notes.jsonl"
    directory.mkdir()

    with pytest.raises(StorageError, match="書き込めません"):
        JsonlStore(directory).append(Note(number=1, text="a"))
