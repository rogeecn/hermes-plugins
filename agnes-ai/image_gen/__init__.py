"""Agnes AI image generation backend.

Wraps the Agnes AI image API (agnes-image-2.0-flash / 2.1-flash) as an
:class:`ImageGenProvider` implementation.

API docs: https://agnes-ai.com/zh-Hans/docs/quickstart
Endpoint: POST https://apihub.agnes-ai.com/v1/images/generations

Supports:
  - Text-to-image (文生图)
  - Image-to-image (图生图) — pass image_url / reference_image_urls
  - Multi-image composition (多图合成)

Auth: AGNES_API_KEY env var (Bearer token)
Base URL: AGNES_BASE_URL env var (default: https://apihub.agnes-ai.com/v1)
"""

from __future__ import annotations

import base64
import logging
import os
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

import httpx

from agent.image_gen_provider import (
    DEFAULT_ASPECT_RATIO,
    ImageGenProvider,
    resolve_aspect_ratio,
    save_b64_image,
    save_url_image,
    success_response,
    error_response,
)

logger = logging.getLogger(__name__)

# ── Constants ────────────────────────────────────────────────────────

DEFAULT_BASE_URL = "https://apihub.agnes-ai.com/v1"

# Aspect ratio → pixel size mapping (Agnes accepts WxH strings)
_ASPECT_TO_SIZE: Dict[str, str] = {
    "landscape": "1024x768",
    "square": "1024x1024",
    "portrait": "768x1024",
}

# Model catalog
_MODELS: Dict[str, Dict[str, Any]] = {
    "agnes-image-2.0-flash": {
        "display": "Agnes Image 2.0 Flash",
        "speed": "~3-5s",
        "strengths": "Fast text-to-image, image-to-image, multi-image composition",
        "price": "$0/img (free tier)",
    },
    "agnes-image-2.1-flash": {
        "display": "Agnes Image 2.1 Flash",
        "speed": "~3-5s",
        "strengths": "Latest image model with improved quality",
        "price": "$0/img (free tier)",
    },
}

DEFAULT_MODEL = "agnes-image-2.0-flash"
DEFAULT_TIMEOUT = 120
MAX_REFERENCE_IMAGES = 4

# Retry config for transient 503 "image queue is full" errors
MAX_RETRIES = 4
RETRY_BASE_DELAY = 5   # seconds; 5, 10, 15, 20


def _resolve_credentials() -> tuple[str, str]:
    """Return (api_key, base_url) from env vars."""
    api_key = os.getenv("AGNES_API_KEY", "").strip()
    base_url = os.getenv("AGNES_BASE_URL", "").strip().rstrip("/") or DEFAULT_BASE_URL
    return api_key, base_url


def _image_ref_to_url(value: str) -> str:
    """Convert a local file path to a data URI; pass through URLs unchanged."""
    ref = (value or "").strip()
    if not ref:
        return ""
    lower = ref.lower()
    if lower.startswith(("http://", "https://", "data:image/")):
        return ref
    path = Path(ref).expanduser()
    if not path.is_file():
        return ref
    import mimetypes
    mime = mimetypes.guess_type(path.name)[0] or "image/png"
    if not mime.startswith("image/"):
        return ref
    encoded = base64.b64encode(path.read_bytes()).decode("ascii")
    return f"data:{mime};base64,{encoded}"


