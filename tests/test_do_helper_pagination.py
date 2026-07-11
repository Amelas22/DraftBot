"""list_objects must follow ContinuationToken to return >1000 keys."""
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from helpers.digital_ocean_helper import DigitalOceanHelper


@pytest.mark.asyncio
async def test_list_objects_paginates():
    pages = [
        {"Contents": [{"Key": "b/team/a.json"}, {"Key": "b/team/b.json"}],
         "IsTruncated": True, "NextContinuationToken": "tok2"},
        {"Contents": [{"Key": "b/team/c.json"}], "IsTruncated": False},
    ]
    calls = []
    fake_s3 = MagicMock()
    async def _list(**kwargs):
        calls.append(kwargs)
        return pages[len(calls) - 1]
    fake_s3.list_objects_v2 = _list

    @asynccontextmanager
    async def _client_ctx():
        yield fake_s3

    helper = DigitalOceanHelper.__new__(DigitalOceanHelper)
    helper.config_valid = True
    helper.bucket = "b"
    helper.logger = MagicMock()
    helper.create_raw_client = AsyncMock(return_value=_client_ctx())

    keys = await helper.list_objects("b/team/")

    assert keys == ["b/team/a.json", "b/team/b.json", "b/team/c.json"]
    assert len(calls) == 2
    assert calls[1].get("ContinuationToken") == "tok2"   # second page requested with token
