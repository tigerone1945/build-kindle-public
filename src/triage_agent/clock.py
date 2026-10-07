"""時刻の取得。テストで時刻を固定できるよう、時刻を返す関数（`Clock`）を、生成元のコンポーネントへ注入する。

時刻の付与は、各レコードの生成元（チケットシステム、Human Review キューなど）が行う（Design 第7.3節）。
"""

from collections.abc import Callable
from datetime import UTC, datetime

Clock = Callable[[], datetime]


def utc_now() -> datetime:
    """現在の時刻を、UTC のタイムゾーンつきで返す。"""
    return datetime.now(UTC)
