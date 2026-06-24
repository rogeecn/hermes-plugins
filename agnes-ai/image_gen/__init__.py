"""Agnes AI image generation backend for Hermes.

Implements Agnes Image 2.1 Flash synchronous API:
POST /images/generations -> image URL or base64 data.

Configuration sources (in priority order):
    1. image_gen.api_key   — config.yaml section
    2. AGNES_AI_API_KEY    — environment variable
    3. image_gen.base_url  — config.yaml section
    4. image_gen.model     — config.yaml section

Image-to-image inputs are documented as HTTP(S) URLs in extra_body.image.
Local file paths are rejected because the API expects URL strings.
"""
from __future__ import annotations

import logging
import os
from typing import Any, Dict, List

import requests

from agent.image_gen_provider import (
    DEFAULT_ASPECT_RATIO,
    ImageGenProvider,
    error_response,
    resolve_aspect_ratio,
    save_b64_image,
    success_response,
)

logger = logging.getLogger(__name__)

_MODEL = "agnes-image-2.1-flash"
_DEFAULT_BASE_URL = "https://apihub.agnes-ai.com/v1"

_SIZE_MAP: Dict[str, str] = {
    "landscape": "1536x1024",
    "square": "1024x1024",
    "portrait": "1024x1536",
}


# ── helpers ──────────────────────────────────────────────────────────


def _is_http_url(value: str) -> bool:
    from urllib.parse import urlparse

    parsed = urlparse(value)
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


def _normalize_image_urls(value: Any) -> List[str]:
    """Normalize image-to-image inputs.

    Agnes Image 2.1 Flash expects HTTP(S) URL strings in extra_body.image.
    Local paths are rejected with a clear error.
    """
    if isinstance(value, str):
        raw = [value]
    elif isinstance(value, (list, tuple)):
        raw = list(value)
    else:
        raw = []

    images: List[str] = []
    for item in raw:
        s = str(item or "").strip()
        if not s:
            continue
        if not _is_http_url(s):
            raise ValueError(
                f"Agnes image-to-image input must be an HTTP(S) URL: {s}"
            )
        images.append(s)
    return images


def _load_config() -> Dict[str, Any]:
    try:
        from hermes_cli.config import load_config

        cfg = load_config()
        section = cfg.get("image_gen") if isinstance(cfg, dict) else None
        return section if isinstance(section, dict) else {}
    except Exception as exc:
        logger.debug("Could not load image_gen config: %s", exc)
        return {}


# ── config resolution helpers ────────────────────────────────────────


def _resolve_api_key(cfg: dict) -> str:
    return str(cfg.get("api_key") or os.environ.get("AGNES_AI_API_KEY", "")).strip()


def _resolve_base_url(cfg: dict) -> str:
    return str(cfg.get("base_url") or _DEFAULT_BASE_URL).strip().rstrip("/")


def _resolve_model(cfg: dict, **kwargs: Any) -> str:
    return str(kwargs.get("model") or cfg.get("model") or _MODEL).strip()


# ── provider ─────────────────────────────────────────────────────────


