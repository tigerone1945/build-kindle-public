"""CLI（CMP-001）：引数の解釈、起動時の検証、結果の表示、終了コード。

業務ロジックは持たない（Pipeline などへ委譲する）。

サブコマンドは、`COMMANDS`（登録表）に登録する。新しいサブコマンドは、`Subcommand` を登録するだけで加えられる。

**ログ・キューの書き込み・読み込みの失敗（`StorageError`）による中断（ERR-007、ADR-013）は、`main` の1か所で行う。**
サブコマンドごとには書かない。全てのサブコマンドが、同じ表示・同じ終了コード（1）になる。サブコマンドは、
中断の表示に要る情報（処理中の Pipeline、処理を完了した件数、レビューID）を、`Progress` へ書き込む。

終了コード（Design 第8.1節）：0（全件を処理した）、1（入力エラー・処理エラーの件があった、または中断）、
2（起動できない。設定不備・API Key 未設定・入力ファイルの不備・引数不正）。
"""

import argparse
import os
import sys
from collections import Counter
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from triage_agent.classifier import AgentsSdkClassifier, Classifier
from triage_agent.config import AppConfig, ConfigError, load_config
from triage_agent.evaluation import EvaluationDatasetError, Evaluator, format_report, load_dataset
from triage_agent.inquiry_io import InputFileError, load_inquiries
from triage_agent.models import Category, InvalidRecord, Priority, ResultKind, ReviewValues
from triage_agent.pipeline import Pipeline
from triage_agent.process_log import ProcessLog
from triage_agent.review import ReviewError, ReviewQueue, ReviewResolver
from triage_agent.storage import StorageError
from triage_agent.tickets import MockTicketSystem, RetryQueue, TicketRegistrationError

EXIT_OK = 0
EXIT_FAILURE = 1
EXIT_STARTUP_ERROR = 2

API_KEY_ENV = "OPENAI_API_KEY"
DEFAULT_CONFIG_DIR = "config"

# 処理結果の区分ごとの件数の要約に使う表示名（REQ-011 AC-2）。
KIND_LABELS = {
    ResultKind.AUTO_REGISTERED: "自動登録",
    ResultKind.HUMAN_REVIEW: "Human Review",
    ResultKind.TICKET_FAILED: "チケット登録失敗",
    ResultKind.INPUT_ERROR: "入力エラー",
    ResultKind.PROCESS_ERROR: "処理エラー",
}


@dataclass
class Progress:
    """コマンドの進捗。処理を中断したときの表示に使う（ERR-007）。コマンドが書き込む。

    - `pipeline`：処理中の問い合わせの `request_id`（`current_request_id`）の取得元。
    - `processed`：処理を完了した件数（`run`・`eval`）。件数を数えないコマンドは `None`。
    - `review_id`：`review resolve` が、対象とするレビューID。
    """

    pipeline: Pipeline | None = None
    processed: int | None = None
    review_id: str | None = None


@dataclass(frozen=True)
class CommandContext:
    """サブコマンドの実行に渡すもの。`classifier` は、テストが注入する分類器（`None` なら、本物を作る）。"""

    args: argparse.Namespace
    config: AppConfig
    classifier: Classifier | None
    progress: Progress = field(default_factory=Progress)


@dataclass(frozen=True)
class Subcommand:
    """登録表の1件。`needs_api_key` が真なら、実行の前に、API Key の有無を検証する（ERR-006）。"""

    name: str
    help: str
    add_arguments: Callable[[argparse.ArgumentParser], None]
    execute: Callable[[CommandContext], int]
    needs_api_key: bool


# --- 共通の部品 ---


def _output_dir(config: AppConfig) -> Path:
    return Path(config.settings.paths.output_dir)


def build_review_resolver(config: AppConfig) -> ReviewResolver:
    """設定の出力先へ書く、Human Review の解決を組み立てる（LLM は使わない）。"""
    output_dir = _output_dir(config)
    return ReviewResolver(
        queue=ReviewQueue(output_dir),
        tickets=MockTicketSystem(output_dir),
        retry_queue=RetryQueue(output_dir),
        process_log=ProcessLog(output_dir),
        master=config.categories,
    )


