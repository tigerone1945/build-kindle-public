"""TASK-010：分類器のリトライ・タイムアウト・トレース設定
（ERR-001、ERR-002、NFR-001、NFR-002、NFR-005、SEC-003 / CMP-006、第10章、ADR-005・014、第12章）。

LLM・ネットワークに接続しない。次の2通りで確認する。
- リトライの流れ：`Runner.run_sync` を、決められた順に、結果または例外を返す差し替えにする。
- タイムアウトと、モデルの拒否：実際の Runner を使い、OpenAI クライアントの `responses.create` を差し替える
  （`create` が期限より長く待つ、または拒否の応答を返す）。バックオフの待機は、実際には待たない関数を注入する。
"""

import asyncio
import time
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace

import pytest
from agents import RunConfig, Runner
from agents.exceptions import (
    MaxTurnsExceeded,
    ModelBehaviorError,
    ModelRefusalError,
    ModelTimeoutError,
    UserError,
)
from fakes import make_result
from llm_stubs import MODEL, create_mock, refusal_response, structured_output_response, stub_client, text_response
from openai import APIConnectionError, APITimeoutError, AsyncOpenAI, OpenAIError, RateLimitError

from triage_agent.classifier import WORKFLOW_NAME, AgentsSdkClassifier
from triage_agent.config import CategoryEntry, LlmSettings, TracingSettings, load_config
from triage_agent.models import Category, Channel, ClassifyStatus, Inquiry

pytestmark = pytest.mark.usefixtures("tracing_disabled")

LLM = LlmSettings(model=MODEL, timeout_seconds=10.0, max_retries=2, retry_backoff_seconds=1.0)
TRACING = TracingSettings(enabled=True, include_sensitive_data=False)
CATEGORIES = {
    category: CategoryEntry(label=f"ラベル-{category.value}", department=None, description=f"説明-{category.value}")
    for category in Category
}
INQUIRY = Inquiry(inquiry_id="INQ-1", channel=Channel.EMAIL, subject="請求書について", body="請求書の内容について確認したい")
SECRET = "SECRET-BODY taro.yamada@example.com"
CONFIG_DIR = Path(__file__).parent.parent / "config"


def api_status_error(message: str = SECRET) -> RateLimitError:
    """HTTP 429 の API エラー。文言に、問い合わせの内容に見立てた文字列を含める。"""
    response = SimpleNamespace(status_code=429, headers={}, request=None)
    return RateLimitError(message, response=response, body=None)  # type: ignore[arg-type]


def connection_error() -> APIConnectionError:
    return APIConnectionError(message=SECRET, request=None)  # type: ignore[arg-type]


def make_classifier(
    client: AsyncOpenAI | None = None,
    *,
    llm: LlmSettings = LLM,
    tracing: TracingSettings = TRACING,
) -> tuple[AgentsSdkClassifier, list[float]]:
    """分類器と、バックオフの待機時間の記録を返す（実際には待たない）。"""
    sleeps: list[float] = []
    classifier = AgentsSdkClassifier(
        llm,
        CATEGORIES,
        5,
        tracing=tracing,
        client=client if client is not None else stub_client(),
        sleep=sleeps.append,
    )
    return classifier, sleeps


class ScriptedRunner:
    """`Runner.run_sync` の差し替え。呼ばれるたびに、決められた順で、結果を返す、または例外を送出する。

    決められた数より多く呼ばれたら、テストの失敗にする（呼び出し回数の上限の確認）。
    """

    def __init__(self, *steps: object) -> None:
        self._steps = list(steps)
        self.calls: list[tuple[tuple[object, ...], dict[str, object]]] = []

    def __call__(self, *args: object, **kwargs: object) -> SimpleNamespace:
        self.calls.append((args, kwargs))
        if not self._steps:
            raise AssertionError(f"決められた回数（{len(self.calls) - 1}回）より多く、LLM が呼ばれた")
        step = self._steps.pop(0)
        if isinstance(step, BaseException):
            raise step
        return SimpleNamespace(final_output=step)

    @property
    def run_configs(self) -> list[RunConfig]:
        configs = [kwargs["run_config"] for _, kwargs in self.calls]
        assert all(isinstance(config, RunConfig) for config in configs)
        return configs  # type: ignore[return-value]


