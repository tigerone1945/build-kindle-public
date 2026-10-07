"""TASK-001 のスモークテスト：パッケージが import でき、テスト基盤が動くことを確認する。"""

import triage_agent


def test_package_is_importable() -> None:
    assert triage_agent.__doc__ is not None
