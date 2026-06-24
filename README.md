## Hermes Plugins

### CPA Model Provider
```yaml
model:
  default: Hermes
  provider: cpa
providers:
  cpa: {}
```


### Agnes AI Video Image Providers

```yaml
platform_toolsets:
  cli:
  - image_gen
  - video_gen
  feishu:
  - image_gen
  - video_gen
# ...
plugins:
  enabled:
  - agnes-ai/image_gen
  - agnes-ai/video_gen
video_gen:
  provider: agnes-ai
  use_gateway: false
  model: agnes-video-v2.0
image_gen:
  provider: agnes-ai
  use_gateway: false
  model: agnes-image-2.1-flash
```

```env
HERMES_ENABLE_PROJECT_PLUGINS=1
AGNES_AI_API_KEY=sk-your-agnes-ai-api-key
```
