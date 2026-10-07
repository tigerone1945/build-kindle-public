"""設定・マスタの読み込みと検証（CMP-002）。

`config/settings.yaml`、`categories.yaml`、`keywords.yaml` を読み込み、Pydantic モデルで検証する。
値の初期値はコードへ持たせず、設定ファイルに置く（すべて必須）。
不備は、不備のある項目名を含む `ConfigError` として送出する（ERR-006）。
"""

from pathlib import Path
from typing import Annotated

import yaml
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictInt,
    StrictStr,
    ValidationError,
    field_validator,
)

from triage_agent.models import Category

SETTINGS_FILE = "settings.yaml"
CATEGORIES_FILE = "categories.yaml"
KEYWORDS_FILE = "keywords.yaml"

# YAML の `true` などを数値として受け取らない（設定の型の取り違えを検出する）。
StrictFloat = Annotated[float, Field(strict=True)]
NonBlankStr = Annotated[StrictStr, Field(min_length=1, pattern=r"\S")]


class ConfigError(Exception):
    """設定が不足している、または値が不正である。メッセージに不備のある項目名を含む。"""


class _ConfigModel(BaseModel):
    # 項目名の打ち間違いを、黙って無視せずに検出する。
    model_config = ConfigDict(extra="forbid")


# --- settings.yaml（DATA-008） ---


class LlmSettings(_ConfigModel):
    model: NonBlankStr
    timeout_seconds: Annotated[StrictFloat, Field(gt=0)]
    max_retries: Annotated[StrictInt, Field(ge=0)]
    retry_backoff_seconds: Annotated[StrictFloat, Field(ge=0)]


class DecisionSettings(_ConfigModel):
    confidence_threshold: Annotated[StrictFloat, Field(ge=0.0, le=1.0)]


class GuardrailSettings(_ConfigModel):
    min_body_length: Annotated[StrictInt, Field(ge=1)]


class PathSettings(_ConfigModel):
    output_dir: Path

    @field_validator("output_dir", mode="before")
    @classmethod
    def _require_non_blank_string(cls, value: object) -> object:
        # `Path("")` は "." になり、空の指定が現在のディレクトリへの出力に化けるため、先に弾く。
        if not isinstance(value, str) or not value.strip():
            raise ValueError("出力先は空でない文字列で指定してください")
        return value


class TracingSettings(_ConfigModel):
    enabled: StrictBool
    include_sensitive_data: StrictBool


class Settings(_ConfigModel):
    llm: LlmSettings
    decision: DecisionSettings
    guardrail: GuardrailSettings
    paths: PathSettings
    tracing: TracingSettings


# --- categories.yaml ---


class CategoryEntry(_ConfigModel):
    label: NonBlankStr
    department: NonBlankStr | None
    description: NonBlankStr


class CategoryMaster(_ConfigModel):
    categories: dict[Category, CategoryEntry]

    @field_validator("categories")
    @classmethod
    def _require_all_categories(
        cls, categories: dict[Category, CategoryEntry]
    ) -> dict[Category, CategoryEntry]:
        missing = [category.value for category in Category if category not in categories]
        if missing:
            raise ValueError(f"カテゴリが不足しています: {', '.join(missing)}")
        return categories


# --- keywords.yaml（BR-010） ---


class Keywords(_ConfigModel):
    # 高リスクキーワードは1つ以上が必須。空にすると BR-001 の検出が黙って働かなくなる（REQ-013 AC-4）。
    # 緊急度キーワードは空でもよい（緊急語がなければ、分類結果の優先度がそのまま使われる）。
    high_risk: Annotated[list[NonBlankStr], Field(min_length=1)]
    urgent: list[NonBlankStr]


class AppConfig(BaseModel):
    """読み込んだ設定の全体。"""

    settings: Settings
    categories: dict[Category, CategoryEntry]
    keywords: Keywords


# --- 読み込み ---


def _read_yaml(path: Path) -> object:
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        raise ConfigError(f"{path.name}: 設定ファイルが見つかりません（{path}）") from None
    except (OSError, UnicodeDecodeError) as error:
        raise ConfigError(f"{path.name}: 設定ファイルを読み取れません（{type(error).__name__}）") from error
    try:
        return yaml.safe_load(text)
    except yaml.YAMLError as error:
        mark = getattr(error, "problem_mark", None)
        where = f"（{mark.line + 1}行目）" if mark is not None else ""
        raise ConfigError(f"{path.name}: YAML として解釈できません{where}") from error


def _format_item(location: tuple[int | str, ...]) -> str:
    # 辞書のキーそのものの不備は、末尾に "[key]" が付く。項目名としては不要。
    return ".".join(str(part) for part in location if part != "[key]") or "(全体)"


def _load[ModelT: BaseModel](model: type[ModelT], config_dir: Path, file_name: str) -> ModelT:
    raw = _read_yaml(config_dir / file_name)
    if not isinstance(raw, dict):
        raise ConfigError(f"{file_name}: 項目名と値の対応（マッピング）で書いてください")
    try:
        return model.model_validate(raw)
    except ValidationError as error:
        problems = [
            f"{file_name}: {_format_item(problem['loc'])}: {problem['msg'].removeprefix('Value error, ')}"
            for problem in error.errors(include_input=False, include_url=False)
        ]
        raise ConfigError("\n".join(problems)) from None


def load_config(config_dir: Path | str) -> AppConfig:
    """`config_dir` の3つの設定ファイルを読み込んで検証する。不備は `ConfigError`（全ファイル分をまとめる）。"""
    directory = Path(config_dir)
    problems: list[str] = []
    settings = master = keywords = None
    try:
        settings = _load(Settings, directory, SETTINGS_FILE)
    except ConfigError as error:
        problems.append(str(error))
    try:
        master = _load(CategoryMaster, directory, CATEGORIES_FILE)
    except ConfigError as error:
        problems.append(str(error))
    try:
        keywords = _load(Keywords, directory, KEYWORDS_FILE)
    except ConfigError as error:
        problems.append(str(error))
    if settings is None or master is None or keywords is None:
        raise ConfigError("\n".join(problems))
    return AppConfig(settings=settings, categories=master.categories, keywords=keywords)
