"""Agnes AI video generation backend.

Wraps the Agnes AI video API (agnes-video-v2.0) as a
:class:`VideoGenProvider` implementation.

API docs: https://agnes-ai.com/zh-Hans/docs/quickstart
Endpoints:
  Create task: POST https://apihub.agnes-ai.com/v1/videos
  Get result:  GET  https://apihub.agnes-ai.com/agnesapi?video_id=<VIDEO_ID>

Supports:
  - Text-to-video (文生视频)
  - Image-to-video (图生视频) — pass image_url
  - Keyframe animation (关键帧动画) — pass reference_image_urls

Auth: AGNES_API_KEY env var (Bearer token)
Base URL: AGNES_BASE_URL env var (default: https://apihub.agnes-ai.com/v1)

Video generation is asynchronous: create a task → poll for completion →
download the resulting MP4 to local cache.
"""

from __future__ import annotations

import logging
import os
import time
from typing import Any, Dict, List, Optional

import httpx

from agent.video_gen_provider import (
    COMMON_ASPECT_RATIOS,
    DEFAULT_ASPECT_RATIO,
    DEFAULT_RESOLUTION,
    VideoGenProvider,
    error_response,
    success_response,
    save_bytes_video,
)

logger = logging.getLogger(__name__)

# ── Constants ────────────────────────────────────────────────────────

DEFAULT_BASE_URL = "https://apihub.agnes-ai.com/v1"
DEFAULT_MODEL = "agnes-video-v2.0"
DEFAULT_TIMEOUT = 300          # total poll budget
DEFAULT_POLL_INTERVAL = 5      # seconds between polls
DEFAULT_DURATION = 5            # seconds
DEFAULT_FRAME_RATE = 24

# num_frames must follow 8n+1 rule and ≤441
# 81 → ~3s, 121 → ~5s, 241 → ~10s, 441 → ~18s
_DURATION_TO_NUM_FRAMES: Dict[int, int] = {
    3: 81,
    5: 121,
    10: 241,
    18: 441,
}

# Resolution → height/width mapping (16:9 landscape default)
_RESOLUTION_TO_DIMS: Dict[str, Dict[str, int]] = {
    "480p": {"height": 480, "width": 854},
    "540p": {"height": 540, "width": 960},
    "720p": {"height": 768, "width": 1152},   # Agnes standard 720p
    "1080p": {"height": 1080, "width": 1920},
}

# Aspect ratio → swap width/height
_ASPECT_IS_PORTRAIT = {"9:16", "3:4", "2:3"}
_ASPECT_IS_SQUARE = {"1:1"}

_MODELS: Dict[str, Dict[str, Any]] = {
    "agnes-video-v2.0": {
        "display": "Agnes Video V2.0",
        "speed": "~60-240s",
        "strengths": "Text-to-video, image-to-video, keyframe animation; cinematic quality",
        "price": "$0/sec (free tier)",
        "modalities": ["text", "image"],
    },
}


def _resolve_credentials() -> tuple[str, str]:
    """Return (api_key, base_url) from env vars."""
    api_key = os.getenv("AGNES_API_KEY", "").strip()
    base_url = os.getenv("AGNES_BASE_URL", "").strip().rstrip("/") or DEFAULT_BASE_URL
    return api_key, base_url


def _resolve_dims(
    resolution: str,
    aspect_ratio: str,
) -> tuple[int, int]:
    """Return (height, width) for the given resolution and aspect ratio."""
    dims = _RESOLUTION_TO_DIMS.get(resolution, _RESOLUTION_TO_DIMS["720p"])
    h, w = dims["height"], dims["width"]
    if aspect_ratio in _ASPECT_IS_PORTRAIT:
        h, w = w, h  # swap for portrait
    elif aspect_ratio in _ASPECT_IS_SQUARE:
        h = w = min(h, w)
    return h, w


