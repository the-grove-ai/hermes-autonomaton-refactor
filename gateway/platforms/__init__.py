"""
Platform adapters for messaging integrations.

Each adapter handles:
- Receiving messages from a platform
- Sending messages/responses back
- Platform-specific authentication
- Message formatting and media handling
"""

from .base import BasePlatformAdapter, MessageEvent, SendResult

# hermes-severance-v1 T2: the QQAdapter/YuanbaoAdapter PEP 562 lazy re-exports
# were removed with the qqbot/yuanbao adapters at severance. Only the shared
# base classes remain exported; the kept adapters (telegram, api_server) are
# imported via their long-form module paths.
__all__ = [
    "BasePlatformAdapter",
    "MessageEvent",
    "SendResult",
]
