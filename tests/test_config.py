"""TASK-003：設定とマスタの読み込み・検証（REQ-013, ERR-006, BR-010）。"""

import copy
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
import yaml

from triage_agent.config import (
    CATEGORIES_FILE,
    KEYWORDS_FILE,
    SETTINGS_FILE,
    AppConfig,
    ConfigError,
    load_config,
)
from triage_agent.models import Category

REPO_CONFIG_DIR = Path(__file__).resolve().parents[1] / "config"

Files = dict[str, Any]
Mutation = Callable[[Files], None]


def read_repo_files() -> Files:
    return {
        name: yaml.safe_load((REPO_CONFIG_DIR / name).read_text(encoding="utf-8"))
        for name in (SETTINGS_FILE, CATEGORIES_FILE, KEYWORDS_FILE)
    }


def write_files(directory: Path, files: Files) -> Path:
    for name, content in files.items():
        (directory / name).write_text(
            yaml.safe_dump(content, allow_unicode=True), encoding="utf-8"
        )
    return directory


def load_with(tmp_path: Path, mutation: Mutation) -> AppConfig:
    files = copy.deepcopy(read_repo_files())
    mutation(files)
    return load_config(write_files(tmp_path, files))


def set_value(file: str, path: str, value: object) -> Mutation:
    def apply(files: Files) -> None:
        *parents, leaf = path.split(".")
        node = files[file]
        for key in parents:
            node = node[key]
        node[leaf] = value

    return apply


def delete_value(file: str, path: str) -> Mutation:
    def apply(files: Files) -> None:
        *parents, leaf = path.split(".")
        node = files[file]
        for key in parents:
            node = node[key]
        del node[leaf]

    return apply


# --- 正常系 ---


def test_repository_config_is_valid_and_matches_the_design() -> None:
    config = load_config(REPO_CONFIG_DIR)

    settings = config.settings
    assert settings.llm.model == "gpt-5.6-luna"
    assert settings.llm.timeout_seconds == 10
    assert settings.llm.max_retries == 2
    assert settings.llm.retry_backoff_seconds == 1.0
    assert settings.decision.confidence_threshold == 0.7
    assert settings.guardrail.min_body_length == 5
    assert settings.paths.output_dir == Path("output")
    assert settings.tracing.enabled is True
    assert settings.tracing.include_sensitive_data is False

    assert set(config.categories) == set(Category)
    assert config.categories[Category.BILLING].department == "請求"
    assert config.categories[Category.UNCLASSIFIED].department is None
    assert config.keywords.high_risk == ["解約", "返金", "訴訟", "法的措置", "個人情報"]
    assert config.keywords.urgent == ["至急", "使えない", "障害"]


def test_config_dir_can_be_given_as_a_string() -> None:
    assert load_config(str(REPO_CONFIG_DIR)).settings.llm.max_retries == 2


@pytest.mark.parametrize(
    ("mutation", "check"),
    [
        (
            set_value(SETTINGS_FILE, "decision.confidence_threshold", 0.55),
            lambda c: c.settings.decision.confidence_threshold == 0.55,
        ),
        (
            set_value(SETTINGS_FILE, "llm.model", "another-model"),
            lambda c: c.settings.llm.model == "another-model",
        ),
        (
            set_value(SETTINGS_FILE, "llm.max_retries", 0),
            lambda c: c.settings.llm.max_retries == 0,
        ),
        (
            set_value(SETTINGS_FILE, "guardrail.min_body_length", 10),
            lambda c: c.settings.guardrail.min_body_length == 10,
        ),
        (
            set_value(SETTINGS_FILE, "paths.output_dir", "var/out"),
            lambda c: c.settings.paths.output_dir == Path("var/out"),
        ),
        (
            set_value(SETTINGS_FILE, "tracing.enabled", False),
            lambda c: c.settings.tracing.enabled is False,
        ),
        (
            set_value(KEYWORDS_FILE, "high_risk", ["炎上"]),
            lambda c: c.keywords.high_risk == ["炎上"],
        ),
        (
            set_value(KEYWORDS_FILE, "urgent", ["今すぐ", "至急"]),
            lambda c: c.keywords.urgent == ["今すぐ", "至急"],
        ),
        (
            set_value(CATEGORIES_FILE, "categories.billing.department", "経理"),
            lambda c: c.categories[Category.BILLING].department == "経理",
        ),
    ],
)
def test_changed_config_values_are_reflected_without_code_changes(
    tmp_path: Path, mutation: Mutation, check: Callable[[AppConfig], bool]
) -> None:
    assert check(load_with(tmp_path, mutation))


def test_integer_values_are_accepted_for_float_settings(tmp_path: Path) -> None:
    config = load_with(tmp_path, set_value(SETTINGS_FILE, "decision.confidence_threshold", 1))
    assert config.settings.decision.confidence_threshold == 1.0


def test_threshold_boundaries_are_valid(tmp_path: Path) -> None:
    for boundary in (0.0, 1.0):
        config = load_with(tmp_path, set_value(SETTINGS_FILE, "decision.confidence_threshold", boundary))
        assert config.settings.decision.confidence_threshold == boundary


