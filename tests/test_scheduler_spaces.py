from datetime import datetime, timezone
from urllib.parse import parse_qs, urlsplit

import pytest

from helpers.digital_ocean_helper import DigitalOceanHelper
from services.scheduler_spaces import DoSchedulerSpaces
from scheduler_fakes import FakeSchedulerSpaces


def _helper() -> DigitalOceanHelper:
    helper = DigitalOceanHelper()
    helper.key = "AKIAX"
    helper.secret = "s"
    helper.region = "nyc3"
    helper.endpoint = "https://magic-draft-logs.nyc3.digitaloceanspaces.com"
    helper.raw_endpoint = "https://nyc3.digitaloceanspaces.com"
    helper.bucket = "magic-draft-logs"
    helper.config_valid = True
    return helper


@pytest.mark.asyncio
async def test_presigned_put_is_virtual_hosted_on_the_page_origin():
    spaces = DoSchedulerSpaces(_helper())
    url = await spaces.presign("put_object", "scheduler/availability/1.json")
    parts = urlsplit(url)
    query = parse_qs(parts.query)
    assert parts.netloc == "magic-draft-logs.nyc3.digitaloceanspaces.com"
    assert parts.path == "/scheduler/availability/1.json"
    assert query["X-Amz-Expires"] == ["604800"]
    assert "content-type" in query["X-Amz-SignedHeaders"][0]


@pytest.mark.asyncio
async def test_presigned_get_is_virtual_hosted_without_content_type():
    spaces = DoSchedulerSpaces(_helper())
    url = await spaces.presign("get_object", "scheduler/availability/1.json")
    parts = urlsplit(url)
    assert parts.netloc == "magic-draft-logs.nyc3.digitaloceanspaces.com"
    assert parts.path == "/scheduler/availability/1.json"
    assert "content-type" not in parse_qs(parts.query)["X-Amz-SignedHeaders"][0]


@pytest.mark.asyncio
async def test_fake_honours_prefix_and_max_bytes_and_counts_calls():
    fake = FakeSchedulerSpaces()
    t = datetime(2026, 10, 22, tzinfo=timezone.utc)
    fake.put("scheduler/availability/1.json", b"12345", t)
    fake.put("scheduler/link/x.json", b"{}", t)
    fake.put("other/y.json", b"{}", t)

    assert await fake.list_modified("scheduler/availability/") == [
        ("scheduler/availability/1.json", t)
    ]
    assert fake.list_calls == ["scheduler/availability/"]
    assert await fake.fetch("scheduler/availability/1.json", 5) == b"12345"
    assert await fake.fetch("scheduler/availability/1.json", 4) is None
    assert await fake.fetch("scheduler/nope.json", 10) is None
    assert len(fake.fetch_calls) == 3

    await fake.put_public_json("scheduler/link/x.json", {"a": 1})
    assert "scheduler/link/x.json" in fake.public
    await fake.delete("scheduler/link/x.json")
    assert "scheduler/link/x.json" not in fake.objects
    assert "scheduler/link/x.json" not in fake.public
