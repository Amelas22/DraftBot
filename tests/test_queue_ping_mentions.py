"""The queue ping notifies the drafter role and nothing a cube name smuggles in."""
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest


@pytest.mark.asyncio
async def test_queue_ping_notifies_only_the_drafter_role():
    from views import send_queue_ping

    channel = SimpleNamespace(send=AsyncMock())
    role = SimpleNamespace(id=555, mention="<@&555>")

    await send_queue_ping(channel, 6, "<@&999> cube @everyone", role)

    args, kwargs = channel.send.await_args
    assert args == ("6 Players in queue to play <@&999> cube @everyone! <@&555>",)
    mentions = kwargs["allowed_mentions"].to_dict()
    assert mentions["roles"] == [555]
    assert "roles" not in mentions["parse"]
    assert "everyone" not in mentions["parse"]
