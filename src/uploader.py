"""
src/uploader.py
Handles automated uploading of local images to Cloudinary.
Returns public HTTPS URLs required by the Instagram Graph API.
"""

import os
from pathlib import Path
import cloudinary
import cloudinary.uploader
from dotenv import load_dotenv

import re

def _sanitize(text: str) -> str:
    """Cloudinary public_id only allows alphanumeric, underscores, and hyphens."""
    return re.sub(r'[^a-zA-Z0-9_\-]', '_', text)


_cloudinary_ready: bool = False

def _init_cloudinary() -> bool:
    """Configure the Cloudinary SDK once per process lifetime."""
    global _cloudinary_ready
    if _cloudinary_ready:
        return True
    # Expects CLOUDINARY_URL=cloudinary://API_KEY:API_SECRET@CLOUD_NAME
    url = os.getenv("CLOUDINARY_URL")
    if not url:
        return False
    cloudinary.config(cloudinary_url=url)
    _cloudinary_ready = True
    return True


def upload_image(image_path: Path) -> str:
    """Uploads a local image and returns the public URL."""
    if not _init_cloudinary():
        raise EnvironmentError("CLOUDINARY_URL not set in .env")

    # Sanitize names for Cloudinary
    project_name = _sanitize(image_path.parent.name)
    filename = _sanitize(image_path.stem)
    public_id = f"cms_uploads/{project_name}/{filename}"

    print(f"  [uploader] Uploading to: {public_id} ...")
    
    try:
        response = cloudinary.uploader.upload(
            str(image_path),
            public_id=public_id,
            resource_type="image",
            overwrite=True
        )
        # Log the full keys we got back to debug missing secure_url
        print(f"  [uploader] Keys received: {list(response.keys())}")
        
        url = response.get("secure_url") or response.get("url")
        if not url:
            print(f"  [ERROR] Cloudinary response missing URL: {response}")
            raise RuntimeError(f"Cloudinary did not return a URL. Response: {response}")
        
        print(f"  [uploader] Success: {url}")
        return str(url)
    except Exception as e:
        print(f"  [ERROR] Cloudinary API call failed: {e}")
        raise

def upload_multiple(image_paths: list[Path]) -> list[str]:
    """Uploads multiple images sequentially."""
    urls = []
    for p in image_paths:
        urls.append(upload_image(p))
    return urls