def _resolve_num_frames(duration: Optional[int]) -> int:
    """Map duration in seconds to the nearest valid num_frames (8n+1 rule)."""
    if duration is None:
        return _DURATION_TO_NUM_FRAMES[DEFAULT_DURATION]
    # Find closest supported duration
    closest = min(_DURATION_TO_NUM_FRAMES.keys(), key=lambda d: abs(d - duration))
    return _DURATION_TO_NUM_FRAMES[closest]


class AgnesVideoGenProvider(VideoGenProvider):
    """Agnes AI video generation backend."""

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
        return [{"id": mid, **meta} for mid, meta in _MODELS.items()]

    def default_model(self) -> Optional[str]:
        return DEFAULT_MODEL

    def get_setup_schema(self) -> Dict[str, Any]:
        return {
            "name": "Agnes AI Video",
            "badge": "free",
            "tag": "agnes-video-v2.0 — text-to-video, image-to-video, keyframe animation (async)",
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
            "aspect_ratios": list(COMMON_ASPECT_RATIOS),
            "resolutions": ["480p", "540p", "720p", "1080p"],
            "max_duration": 18,
            "min_duration": 3,
            "supports_audio": False,
            "supports_negative_prompt": True,
            "max_reference_images": 4,
        }

    def generate(
        self,
        prompt: str,
        *,
        model: Optional[str] = None,
        image_url: Optional[str] = None,
        reference_image_urls: Optional[List[str]] = None,
        duration: Optional[int] = None,
        aspect_ratio: str = DEFAULT_ASPECT_RATIO,
        resolution: str = DEFAULT_RESOLUTION,
        negative_prompt: Optional[str] = None,
        audio: Optional[bool] = None,
        seed: Optional[int] = None,
        **kwargs: Any,
    ) -> Dict[str, Any]:
        """Generate a video via the Agnes AI async video API.

        Routing:
          - image_url present → image-to-video
          - reference_image_urls present → keyframe animation
          - otherwise → text-to-video
        """
        api_key, base_url = _resolve_credentials()
        if not api_key:
            return error_response(
                error="AGNES_API_KEY not set",
                error_type="auth_missing",
                provider="agnes",
                model=model or DEFAULT_MODEL,
                prompt=prompt,
                aspect_ratio=aspect_ratio,
            )

        used_model = (model or DEFAULT_MODEL).strip()
        h, w = _resolve_dims(resolution, aspect_ratio)
        num_frames = _resolve_num_frames(duration)
        modality = "text"

        # Build request body
        body: Dict[str, Any] = {
            "model": used_model,
            "prompt": prompt,
            "height": h,
            "width": w,
            "num_frames": num_frames,
            "frame_rate": DEFAULT_FRAME_RATE,
        }

        if image_url:
            body["image"] = image_url
            body["mode"] = "ti2vid"
            modality = "image"

        if reference_image_urls:
            body["extra_body"] = {
                "image": reference_image_urls,
                "mode": "keyframes",
            }
            modality = "image"

        if negative_prompt:
            body["negative_prompt"] = negative_prompt

        if seed is not None:
            body["seed"] = seed

        create_url = f"{base_url}/videos"
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }

        try:
            # Step 1: Create video task
            with httpx.Client(timeout=60) as client:
                resp = client.post(create_url, headers=headers, json=body)
                resp.raise_for_status()
                task = resp.json()

            video_id = task.get("video_id") or task.get("task_id")
            if not video_id:
                return error_response(
                    error=f"Agnes video API returned no video_id/task_id: {task}",
                    error_type="missing_task_id",
                    provider="agnes",
                    model=used_model,
                    prompt=prompt,
                    aspect_ratio=aspect_ratio,
                )

            logger.info("Agnes video task created: video_id=%s, status=%s", video_id, task.get("status"))

            # Step 2: Poll for completion
            # Agnes uses a different base for results: /agnesapi?video_id=
            # But it's on the same host
            poll_base = base_url.replace("/v1", "")  # → https://apihub.agnes-ai.com
            result_url = f"{poll_base}/agnesapi"
            video_path = self._poll_for_completion(
                video_id, result_url, headers, api_key, base_url
            )

            if video_path is None:
                return error_response(
                    error=f"Agnes video task {video_id} timed out or failed",
                    error_type="timeout",
                    provider="agnes",
                    model=used_model,
                    prompt=prompt,
                    aspect_ratio=aspect_ratio,
                )

            actual_duration = int(num_frames / DEFAULT_FRAME_RATE)

            return success_response(
                video=video_path,
                model=used_model,
                prompt=prompt,
                modality=modality,
                aspect_ratio=aspect_ratio,
                duration=actual_duration,
                provider="agnes",
            )

        except httpx.HTTPStatusError as exc:
            err_msg = f"Agnes API HTTP {exc.response.status_code}: {exc.response.text[:500]}"
            logger.warning(err_msg)
            return error_response(
                error=err_msg,
                error_type="api_error",
                provider="agnes",
                model=used_model,
                prompt=prompt,
                aspect_ratio=aspect_ratio,
            )
        except Exception as exc:
            logger.warning("Agnes video generation failed: %s", exc, exc_info=True)
            return error_response(
                error=f"Agnes video generation failed: {exc}",
                error_type=type(exc).__name__,
                provider="agnes",
                model=used_model,
                prompt=prompt,
                aspect_ratio=aspect_ratio,
            )

    def _poll_for_completion(
        self,
        video_id: str,
        result_url: str,
        headers: Dict[str, str],
        api_key: str,
        base_url: str,
    ) -> Optional[str]:
        """Poll the Agnes API until the video is ready; download and cache it.

        Returns the local file path, or None on timeout/failure.
        """
        # Try the recommended endpoint first: /agnesapi?video_id=
        # Fallback to legacy: /v1/videos/<task_id>
        legacy_url = f"{base_url}/videos/{video_id}"
        use_legacy = False

        elapsed = 0
        poll_interval = DEFAULT_POLL_INTERVAL

        while elapsed < DEFAULT_TIMEOUT:
            try:
                with httpx.Client(timeout=30) as client:
                    if not use_legacy:
                        resp = client.get(
                            result_url,
                            params={"video_id": video_id},
                            headers=headers,
                        )
                    else:
                        resp = client.get(legacy_url, headers=headers)

                    if resp.status_code == 404 and not use_legacy:
                        logger.debug("Agnes recommended endpoint 404, falling back to legacy /v1/videos/<id>")
                        use_legacy = True
                        continue

                    resp.raise_for_status()
                    body = resp.json()

                status = (body.get("status") or "").lower()

                if status == "completed":
                    video_url = body.get("url")
                    if not video_url:
                        logger.error("Agnes video completed but no url in response: %s", body)
                        return None
                    # Download the video
                    return self._download_video(video_url)

                if status in ("failed", "error", "cancelled"):
                    logger.error("Agnes video task %s failed: %s", video_id, body.get("error"))
                    return None

                logger.debug("Agnes video %s: status=%s progress=%s", video_id, status, body.get("progress"))

            except Exception as exc:
                logger.debug("Agnes poll error (non-fatal): %s", exc)

            time.sleep(poll_interval)
            elapsed += poll_interval

        logger.warning("Agnes video %s timed out after %ss", video_id, DEFAULT_TIMEOUT)
        return None

    def _download_video(self, video_url: str) -> Optional[str]:
        """Download the video MP4 to local cache and return the file path."""
        try:
            with httpx.Client(timeout=120) as client:
                resp = client.get(video_url)
                resp.raise_for_status()
                local_path = save_bytes_video(resp.content, prefix="agnes", extension="mp4")
                return str(local_path)
        except Exception as exc:
            logger.error("Failed to download Agnes video from %s: %s", video_url, exc)
            return None


# ── Plugin entry point ───────────────────────────────────────────────


def register(ctx) -> None:
    """Plugin entry point — wire AgnesVideoGenProvider into the registry."""
    ctx.register_video_gen_provider(AgnesVideoGenProvider())