# --- 異常系：ConfigError にはファイル名と項目名が含まれる（ERR-006） ---


@pytest.mark.parametrize(
    ("mutation", "expected"),
    [
        # 閾値
        (set_value(SETTINGS_FILE, "decision.confidence_threshold", 1.5), "decision.confidence_threshold"),
        (set_value(SETTINGS_FILE, "decision.confidence_threshold", -0.1), "decision.confidence_threshold"),
        (set_value(SETTINGS_FILE, "decision.confidence_threshold", "high"), "decision.confidence_threshold"),
        (set_value(SETTINGS_FILE, "decision.confidence_threshold", True), "decision.confidence_threshold"),
        (delete_value(SETTINGS_FILE, "decision.confidence_threshold"), "decision.confidence_threshold"),
        # リトライ回数・間隔・タイムアウト
        (set_value(SETTINGS_FILE, "llm.max_retries", -1), "llm.max_retries"),
        (set_value(SETTINGS_FILE, "llm.max_retries", 1.5), "llm.max_retries"),
        (set_value(SETTINGS_FILE, "llm.max_retries", "2"), "llm.max_retries"),
        (set_value(SETTINGS_FILE, "llm.timeout_seconds", 0), "llm.timeout_seconds"),
        (set_value(SETTINGS_FILE, "llm.timeout_seconds", -3), "llm.timeout_seconds"),
        (set_value(SETTINGS_FILE, "llm.retry_backoff_seconds", -1.0), "llm.retry_backoff_seconds"),
        # 最小文字数
        (set_value(SETTINGS_FILE, "guardrail.min_body_length", 0), "guardrail.min_body_length"),
        (set_value(SETTINGS_FILE, "guardrail.min_body_length", 2.5), "guardrail.min_body_length"),
        # モデル・出力先・トレース
        (set_value(SETTINGS_FILE, "llm.model", ""), "llm.model"),
        (set_value(SETTINGS_FILE, "llm.model", "   "), "llm.model"),
        (set_value(SETTINGS_FILE, "llm.model", 123), "llm.model"),
        (delete_value(SETTINGS_FILE, "llm.model"), "llm.model"),
        (set_value(SETTINGS_FILE, "paths.output_dir", ""), "paths.output_dir"),
        (set_value(SETTINGS_FILE, "paths.output_dir", None), "paths.output_dir"),
        (set_value(SETTINGS_FILE, "tracing.enabled", "yes"), "tracing.enabled"),
        (delete_value(SETTINGS_FILE, "tracing.include_sensitive_data"), "tracing.include_sensitive_data"),
        # セクションの欠落・未知の項目（打ち間違い）
        (delete_value(SETTINGS_FILE, "llm"), "llm"),
        (set_value(SETTINGS_FILE, "decision.confidence_treshold", 0.7), "decision.confidence_treshold"),
        # キーワード
        (set_value(KEYWORDS_FILE, "high_risk", ["解約", ""]), "high_risk.1"),
        (set_value(KEYWORDS_FILE, "high_risk", ["   "]), "high_risk.0"),
        # 高リスクキーワードが1つもない（BR-001 の検出を設定だけで無効にできない。REQ-013 AC-4）
        (set_value(KEYWORDS_FILE, "high_risk", []), "high_risk"),
        (set_value(KEYWORDS_FILE, "urgent", ["至急", 123]), "urgent.1"),
        (set_value(KEYWORDS_FILE, "urgent", "至急"), "urgent"),
        (set_value(KEYWORDS_FILE, "urgent", None), "urgent"),
        (delete_value(KEYWORDS_FILE, "urgent"), "urgent"),
        # マスタ
        (delete_value(CATEGORIES_FILE, "categories.complaint"), "complaint"),
        (set_value(CATEGORIES_FILE, "categories.spam", {"label": "迷惑", "department": None, "description": "x"}), "spam"),
        (set_value(CATEGORIES_FILE, "categories.sales.label", ""), "categories.sales.label"),
        (set_value(CATEGORIES_FILE, "categories.sales.department", ""), "categories.sales.department"),
        (delete_value(CATEGORIES_FILE, "categories.sales.department"), "categories.sales.department"),
        (delete_value(CATEGORIES_FILE, "categories.support.description"), "categories.support.description"),
        (set_value(CATEGORIES_FILE, "categories", []), "categories"),
    ],
)
def test_invalid_config_raises_config_error_naming_the_item(
    tmp_path: Path, mutation: Mutation, expected: str
) -> None:
    with pytest.raises(ConfigError) as exc_info:
        load_with(tmp_path, mutation)
    assert expected in str(exc_info.value)


@pytest.mark.parametrize(
    ("mutation", "file_name"),
    [
        (set_value(SETTINGS_FILE, "llm.max_retries", -1), SETTINGS_FILE),
        (set_value(KEYWORDS_FILE, "urgent", None), KEYWORDS_FILE),
        (delete_value(CATEGORIES_FILE, "categories.billing"), CATEGORIES_FILE),
    ],
)
def test_config_error_names_the_file(tmp_path: Path, mutation: Mutation, file_name: str) -> None:
    with pytest.raises(ConfigError, match=file_name):
        load_with(tmp_path, mutation)


