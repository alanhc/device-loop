import pytest

from coordinator import db


class FakeClock:
    """可控時鐘:測 TTL 過期與 grace window 不用真的 sleep。"""

    def __init__(self, start: str = "2026-09-05T12:00:00+00:00"):
        from datetime import datetime

        self._now = datetime.fromisoformat(start)

    def __call__(self) -> str:
        return self._now.isoformat(timespec="seconds")

    def advance(self, seconds: int) -> None:
        from datetime import timedelta

        self._now += timedelta(seconds=seconds)


@pytest.fixture()
def clock():
    return FakeClock()


@pytest.fixture()
def conn():
    c = db.connect(":memory:")
    db.init_db(c)
    db.seed_db(c)
    yield c
    c.close()
