"""Agnes AI video generation backend for Hermes.

Implements Agnes-Video-V2.0 asynchronous API:
POST /videos -> task id, then GET /videos/{task_id} until completed.

Configuration uses video_gen settings:

    video_gen.api_key
    video_gen.base_url   (optional; defaults to built-in Agnes endpoint)
    video_gen.model
    video_gen.timeout
    video_gen.poll_interval
"""
from __future__ import annotations

import logging
import math
import os
import time
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

import requests

from agent.video_gen_provider import (
    DEFAULT_ASPECT_RATIO,
    DEFAULT_RESOLUTION,
    VideoGenProvider,
    error_response,
    success_response,
)

logger = logging.getLogger(__name__)

_MODEL = "agnes-video-v2.0"
_DEFAULT_BASE_URL = "https://apihub.agnes-ai.com/v1"
_DEFAULT_FPS = 24
_DEFAULT_FRAMES = 121
_MAX_FRAMES = 441

_ASPECT_TO_SIZE: Dict[str, tuple[int, int]] = {
    "16:9": (1280, 720),
    "9:16": (720, 1280),
    "1:1": (768, 768),
    "4:3": (1024, 768),
    "3:4": (768, 1024),
    "3:2": (1152, 768),
    "2:3": (768, 1152),
}
_RESOLUTION_SCALE: Dict[str, int] = {
    "480p": 480,
    "540p": 540,
    "720p": 720,
    "1080p": 1080,
}

_COMPLETED_STATUSES = {"completed", "succeeded", "success", "done"}
_FAILED_STATUSES = {"failed", "error", "cancelled", "canceled"}


def _is_http_url(value: str) -> bool:
    parsed = urlparse(value)
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


def _normalize_urls(value: Any) -> List[str]:
    if value is None:
        return []
    if isinstance(value, str):
        items = [value]
    elif isinstance(value, (list, tuple)):
        items = list(value)
    else:
        return []

    urls: List[str] = []
    for item in items:
        url = str(item or "").strip()
        if not url:
            continue
        if not _is_http_url(url):
            raise ValueError(
                f"Agnes video input images must be HTTP(S) URLs: {url}")
        if url not in urls:
            urls.append(url)
    return urls


def _load_config() -> Dict[str, Any]:
    try:
        from hermes_cli.config import load_config

        cfg = load_config()
        video_cfg = cfg.get("video_gen") if isinstance(cfg, dict) else None
        if isinstance(video_cfg, dict):
            return {
                k: v
                for k, v in video_cfg.items() if v not in (None, "")
            }
        return {}
    except Exception as exc:
        logger.debug("Could not load Agnes video config: %s", exc)
        return {}


def _coerce_positive_int(value: Any, default: int) -> int:
    try:
        n = int(value)
        return n if n > 0 else default
    except Exception:
        return default


def _frames_for_duration(duration: Optional[int]) -> int:
    if not duration:
        return _DEFAULT_FRAMES
    target = max(1, int(duration)) * _DEFAULT_FPS
    target = min(target, _MAX_FRAMES)
    # Agnes requires 8n + 1. Use nearest valid frame count, minimum 9.
    n = max(1, round((target - 1) / 8))
    frames = 8 * n + 1
    return min(frames, _MAX_FRAMES)


def _size_for(aspect_ratio: str, resolution: str) -> tuple[int, int]:
    width, height = _ASPECT_TO_SIZE.get(aspect_ratio,
                                        _ASPECT_TO_SIZE[DEFAULT_ASPECT_RATIO])
    target_short = _RESOLUTION_SCALE.get(resolution,
                                         _RESOLUTION_SCALE[DEFAULT_RESOLUTION])
    short = min(width, height)
    scale = target_short / short
    width = int(round(width * scale / 8) * 8)
    height = int(round(height * scale / 8) * 8)
    return max(8, width), max(8, height)


def _extract_task_id(result: Dict[str, Any]) -> Optional[str]:
    body = result.get("body") if isinstance(result.get("body"),
                                            dict) else result
    for key in ("task_id", "id"):
        value = body.get(key) if isinstance(body, dict) else None
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _extract_status(result: Dict[str, Any]) -> str:
    body = result.get("body") if isinstance(result.get("body"),
                                            dict) else result
    value = body.get("status") if isinstance(body, dict) else None
    return str(value or "").strip().lower()


def _extract_body(result: Dict[str, Any]) -> Dict[str, Any]:
    body = result.get("body") if isinstance(result.get("body"),
                                            dict) else result
    return body if isinstance(body, dict) else {}


