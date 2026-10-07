"""WebSocket publisher contract for WiSense-Twin.

Defines the `WebSocketPublisher` Protocol, establishing the interface for streaming
versioned `StateMessage` payloads over WebSocket connections to external consumers
(Unreal Engine 5 digital twin, web monitoring dashboards, diagnostic tools).
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from wisense_backend.contracts.state_message import StateMessage


@runtime_checkable
class WebSocketPublisher(Protocol):
    """Protocol defining the interface for WebSocket state message publishers."""

    def start(self) -> None:
        """Start the WebSocket server in the background and begin accepting connections."""
        ...

    def stop(self) -> None:
        """Stop accepting new connections, close existing client connections, and shut down the server."""
        ...

    def publish(self, message: StateMessage) -> None:
        """Broadcast a StateMessage to all currently connected clients.

        Must be a safe no-op when zero clients are connected.

        Args:
            message: The StateMessage to serialize and broadcast.
        """
        ...

    @property
    def client_count(self) -> int:
        """Return the number of currently connected clients."""
        ...

    def __enter__(self) -> WebSocketPublisher:
        """Enter context manager, starting the publisher."""
        ...

    def __exit__(self, exc_type: object, exc_val: object, exc_tb: object) -> None:
        """Exit context manager, stopping the publisher."""
        ...