class AgnesImageGenProvider(ImageGenProvider):
    @property
    def name(self) -> str:
        return "agnes-ai"

    @property
    def display_name(self) -> str:
        return "Agnes AI"

    def is_available(self) -> bool:
        cfg = _load_config()
        api_key = _resolve_api_key(cfg)
        base_url = _resolve_base_url(cfg)
        return bool(api_key and base_url)

    def list_models(self) -> List[Dict[str, Any]]:
        cfg = _load_config()
        model = _resolve_model(cfg)
        return [
            {
                "id": model,
                "display": model,
                "speed": "provider-dependent",
                "strengths": "OpenAI-compatible image generation, image-to-image via extra_body.image",
            }
        ]

    def default_model(self) -> str:
        cfg = _load_config()
        return _resolve_model(cfg)

    def get_setup_schema(self) -> Dict[str, Any]:
        return {
            "name": "Agnes AI",
            "badge": "free",
            "tag": "Image generate by Agnes AI",
            "env_vars": [{"key": "AGNES_AI_API_KEY"}],
        }

    def generate(
        self,
        prompt: str,
        aspect_ratio: str = DEFAULT_ASPECT_RATIO,
        **kwargs: Any,
    ) -> Dict[str, Any]:
        cfg = _load_config()
        api_key = _resolve_api_key(cfg)
        base_url = _resolve_base_url(cfg)
        model = _resolve_model(cfg, **kwargs)
        aspect = resolve_aspect_ratio(aspect_ratio)

        # validation
        if not prompt or not str(prompt).strip():
            return error_response(
                error="Prompt is required",
                error_type="invalid_argument",
                provider=self.name,
                aspect_ratio=aspect,
            )
        if not api_key or not base_url:
            return error_response(
                error="image_gen.api_key and image_gen.base_url are required",
                error_type="auth_required",
                provider=self.name,
                model=model,
                prompt=prompt,
                aspect_ratio=aspect,
            )

        # build payload
        payload: Dict[str, Any] = {
            "model": model,
            "prompt": str(prompt).strip(),
            "size": _SIZE_MAP.get(aspect, _SIZE_MAP["square"]),
            "n": 1,
        }

        # image-to-image
        input_images = _normalize_image_urls(
            kwargs.get("images") or kwargs.get("image") or kwargs.get("input_image")
        )
        if input_images:
            payload["extra_body"] = {
                "image": input_images,
                "response_format": "url",
            }

        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }

        # call API
        try:
            resp = requests.post(
                f"{base_url}/images/generations",
                headers=headers,
                json=payload,
                timeout=180,
            )
            resp.raise_for_status()
            result = resp.json()
        except requests.HTTPError as exc:
            return self._handle_http_error(exc, model, prompt, aspect)
        except requests.Timeout:
            return error_response(
                error="Image generation timed out (180s)",
                error_type="timeout",
                provider=self.name,
                model=model,
                prompt=prompt,
                aspect_ratio=aspect,
            )
        except Exception as exc:
            return error_response(
                error=f"Image generation error: {exc}",
                error_type="api_error",
                provider=self.name,
                model=model,
                prompt=prompt,
                aspect_ratio=aspect,
            )

        # parse response
        data = result.get("data") if isinstance(result, dict) else None
        if not data:
            return error_response(
                error=f"Provider returned no data: {str(result)[:500]}",
                error_type="empty_response",
                provider=self.name,
                model=model,
                prompt=prompt,
                aspect_ratio=aspect,
            )

        first = data[0]
        image_ref = None

        if isinstance(first, dict):
            if first.get("url"):
                image_ref = first["url"]
            elif first.get("b64_json"):
                image_ref = save_b64_image(first["b64_json"])

        if not image_ref:
            return error_response(
                error=f"No image URL or b64_json in response: {str(first)[:500]}",
                error_type="empty_response",
                provider=self.name,
                model=model,
                prompt=prompt,
                aspect_ratio=aspect,
            )

        return success_response(
            image=image_ref,
            provider=self.name,
            model=model,
            prompt=prompt,
            aspect_ratio=aspect,
        )

    # ── error helpers ───────────────────────────────────────────────

    @staticmethod
    def _handle_http_error(
        exc: requests.HTTPError,
        model: str,
        prompt: str,
        aspect: str,
    ) -> Dict[str, Any]:
        resp = exc.response
        status = resp.status_code if resp is not None else 0
        try:
            body = resp.json() if resp is not None else {}
            err = (
                body.get("error", {}).get("message")
                if isinstance(body.get("error"), dict)
                else body.get("error")
            )
            err = err or str(body)[:500]
        except Exception:
            err = resp.text[:500] if resp is not None else str(exc)
        return error_response(
            error=f"Image generation failed ({status}): {err}",
            error_type="api_error",
            provider="agnes-ai",
            model=model,
            prompt=prompt,
            aspect_ratio=aspect,
        )


def register(ctx: Any) -> None:
    ctx.register_image_gen_provider(AgnesImageGenProvider())
