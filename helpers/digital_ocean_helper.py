import os
import json
import logging
from typing import AsyncIterator, Dict, Any, Optional, Tuple, List
from dataclasses import dataclass
from datetime import datetime, timezone
import aiobotocore.session
from botocore.config import Config


# Draft logs carry every player's picks and decklist, so nothing the bot uploads
# is public by default. Articles and league pages share this bucket and ARE
# published on purpose -- they pass acl="public-read" explicitly.
PRIVATE = "private"
PUBLIC = "public-read"


@dataclass
class UploadResult:
    """Result of an upload operation to Digital Ocean Spaces."""
    success: bool
    object_path: Optional[str] = None


class DigitalOceanHelper:
    """Helper class for interacting with Digital Ocean Spaces"""
    
    def __init__(self):
        self.logger = logging.getLogger(__name__)
        self._load_config()
        
    def _load_config(self) -> None:
        """Load Digital Ocean Spaces configuration from environment variables"""
        self.region = os.getenv("DO_SPACES_REGION")
        self.endpoint = os.getenv("DO_SPACES_ENDPOINT")  # Bucket-specific endpoint for operations
        self.raw_endpoint = os.getenv("DO_SPACES_RAW_ENDPOINT")  # Region-only endpoint for listing
        self.key = os.getenv("DO_SPACES_KEY")
        self.secret = os.getenv("DO_SPACES_SECRET")
        self.bucket = os.getenv("DO_SPACES_BUCKET")

        self.config_valid = all([
            self.region,
            self.endpoint,
            self.key,
            self.secret,
            self.bucket
        ])

        if not self.config_valid:
            self.logger.warning("Missing DigitalOcean Spaces configuration")
    
    async def create_client(self):
        """Create and return an S3 client for Digital Ocean Spaces (bucket-specific endpoint)"""
        if not self.config_valid:
            return None

        session = aiobotocore.session.AioSession()
        return session.create_client(
            's3',
            region_name=self.region,
            endpoint_url=self.endpoint,  # Bucket-specific endpoint
            aws_access_key_id=self.key,
            aws_secret_access_key=self.secret
        )

    async def create_raw_client(self, config: Optional[Config] = None):
        """Create and return an S3 client for listing operations (raw endpoint)"""
        if not self.config_valid or not self.raw_endpoint:
            return None

        session = aiobotocore.session.AioSession()
        return session.create_client(
            's3',
            region_name=self.region,
            endpoint_url=self.raw_endpoint,  # Region-only endpoint for listing
            aws_access_key_id=self.key,
            aws_secret_access_key=self.secret,
            config=config,
        )
    
    async def upload_json(
        self,
        data: Dict[str, Any],
        folder: str,
        filename: str,
        acl: str = PRIVATE
    ) -> UploadResult:
        """
        Upload JSON data to Digital Ocean Spaces

        Args:
            data: The JSON data to upload
            folder: The folder path within the bucket
            filename: The filename to use
            acl: PRIVATE unless the caller is deliberately publishing

        Returns:
            UploadResult with success status and object_path
        """
        if not self.config_valid:
            self.logger.warning("Cannot upload: Missing Digital Ocean Spaces configuration")
            return UploadResult(success=False)

        try:
            client = await self.create_client()
            if not client:
                return UploadResult(success=False)

            object_path = f'{folder}/{filename}'

            async with client as s3:
                await s3.put_object(
                    Bucket=self.bucket,
                    Key=object_path,
                    Body=json.dumps(data),
                    ContentType='application/json',
                    ACL=acl
                )

            self.logger.info(f"Data uploaded to DigitalOcean Space: {object_path}")
            return UploadResult(success=True, object_path=object_path)

        except Exception as e:
            self.logger.error(f"Error uploading to DigitalOcean Space: {e}")
            return UploadResult(success=False)
    
    async def upload_text(
        self,
        content: str,
        folder: str,
        filename: str,
        acl: str = PRIVATE
    ) -> UploadResult:
        """
        Upload text content to Digital Ocean Spaces

        Args:
            content: The text content to upload
            folder: The folder path within the bucket
            filename: The filename to use
            acl: PRIVATE unless the caller is deliberately publishing

        Returns:
            UploadResult with success status and object_path
        """
        if not self.config_valid:
            self.logger.warning("Cannot upload: Missing Digital Ocean Spaces configuration")
            return UploadResult(success=False)

        try:
            client = await self.create_client()
            if not client:
                return UploadResult(success=False)

            object_path = f'{folder}/{filename}'

            async with client as s3:
                await s3.put_object(
                    Bucket=self.bucket,
                    Key=object_path,
                    Body=content,
                    ContentType='text/plain',
                    ACL=acl
                )

            self.logger.info(f"Text uploaded to DigitalOcean Space: {object_path}")
            return UploadResult(success=True, object_path=object_path)

        except Exception as e:
            self.logger.error(f"Error uploading to DigitalOcean Space: {e}")
            return UploadResult(success=False)

    async def upload_public_html(
        self,
        content: str,
        folder: str,
        filename: str,
    ) -> UploadResult:
        """Publish an HTML page anyone with the link can read.

        Deliberately separate from upload_text, on two counts:

        * It uses the RAW (region-level) client. The bucket-specific endpoint
          prefixes the bucket onto the key -- every draft log is really at
          `magic-draft-logs/team/...`, not `team/...` -- which is invisible for
          a private object read back the same way, but fatal here: the URL
          get_public_url advertises would 404.
        * ACL is public-read with no parameter to make it otherwise. Everything
          else this helper uploads is private by default because a draft log is
          an opponent's whole pool; a page published on purpose is the
          exception, and making that explicit in the method name keeps the two
          from being confused at a call site.

        CacheControl is no-cache so a rebuilt page under the same name is
        actually seen.
        """
        if not self.config_valid:
            self.logger.warning("Cannot publish: Missing Digital Ocean Spaces configuration")
            return UploadResult(success=False)

        try:
            object_path = f'{folder}/{filename}'
            await self._put_public(object_path, content.encode("utf-8"), 'text/html; charset=utf-8')
            self.logger.info(f"Published page to DigitalOcean Space: {object_path}")
            return UploadResult(success=True, object_path=object_path)

        except Exception as e:
            self.logger.error(f"Error publishing to DigitalOcean Space: {e}")
            return UploadResult(success=False)

    async def _put_public(self, key: str, body: bytes, content_type: str) -> None:
        """Write a public-read, no-cache object at the true key. Raises on failure.

        The raw client, for the reason upload_public_html gives: the
        bucket-specific endpoint would store it one level deeper than the URL
        get_public_url advertises.
        """
        client = await self.create_raw_client()
        if client is None:
            raise RuntimeError("DO_SPACES_RAW_ENDPOINT is not set -- cannot publish")
        async with client as s3:
            await s3.put_object(
                Bucket=self.bucket,
                Key=key,
                Body=body,
                ContentType=content_type,
                CacheControl='no-cache',
                ACL=PUBLIC,
            )

    async def download_json(self, object_path: str) -> Optional[Dict[str, Any]]:
        """
        Download and parse JSON data from Digital Ocean Spaces

        Args:
            object_path: The object key within the bucket (WITHOUT bucket prefix)
                        e.g., "team/PowerLSV-123.json" NOT "magic-draft-logs/team/..."

        Returns:
            Parsed JSON data or None if download failed
        """
        if not self.config_valid:
            self.logger.warning("Cannot download: Missing Digital Ocean Spaces configuration")
            return None

        try:
            client = await self.create_client()
            if not client:
                return None

            async with client as s3:
                response = await s3.get_object(
                    Bucket=self.bucket,
                    Key=object_path
                )
                body = await response['Body'].read()
                data = json.loads(body.decode('utf-8'))

            self.logger.info(f"Data downloaded from DigitalOcean Space: {object_path}")
            return data

        except Exception as e:
            self.logger.error(f"Error downloading from DigitalOcean Space: {e}")
            return None

    async def list_objects(self, prefix: str) -> List[str]:
        """
        List objects in Digital Ocean Spaces with a given prefix

        Args:
            prefix: The prefix to filter objects by

        Returns:
            List of object keys (with bucket prefix if using raw endpoint)
        """
        if not self.config_valid:
            self.logger.warning("Cannot list: Missing Digital Ocean Spaces configuration")
            return []

        try:
            # Use raw_client for listing (returns keys with bucket prefix)
            client = await self.create_raw_client()
            if not client:
                self.logger.warning("Failed to create raw client for listing")
                return []

            async with client as s3:
                self.logger.debug(f"Listing objects with bucket={self.bucket}, prefix={prefix}")
                keys = [obj["Key"] async for obj in self._list_pages(s3, prefix)]
                self.logger.debug(f"Found {len(keys)} keys for prefix {prefix}")
                return keys

        except Exception as e:
            self.logger.error(f"Error listing objects: {e}", exc_info=True)
            return []

    async def _list_pages(self, s3: Any, prefix: str) -> AsyncIterator[Dict[str, Any]]:
        """Every object under prefix, across all list_objects_v2 pages."""
        continuation_token = None
        while True:
            kwargs: Dict[str, Any] = {"Bucket": self.bucket, "Prefix": prefix}
            if continuation_token:
                kwargs["ContinuationToken"] = continuation_token
            response = await s3.list_objects_v2(**kwargs)
            for obj in response.get("Contents", []):
                yield obj
            continuation_token = response.get("NextContinuationToken")
            if not (response.get("IsTruncated") and continuation_token):
                # Stop when the API says we're done, or defensively if it
                # claims truncation but omits the token (avoids re-fetching
                # the first page forever).
                break

    async def create_presign_client(self):
        """Client for presigning URLs the browser will use.

        Region endpoint with virtual-hosted addressing, so the URL is
        https://<bucket>.<region>.digitaloceanspaces.com/<key>. The
        bucket-specific create_client() double-nests the key in presigned URLs;
        never presign with it.
        """
        return await self.create_raw_client(
            Config(s3={"addressing_style": "virtual"}, signature_version="s3v4")
        )

    async def list_objects_with_modified(self, prefix: str) -> List[Tuple[str, datetime]]:
        """List every object under prefix with its LastModified (aware UTC).

        Pages through all results. Raises on failure (unlike list_objects, which
        returns []), so a caller can tell "empty" from "could not list".
        """
        client = await self.create_raw_client()
        if not client:
            raise RuntimeError("Spaces is not configured")

        found: List[Tuple[str, datetime]] = []
        bucket_prefix = f"{self.bucket}/"
        async with client as s3:
            async for obj in self._list_pages(s3, prefix):
                key = obj["Key"]
                # The raw endpoint may hand keys back prefixed with the bucket.
                if not key.startswith(prefix) and key.startswith(bucket_prefix):
                    key = key[len(bucket_prefix):]
                modified = obj["LastModified"]
                if modified.tzinfo is None:
                    modified = modified.replace(tzinfo=timezone.utc)
                found.append((key, modified.astimezone(timezone.utc)))
        return found

    async def download_bytes(self, key: str, max_bytes: int) -> Optional[bytes]:
        """Download an object's raw bytes; None if it is larger than max_bytes.

        ContentLength short-circuits an honest oversized object without reading
        it; the read itself is also capped at max_bytes + 1, so a wrong
        ContentLength cannot make us buffer more. Raises on failure.
        """
        client = await self.create_raw_client()
        if not client:
            raise RuntimeError("Spaces is not configured")

        async with client as s3:
            response = await s3.get_object(Bucket=self.bucket, Key=key)
            if response["ContentLength"] > max_bytes:
                self.logger.warning(f"{key} is {response['ContentLength']} bytes, over {max_bytes}; skipped")
                response["Body"].close()
                return None
            # StreamingBody.read(n) may return fewer than n bytes, so loop until
            # EOF or until we have seen more than max_bytes.
            buf = b""
            while len(buf) <= max_bytes:
                chunk: bytes = await response["Body"].read(max_bytes + 1 - len(buf))
                if not chunk:
                    break
                buf += chunk
            if len(buf) > max_bytes:
                self.logger.warning(f"{key} is over {max_bytes} bytes; skipped")
                response["Body"].close()
                return None
            return buf

    async def put_public_json(self, key: str, body: Dict[str, Any]) -> None:
        """Write a public-read JSON object at the true key. Raises on failure."""
        await self._put_public(key, json.dumps(body).encode("utf-8"), "application/json")

    async def delete_object(self, key: str) -> None:
        """Delete one object at the true key. Raises on failure."""
        client = await self.create_raw_client()
        if client is None:
            raise RuntimeError("Spaces is not configured")
        async with client as s3:
            await s3.delete_object(Bucket=self.bucket, Key=key)

    def get_public_url(self, object_path: str) -> str:
        """
        Get the public URL for an object in Digital Ocean Spaces

        Args:
            object_path: The object path within the bucket

        Returns:
            The public URL
        """
        return f"https://{self.bucket}.{self.region}.digitaloceanspaces.com/{object_path}"