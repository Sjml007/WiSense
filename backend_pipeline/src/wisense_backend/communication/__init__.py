"""Communication package for WiSense-Twin.

Provides network transport and publishing layers (e.g. WebSocket publishers)
for external twin integration.
"""

from wisense_backend.communication.websocket_server import (
    WebSocketConfig,
    WebSocketStatePublisher,
)

__all__ = [
    "WebSocketConfig",
    "WebSocketStatePublisher",
]