def _extract_video_url(result: Dict[str, Any]) -> Optional[str]:
    body = _extract_body(result)
    candidates = [
        body.get("video_url"),
        body.get("url"),
        body.get("remixed_from_video_id"
                 ),  # present in Agnes docs final response example
    ]
    data = body.get("data")
    if isinstance(data, dict):
        candidates.extend([data.get("video_url"), data.get("url")])
    if isinstance(data, list) and data:
        first = data[0]
        if isinstance(first, dict):
            candidates.extend([first.get("video_url"), first.get("url")])
    for value in candidates:
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _http_error(exc: requests.HTTPError) -> str:
    resp = exc.response
    status = resp.status_code if resp is not None else 0
    try:
        body = resp.json() if resp is not None else {}
        if isinstance(body, dict):
            err_obj = body.get("error")
            if isinstance(err_obj, dict):
                msg = err_obj.get("message") or str(err_obj)
            else:
                msg = err_obj or body.get("message") or str(body)
        else:
            msg = str(body)
    except Exception:
        msg = resp.text[:500] if resp is not None else str(exc)
    return f"Agnes video API failed ({status}): {msg}"


class AgnesVideoGenProvider(VideoGenProvider):

    @property
    def name(self) -> str:
        return "agnes-ai"

    @property
    def display_name(self) -> str:
        return "Agnes AI Video"

    def is_available(self) -> bool:
        cfg = _load_config()
        api_key = str(
            cfg.get("api_key")
            or os.environ.get("AGNES_AI_API_KEY", "")).strip()
        base_url = str(cfg.get("base_url") or _DEFAULT_BASE_URL).strip()
        return bool(api_key and base_url)

    def list_models(self) -> List[Dict[str, Any]]:
        return [{
            "id": _MODEL,
            "display": "Agnes Video V2.0",
            "speed": "async / polling",
            "strengths":
            "text-to-video, image-to-video, multi-image video, keyframe animation",
            "modalities": ["text", "image"],
        }]

    def default_model(self) -> str:
        cfg = _load_config()
        return str(cfg.get("model") or _MODEL).strip() or _MODEL

    def capabilities(self) -> Dict[str, Any]:
        return {
            "modalities": ["text", "image"],
            "aspect_ratios": list(_ASPECT_TO_SIZE.keys()),
            "resolutions": list(_RESOLUTION_SCALE.keys()),
            "max_duration": 18,
            "min_duration": 1,
            "supports_audio": False,
            "supports_negative_prompt": True,
            "max_reference_images": 16,
            "supports_multi_image": True,
            "supports_keyframes": True,
        }

    def get_setup_schema(self) -> Dict[str, Any]:
        return {
            "name": "Agnes AI",
            "badge": "free",
            "tag": "Video generate by Agnes AI",
            "env_vars": [{
                "key": "AGNES_AI_API_KEY"
            }],
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
        cfg = _load_config()
        api_key = str(
            cfg.get("api_key")
            or os.environ.get("AGNES_AI_API_KEY", "")).strip()
        base_url = str(cfg.get("base_url")
                       or _DEFAULT_BASE_URL).strip().rstrip("/")
        model_id = str(model or cfg.get("model") or _MODEL).strip() or _MODEL
        if model_id != _MODEL:
            logger.debug("Agnes video model override %r normalized to %s",
                         model_id, _MODEL)
            model_id = _MODEL

        if not prompt or not str(prompt).strip():
            return error_response(error="Prompt is required",
                                  error_type="invalid_argument",
                                  provider=self.name,
                                  model=model_id,
                                  aspect_ratio=aspect_ratio)
        if not api_key:
            return error_response(
                error="video_gen.api_key is required",
                error_type="auth_required",
                provider=self.name,
                model=model_id,
                prompt=prompt,
                aspect_ratio=aspect_ratio)
        if not base_url:
            return error_response(
                error="video_gen.base_url is required",
                error_type="auth_required",
                provider=self.name,
                model=model_id,
                prompt=prompt,
                aspect_ratio=aspect_ratio)

        try:
            primary_images = _normalize_urls(image_url)
            reference_images = _normalize_urls(reference_image_urls)
        except ValueError as exc:
            return error_response(error=str(exc),
                                  error_type="invalid_argument",
                                  provider=self.name,
                                  model=model_id,
                                  prompt=prompt,
                                  aspect_ratio=aspect_ratio)

        all_images = primary_images + [
            u for u in reference_images if u not in primary_images
        ]
        mode = str(kwargs.get("mode") or kwargs.get("generation_mode")
                   or "").strip().lower()
        if mode in {"keyframe", "keyframes", "keyframe_animation"}:
            mode = "keyframes"
        elif mode in {
                "multi", "multi_image", "multi-image", "multi_image_video"
        }:
            mode = "multi_image"
        elif not mode:
            mode = "image" if all_images else "text"

        width, height = _size_for(aspect_ratio, resolution)
        frames = _coerce_positive_int(kwargs.get("num_frames"),
                                      _frames_for_duration(duration))
        if frames > _MAX_FRAMES:
            frames = _MAX_FRAMES
        if (frames - 1) % 8 != 0:
            frames = 8 * max(1, round((frames - 1) / 8)) + 1
            frames = min(frames, _MAX_FRAMES)
        fps = _coerce_positive_int(kwargs.get("frame_rate"), _DEFAULT_FPS)
        fps = max(1, min(60, fps))
        actual_duration = max(1, int(round(frames / fps)))

        payload: Dict[str, Any] = {
            "model": _MODEL,
            "prompt": str(prompt).strip(),
            "height": height,
            "width": width,
            "num_frames": frames,
            "frame_rate": fps,
        }
        if negative_prompt:
            payload["negative_prompt"] = negative_prompt
        if seed is not None:
            payload["seed"] = int(seed)

        # Agnes docs distinguish single image via top-level `image`; multi-image
        # and keyframes via `extra_body.image`, with `extra_body.mode=keyframes`.
        if mode == "keyframes":
            if len(all_images) < 2:
                return error_response(
                    error="Keyframe animation requires at least two image URLs",
                    error_type="invalid_argument",
                    provider=self.name,
                    model=model_id,
                    prompt=prompt,
                    aspect_ratio=aspect_ratio)
            payload["extra_body"] = {"image": all_images, "mode": "keyframes"}
        elif len(all_images) > 1 or mode == "multi_image":
            if not all_images:
                return error_response(
                    error="Multi-image video requires image URLs",
                    error_type="invalid_argument",
                    provider=self.name,
                    model=model_id,
                    prompt=prompt,
                    aspect_ratio=aspect_ratio)
            payload["extra_body"] = {"image": all_images}
        elif len(all_images) == 1:
            payload["image"] = all_images[0]

        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json"
        }
        create_timeout = _coerce_positive_int(cfg.get("request_timeout"), 180)
        poll_timeout = _coerce_positive_int(cfg.get("timeout"), 1800)
        poll_interval = _coerce_positive_int(cfg.get("poll_interval"), 5)

        try:
            create_resp = requests.post(f"{base_url}/videos",
                                        headers=headers,
                                        json=payload,
                                        timeout=create_timeout)
            create_resp.raise_for_status()
            create_result = create_resp.json()
            task_id = _extract_task_id(create_result)
            if not task_id:
                return error_response(
                    error=
                    f"Agnes returned no task id: {str(create_result)[:500]}",
                    error_type="empty_response",
                    provider=self.name,
                    model=model_id,
                    prompt=prompt,
                    aspect_ratio=aspect_ratio)

            deadline = time.monotonic() + poll_timeout
            last_result: Dict[str, Any] = create_result if isinstance(
                create_result, dict) else {}
            while time.monotonic() < deadline:
                status = _extract_status(last_result)
                if status in _COMPLETED_STATUSES:
                    video_url = _extract_video_url(last_result)
                    if not video_url:
                        return error_response(
                            error=
                            f"Agnes task completed but no video URL was found: {str(last_result)[:500]}",
                            error_type="empty_response",
                            provider=self.name,
                            model=model_id,
                            prompt=prompt,
                            aspect_ratio=aspect_ratio)
                    modality = "image" if all_images else "text"
                    return success_response(
                        video=video_url,
                        model=model_id,
                        prompt=prompt,
                        modality=modality,
                        aspect_ratio=aspect_ratio,
                        duration=actual_duration,
                        provider=self.name,
                        extra={
                            "task_id": task_id,
                            "status": status,
                            "mode": mode,
                            "size": f"{width}x{height}",
                            "num_frames": frames,
                            "frame_rate": fps,
                        },
                    )
                if status in _FAILED_STATUSES:
                    body = _extract_body(last_result)
                    err = body.get("error") or body.get("message") or str(
                        last_result)[:500]
                    return error_response(
                        error=f"Agnes video task failed: {err}",
                        error_type="api_error",
                        provider=self.name,
                        model=model_id,
                        prompt=prompt,
                        aspect_ratio=aspect_ratio)

                time.sleep(poll_interval)
                poll_resp = requests.get(
                    f"{base_url}/videos/{task_id}",
                    headers={"Authorization": f"Bearer {api_key}"},
                    timeout=create_timeout)
                poll_resp.raise_for_status()
                polled = poll_resp.json()
                if isinstance(polled, dict):
                    last_result = polled

            return error_response(
                error=f"Agnes video task timed out after {poll_timeout}s",
                error_type="timeout",
                provider=self.name,
                model=model_id,
                prompt=prompt,
                aspect_ratio=aspect_ratio)
        except requests.HTTPError as exc:
            return error_response(error=_http_error(exc),
                                  error_type="api_error",
                                  provider=self.name,
                                  model=model_id,
                                  prompt=prompt,
                                  aspect_ratio=aspect_ratio)
        except requests.Timeout:
            return error_response(error="Agnes video API request timed out",
                                  error_type="timeout",
                                  provider=self.name,
                                  model=model_id,
                                  prompt=prompt,
                                  aspect_ratio=aspect_ratio)
        except Exception as exc:
            return error_response(error=f"Agnes video API error: {exc}",
                                  error_type="api_error",
                                  provider=self.name,
                                  model=model_id,
                                  prompt=prompt,
                                  aspect_ratio=aspect_ratio)


def register(ctx: Any) -> None:
    ctx.register_video_gen_provider(AgnesVideoGenProvider())