def test_empty_high_risk_keywords_error_names_the_file_and_the_item(tmp_path: Path) -> None:
    with pytest.raises(ConfigError) as exc_info:
        load_with(tmp_path, set_value(KEYWORDS_FILE, "high_risk", []))
    message = str(exc_info.value)
    assert message.startswith(f"{KEYWORDS_FILE}: high_risk:")
    # 緊急度キーワードは、空でなければ問題ない（不備は高リスクキーワードだけ）。
    assert "urgent" not in message


def test_empty_urgent_keywords_are_allowed(tmp_path: Path) -> None:
    # 緊急語がなければ、分類結果の優先度がそのまま使われる（REQ-004 AC-2）。安全側に倒れるため空を許す。
    config = load_with(tmp_path, set_value(KEYWORDS_FILE, "urgent", []))
    assert config.keywords.urgent == []
    assert config.keywords.high_risk == read_repo_files()[KEYWORDS_FILE]["high_risk"]


def test_empty_high_risk_keywords_are_reported_together_with_other_problems(tmp_path: Path) -> None:
    def break_two_files(files: Files) -> None:
        files[KEYWORDS_FILE]["high_risk"] = []
        files[SETTINGS_FILE]["llm"]["max_retries"] = -1

    with pytest.raises(ConfigError) as exc_info:
        load_with(tmp_path, break_two_files)
    message = str(exc_info.value)
    assert f"{KEYWORDS_FILE}: high_risk" in message
    assert "llm.max_retries" in message


def test_missing_category_is_named_in_the_message(tmp_path: Path) -> None:
    with pytest.raises(ConfigError) as exc_info:
        load_with(tmp_path, delete_value(CATEGORIES_FILE, "categories.complaint"))
    assert "complaint" in str(exc_info.value)


def test_all_problems_are_reported_together(tmp_path: Path) -> None:
    def break_everything(files: Files) -> None:
        files[SETTINGS_FILE]["llm"]["max_retries"] = -1
        files[KEYWORDS_FILE]["urgent"] = [""]
        del files[CATEGORIES_FILE]["categories"]["sales"]

    with pytest.raises(ConfigError) as exc_info:
        load_with(tmp_path, break_everything)
    message = str(exc_info.value)
    assert "llm.max_retries" in message
    assert "urgent.0" in message
    assert "sales" in message


def test_multiple_problems_in_one_file_are_all_reported(tmp_path: Path) -> None:
    def break_two(files: Files) -> None:
        files[SETTINGS_FILE]["llm"]["timeout_seconds"] = 0
        files[SETTINGS_FILE]["guardrail"]["min_body_length"] = 0

    with pytest.raises(ConfigError) as exc_info:
        load_with(tmp_path, break_two)
    message = str(exc_info.value)
    assert "llm.timeout_seconds" in message
    assert "guardrail.min_body_length" in message


# --- 異常系：ファイルそのものの不備 ---


@pytest.mark.parametrize("file_name", [SETTINGS_FILE, CATEGORIES_FILE, KEYWORDS_FILE])
def test_missing_file_raises_config_error(tmp_path: Path, file_name: str) -> None:
    write_files(tmp_path, read_repo_files())
    (tmp_path / file_name).unlink()
    with pytest.raises(ConfigError, match=file_name):
        load_config(tmp_path)


def test_missing_directory_raises_config_error(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match=SETTINGS_FILE):
        load_config(tmp_path / "does-not-exist")


def test_broken_yaml_raises_config_error_with_the_line(tmp_path: Path) -> None:
    write_files(tmp_path, read_repo_files())
    (tmp_path / SETTINGS_FILE).write_text("llm:\n  model: [unclosed\n", encoding="utf-8")
    with pytest.raises(ConfigError, match=rf"{SETTINGS_FILE}.*行目"):
        load_config(tmp_path)


@pytest.mark.parametrize("content", ["", "- a\n- b\n", "just a string\n"])
def test_non_mapping_top_level_raises_config_error(tmp_path: Path, content: str) -> None:
    write_files(tmp_path, read_repo_files())
    (tmp_path / KEYWORDS_FILE).write_text(content, encoding="utf-8")
    with pytest.raises(ConfigError, match=KEYWORDS_FILE):
        load_config(tmp_path)


def test_undecodable_file_raises_config_error(tmp_path: Path) -> None:
    write_files(tmp_path, read_repo_files())
    (tmp_path / SETTINGS_FILE).write_bytes(b"\xff\xfe\x00invalid")
    with pytest.raises(ConfigError, match=SETTINGS_FILE):
        load_config(tmp_path)


def test_error_message_does_not_echo_the_offending_value(tmp_path: Path) -> None:
    """設定に誤って貼り付けた秘密の値などを、エラーメッセージへ出さない。"""
    secret = "sk-test-SECRET-VALUE"
    with pytest.raises(ConfigError) as exc_info:
        load_with(tmp_path, set_value(SETTINGS_FILE, "llm.max_retries", secret))
    assert secret not in str(exc_info.value)
