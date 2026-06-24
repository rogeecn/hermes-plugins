"""CPA OpenAI-compatible provider profile."""

from providers import register_provider
from providers.base import ProviderProfile


cpa = ProviderProfile(
    name="cpa",
    aliases=("ipao-cpa",),
    display_name="CPA",
    description="CPA OpenAI-compatible endpoint",
    env_vars=("CPA_API_KEY", "CPA_BASE_URL"),
    base_url="https://cpa.ipao.vip/v1",
    auth_type="api_key",
    fallback_models=("Hermes",),
)

register_provider(cpa)
