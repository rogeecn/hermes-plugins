## Hermes Plugins

This repo holds user plugins for [Hermes Agent](https://github.com/NousResearch/hermes-agent), synced from `~/.hermes/plugins/` via `./sync.sh`.

## Plugins

| Plugin | Kind | Description |
|---|---|---|
| `agnes-ai/image_gen` | backend | Agnes AI image generation (text-to-image, image-to-image, multi-image composition) |
| `agnes-ai/video_gen` | backend | Agnes AI video generation (text-to-video, image-to-video, keyframe animation) |
| `model-providers/cpa` | model-provider | CPA OpenAI-compatible endpoint (auto-discovered, no `plugins.enabled` entry needed) |

## Setup

### 1. Environment Variables

```bash
cp .env.example .env
# Edit .env and fill in your API keys
```

Required variables:

| Variable | Plugin | Purpose |
|---|---|---|
| `AGNES_API_KEY` | agnes-ai/image_gen, agnes-ai/video_gen | Agnes AI Bearer token |
| `AGNES_BASE_URL` | agnes-ai/* (optional) | Override base URL (default: `https://apihub.agnes-ai.com/v1`) |
| `CPA_API_KEY` | model-providers/cpa | CPA API key |
| `CPA_BASE_URL` | model-providers/cpa (optional) | Override base URL |
| `HERMES_ENABLE_PROJECT_PLUGINS` | all | Set to `1` if using project-level plugins |

### 2. Config

Copy relevant sections from [`config.example.yaml`](./config.example.yaml) into your `~/.hermes/config.yaml`.

Key sections:

```yaml
plugins:
  enabled:
    - agnes-ai/image_gen
    - agnes-ai/video_gen

model:
  default: glm-5.2
  provider: cpa

image_gen:
  provider: agnes
  model: agnes-image-2.0-flash

video_gen:
  provider: agnes
  model: agnes-video-v2.0
```

### 3. Sync

```bash
./sync.sh          # sync all plugins from ~/.hermes/plugins/
./sync.sh agnes-ai # sync a single plugin
./sync.sh --dry    # preview changes
```

### 4. Restart

```bash
hermes gateway restart   # or /restart in your messaging platform
```

## Available Models

### CPA Provider

| Model | Type |
|---|---|
| `glm-5.2` | Chat (main) |
| `deepseek-v4-flash` | Chat (fast, for auxiliary tasks) |
| `deepseek-v4-pro` | Chat (heavy reasoning) |
| `qwen3.5-122b-a10b` | Vision / multimodal |
| `qwen3.5-35b-a3b` | Chat (lightweight) |
| `qwen3.7-max` | Chat (max) |

### Agnes AI

| Model | Type |
|---|---|
| `agnes-image-2.0-flash` | Image generation |
| `agnes-image-2.1-flash` | Image generation (latest) |
| `agnes-video-v2.0` | Video generation (async) |
