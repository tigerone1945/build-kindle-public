"""追記専用の JSONL ストア（CMP-009）。

チケット、Human Review 項目、解決記録、再試行キュー、処理ログは、すべてこのストアへ追記する（ADR-006）。
1レコード1行で書き、書き込みのたびにフラッシュする。既存の行は書き換えない（REQ-012 AC-3）。

記録には個人情報が含まれうるため、エラーメッセージには、原因の種類と行番号だけを示し、記録の内容を含めない。
"""

import io
from pathlib import Path

from pydantic import BaseModel, ValidationError

JSON_INVALID = "json_invalid"
VALIDATION_ERROR = "validation_error"


class StorageError(Exception):
    """ストアへ書き込めない、または読み込めない（壊れた行を含む）。メッセージに記録の内容を含めない。

    書き込みの失敗は握りつぶさない。追跡できない処理を続けないため、呼び出し側が、異常終了させる（Design 第10章）。
    """


class JsonlStore:
    """1レコード1行の追記専用ストア。"""

    def __init__(self, path: Path) -> None:
        self.path = path

    def append(self, record: BaseModel) -> None:
        """レコードを1行として末尾へ追記する。出力先のディレクトリがなければ作成する。"""
        # `model_dump_json` は、文字列中の改行を `\n` へエスケープするため、1レコードは必ず1行になる。
        line = (record.model_dump_json() + "\n").encode("utf-8")
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a+b") as handle:
                # 書き込み中の中断で、末尾に改行のない壊れた行が残ることがある（Design R-08）。
                # そのまま追記すると、新しいレコードまで同じ行に連なって壊れるため、先に改行で区切る。
                # 壊れた行は、そのまま残る（読み込み時に、行番号つきのエラーになる）。
                if _ends_without_newline(handle):
                    handle.write(b"\n")
                handle.write(line)
                handle.flush()
        except OSError as error:
            raise StorageError(f"{self.path.name}: 書き込めません（{type(error).__name__}）") from error

    def read_all[ModelT: BaseModel](self, model: type[ModelT]) -> list[ModelT]:
        """全レコードを、ファイル内の順序で返す。ファイルがなければ、まだ何も書かれていないものとして空を返す。

        壊れた行があれば、行番号を示す `StorageError` にする。空行は無視する（行番号は数え続ける）。
        """
        try:
            text = self.path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return []
        except UnicodeDecodeError:
            # 例外の連鎖にも、読めなかったバイト列を残さない。
            raise StorageError(f"{self.path.name}: UTF-8 として読み取れません") from None
        except OSError as error:
            raise StorageError(f"{self.path.name}: 読み取れません（{type(error).__name__}）") from error

        records: list[ModelT] = []
        # `splitlines()` は、JSON の文字列に含まれうる U+2028 などでも分割し、行番号がずれるため、改行だけで分割する。
        for line_number, line in enumerate(text.split("\n"), start=1):
            if not line.strip():
                continue
            try:
                records.append(model.model_validate_json(line))
            except ValidationError as error:
                kinds = {detail["type"] for detail in error.errors(include_url=False, include_context=False, include_input=False)}
                kind = JSON_INVALID if JSON_INVALID in kinds else VALIDATION_ERROR
                # `ValidationError` は、行の内容（入力値）を保持するため、連鎖させない。
                raise StorageError(f"{self.path.name}: {line_number}行目が壊れています（{kind}）") from None
        return records


def _ends_without_newline(handle: io.BufferedRandom) -> bool:
    """追記先が、空でなく、かつ改行で終わっていないか。"""
    if handle.seek(0, io.SEEK_END) == 0:
        return False
    handle.seek(-1, io.SEEK_END)
    return handle.read(1) != b"\n"