def build_pipeline(config: AppConfig, classifier: Classifier | None, output_dir: Path | None = None) -> Pipeline:
    """Pipeline を組み立てる。出力先は、既定では設定の出力先（`eval` は、一時ディレクトリを渡す）。

    `classifier` がなければ、Agents SDK の分類器を作る。
    """
    if output_dir is None:
        output_dir = _output_dir(config)
    if classifier is None:
        classifier = AgentsSdkClassifier(
            config.settings.llm,
            config.categories,
            config.settings.guardrail.min_body_length,
            tracing=config.settings.tracing,
        )
    return Pipeline(
        classifier=classifier,
        config=config,
        tickets=MockTicketSystem(output_dir),
        retry_queue=RetryQueue(output_dir),
        review_queue=ReviewQueue(output_dir),
        process_log=ProcessLog(output_dir),
    )


def _print_error(message: str) -> None:
    print(message, file=sys.stderr)


# --- run ---


def _add_run_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--input", required=True, metavar="FILE", help="問い合わせの入力ファイル（.json または .jsonl）")


def _run(context: CommandContext) -> int:
    """入力ファイルの全件を処理し、結果を1件1行の JSON で標準出力へ出す（REQ-001、REQ-011）。"""
    try:
        records = load_inquiries(Path(context.args.input))
    except InputFileError as error:
        # 処理を開始しない（REQ-001 AC-4）。メッセージは、原因の種類と位置だけで、ファイルの内容を含まない。
        _print_error(f"入力ファイルを処理できません: {error}")
        return EXIT_STARTUP_ERROR

    pipeline = build_pipeline(context.config, context.classifier)
    progress = context.progress
    progress.pipeline = pipeline
    progress.processed = 0

    counts: Counter[ResultKind] = Counter()
    for record in records:
        # リクエストIDの付与とログ記録は、Pipeline が行う。CLI は行わない。
        result = pipeline.record_invalid_input(record) if isinstance(record, InvalidRecord) else pipeline.process(record)
        # 結果は、1件ごとに出力する（中断しても、それまでの結果は出力されている。ERR-007）。
        print(result.model_dump_json(), flush=True)
        counts[result.kind] += 1
        progress.processed += 1

    _print_summary(counts)
    has_errors = counts[ResultKind.INPUT_ERROR] > 0 or counts[ResultKind.PROCESS_ERROR] > 0
    return EXIT_FAILURE if has_errors else EXIT_OK


def _print_summary(counts: Counter[ResultKind]) -> None:
    """処理結果ごとの件数の要約を、標準エラー出力へ出す（REQ-011 AC-2、REQ-001 AC-7）。"""
    lines = [f"処理結果: {sum(counts.values())}件"]
    lines.extend(f"  {label}: {counts[kind]}件" for kind, label in KIND_LABELS.items())
    _print_error("\n".join(lines))


# --- eval ---


def _add_eval_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--dataset", required=True, metavar="FILE", help="正解つきの評価データセット（.jsonl のみ）")


def _eval(context: CommandContext) -> int:
    """評価データセットの全件を処理し、指標と不一致を標準出力へ出す（REQ-015）。

    データセットが不正なときは、評価を始めず、終了コード 2。処理エラー（想定外の例外）の件があれば、`run` と同じく、
    終了コード 1（不一致だけなら 0）。書き込み・読み込みの失敗は、`main` の共通の入口が中断する（ERR-007）。
    """
    try:
        rows = load_dataset(Path(context.args.dataset))
    except EvaluationDatasetError as error:
        # 評価を始めない（REQ-015 AC-4・AC-5）。メッセージは、原因の種類と位置だけで、内容を含まない。
        _print_error(f"評価データセットを処理できません: {error}")
        return EXIT_STARTUP_ERROR

    evaluator = Evaluator(lambda output_dir: build_pipeline(context.config, context.classifier, output_dir))
    report = evaluator.evaluate(rows, context.progress)
    print(format_report(report), flush=True)
    if report.process_errors > 0:
        _print_error(f"処理エラー: {report.process_errors}件（想定外の例外。不一致の一覧に、例外の種類を示した）")
        return EXIT_FAILURE
    return EXIT_OK


