"""The Spaces operations the scheduler needs, behind a narrow Protocol."""

from datetime import datetime
from typing import Any, Literal, Protocol

from helpers.digital_ocean_helper import DigitalOceanHelper

PREFIX = "scheduler/"
PAGE_URL = "https://magic-draft-logs.nyc3.digitaloceanspaces.com/scheduler/index.html"
PRESIGN_SECONDS = 604800


class SpacesError(Exception):
    """A Spaces call failed; callers log it and retry on the next poll."""


class SchedulerSpaces(Protocol):
    async def presign(self, method: Literal["get_object", "put_object"], key: str) -> str: ...

    async def put_public_json(self, key: str, body: dict[str, Any]) -> None: ...

    async def list_modified(self, prefix: str) -> list[tuple[str, datetime]]: ...

    async def fetch(self, key: str, max_bytes: int) -> bytes | None: ...

    async def delete(self, key: str) -> None: ...


class DoSchedulerSpaces:
    """SchedulerSpaces on DigitalOceanHelper."""

    def __init__(self, helper: DigitalOceanHelper) -> None:
        self._helper = helper

    async def presign(self, method: Literal["get_object", "put_object"], key: str) -> str:
        params: dict[str, Any] = {"Bucket": self._helper.bucket, "Key": key}
        if method == "put_object":
            params["ContentType"] = "application/json"
        try:
            client = await self._helper.create_presign_client()
            if client is None:
                raise SpacesError("Spaces is not configured")
            async with client as s3:
                url: str = await s3.generate_presigned_url(
                    method, Params=params, ExpiresIn=PRESIGN_SECONDS
                )
            return url
        except SpacesError:
            raise
        except Exception as e:
            raise SpacesError(f"presign {method} {key} failed: {e}") from e

    async def put_public_json(self, key: str, body: dict[str, Any]) -> None:
        if not await self._helper.put_public_json(key, body):
            raise SpacesError(f"put {key} failed")

    async def list_modified(self, prefix: str) -> list[tuple[str, datetime]]:
        try:
            return await self._helper.list_objects_with_modified(prefix)
        except Exception as e:
            raise SpacesError(f"list {prefix} failed: {e}") from e

    async def fetch(self, key: str, max_bytes: int) -> bytes | None:
        try:
            return await self._helper.download_bytes(key, max_bytes)
        except Exception as e:
            raise SpacesError(f"fetch {key} failed: {e}") from e

    async def delete(self, key: str) -> None:
        if not await self._helper.delete_object(key):
            raise SpacesError(f"delete {key} failed")
