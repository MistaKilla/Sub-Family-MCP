"""Model provider adapters. Currently every supported backend speaks the
OpenAI Chat Completions dialect; the protocol leaves room for native adapters.
"""

from sub_family.providers.base import ChatResult, ProviderHealth, Usage
from sub_family.providers.openai_compat import OpenAICompatProvider

__all__ = ["ChatResult", "OpenAICompatProvider", "ProviderHealth", "Usage"]