# --- review ---


def _add_review_arguments(parser: argparse.ArgumentParser) -> None:
    actions = parser.add_subparsers(dest="review_command", required=True, metavar="ACTION")
    list_parser = actions.add_parser("list", help="未対応の Human Review 項目を、1件1行の JSON で表示する")
    _add_config_dir(list_parser, default=argparse.SUPPRESS)
    resolve_parser = actions.add_parser(
        "resolve", help="Human Review 項目を確定して、チケットを登録する（修正の指定がなければ、承認）"
    )
    _add_config_dir(resolve_parser, default=argparse.SUPPRESS)
    resolve_parser.add_argument("review_id", metavar="REVIEW_ID", help="確定する項目のレビューID")
    resolve_parser.add_argument("--category", choices=[category.value for category in Category], help="修正後のカテゴリ")
    resolve_parser.add_argument("--priority", choices=[priority.value for priority in Priority], help="修正後の優先度")
    resolve_parser.add_argument("--department", help="修正後の担当部署（カテゴリのマスタにある部署に限る）")
    resolve_parser.add_argument("--reviewer", help="担当者の名前（記録に残す）")


def _review(context: CommandContext) -> int:
    if context.args.review_command == "list":
        return _review_list(context)
    return _review_resolve(context)


def _review_list(context: CommandContext) -> int:
    """未対応の項目を、1件1行の JSON で標準出力へ出す（REQ-010 AC-1）。読み込みに失敗したら、何も出さずに中断する。"""
    for item in ReviewQueue(_output_dir(context.config)).list_pending():
        print(item.model_dump_json(), flush=True)
    return EXIT_OK


def _review_resolve(context: CommandContext) -> int:
    """項目を確定する（REQ-010 AC-2〜AC-9）。確定できなければ、エラーを表示して、終了コード 1 で終わる。"""
    args = context.args
    # 中断したときに、レビューIDを表示する（ERR-007）。
    context.progress.review_id = args.review_id
    corrections = ReviewValues(
        category=Category(args.category) if args.category is not None else None,
        priority=Priority(args.priority) if args.priority is not None else None,
        department=args.department,
    )
    try:
        resolution = build_review_resolver(context.config).resolve(args.review_id, corrections, args.reviewer)
    except ReviewError as error:
        _print_error(f"エラー: {error}")
        return EXIT_FAILURE
    except TicketRegistrationError as error:
        # 再試行キューへの登録は、resolve が済ませている。項目は、未対応のまま残る（REQ-010 AC-7）。
        _print_error(f"エラー: チケットを登録できませんでした（{error}）。再試行キューへ登録しました。項目は、未対応のままです")
        return EXIT_FAILURE
    print(resolution.model_dump_json(), flush=True)
    return EXIT_OK


# 登録表。
COMMANDS: dict[str, Subcommand] = {
    "run": Subcommand(
        name="run",
        help="入力ファイルの問い合わせを処理する",
        add_arguments=_add_run_arguments,
        execute=_run,
        needs_api_key=True,
    ),
    "eval": Subcommand(
        name="eval",
        help="正解つきのデータセットで、分類精度・Routing 精度・Escalation 妥当性を測る",
        add_arguments=_add_eval_arguments,
        execute=_eval,
        needs_api_key=True,
    ),
    "review": Subcommand(
        name="review",
        help="Human Review の一覧と解決",
        add_arguments=_add_review_arguments,
        execute=_review,
        needs_api_key=False,
    ),
}


# --- 引数の解釈 ---


def _add_config_dir(parser: argparse.ArgumentParser, *, default: object) -> None:
    parser.add_argument(
        "--config-dir",
        default=default,
        metavar="DIR",
        help=f"設定ファイル（settings.yaml・categories.yaml・keywords.yaml）のディレクトリ（既定：{DEFAULT_CONFIG_DIR}）",
    )


