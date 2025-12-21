import os
import json
import logging
from typing import Dict, Any, Optional, Tuple, List
from dataclasses import dataclass
import aiobotocore.session


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

    async def create_raw_client(self):
        """Create and return an S3 client for listing operations (raw endpoint)"""
        if not self.config_valid or not self.raw_endpoint:
            return None

        session = aiobotocore.session.AioSession()
        return session.create_client(
            's3',
            region_name=self.region,
            endpoint_url=self.raw_endpoint,  # Region-only endpoint for listing
            aws_access_key_id=self.key,
            aws_secret_access_key=self.secret
        )
    
    async def upload_json(
        self,
        data: Dict[str, Any],
        folder: str,
        filename: str
    ) -> UploadResult:
        """
        Upload JSON data to Digital Ocean Spaces

        Args:
            data: The JSON data to upload
            folder: The folder path within the bucket
            filename: The filename to use

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
                    ACL='public-read'
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
        filename: str
    ) -> UploadResult:
        """
        Upload text content to Digital Ocean Spaces

        Args:
            content: The text content to upload
            folder: The folder path within the bucket
            filename: The filename to use

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
                    ACL='public-read'
                )

            self.logger.info(f"Text uploaded to DigitalOcean Space: {object_path}")
            return UploadResult(success=True, object_path=object_path)

        except Exception as e:
            self.logger.error(f"Error uploading to DigitalOcean Space: {e}")
            return UploadResult(success=False)
    
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
                response = await s3.list_objects_v2(
                    Bucket=self.bucket,
                    Prefix=prefix
                )

                if 'Contents' not in response:
                    self.logger.debug(f"No contents found for prefix {prefix}")
                    return []

                keys = [obj['Key'] for obj in response['Contents']]
                self.logger.debug(f"Found {len(keys)} keys for prefix {prefix}")
                return keys

        except Exception as e:
            self.logger.error(f"Error listing objects: {e}", exc_info=True)
            return []

    def get_public_url(self, object_path: str) -> str:
        """
        Get the public URL for an object in Digital Ocean Spaces

        Args:
            object_path: The object path within the bucket

        Returns:
            The public URL
        """
        return f"https://{self.bucket}.{self.region}.digitaloceanspaces.com/{object_path}"