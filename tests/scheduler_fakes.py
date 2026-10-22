"""In-memory stand-in for the scheduler's Spaces access."""

from datetime import datetime, timezone
from typing import Any, Literal
import json

from services.scheduler_spaces import SpacesError


class FakeSchedulerSpaces:
    """Implements SchedulerSpaces over a dict. Tests simulate the page with put()."""

    def __init__(self) -> None:
        self.objects: dict[str, tuple[bytes, datetime]] = {}
        self.public: set[str] = set()
        self.fetch_calls: list[str] = []
        self.list_calls: list[str] = []

    def put(self, key: str, body: bytes, modified: datetime) -> None:
        """Write an object as the page would (private, given LastModified)."""
        self.objects[key] = (body, modified)

    async def presign(self, method: Literal["get_object", "put_object"], key: str) -> str:
        return f"https://fake/{method}/{key}"

    async def put_public_json(self, key: str, body: dict[str, Any]) -> None:
        self.objects[key] = (json.dumps(body).encode(), datetime.now(timezone.utc))
        self.public.add(key)

    async def list_modified(self, prefix: str) -> list[tuple[str, datetime]]:
        self.list_calls.append(prefix)
        return sorted((k, m) for k, (_, m) in self.objects.items() if k.startswith(prefix))

    async def fetch(self, key: str, max_bytes: int) -> bytes | None:
        self.fetch_calls.append(key)
        found = self.objects.get(key)
        if found is None:
            raise SpacesError(f"fetch {key} failed: NoSuchKey")
        if len(found[0]) > max_bytes:
            return None
        return found[0]

    async def delete(self, key: str) -> None:
        self.objects.pop(key, None)
        self.public.discard(key)