def build_parser() -> argparse.ArgumentParser:
    """引数の解釈。共通オプション `--config-dir` は、サブコマンドの前後のどちらにも置ける。"""
    parser = argparse.ArgumentParser(prog="triage-agent", description="問い合わせトリアージAIエージェント")
    _add_config_dir(parser, default=DEFAULT_CONFIG_DIR)
    subparsers = parser.add_subparsers(dest="command", required=True, metavar="COMMAND")
    for command in COMMANDS.values():
        subparser = subparsers.add_parser(command.name, help=command.help)
        # 指定がなければ、サブコマンドの側は何も設定しない（サブコマンドの前に指定した値を、上書きしない）。
        _add_config_dir(subparser, default=argparse.SUPPRESS)
        command.add_arguments(subparser)
    return parser


# --- 起動時の検証 ---


def _has_api_key() -> bool:
    return bool(os.environ.get(API_KEY_ENV, "").strip())


def _report_missing_api_key() -> None:
    """API Key の設定方法を示す（Design 第8.1節）。アプリ自身は `.env` を読み込まない。"""
    _print_error(
        f"{API_KEY_ENV} が設定されていません。環境変数 {API_KEY_ENV} に、OpenAI の API Key を設定してください。\n"
        f"  方法1: export {API_KEY_ENV}=... を実行してから、uv run triage-agent run ... を実行する\n"
        f"  方法2: .env を用意して（.env.example を .env にコピーし、値を設定する）、"
        f"uv run --env-file .env triage-agent run ... を実行する\n"
        "（このアプリは、.env を自動では読み込みません）"
    )


# --- 中断（ERR-007） ---


def _report_abort(error: StorageError, progress: Progress) -> None:
    """ログ・キューの書き込み・読み込みの失敗による中断を、標準エラー出力へ表示する。

    失敗したファイル名と原因の種類は、`StorageError` のメッセージが持つ（問い合わせの内容を含まない）。
    中断した問い合わせの `request_id`、レビューID、処理を完了した件数は、該当する場合だけ表示する。
    """
    lines = [
        "処理を中断しました。ログまたはキューのファイルの書き込み・読み込みに失敗したため、残りは処理していません。",
        f"失敗: {error}",
    ]
    request_id = progress.pipeline.current_request_id if progress.pipeline is not None else None
    if request_id is not None:
        lines.append(f"中断した問い合わせのリクエストID: {request_id}")
    if progress.review_id is not None:
        lines.append(f"レビューID: {progress.review_id}")
    if progress.processed is not None:
        lines.append(f"処理を完了した件数: {progress.processed}件")
    _print_error("\n".join(lines))


# --- 入口 ---


def main(argv: Sequence[str] | None = None, *, classifier: Classifier | None = None) -> int:
    """CLI の入口。終了コードを返す（`python -m triage_agent` とコンソールスクリプトの両方が、これを使う）。

    `classifier` は、テストが、LLM に接続しない分類器を注入するためのもの。
    引数の不正は、argparse が、使い方を表示して、終了コード 2 で終了する。
    """
    args = build_parser().parse_args(argv)
    command = COMMANDS[args.command]

    try:
        config = load_config(args.config_dir)
    except ConfigError as error:
        # 問い合わせの処理を開始しない。不備のある項目名を示す（ERR-006）。値は含まれない。
        _print_error(f"設定に不備があります:\n{error}")
        return EXIT_STARTUP_ERROR

    if command.needs_api_key and not _has_api_key():
        # LLM を使う処理の開始前に検出する（ERR-006、SEC-001）。
        _report_missing_api_key()
        return EXIT_STARTUP_ERROR

    progress = Progress()
    try:
        return command.execute(CommandContext(args=args, config=config, classifier=classifier, progress=progress))
    except StorageError as error:
        _report_abort(error, progress)
        return EXIT_FAILURE