def script(monkeypatch: pytest.MonkeyPatch, *steps: object) -> ScriptedRunner:
    runner = ScriptedRunner(*steps)
    monkeypatch.setattr(Runner, "run_sync", runner)
    return runner


GOOD = make_result()
INVALID = make_result(confidence=1.5)


class TestRetryLoop:
    """試行は、最大 `max_retries + 1` 回。LLM 失敗と形式不正は、次の試行へ進む（ERR-001、ERR-002、NFR-002）。"""

    def test_success_on_the_first_attempt_calls_once(self, monkeypatch: pytest.MonkeyPatch) -> None:
        runner = script(monkeypatch, GOOD)
        classifier, sleeps = make_classifier()
        outcome = classifier.classify(INQUIRY, "req-1")
        assert outcome.status is ClassifyStatus.SUCCESS
        assert outcome.classification == GOOD
        assert outcome.attempts == 1
        assert len(runner.calls) == 1
        assert sleeps == []

    def test_two_failures_then_success_calls_three_times(self, monkeypatch: pytest.MonkeyPatch) -> None:
        runner = script(monkeypatch, connection_error(), api_status_error(), GOOD)
        classifier, sleeps = make_classifier()
        outcome = classifier.classify(INQUIRY, "req-1")
        assert outcome.status is ClassifyStatus.SUCCESS
        assert outcome.classification == GOOD
        assert outcome.attempts == 3
        assert outcome.error_detail is None
        assert len(runner.calls) == 3
        assert sleeps == [1.0, 2.0]

    def test_three_api_errors_are_an_llm_failure(self, monkeypatch: pytest.MonkeyPatch) -> None:
        runner = script(monkeypatch, connection_error(), api_status_error(), connection_error())
        classifier, sleeps = make_classifier()
        outcome = classifier.classify(INQUIRY, "req-1")
        assert outcome.status is ClassifyStatus.LLM_FAILURE
        assert outcome.classification is None
        assert outcome.attempts == 3
        assert len(runner.calls) == 3
        # 最後の失敗のあとは、待たない（次の試行がないため）。
        assert sleeps == [1.0, 2.0]

    def test_three_invalid_outputs_are_an_invalid_output(self, monkeypatch: pytest.MonkeyPatch) -> None:
        runner = script(monkeypatch, INVALID, INVALID, INVALID)
        classifier, sleeps = make_classifier()
        outcome = classifier.classify(INQUIRY, "req-1")
        assert outcome.status is ClassifyStatus.INVALID_OUTPUT
        assert outcome.classification is None
        assert outcome.attempts == 3
        assert outcome.error_detail == "confidence: 0.0〜1.0の範囲外です"
        assert len(runner.calls) == 3
        assert sleeps == [1.0, 2.0]

    def test_a_model_behavior_error_is_an_invalid_output(self, monkeypatch: pytest.MonkeyPatch) -> None:
        runner = script(monkeypatch, *[ModelBehaviorError("Invalid JSON") for _ in range(3)])
        outcome = make_classifier()[0].classify(INQUIRY, "req-1")
        assert outcome.status is ClassifyStatus.INVALID_OUTPUT
        assert outcome.attempts == 3
        assert len(runner.calls) == 3

    def test_an_invalid_output_then_success_is_a_success(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # 形式違反は、再度分類を得る（ERR-002）。
        script(monkeypatch, INVALID, GOOD)
        outcome = make_classifier()[0].classify(INQUIRY, "req-1")
        assert outcome.status is ClassifyStatus.SUCCESS
        assert outcome.attempts == 2

    @pytest.mark.parametrize(
        ("steps", "expected"),
        [
            ((connection_error(), INVALID, INVALID), ClassifyStatus.INVALID_OUTPUT),
            ((INVALID, INVALID, connection_error()), ClassifyStatus.LLM_FAILURE),
            ((INVALID, connection_error(), ModelBehaviorError("x")), ClassifyStatus.INVALID_OUTPUT),
        ],
    )
    def test_the_last_failure_decides_the_kind(
        self, monkeypatch: pytest.MonkeyPatch, steps: tuple[object, ...], expected: ClassifyStatus
    ) -> None:
        script(monkeypatch, *steps)
        outcome = make_classifier()[0].classify(INQUIRY, "req-1")
        assert outcome.status is expected
        assert outcome.attempts == 3

    def test_max_retries_zero_calls_once(self, monkeypatch: pytest.MonkeyPatch) -> None:
        llm = LlmSettings(model=MODEL, timeout_seconds=10.0, max_retries=0, retry_backoff_seconds=1.0)
        runner = script(monkeypatch, connection_error())
        classifier, sleeps = make_classifier(llm=llm)
        outcome = classifier.classify(INQUIRY, "req-1")
        assert outcome.status is ClassifyStatus.LLM_FAILURE
        assert outcome.attempts == 1
        assert len(runner.calls) == 1
        assert sleeps == []

    @pytest.mark.parametrize("max_retries", [0, 1, 2, 3, 5])
    def test_the_number_of_calls_never_exceeds_the_limit(self, monkeypatch: pytest.MonkeyPatch, max_retries: int) -> None:
        # NFR-002：呼び出しは、リトライを含めて、最大（リトライ回数 + 1）回。1回多く呼ばれると、差し替えが失敗にする。
        llm = LlmSettings(model=MODEL, timeout_seconds=10.0, max_retries=max_retries, retry_backoff_seconds=0.0)
        runner = script(monkeypatch, *[connection_error() for _ in range(max_retries + 1)])
        outcome = make_classifier(llm=llm)[0].classify(INQUIRY, "req-1")
        assert outcome.attempts == max_retries + 1
        assert len(runner.calls) == max_retries + 1

    def test_the_backoff_doubles_from_the_configured_base(self, monkeypatch: pytest.MonkeyPatch) -> None:
        llm = LlmSettings(model=MODEL, timeout_seconds=10.0, max_retries=3, retry_backoff_seconds=0.5)
        script(monkeypatch, *[connection_error() for _ in range(4)])
        classifier, sleeps = make_classifier(llm=llm)
        classifier.classify(INQUIRY, "req-1")
        assert sleeps == [0.5, 1.0, 2.0]

    def test_without_an_injected_sleep_the_wait_really_happens(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # `sleep` を注入しなければ、実際に待つ（`time.sleep`）。短い待ち時間で、待ったことを確認する。
        llm = LlmSettings(model=MODEL, timeout_seconds=10.0, max_retries=1, retry_backoff_seconds=0.2)
        script(monkeypatch, connection_error(), GOOD)
        classifier = AgentsSdkClassifier(llm, CATEGORIES, 5, tracing=TRACING, client=stub_client())
        started = time.monotonic()
        outcome = classifier.classify(INQUIRY, "req-1")
        assert outcome.status is ClassifyStatus.SUCCESS
        assert time.monotonic() - started >= 0.2

    def test_the_input_guardrail_is_not_retried(self) -> None:
        # 実際の Runner。本文が短く、入力ガードレールに該当する：LLM は呼ばれず、再試行も待機もしない。
        client = stub_client()
        classifier, sleeps = make_classifier(client)
        outcome = classifier.classify(Inquiry(inquiry_id="INQ-2", channel=Channel.EMAIL, body="あいう"), "req-1")
        assert outcome.status is ClassifyStatus.GUARDRAIL_TRIPPED
        assert outcome.attempts == 0
        assert create_mock(client).await_count == 0
        assert sleeps == []

    def test_the_client_has_no_automatic_retries(self) -> None:
        # クライアント層とアプリ層の両方で再試行すると、呼び出しが最大9回になる（ADR-005）。
        assert make_classifier()[0].client.max_retries == 0


class TestUnexpectedExceptions:
    """LLM 失敗・形式不正以外の例外は、リトライせず、握りつぶさずに送出する（Pipeline が扱う。ERR-005）。"""

    @pytest.mark.parametrize(
        "error",
        [RuntimeError("boom"), UserError("bad usage"), MaxTurnsExceeded("too many turns"), KeyError("k")],
        ids=lambda error: type(error).__name__,
    )
    def test_are_raised_without_retry(self, monkeypatch: pytest.MonkeyPatch, error: Exception) -> None:
        runner = script(monkeypatch, error, GOOD)
        classifier, sleeps = make_classifier()
        with pytest.raises(type(error)):
            classifier.classify(INQUIRY, "req-1")
        assert len(runner.calls) == 1
        assert sleeps == []

    def test_are_raised_even_after_a_retry(self, monkeypatch: pytest.MonkeyPatch) -> None:
        runner = script(monkeypatch, connection_error(), RuntimeError("boom"), GOOD)
        classifier, sleeps = make_classifier()
        with pytest.raises(RuntimeError):
            classifier.classify(INQUIRY, "req-1")
        assert len(runner.calls) == 2
        assert sleeps == [1.0]


class TestErrorDetail:
    """`error_detail` は、例外の種類と要約だけで、問い合わせの内容を含まない（ERR-002、SEC-002、ADR-014）。"""

    @pytest.mark.parametrize(
        ("error", "expected"),
        [
            (api_status_error(), "RateLimitError（HTTP 429）"),
            (connection_error(), "APIConnectionError"),
            (APITimeoutError(request=None), "APITimeoutError"),  # type: ignore[arg-type]
            (OpenAIError(SECRET), "OpenAIError"),
            (ModelTimeoutError(10.0), "ModelTimeoutError（期限 10秒）"),
            (ModelTimeoutError(0.25), "ModelTimeoutError（期限 0.25秒）"),
        ],
        ids=lambda value: value if isinstance(value, str) else type(value).__name__,
    )
    def test_an_llm_failure_shows_the_kind_and_a_summary(
        self, monkeypatch: pytest.MonkeyPatch, error: Exception, expected: str
    ) -> None:
        script(monkeypatch, error, error, error)
        outcome = make_classifier()[0].classify(INQUIRY, "req-1")
        assert outcome.status is ClassifyStatus.LLM_FAILURE
        assert outcome.error_detail == expected
        assert "SECRET" not in outcome.error_detail
        assert "@" not in outcome.error_detail

    @pytest.mark.parametrize(
        "error", [ModelBehaviorError(SECRET), ModelRefusalError(SECRET)], ids=lambda error: type(error).__name__
    )
    def test_an_invalid_output_from_the_sdk_shows_only_the_kind(
        self, monkeypatch: pytest.MonkeyPatch, error: Exception
    ) -> None:
        # モデルの拒否の文面は、問い合わせの内容を引用しうる（ADR-014）。例外の文言は、使わない。
        script(monkeypatch, error, error, error)
        outcome = make_classifier()[0].classify(INQUIRY, "req-1")
        assert outcome.status is ClassifyStatus.INVALID_OUTPUT
        assert outcome.error_detail == type(error).__name__


class TestRunConfig:
    """実行トレースの設定は、設定値で決まる（SEC-003、NFR-005、Design 第12章）。"""

    @pytest.mark.parametrize(
        ("enabled", "include_sensitive_data"), [(True, False), (True, True), (False, False), (False, True)]
    )
    def test_the_run_config_follows_the_tracing_settings(
        self, monkeypatch: pytest.MonkeyPatch, enabled: bool, include_sensitive_data: bool
    ) -> None:
        runner = script(monkeypatch, GOOD)
        tracing = TracingSettings(enabled=enabled, include_sensitive_data=include_sensitive_data)
        make_classifier(tracing=tracing)[0].classify(INQUIRY, "req-9")
        (config,) = runner.run_configs
        assert config.workflow_name == WORKFLOW_NAME == "triage-classification"
        assert config.tracing_disabled is (not enabled)
        assert config.trace_include_sensitive_data is include_sensitive_data

    def test_the_request_id_and_the_inquiry_id_are_in_the_trace_metadata(self, monkeypatch: pytest.MonkeyPatch) -> None:
        runner = script(monkeypatch, GOOD)
        make_classifier()[0].classify(INQUIRY, "req-9")
        assert runner.run_configs[0].trace_metadata == {"request_id": "req-9", "inquiry_id": "INQ-1"}

    def test_the_metadata_has_no_inquiry_id_when_the_inquiry_has_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        runner = script(monkeypatch, GOOD)
        inquiry = Inquiry(channel=Channel.EMAIL, body="請求書の内容について確認したい")
        make_classifier()[0].classify(inquiry, "req-9")
        assert runner.run_configs[0].trace_metadata == {"request_id": "req-9"}

    def test_the_trace_metadata_has_no_inquiry_content(self, monkeypatch: pytest.MonkeyPatch) -> None:
        runner = script(monkeypatch, GOOD)
        inquiry = Inquiry(
            inquiry_id="INQ-1", channel=Channel.EMAIL, subject=SECRET, body=SECRET, sender="taro.yamada@example.com"
        )
        make_classifier()[0].classify(inquiry, "req-9")
        assert "SECRET" not in repr(runner.run_configs[0].trace_metadata)
        assert "@" not in repr(runner.run_configs[0].trace_metadata)

    def test_every_attempt_uses_the_same_run_config_values(self, monkeypatch: pytest.MonkeyPatch) -> None:
        runner = script(monkeypatch, connection_error(), connection_error(), GOOD)
        tracing = TracingSettings(enabled=True, include_sensitive_data=True)
        make_classifier(tracing=tracing)[0].classify(INQUIRY, "req-9")
        assert len(runner.run_configs) == 3
        for config in runner.run_configs:
            assert config.trace_include_sensitive_data is True
            assert config.trace_metadata == {"request_id": "req-9", "inquiry_id": "INQ-1"}

    def test_the_shipped_settings_do_not_send_sensitive_data_to_the_trace(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # 既定は、機密データを含めない（SEC-003、ADR-007）。設定ファイルの値が、そのまま RunConfig に届く。
        tracing = load_config(CONFIG_DIR).settings.tracing
        runner = script(monkeypatch, GOOD)
        make_classifier(tracing=tracing)[0].classify(INQUIRY, "req-9")
        assert runner.run_configs[0].trace_include_sensitive_data is False


class TestWithTheRealRunner:
    """実際の Runner で、タイムアウトと、モデルの拒否を起こす（LLM は、`create` の差し替え。ネットワークなし）。"""

    SHORT_TIMEOUT = LlmSettings(model=MODEL, timeout_seconds=0.2, max_retries=2, retry_backoff_seconds=1.0)

    @staticmethod
    def slow_create(seconds: float = 30.0) -> Callable[..., object]:
        """`ModelSettings.timeout` より長く待つ `create`。SDK が期限で打ち切る（キャンセルされる）。"""

        async def create(*args: object, **kwargs: object) -> object:
            await asyncio.sleep(seconds)
            raise AssertionError("期限で打ち切られるはず")

        return create

    def test_three_timeouts_are_an_llm_failure(self) -> None:
        client = stub_client()
        create_mock(client).side_effect = self.slow_create()
        classifier, sleeps = make_classifier(client, llm=self.SHORT_TIMEOUT)
        outcome = classifier.classify(INQUIRY, "req-1")
        assert outcome.status is ClassifyStatus.LLM_FAILURE
        assert outcome.attempts == 3
        # `create` の呼び出し回数が、試行の回数と一致し、上限（max_retries + 1）を超えない。
        assert create_mock(client).await_count == 3
        assert sleeps == [1.0, 2.0]
        assert outcome.error_detail == "ModelTimeoutError（期限 0.2秒）"

    def test_a_timeout_then_success_is_a_success_on_the_second_attempt(self) -> None:
        client = stub_client()
        mock = create_mock(client)
        slow = self.slow_create()
        good = structured_output_response(GOOD, MODEL)

        async def first_slow_then_good(*args: object, **kwargs: object) -> object:
            if mock.await_count == 1:
                return await slow()
            return good

        mock.side_effect = first_slow_then_good
        classifier, sleeps = make_classifier(client, llm=self.SHORT_TIMEOUT)
        outcome = classifier.classify(INQUIRY, "req-1")
        assert outcome.status is ClassifyStatus.SUCCESS
        assert outcome.classification == GOOD
        assert outcome.attempts == 2
        assert mock.await_count == 2
        assert sleeps == [1.0]

    def test_api_errors_are_retried_with_the_real_runner(self) -> None:
        client = stub_client()
        create_mock(client).side_effect = api_status_error()
        classifier, sleeps = make_classifier(client)
        outcome = classifier.classify(INQUIRY, "req-1")
        assert outcome.status is ClassifyStatus.LLM_FAILURE
        assert outcome.attempts == 3
        assert create_mock(client).await_count == 3
        assert outcome.error_detail == "RateLimitError（HTTP 429）"
        assert sleeps == [1.0, 2.0]

    def test_three_refusals_are_an_invalid_output(self) -> None:
        client = stub_client()
        mock = create_mock(client)
        mock.side_effect = None
        mock.return_value = refusal_response(SECRET, MODEL)
        classifier, sleeps = make_classifier(client)
        outcome = classifier.classify(INQUIRY, "req-1")
        assert outcome.status is ClassifyStatus.INVALID_OUTPUT
        assert outcome.attempts == 3
        assert mock.await_count == 3
        assert sleeps == [1.0, 2.0]
        # 拒否の文面（問い合わせの内容を引用しうる）を、失敗の理由に含めない（ERR-002、ADR-014）。
        assert outcome.error_detail == "ModelRefusalError"
        assert SECRET not in repr(outcome)

    def test_a_refusal_then_success_is_a_success_on_the_second_attempt(self) -> None:
        client = stub_client()
        mock = create_mock(client)
        mock.side_effect = [refusal_response(SECRET, MODEL), structured_output_response(GOOD, MODEL)]
        classifier, sleeps = make_classifier(client)
        outcome = classifier.classify(INQUIRY, "req-1")
        assert outcome.status is ClassifyStatus.SUCCESS
        assert outcome.classification == GOOD
        assert outcome.attempts == 2
        assert mock.await_count == 2
        assert sleeps == [1.0]

    def test_an_invalid_json_output_is_an_invalid_output_without_the_content(self) -> None:
        # モデルが、規定の形式でない出力を返した（`ModelBehaviorError`）。
        client = stub_client()
        mock = create_mock(client)
        mock.side_effect = None
        mock.return_value = text_response(f'{{"summary": "{SECRET}", ', MODEL)
        outcome = make_classifier(client)[0].classify(INQUIRY, "req-1")
        assert outcome.status is ClassifyStatus.INVALID_OUTPUT
        assert outcome.attempts == 3
        assert outcome.error_detail == "ModelBehaviorError"
        assert SECRET not in repr(outcome)
