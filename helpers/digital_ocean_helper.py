import os
import json
import boto3
import logging
from typing import Dict, Any, Optional, Tuple


class DigitalOceanHelper:
    """Helper class for interacting with Digital Ocean Spaces"""
    
    def __init__(self):
        self.logger = logging.getLogger(__name__)
        self._load_config()
        
    def _load_config(self) -> None:
        """Load Digital Ocean Spaces configuration from environment variables"""
        self.region = os.getenv("DO_SPACES_REGION")
        self.endpoint = os.getenv("DO_SPACES_ENDPOINT")
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
        """Create and return an S3 client for Digital Ocean Spaces"""
        if not self.config_valid:
            return None
            
        session = boto3.session.Session()
        return session.client(
            's3',
            region_name=self.region,
            endpoint_url=self.endpoint,
            aws_access_key_id=self.key,
            aws_secret_access_key=self.secret
        )
    
    async def upload_json(
        self, 
        data: Dict[str, Any], 
        folder: str, 
        filename: str
    ) -> Tuple[bool, Optional[str]]:
        """
        Upload JSON data to Digital Ocean Spaces
        
        Args:
            data: The JSON data to upload
            folder: The folder path within the bucket
            filename: The filename to use
            
        Returns:
            Tuple of (success, object_path)
        """
        if not self.config_valid:
            self.logger.warning("Cannot upload: Missing Digital Ocean Spaces configuration")
            return False, None
            
        try:
            client = await self.create_client()
            if not client:
                return False, None
                
            object_path = f'{folder}/{filename}'
            
            client.put_object(
                Bucket=self.bucket,
                Key=object_path,
                Body=json.dumps(data),
                ContentType='application/json',
                ACL='public-read'
            )
            
            self.logger.info(f"Data uploaded to DigitalOcean Space: {object_path}")
            return True, object_path
            
        except Exception as e:
            self.logger.error(f"Error uploading to DigitalOcean Space: {e}")
            return False, None
    
    async def upload_text(
        self, 
        content: str, 
        folder: str, 
        filename: str
    ) -> Tuple[bool, Optional[str]]:
        """
        Upload text content to Digital Ocean Spaces
        
        Args:
            content: The text content to upload
            folder: The folder path within the bucket
            filename: The filename to use
            
        Returns:
            Tuple of (success, object_path)
        """
        if not self.config_valid:
            self.logger.warning("Cannot upload: Missing Digital Ocean Spaces configuration")
            return False, None
            
        try:
            client = await self.create_client()
            if not client:
                return False, None
                
            object_path = f'{folder}/{filename}'
            
            client.put_object(
                Bucket=self.bucket,
                Key=object_path,
                Body=content,
                ContentType='text/plain',
                ACL='public-read'
            )
            
            self.logger.info(f"Text uploaded to DigitalOcean Space: {object_path}")
            return True, object_path
            
        except Exception as e:
            self.logger.error(f"Error uploading to DigitalOcean Space: {e}")
            return False, None
    
    def get_public_url(self, object_path: str) -> str:
        """
        Get the public URL for an object in Digital Ocean Spaces
        
        Args:
            object_path: The object path within the bucket
            
        Returns:
            The public URL
        """
        return f"https://{self.bucket}.{self.region}.digitaloceanspaces.com/{object_path}"