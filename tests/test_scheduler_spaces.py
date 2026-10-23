from datetime import datetime, timezone
from unittest.mock import MagicMock
from urllib.parse import parse_qs, urlsplit

import pytest

from helpers.digital_ocean_helper import DigitalOceanHelper
from services.scheduler_spaces import DoSchedulerSpaces, SpacesError
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
    with pytest.raises(SpacesError):
        await fake.fetch("scheduler/nope.json", 10)
    assert len(fake.fetch_calls) == 3

    await fake.put_public_json("scheduler/link/x.json", {"a": 1})
    assert "scheduler/link/x.json" in fake.public
    await fake.delete("scheduler/link/x.json")
    assert "scheduler/link/x.json" not in fake.objects
    assert "scheduler/link/x.json" not in fake.public


class _Client:
    """Async-context stand-in for an S3 client."""

    def __init__(self, **ops):
        for name, fn in ops.items():
            setattr(self, name, fn)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


def _patched(helper, method, client):
    async def factory():
        return client

    setattr(helper, method, factory)


@pytest.mark.asyncio
async def test_list_pages_normalises_times_and_bucket_prefix():
    helper = _helper()
    naive = datetime(2026, 10, 1, 12, 0)
    aware = datetime(2026, 10, 2, 12, 0, tzinfo=timezone.utc)
    calls = []

    async def list_objects_v2(**kwargs):
        calls.append(kwargs)
        if "ContinuationToken" not in kwargs:
            return {
                "Contents": [{"Key": "magic-draft-logs/scheduler/a.json", "LastModified": naive}],
                "IsTruncated": True,
                "NextContinuationToken": "t2",
            }
        return {"Contents": [{"Key": "scheduler/b.json", "LastModified": aware}], "IsTruncated": False}

    _patched(helper, "create_raw_client", _Client(list_objects_v2=list_objects_v2))
    result = await helper.list_objects_with_modified("scheduler/")

    assert result == [
        ("scheduler/a.json", naive.replace(tzinfo=timezone.utc)),
        ("scheduler/b.json", aware),
    ]
    assert result[0][1].tzinfo is not None
    assert calls[1]["ContinuationToken"] == "t2"


def _get_object_client(body_bytes: bytes, content_length: int, body):
    pos = 0

    async def read(amt=None):
        # Like aiohttp's StreamReader: at most 2 bytes per call, b"" at EOF.
        nonlocal pos
        n = 2 if amt is None else min(amt, 2)
        chunk = body_bytes[pos:pos + n]
        pos += len(chunk)
        return chunk

    body.read = read

    async def get_object(**kwargs):
        return {"ContentLength": content_length, "Body": body}

    return _Client(get_object=get_object)


@pytest.mark.asyncio
async def test_download_bytes_refuses_oversized_by_content_length_and_closes():
    helper = _helper()
    body = MagicMock()
    _patched(helper, "create_raw_client", _get_object_client(b"x" * 10, 10, body))
    assert await helper.download_bytes("scheduler/k.json", 5) is None
    body.close.assert_called_once()


@pytest.mark.asyncio
async def test_download_bytes_caps_the_read_when_content_length_lies():
    helper = _helper()
    body = MagicMock()
    _patched(helper, "create_raw_client", _get_object_client(b"x" * 100, 3, body))
    assert await helper.download_bytes("scheduler/k.json", 5) is None
    body.close.assert_called_once()


@pytest.mark.asyncio
async def test_download_bytes_returns_small_body():
    helper = _helper()
    _patched(helper, "create_raw_client", _get_object_client(b"hello", 5, MagicMock()))
    assert await helper.download_bytes("scheduler/k.json", 5) == b"hello"


@pytest.mark.asyncio
async def test_download_bytes_assembles_a_body_delivered_in_short_reads():
    helper = _helper()
    _patched(helper, "create_raw_client", _get_object_client(b"abcdefg", 7, MagicMock()))
    assert await helper.download_bytes("scheduler/k.json", 10) == b"abcdefg"


@pytest.mark.asyncio
async def test_download_bytes_short_reads_over_limit_returns_none_and_closes():
    helper = _helper()
    body = MagicMock()
    _patched(helper, "create_raw_client", _get_object_client(b"abcdefghij", 3, body))
    assert await helper.download_bytes("scheduler/k.json", 5) is None
    body.close.assert_called_once()


class _Stop(Exception):
    pass


def _capture_urls(monkeypatch: pytest.MonkeyPatch, helper: DigitalOceanHelper) -> list[str]:
    """Build the REAL raw client and record the URL of each request it would send.

    A before-send handler captures request.url and aborts, so nothing touches
    the network (credentials are dummies).
    """
    urls: list[str] = []
    real = helper.create_raw_client

    class Hooked:
        def __init__(self, ctx):
            self._ctx = ctx

        async def __aenter__(self):
            client = await self._ctx.__aenter__()

            def grab(request, **_):
                urls.append(request.url)
                raise _Stop

            client.meta.events.register("before-send.s3.*", grab)
            return client

        async def __aexit__(self, *exc):
            return await self._ctx.__aexit__(*exc)

    async def with_hook():
        return Hooked(await real())

    monkeypatch.setattr(helper, "create_raw_client", with_hook)
    return urls


@pytest.mark.asyncio
async def test_public_json_download_and_delete_address_the_true_key(monkeypatch):
    # The bucket-specific endpoint (create_client) prefixes the bucket onto the
    # key, so `scheduler/x.json` would live at magic-draft-logs/scheduler/x.json
    # and the page (which addresses the true key) would 404. The raw endpoint
    # carries the bucket once, in the path.
    helper = _helper()
    urls = _capture_urls(monkeypatch, helper)
    key = "scheduler/link/tok.json"

    with pytest.raises(_Stop):
        await helper.put_public_json(key, {"a": 1})
    with pytest.raises(_Stop):
        await helper.download_bytes(key, 100)
    with pytest.raises(_Stop):
        await helper.delete_object(key)

    assert len(urls) == 3
    for url in urls:
        assert url.split("?")[0] == f"https://nyc3.digitaloceanspaces.com/magic-draft-logs/{key}"
        assert "magic-draft-logs/magic-draft-logs" not in url
        assert "//magic-draft-logs." not in url
