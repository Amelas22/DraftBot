"""Fakes and fixtures for the hosted-Draftmancer tests: a stand-in for the
control API client, a Discord channel that remembers what it was sent, and a
hosted draft seeded in the test database."""
from datetime import datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import discord
import pytest
import pytest_asyncio
from sqlalchemy import select, update

from database.db_session import AsyncSessionLocal
from models.draft_session import DraftSession
from services import hosted_draft
from services.hosted_draftmancer import HostedSeat, HostedStatus

from conftest import seed_session

SID = "hosted-1"
DRAFT_ID = "ABC123"
TOKEN = "s3cret"
SIGN_UPS = {"111": "Alice", "222": "Bob", "333": "Cara", "444": "Dan"}
LOG = {
    "sessionID": "DB" + DRAFT_ID,
    "time": 1700000000000,
    "users": {f"u{i}": {"userName": n, "seatNum": i, "picks": []}
              for i, n in enumerate(SIGN_UPS.values())},
}


# --- fakes --------------------------------------------------------------------

class FakeMessage:
    def __init__(self, channel: "FakeChannel", mid: int, content: str | None):
        self.channel, self.id, self.content = channel, mid, content
        self.deleted = False

    async def edit(self, content: str | None = None, **_: Any) -> None:
        self.content = content
        self.channel.edits.append((self.id, content))

    async def delete(self) -> None:
        self.deleted = True


class FakeChannel:
    def __init__(self) -> None:
        self.id = 999
        self.name = "draft-chat"
        self.guild = MagicMock(id=1)
        self.guild.name = "Guild"
        self.messages: list[FakeMessage] = []
        self.edits: list[tuple[int, str | None]] = []

    async def send(self, content: str | None = None, **_: Any) -> FakeMessage:
        message = FakeMessage(self, 1000 + len(self.messages), content)
        self.messages.append(message)
        return message

    async def fetch_message(self, mid: int) -> FakeMessage:
        for m in self.messages:
            if m.id == mid and not m.deleted:
                return m
        raise discord.NotFound(MagicMock(status=404), "gone")

    def sent(self, needle: str) -> list[FakeMessage]:
        return [m for m in self.messages if m.content and needle in m.content]


class FakeHosted:
    """Stands in for HostedDraftmancer: records calls, answers status from `seats`."""

    def __init__(self) -> None:
        self.calls: list[tuple[Any, ...]] = []
        self.stage = "gathering"
        self.seats: list[HostedSeat] = []
        self.start_missing: list[str] = []
        self.log: dict[str, Any] | None = None

    def call_names(self) -> list[str]:
        return [c[0] for c in self.calls]

    async def status(self, draft_id: str) -> HostedStatus:
        return HostedStatus(stage=self.stage, seats=list(self.seats), pick_number=None)

    async def set_players(self, draft_id: str, names: list[str]) -> None:
        self.calls.append(("set_players", draft_id, list(names)))
        self.seats = [seat(n, connected=False) for n in names]

    async def set_seating(self, draft_id: str, names: list[str]) -> None:
        self.calls.append(("set_seating", draft_id, list(names)))

    async def ready_check(self, draft_id: str) -> None:
        self.calls.append(("ready_check", draft_id))
        self.seats = [seat(s.name, s.connected) for s in self.seats]

    async def start(self, draft_id: str) -> list[str]:
        self.calls.append(("start", draft_id))
        if not self.start_missing:
            self.stage = "drafting"
        return list(self.start_missing)

    async def pause(self, draft_id: str) -> None:
        self.calls.append(("pause", draft_id))

    async def resume(self, draft_id: str) -> None:
        self.calls.append(("resume", draft_id))

    async def create(self, spec: dict[str, Any]) -> None:
        self.calls.append(("create", spec))

    async def stop(self, draft_id: str) -> None:
        self.calls.append(("stop", draft_id))

    async def release_log(self, draft_id: str) -> None:
        self.calls.append(("release_log", draft_id))

    async def replace_with_bot(self, draft_id: str, name: str) -> None:
        self.calls.append(("replace_with_bot", draft_id, name))

    async def fetch_log(self, draft_id: str) -> dict[str, Any] | None:
        self.calls.append(("fetch_log", draft_id))
        return self.log

    def fail(self, name: str, error: BaseException) -> None:
        """Make the client method `name` raise `error` from now on."""
        async def raising(*_: Any, **__: Any) -> Any:
            self.calls.append((name, "raised"))
            raise error
        setattr(self, name, raising)


def seat(name: str, connected: bool = True, ready: str = "unknown") -> HostedSeat:
    return HostedSeat(name=name, user_id="seat-" + name, connected=connected,
                      disconnected_since=None if connected else 1.0, ready=ready)


def everyone(connected: bool = True, ready: str = "unknown", **overrides: tuple[bool, str]
             ) -> list[HostedSeat]:
    seats = []
    for name in SIGN_UPS.values():
        c, r = overrides.get(name, (connected, ready))
        seats.append(seat(name, c, r))
    return seats


@pytest_asyncio.fixture
async def hosted(test_db, monkeypatch):
    fake = FakeHosted()
    monkeypatch.setattr(hosted_draft, "hosted_client", lambda: fake)
    monkeypatch.setattr(hosted_draft, "START_DELAY_SECONDS", 0)
    monkeypatch.setenv("DRAFTMANCER_API_URL", "http://api.test")
    monkeypatch.setenv("DRAFTMANCER_API_TOKEN", TOKEN)
    clock = {"now": 1_000_000.0}
    monkeypatch.setattr(hosted_draft, "_now", lambda: clock["now"])
    fake.clock = clock  # type: ignore[attr-defined]
    hosted_draft.forget_all()
    yield fake
    hosted_draft.forget_all()


@pytest.fixture
def channel() -> FakeChannel:
    return FakeChannel()


@pytest.fixture
def bot(channel: FakeChannel) -> MagicMock:
    b = MagicMock()
    b.get_channel.return_value = channel
    b.get_guild.return_value = MagicMock(id=1)
    return b


@pytest.fixture
def dms():
    with patch("services.hosted_draft.send_ready_check_dms", AsyncMock()) as send:
        yield send


async def _seed(stage: str | None = "teams", route: str | None = "hosted", **kw: Any) -> None:
    kw.setdefault("start", datetime.now())  # The poller only looks at recent drafts.
    await seed_session(session_id=SID, guild="1", stype="random", stage=stage,
                       draft_channel_id="999", sign_ups=dict(SIGN_UPS), draft_id=DRAFT_ID, **kw)
    async with AsyncSessionLocal() as s:
        await s.execute(update(DraftSession).where(DraftSession.session_id == SID)
                        .values(draftmancer_route=route))
        await s.commit()


async def _row() -> DraftSession:
    async with AsyncSessionLocal() as s:
        row = await s.scalar(select(DraftSession).filter_by(session_id=SID))
        assert row is not None
        return row
