"""TASK-014：CLI（`run`）と結果の出力（REQ-001、REQ-011、ERR-006、ERR-007、SEC-001 / CMP-001、第5.3節、第8.1節、ADR-013）。

分類器は Fake（LLM・ネットワークなし）を注入して、`main(argv)` を呼ぶ。設定は、リポジトリの設定のコピーで、
出力先を `tmp_path` へ向け、実行トレースを無効にしたもの（リポジトリの `output/` を汚さない）。
`python -m triage_agent` とコンソールスクリプトは、別のプロセスで実行する（LLM は、入力ガードレール該当の入力で、呼ばれない）。
"""

import json
import os
import re
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml
from agents import Runner
from fakes import FakeClassifier, make_result

from triage_agent import cli
from triage_agent.cli import CommandContext, Subcommand, build_parser, main
from triage_agent.models import (
    ClassifyOutcome,
    ClassifyStatus,
    Inquiry,
    ProcessLogRecord,
    ProcessResult,
    ResultKind,
)
from triage_agent.storage import JsonlStore, StorageError

API_KEY = "sk-dummy-SECRET-KEY-FOR-TESTS"
SENDER = "taro.yamada@example.com"
UUID_PATTERN = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}")


@pytest.fixture(autouse=True)
def _isolated_environment(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """API Key はダミー。作業ディレクトリは `tmp_path`（相対パスの既定値が、リポジトリの `config/`・`output/` に触れない）。"""
    monkeypatch.setenv("OPENAI_API_KEY", API_KEY)
    monkeypatch.chdir(tmp_path)


@pytest.fixture
def output_dir(tmp_path: Path) -> Path:
    return tmp_path / "out"


def record(inquiry_id: str, body: str = "請求書の内容について確認したい", **overrides: object) -> dict[str, object]:
    values: dict[str, object] = {
        "inquiry_id": inquiry_id,
        "channel": "email",
        "subject": "請求書について",
        "body": body,
        "sender": SENDER,
    }
    values.update(overrides)
    return values


def write_jsonl(path: Path, records: list[dict[str, object]]) -> Path:
    path.write_text("\n".join(json.dumps(item, ensure_ascii=False) for item in records) + "\n", encoding="utf-8")
    return path


def success(**overrides: object) -> ClassifyOutcome:
    return ClassifyOutcome(status=ClassifyStatus.SUCCESS, classification=make_result(**overrides), attempts=1)


class Cli:
    """`main` を呼び、標準出力・標準エラー出力・終了コードを返す。"""

    def __init__(self, capsys: pytest.CaptureFixture[str], config_dir: Path) -> None:
        self._capsys = capsys
        self.config_dir = config_dir

    def run(self, *args: str, classifier: object | None = None, with_config: bool = True) -> tuple[int, str, str]:
        argv = list(args)
        if with_config:
            argv += ["--config-dir", str(self.config_dir)]
        code = main(argv, classifier=classifier)  # type: ignore[arg-type]
        captured = self._capsys.readouterr()
        return code, captured.out, captured.err


@pytest.fixture
def cli_run(capsys: pytest.CaptureFixture[str], config_dir: Path) -> Cli:
    return Cli(capsys, config_dir)


def lines_of(text: str) -> list[dict[str, object]]:
    return [json.loads(line) for line in text.splitlines() if line.strip()]


def read_log(output_dir: Path) -> list[ProcessLogRecord]:
    return JsonlStore(output_dir / "process_log.jsonl").read_all(ProcessLogRecord)


class TestNormalRun:
    """REQ-001 AC-1、REQ-011：全件を、ファイルの順に処理し、1件1行の JSON で出力する。要約を標準エラー出力へ出す。"""

    def make_input(self, tmp_path: Path) -> Path:
        return write_jsonl(
            tmp_path / "in.jsonl",
            [record("INQ-1"), record("INQ-2", body="解約したいです"), record("INQ-3", body="連携でエラーが出る")],
        )

    def test_stdout_has_one_json_line_per_inquiry_in_the_file_order(self, cli_run: Cli, tmp_path: Path) -> None:
        path = self.make_input(tmp_path)
        code, out, _ = cli_run.run("run", "--input", str(path), classifier=FakeClassifier(success()))
        assert code == 0
        assert len(out.splitlines()) == 3
        results = [ProcessResult.model_validate(item) for item in lines_of(out)]
        assert [result.inquiry_id for result in results] == ["INQ-1", "INQ-2", "INQ-3"]
        assert [result.kind for result in results] == [
            ResultKind.AUTO_REGISTERED,
            ResultKind.HUMAN_REVIEW,
            ResultKind.AUTO_REGISTERED,
        ]

    def test_each_line_carries_the_fields_of_the_requirement(self, cli_run: Cli, tmp_path: Path) -> None:
        path = self.make_input(tmp_path)
        _, out, _ = cli_run.run("run", "--input", str(path), classifier=FakeClassifier(success()))
        auto, review, _ = lines_of(out)
        # 区分、カテゴリ、最終的な優先度、確信度、担当部署、チケットID / レビューID、Human Review の理由（REQ-011 AC-1）。
        assert auto["kind"] == "auto_registered"
        assert auto["category"] == "billing"
        assert auto["priority"] == "medium"
        assert auto["confidence"] == 0.9
        assert auto["department"] == "請求"
        assert str(auto["ticket_id"]).startswith("TCK-")
        assert review["kind"] == "human_review"
        assert str(review["review_id"]).startswith("REV-")
        assert review["reasons"] == ["HIGH_RISK_KEYWORD"]
        assert review["ticket_id"] is None

    def test_the_summary_goes_to_stderr(self, cli_run: Cli, tmp_path: Path) -> None:
        path = self.make_input(tmp_path)
        _, out, err = cli_run.run("run", "--input", str(path), classifier=FakeClassifier(success()))
        assert "処理結果: 3件" in err
        assert "自動登録: 2件" in err
        assert "Human Review: 1件" in err
        assert "チケット登録失敗: 0件" in err
        assert "入力エラー: 0件" in err
        assert "処理エラー: 0件" in err
        # 標準出力は、結果の JSONL だけである（要約を混ぜない）。
        assert all(line.startswith("{") for line in out.splitlines())

    def test_the_output_is_readable_japanese_json(self, cli_run: Cli, tmp_path: Path) -> None:
        path = self.make_input(tmp_path)
        _, out, _ = cli_run.run("run", "--input", str(path), classifier=FakeClassifier(success()))
        assert "請求" in out
        assert "\\u" not in out

    def test_results_are_written_to_the_files_of_the_output_directory(
        self, cli_run: Cli, tmp_path: Path, output_dir: Path
    ) -> None:
        path = self.make_input(tmp_path)
        _, out, _ = cli_run.run("run", "--input", str(path), classifier=FakeClassifier(success()))
        assert (output_dir / "tickets.jsonl").exists()
        assert (output_dir / "review_queue.jsonl").exists()
        assert [item.request_id for item in read_log(output_dir)] == [item["request_id"] for item in lines_of(out)]

    def test_a_json_array_and_a_single_object_are_accepted(self, cli_run: Cli, tmp_path: Path) -> None:
        array = tmp_path / "a.json"
        array.write_text(json.dumps([record("INQ-1"), record("INQ-2")], ensure_ascii=False), encoding="utf-8")
        single = tmp_path / "s.json"
        single.write_text(json.dumps(record("INQ-3"), ensure_ascii=False), encoding="utf-8")
        classifier = FakeClassifier(success())
        assert len(lines_of(cli_run.run("run", "--input", str(array), classifier=classifier)[1])) == 2
        assert len(lines_of(cli_run.run("run", "--input", str(single), classifier=classifier)[1])) == 1

    def test_results_with_human_review_and_ticket_failures_still_exit_zero(self, cli_run: Cli, tmp_path: Path) -> None:
        # 終了コード 0：全件を処理した（Human Review や再試行キュー行きを含む）。
        outcome = ClassifyOutcome(status=ClassifyStatus.LLM_FAILURE, attempts=3, error_detail="APIConnectionError")
        path = write_jsonl(tmp_path / "in.jsonl", [record("INQ-1"), record("INQ-2")])
        code, out, _ = cli_run.run("run", "--input", str(path), classifier=FakeClassifier(outcome))
        assert code == 0
        assert [item["kind"] for item in lines_of(out)] == ["human_review", "human_review"]


class TestInputErrors:
    """不正な1件：`input_error` として出力し、他の件は処理する。終了コード 1（REQ-001 AC-5、ERR-004）。"""

    def test_an_invalid_line_is_reported_and_the_rest_is_processed(
        self, cli_run: Cli, tmp_path: Path, output_dir: Path
    ) -> None:
        path = tmp_path / "in.jsonl"
        path.write_text(
            json.dumps(record("INQ-1")) + "\n" + '{"channel": "email"}\n' + json.dumps(record("INQ-3")) + "\n",
            encoding="utf-8",
        )
        classifier = FakeClassifier(success())
        code, out, err = cli_run.run("run", "--input", str(path), classifier=classifier)
        assert code == 1
        first, bad, third = lines_of(out)
        assert (first["kind"], third["kind"]) == ("auto_registered", "auto_registered")
        assert bad["kind"] == "input_error"
        assert bad["position"] == 2
        assert bad["error"] == "body: missing"
        assert len(classifier.calls) == 2
        assert "入力エラー: 1件" in err
        # 処理ログにも記録される。
        log = read_log(output_dir)
        assert [item.kind for item in log] == [
            ResultKind.AUTO_REGISTERED,
            ResultKind.INPUT_ERROR,
            ResultKind.AUTO_REGISTERED,
        ]
        assert log[1].position == 2
        assert log[1].request_id == bad["request_id"]

    def test_the_input_values_appear_nowhere(self, cli_run: Cli, tmp_path: Path, output_dir: Path) -> None:
        # 本文が配列（メールアドレスを含む）、`form_fields` のキーがメールアドレス、という不正な入力。
        path = tmp_path / "in.jsonl"
        path.write_text(
            json.dumps({"channel": "email", "body": [SENDER]})
            + "\n"
            + json.dumps({"channel": "email", "body": "本文", "form_fields": {SENDER: 1}})
            + "\n",
            encoding="utf-8",
        )
        code, out, err = cli_run.run("run", "--input", str(path), classifier=FakeClassifier(success()))
        assert code == 1
        assert [item["kind"] for item in lines_of(out)] == ["input_error", "input_error"]
        raw_log = (output_dir / "process_log.jsonl").read_text(encoding="utf-8")
        for text in (out, err, raw_log):
            assert SENDER not in text
        assert all(item.input is None for item in read_log(output_dir))

    def test_a_process_error_also_exits_with_one(self, cli_run: Cli, tmp_path: Path) -> None:
        class Raising:
            def classify(self, inquiry: Inquiry, request_id: str) -> ClassifyOutcome:
                raise RuntimeError(f"boom {SENDER}")

        path = write_jsonl(tmp_path / "in.jsonl", [record("INQ-1")])
        code, out, err = cli_run.run("run", "--input", str(path), classifier=Raising())
        assert code == 1
        (item,) = lines_of(out)
        assert item["kind"] == "process_error"
        assert item["error"] == "RuntimeError"
        assert SENDER not in out + err
        assert "処理エラー: 1件" in err


class TestStartupErrors:
    """起動できない：処理を開始せず、原因を示して、終了コード 2（ERR-006、REQ-001 AC-4、AC-7）。"""

    def test_a_missing_input_file(self, cli_run: Cli, tmp_path: Path) -> None:
        classifier = FakeClassifier(success())
        code, out, err = cli_run.run("run", "--input", str(tmp_path / "missing.jsonl"), classifier=classifier)
        assert code == 2
        assert out == ""
        assert "入力ファイルを処理できません" in err
        assert classifier.calls == []

    @pytest.mark.parametrize("name", ["in.txt", "in.csv", "in"])
    def test_an_unsupported_extension(self, cli_run: Cli, tmp_path: Path, name: str) -> None:
        path = tmp_path / name
        path.write_text(json.dumps(record("INQ-1")), encoding="utf-8")
        classifier = FakeClassifier(success())
        code, out, err = cli_run.run("run", "--input", str(path), classifier=classifier)
        assert code == 2
        assert out == ""
        assert "入力ファイルを処理できません" in err
        assert classifier.calls == []

    def test_a_json_syntax_error_does_not_show_the_content(self, cli_run: Cli, tmp_path: Path, output_dir: Path) -> None:
        path = tmp_path / "in.json"
        path.write_text(f'{{"body": "{SENDER}", ', encoding="utf-8")
        classifier = FakeClassifier(success())
        code, out, err = cli_run.run("run", "--input", str(path), classifier=classifier)
        assert code == 2
        assert out == ""
        assert "json_invalid" in err
        assert SENDER not in err
        assert classifier.calls == []
        assert not output_dir.exists()

    @pytest.mark.parametrize("content", ["", "  \n\n", "[]"], ids=["empty", "blank", "empty-array"])
    def test_no_inquiries_is_a_normal_end_with_a_summary(self, cli_run: Cli, tmp_path: Path, content: str) -> None:
        path = tmp_path / "in.json"
        path.write_text(content, encoding="utf-8")
        classifier = FakeClassifier(success())
        code, out, err = cli_run.run("run", "--input", str(path), classifier=classifier)
        assert code == 0
        assert out == ""
        assert "処理結果: 0件" in err
        assert "自動登録: 0件" in err
        assert classifier.calls == []

    def test_an_empty_jsonl_file(self, cli_run: Cli, tmp_path: Path) -> None:
        path = tmp_path / "in.jsonl"
        path.write_bytes(b"")
        code, out, err = cli_run.run("run", "--input", str(path), classifier=FakeClassifier(success()))
        assert (code, out) == (0, "")
        assert "処理結果: 0件" in err

    def test_a_missing_setting_names_the_item(self, cli_run: Cli, config_dir: Path, tmp_path: Path) -> None:
        settings = yaml.safe_load((config_dir / "settings.yaml").read_text(encoding="utf-8"))
        del settings["decision"]["confidence_threshold"]
        (config_dir / "settings.yaml").write_text(yaml.safe_dump(settings), encoding="utf-8")
        path = write_jsonl(tmp_path / "in.jsonl", [record("INQ-1")])
        classifier = FakeClassifier(success())
        code, out, err = cli_run.run("run", "--input", str(path), classifier=classifier)
        assert code == 2
        assert out == ""
        assert "設定に不備があります" in err
        assert "confidence_threshold" in err
        assert classifier.calls == []

    def test_an_invalid_setting_value_names_the_item(self, cli_run: Cli, config_dir: Path, tmp_path: Path) -> None:
        settings = yaml.safe_load((config_dir / "settings.yaml").read_text(encoding="utf-8"))
        settings["decision"]["confidence_threshold"] = 1.5
        (config_dir / "settings.yaml").write_text(yaml.safe_dump(settings), encoding="utf-8")
        path = write_jsonl(tmp_path / "in.jsonl", [record("INQ-1")])
        code, _, err = cli_run.run("run", "--input", str(path), classifier=FakeClassifier(success()))
        assert code == 2
        assert "confidence_threshold" in err

    def test_an_empty_high_risk_keyword_list_is_rejected(self, cli_run: Cli, config_dir: Path, tmp_path: Path) -> None:
        (config_dir / "keywords.yaml").write_text("high_risk: []\nurgent: []\n", encoding="utf-8")
        path = write_jsonl(tmp_path / "in.jsonl", [record("INQ-1")])
        code, _, err = cli_run.run("run", "--input", str(path), classifier=FakeClassifier(success()))
        assert code == 2
        assert "high_risk" in err

    def test_a_missing_config_directory(self, cli_run: Cli, tmp_path: Path) -> None:
        path = write_jsonl(tmp_path / "in.jsonl", [record("INQ-1")])
        code, _, err = cli_run.run("run", "--input", str(path), "--config-dir", str(tmp_path / "nope"), with_config=False)
        assert code == 2
        assert "設定に不備があります" in err


class TestApiKey:
    """`OPENAI_API_KEY` は、LLM を使う前に検証する。値は、どこへも出さない（ERR-006、SEC-001）。"""

    @pytest.mark.parametrize("value", [None, "", "   "], ids=["unset", "empty", "blank"])
    def test_a_missing_key_is_detected_before_anything_is_processed(
        self, cli_run: Cli, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, value: str | None
    ) -> None:
        if value is None:
            monkeypatch.delenv("OPENAI_API_KEY")
        else:
            monkeypatch.setenv("OPENAI_API_KEY", value)
        path = write_jsonl(tmp_path / "in.jsonl", [record("INQ-1")])
        classifier = FakeClassifier(success())
        code, out, err = cli_run.run("run", "--input", str(path), classifier=classifier)
        assert code == 2
        assert out == ""
        assert classifier.calls == []
        # 環境変数名と、2通りの設定方法（Design 第8.1節）。
        assert "OPENAI_API_KEY" in err
        assert "export OPENAI_API_KEY=" in err
        assert "--env-file .env" in err

    def test_the_key_is_checked_before_the_input_file(self, cli_run: Cli, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("OPENAI_API_KEY")
        code, _, err = cli_run.run("run", "--input", str(tmp_path / "missing.jsonl"), classifier=FakeClassifier(success()))
        assert code == 2
        assert "OPENAI_API_KEY" in err
        assert "入力ファイル" not in err

    def test_the_key_value_is_never_printed_or_written(
        self, cli_run: Cli, tmp_path: Path, output_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        path = write_jsonl(tmp_path / "in.jsonl", [record("INQ-1"), record("INQ-2", body="解約したい")])
        _, out, err = cli_run.run("run", "--input", str(path), classifier=FakeClassifier(success()))
        assert API_KEY not in out + err
        for written in output_dir.iterdir():
            assert API_KEY not in written.read_text(encoding="utf-8")
        # 未設定のときのメッセージにも、（別の）キーの値は現れない。
        monkeypatch.setenv("OPENAI_API_KEY", " ")
        _, _, missing = cli_run.run("run", "--input", str(path), classifier=FakeClassifier(success()))
        assert API_KEY not in missing

    def test_the_app_does_not_read_a_dot_env_file(
        self, cli_run: Cli, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # `.env` があっても、自動では読み込まない（追加の依存を増やさない。Design 第8.1節）。
        monkeypatch.chdir(tmp_path)
        (tmp_path / ".env").write_text("OPENAI_API_KEY=from-dot-env\n", encoding="utf-8")
        monkeypatch.delenv("OPENAI_API_KEY")
        path = write_jsonl(tmp_path / "in.jsonl", [record("INQ-1")])
        code, _, _ = cli_run.run("run", "--input", str(path), classifier=FakeClassifier(success()))
        assert code == 2
        assert "OPENAI_API_KEY" not in os.environ


class SabotagingClassifier:
    """`on_call` 回目の分類で、決められた妨害（ファイルの書き込みを失敗させる）を行う。"""

    def __init__(self, outcome: ClassifyOutcome, on_call: int, sabotage: Callable[[], None]) -> None:
        self.outcome = outcome
        self.on_call = on_call
        self.sabotage = sabotage
        self.calls: list[tuple[Inquiry, str]] = []

    def classify(self, inquiry: Inquiry, request_id: str) -> ClassifyOutcome:
        self.calls.append((inquiry, request_id))
        if len(self.calls) == self.on_call:
            self.sabotage()
        return self.outcome


def make_unwritable(path: Path) -> Callable[[], None]:
    """そのファイルを、同じ名前のディレクトリに置き換える（以後の追記が失敗する）。"""

    def sabotage() -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            path.unlink()
        path.mkdir()

    return sabotage


class TestAbortOnStorageFailure:
    """ログ・キューの書き込み失敗：処理を中断し、標準エラー出力へ表示して、終了コード 1（ERR-007、ADR-013）。"""

    def make_input(self, tmp_path: Path) -> Path:
        return write_jsonl(
            tmp_path / "in.jsonl",
            [
                record("INQ-1", subject="件名の固有の断片AAA"),
                record("INQ-2", subject="件名の固有の断片BBB", body="本文の固有の断片CCC"),
                record("INQ-3", subject="件名の固有の断片DDD"),
            ],
        )

    def test_the_second_inquiry_fails_to_write_the_log(self, cli_run: Cli, tmp_path: Path, output_dir: Path) -> None:
        path = self.make_input(tmp_path)
        classifier = SabotagingClassifier(
            success(summary="要約の固有の断片EEE"), on_call=2, sabotage=make_unwritable(output_dir / "process_log.jsonl")
        )
        code, out, err = cli_run.run("run", "--input", str(path), classifier=classifier)
        assert code == 1
        # 標準出力には、1件目の結果だけがある（中断した2件目の結果は出ない）。
        (first,) = lines_of(out)
        assert first["inquiry_id"] == "INQ-1"
        # 3件目は処理されない（分類器が呼ばれない）。
        assert len(classifier.calls) == 2
        # 標準エラー出力：中断した旨、失敗したファイル名、原因の種類、2件目の request_id、処理済みの件数。
        assert "処理を中断しました" in err
        assert "process_log.jsonl" in err
        assert "IsADirectoryError" in err
        assert classifier.calls[1][1] in err
        assert "処理を完了した件数: 1件" in err
        assert first["request_id"] not in err
        # 問い合わせの内容は、含まれない。
        for content in ("件名の固有の断片", "本文の固有の断片CCC", SENDER, "要約の固有の断片EEE"):
            assert content not in err
            assert content not in out

    def test_the_review_queue_failure_aborts_too(self, cli_run: Cli, tmp_path: Path, output_dir: Path) -> None:
        # 2件目が Human Review（解約）で、キューへ書き込めない。
        path = self.make_input(tmp_path)
        path.write_text(
            path.read_text(encoding="utf-8").replace("本文の固有の断片CCC", "解約したい 本文の固有の断片CCC"), encoding="utf-8"
        )
        (output_dir).mkdir()
        (output_dir / "review_queue.jsonl").mkdir()
        classifier = FakeClassifier(success())
        code, out, err = cli_run.run("run", "--input", str(path), classifier=classifier)
        assert code == 1
        assert [item["inquiry_id"] for item in lines_of(out)] == ["INQ-1"]
        assert "review_queue.jsonl" in err
        assert "処理を完了した件数: 1件" in err
        assert len(classifier.calls) == 2
        assert "本文の固有の断片CCC" not in err

    def test_the_retry_queue_failure_aborts_too(self, cli_run: Cli, tmp_path: Path, output_dir: Path) -> None:
        # チケットの登録先は失敗させず（ERR-003 の経路へ入り）、再試行キューへ書き込めない。
        (output_dir / "tickets.jsonl").mkdir(parents=True)
        (output_dir / "retry_queue.jsonl").mkdir()
        path = write_jsonl(tmp_path / "in.jsonl", [record("INQ-1")])
        code, out, err = cli_run.run("run", "--input", str(path), classifier=FakeClassifier(success()))
        assert code == 1
        assert out == ""
        assert "retry_queue.jsonl" in err
        assert "処理を完了した件数: 0件" in err

    def test_a_failure_to_record_an_input_error_aborts_too(self, cli_run: Cli, tmp_path: Path, output_dir: Path) -> None:
        (output_dir / "process_log.jsonl").mkdir(parents=True)
        path = tmp_path / "in.jsonl"
        path.write_text('{"channel": "email"}\n' + json.dumps(record("INQ-2")) + "\n", encoding="utf-8")
        classifier = FakeClassifier(success())
        code, out, err = cli_run.run("run", "--input", str(path), classifier=classifier)
        assert code == 1
        assert out == ""
        assert classifier.calls == []
        assert "process_log.jsonl" in err
        assert "処理を完了した件数: 0件" in err
        assert UUID_PATTERN.search(err)

    def test_the_results_before_the_abort_are_already_output(self, cli_run: Cli, tmp_path: Path, output_dir: Path) -> None:
        path = self.make_input(tmp_path)
        classifier = SabotagingClassifier(success(), on_call=3, sabotage=make_unwritable(output_dir / "process_log.jsonl"))
        _, out, err = cli_run.run("run", "--input", str(path), classifier=classifier)
        assert [item["inquiry_id"] for item in lines_of(out)] == ["INQ-1", "INQ-2"]
        assert "処理を完了した件数: 2件" in err

    def test_the_abort_is_distinguished_from_the_completion_by_the_message(
        self, cli_run: Cli, tmp_path: Path, output_dir: Path
    ) -> None:
        # 終了コード 1 は、「全件を処理したがエラーの件があった」場合と共通。両者は、標準エラー出力で区別する。
        bad = tmp_path / "bad.jsonl"
        bad.write_text('{"channel": "email"}\n', encoding="utf-8")
        code_finished, _, finished = cli_run.run("run", "--input", str(bad), classifier=FakeClassifier(success()))
        # 中断のほうは、書き込み先を壊してから実行する（同じ出力先を使うため、あとに行う）。
        sabotage = make_unwritable(output_dir / "process_log.jsonl")
        classifier = SabotagingClassifier(success(), on_call=1, sabotage=sabotage)
        code_aborted, _, aborted = cli_run.run("run", "--input", str(self.make_input(tmp_path)), classifier=classifier)
        assert code_finished == code_aborted == 1
        assert "処理を中断しました" in aborted
        assert "処理を中断しました" not in finished


class TestSharedEntry:
    """中断の処理は、サブコマンドに依存しない共通の入口で行われる（ERR-007、CMP-001）。"""

    def test_a_storage_error_from_any_subcommand_is_reported_in_the_same_form(
        self, cli_run: Cli, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def failing(context: CommandContext) -> int:
            raise StorageError("review_queue.jsonl: 1行目が壊れています（json_invalid）")

        monkeypatch.setitem(
            cli.COMMANDS, "run", Subcommand(name="run", help="差し替え", add_arguments=lambda parser: None, execute=failing, needs_api_key=False)
        )
        code, out, err = cli_run.run("run")
        assert code == 1
        assert out == ""
        assert "処理を中断しました" in err
        assert "review_queue.jsonl: 1行目が壊れています（json_invalid）" in err
        # 進捗を持たないコマンドでは、リクエストID・件数は表示されない。
        assert "リクエストID" not in err
        assert "件数" not in err

    def test_the_review_id_and_the_count_are_shown_when_the_command_provides_them(
        self, cli_run: Cli, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def failing(context: CommandContext) -> int:
            context.progress.review_id = "REV-abcd1234"
            context.progress.processed = 4
            raise StorageError("review_resolutions.jsonl: 書き込めません（PermissionError）")

        monkeypatch.setitem(
            cli.COMMANDS, "run", Subcommand(name="run", help="差し替え", add_arguments=lambda parser: None, execute=failing, needs_api_key=False)
        )
        code, _, err = cli_run.run("run")
        assert code == 1
        assert "レビューID: REV-abcd1234" in err
        assert "処理を完了した件数: 4件" in err

    def test_a_newly_registered_subcommand_is_parsed_and_uses_the_same_entry(
        self, cli_run: Cli, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        seen: list[str] = []

        def execute(context: CommandContext) -> int:
            seen.append(context.args.target)
            raise StorageError("x.jsonl: 読み取れません（OSError）")

        monkeypatch.setitem(
            cli.COMMANDS,
            "extra",
            Subcommand(
                name="extra",
                help="追加のサブコマンド",
                add_arguments=lambda parser: parser.add_argument("--target", required=True),
                execute=execute,
                needs_api_key=False,
            ),
        )
        code, _, err = cli_run.run("extra", "--target", "t")
        assert (code, seen) == (1, ["t"])
        assert "x.jsonl: 読み取れません（OSError）" in err

    def test_the_api_key_is_checked_only_for_commands_that_need_it(
        self, cli_run: Cli, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("OPENAI_API_KEY")
        monkeypatch.setitem(
            cli.COMMANDS, "run", Subcommand(name="run", help="差し替え", add_arguments=lambda parser: None, execute=lambda c: 0, needs_api_key=False)
        )
        assert cli_run.run("run")[0] == 0


class TestArguments:
    def test_the_default_config_directory(self) -> None:
        assert build_parser().parse_args(["run", "--input", "x.jsonl"]).config_dir == "config"

    def test_the_config_dir_can_precede_or_follow_the_subcommand(
        self, cli_run: Cli, tmp_path: Path, output_dir: Path
    ) -> None:
        path = write_jsonl(tmp_path / "in.jsonl", [record("INQ-1")])
        config = str(cli_run.config_dir)
        code, out, _ = cli_run.run(
            "--config-dir", config, "run", "--input", str(path), classifier=FakeClassifier(success()), with_config=False
        )
        assert (code, len(lines_of(out))) == (0, 1)
        # 指定した設定（出力先が `tmp_path/out`）が使われた（サブコマンドの既定値で、上書きされていない）。
        assert len(read_log(output_dir)) == 1
        code, out, _ = cli_run.run(
            "run", "--input", str(path), "--config-dir", config, classifier=FakeClassifier(success()), with_config=False
        )
        assert (code, len(lines_of(out))) == (0, 1)
        assert len(read_log(output_dir)) == 2

    def test_without_the_option_the_default_directory_is_used(self, cli_run: Cli, tmp_path: Path) -> None:
        # 既定の `config` は、作業ディレクトリからの相対。ここには、ないため、設定の不備（終了コード 2）になる。
        path = write_jsonl(tmp_path / "in.jsonl", [record("INQ-1")])
        code, _, err = cli_run.run("run", "--input", str(path), classifier=FakeClassifier(success()), with_config=False)
        assert code == 2
        assert "設定に不備があります" in err
        assert "config" in err

    @pytest.mark.parametrize("argv", [[], ["run"], ["unknown"], ["run", "--input"]], ids=["none", "no-input", "unknown", "no-value"])
    def test_invalid_arguments_exit_with_two(self, argv: list[str], capsys: pytest.CaptureFixture[str]) -> None:
        with pytest.raises(SystemExit) as raised:
            main(argv)
        assert raised.value.code == 2
        assert capsys.readouterr().out == ""


class TestEntryPoints:
    """`python -m triage_agent` とコンソールスクリプトは、同じ入口を使う。別のプロセスで実行する。

    LLM は呼ばれない：本文が短く、入力ガードレールに該当する入力を使う。念のため、API の接続先を、
    接続できないローカルのアドレスにし、実行トレースを無効にする。
    """

    def run_process(self, command: list[str], config_dir: Path, tmp_path: Path) -> subprocess.CompletedProcess[str]:
        path = write_jsonl(tmp_path / "short.jsonl", [record("INQ-1", body="あいう")])
        env = {
            **os.environ,
            "OPENAI_API_KEY": API_KEY,
            "OPENAI_BASE_URL": "http://127.0.0.1:9",
            "OPENAI_AGENTS_DISABLE_TRACING": "1",
        }
        return subprocess.run(
            [*command, "run", "--input", str(path), "--config-dir", str(config_dir)],
            capture_output=True,
            text=True,
            check=False,
            cwd=tmp_path,
            env=env,
            timeout=120,
        )

    def check(self, completed: subprocess.CompletedProcess[str]) -> None:
        assert completed.returncode == 0, completed.stderr
        (item,) = lines_of(completed.stdout)
        assert item["kind"] == "human_review"
        assert item["category"] == "unclassified"
        assert item["reasons"] == ["INPUT_GUARDRAIL"]
        assert "処理結果: 1件" in completed.stderr
        assert API_KEY not in completed.stdout + completed.stderr

    def test_python_dash_m(self, config_dir: Path, tmp_path: Path) -> None:
        self.check(self.run_process([sys.executable, "-m", "triage_agent"], config_dir, tmp_path))

    def test_the_console_script(self, config_dir: Path, tmp_path: Path) -> None:
        script = Path(sys.executable).parent / "triage-agent"
        assert script.exists(), "コンソールスクリプトがインストールされていない（uv sync を実行する）"
        self.check(self.run_process([str(script)], config_dir, tmp_path))


class TestRealClassifierWiring:
    """分類器を注入しないとき、設定が、本物の分類器（Agents SDK）へ届く（SEC-003、ADR-005）。

    `Runner.run_sync` だけを差し替えて、渡された Agent と実行設定（`RunConfig`）を捕捉する（LLM・ネットワークなし）。
    """

    def run_with(
        self, cli_run: Cli, config_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, **changes: object
    ) -> tuple[SimpleNamespace, dict[str, object]]:
        settings = yaml.safe_load((config_dir / "settings.yaml").read_text(encoding="utf-8"))
        for section, values in changes.items():
            settings[section].update(values)  # type: ignore[arg-type]
        (config_dir / "settings.yaml").write_text(yaml.safe_dump(settings, allow_unicode=True), encoding="utf-8")
        captured: dict[str, object] = {}

        def fake_run_sync(agent: object, input_text: str, **kwargs: object) -> SimpleNamespace:
            captured.update(kwargs)
            captured["agent"] = agent
            return SimpleNamespace(final_output=make_result())

        monkeypatch.setattr(Runner, "run_sync", fake_run_sync)
        path = write_jsonl(tmp_path / "in.jsonl", [record("INQ-1")])
        code, out, err = cli_run.run("run", "--input", str(path))
        assert code == 0, err
        return SimpleNamespace(code=code, out=out, err=err), captured

    @pytest.mark.parametrize(("enabled", "include"), [(True, True), (False, False), (True, False)])
    def test_the_tracing_settings_reach_the_run_config(
        self, cli_run: Cli, config_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, enabled: bool, include: bool
    ) -> None:
        _, captured = self.run_with(
            cli_run, config_dir, tmp_path, monkeypatch, tracing={"enabled": enabled, "include_sensitive_data": include}
        )
        run_config = captured["run_config"]
        assert run_config.tracing_disabled is (not enabled)  # type: ignore[attr-defined]
        assert run_config.trace_include_sensitive_data is include  # type: ignore[attr-defined]

    def test_the_llm_settings_reach_the_agent(
        self, cli_run: Cli, config_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _, captured = self.run_with(
            cli_run, config_dir, tmp_path, monkeypatch, llm={"model": "custom-model", "timeout_seconds": 7.5}
        )
        agent = captured["agent"]
        assert agent.model.model == "custom-model"  # type: ignore[attr-defined]
        assert agent.model_settings.timeout == 7.5  # type: ignore[attr-defined]

    def test_the_result_of_the_real_classifier_is_processed(
        self, cli_run: Cli, config_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        run, _ = self.run_with(cli_run, config_dir, tmp_path, monkeypatch)
        (item,) = lines_of(run.out)
        assert item["kind"] == "auto_registered"
        assert API_KEY not in run.out + run.err