class AgnesImageGenProvider(ImageGenProvider):
    """Agnes AI image generation backend."""

    @property
    def name(self) -> str:
        return "agnes"

    @property
    def display_name(self) -> str:
        return "Agnes AI"

    def is_available(self) -> bool:
        api_key, _ = _resolve_credentials()
        return bool(api_key)

    def list_models(self) -> List[Dict[str, Any]]:
        return [
            {
                "id": mid,
                "display": meta.get("display", mid),
                "speed": meta.get("speed", ""),
                "strengths": meta.get("strengths", ""),
                "price": meta.get("price", ""),
            }
            for mid, meta in _MODELS.items()
        ]

    def default_model(self) -> Optional[str]:
        return DEFAULT_MODEL

    def get_setup_schema(self) -> Dict[str, Any]:
        return {
            "name": "Agnes AI",
            "badge": "free",
            "tag": "agnes-image-2.0-flash — text-to-image, image-to-image, multi-image composition",
            "env_vars": [
                {
                    "key": "AGNES_API_KEY",
                    "prompt": "Agnes AI API key",
                    "url": "https://agnes-ai.com",
                },
                {
                    "key": "AGNES_BASE_URL",
                    "prompt": "Agnes AI base URL (default: https://apihub.agnes-ai.com/v1)",
                    "url": "",
                },
            ],
        }

    def capabilities(self) -> Dict[str, Any]:
        return {
            "modalities": ["text", "image"],
            "max_reference_images": MAX_REFERENCE_IMAGES,
        }

    def generate(
        self,
        prompt: str,
        aspect_ratio: str = DEFAULT_ASPECT_RATIO,
        *,
        image_url: Optional[str] = None,
        reference_image_urls: Optional[List[str]] = None,
        **kwargs: Any,
    ) -> Dict[str, Any]:
        """Generate or edit an image via the Agnes AI API.

        Routing:
          - image_url or reference_image_urls present → image-to-image / multi-image
          - otherwise → text-to-image
        """
        api_key, base_url = _resolve_credentials()
        if not api_key:
            return error_response(
                error="AGNES_API_KEY not set",
                error_type="auth_missing",
                provider="agnes",
                prompt=prompt,
                aspect_ratio=aspect_ratio,
            )

        aspect = resolve_aspect_ratio(aspect_ratio)
        size = _ASPECT_TO_SIZE.get(aspect, "1024x768")
        model = (kwargs.get("model") or DEFAULT_MODEL).strip()

        # Determine modality and build image array
        image_inputs: List[str] = []
        modality = "text"

        if image_url:
            normalized = _image_ref_to_url(image_url)
            if normalized:
                image_inputs.append(normalized)
                modality = "image"

        if reference_image_urls:
            for ref in reference_image_urls[:MAX_REFERENCE_IMAGES]:
                normalized = _image_ref_to_url(ref)
                if normalized:
                    image_inputs.append(normalized)
                    modality = "image"

        # Build request body
        body: Dict[str, Any] = {
            "model": model,
            "prompt": prompt,
            "size": size,
            "extra_body": {
                "response_format": "url",
            },
        }

        if image_inputs:
            body["extra_body"]["image"] = image_inputs

        url = f"{base_url}/images/generations"
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }

        # Agnes API may return 503 "image queue is full" under load;
        # retry with exponential backoff.
        last_error: Optional[str] = None
        for attempt in range(MAX_RETRIES + 1):
            try:
                with httpx.Client(timeout=DEFAULT_TIMEOUT) as client:
                    response = client.post(url, headers=headers, json=body)
                    response.raise_for_status()
                    result = response.json()
                break  # success
            except httpx.HTTPStatusError as exc:
                status = exc.response.status_code
                body_text = exc.response.text[:500]
                # 503 with "queue is full" → transient, retryable
                if status == 503 and attempt < MAX_RETRIES:
                    delay = RETRY_BASE_DELAY * (attempt + 1)
                    logger.info(
                        "Agnes API 503 (queue full), retry %d/%d in %ds",
                        attempt + 1, MAX_RETRIES, delay,
                    )
                    last_error = f"Agnes API HTTP {status}: {body_text}"
                    time.sleep(delay)
                    continue
                err_msg = f"Agnes API HTTP {status}: {body_text}"
                logger.warning(err_msg)
                return error_response(
                    error=err_msg,
                    error_type="api_error",
                    provider="agnes",
                    model=model,
                    prompt=prompt,
                    aspect_ratio=aspect,
                )
            except Exception as exc:
                logger.warning("Agnes image generation failed: %s", exc, exc_info=True)
                return error_response(
                    error=f"Agnes image generation failed: {exc}",
                    error_type=type(exc).__name__,
                    provider="agnes",
                    model=model,
                    prompt=prompt,
                    aspect_ratio=aspect,
                )
        else:
            # All retries exhausted
            return error_response(
                error=last_error or "Agnes API exhausted all retries",
                error_type="api_error",
                provider="agnes",
                model=model,
                prompt=prompt,
                aspect_ratio=aspect,
            )

        # Parse response: data[0].url or data[0].b64_json
        data_list = result.get("data", [])
        if not data_list:
            return error_response(
                error="Agnes API returned empty data array",
                error_type="empty_response",
                provider="agnes",
                model=model,
                prompt=prompt,
                aspect_ratio=aspect,
            )

        first = data_list[0]
        image_url_out = first.get("url")
        b64_json = first.get("b64_json")

        if image_url_out:
            # Download URL to local cache for reliable delivery
            try:
                local_path = save_url_image(image_url_out, prefix="agnes")
                image_out = str(local_path)
            except Exception as exc:
                logger.debug("Failed to cache Agnes image URL, returning raw URL: %s", exc)
                image_out = image_url_out
        elif b64_json:
            local_path = save_b64_image(b64_json, prefix="agnes")
            image_out = str(local_path)
        else:
            return error_response(
                error="Agnes API returned no url or b64_json in data[0]",
                error_type="missing_image",
                provider="agnes",
                model=model,
                prompt=prompt,
                aspect_ratio=aspect,
            )

        return success_response(
            image=image_out,
            model=model,
            prompt=prompt,
            aspect_ratio=aspect,
            provider="agnes",
            modality=modality,
        )


# ── Plugin entry point ───────────────────────────────────────────────


def register(ctx) -> None:
    """Plugin entry point — wire AgnesImageGenProvider into the registry."""
    ctx.register_image_gen_provider(AgnesImageGenProvider())
