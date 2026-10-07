"""WebSocket state publisher implementation for WiSense-Twin.

Broadcasts serialized `StateMessage` payloads over standard WebSocket connections
to external consumers such as Unreal Engine 5 or monitoring dashboards.
"""

from __future__ import annotations

import json
import logging
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

import websockets.sync.server as ws_server
from websockets.exceptions import ConnectionClosed
from websockets.sync.server import Server, ServerConnection

from wisense_backend.contracts.state_message import StateMessage
from wisense_backend.contracts.websocket_publisher import WebSocketPublisher

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class WebSocketConfig:
    """Configuration settings for the WebSocket publisher server.

    Attributes:
        host: Hostname or IP address to bind to (default "127.0.0.1").
        port: TCP port number to listen on (default 8765, or 0 for ephemeral test port).
    """

    host: str = "127.0.0.1"
    port: int = 8765

    def __post_init__(self) -> None:
        """Validate configuration settings."""
        if not self.host or not isinstance(self.host, str) or not self.host.strip():
            raise ValueError(f"host must be a non-empty string, got {self.host!r}")
        if not isinstance(self.port, int) or isinstance(self.port, bool):
            raise TypeError(f"port must be an integer, got {type(self.port).__name__}")
        if self.port < 0 or self.port > 65535:
            raise ValueError(f"port must be in range [0, 65535], got {self.port}")

    def to_dict(self) -> dict[str, Any]:
        """Convert configuration to dictionary."""
        return {
            "host": self.host,
            "port": self.port,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> WebSocketConfig:
        """Construct WebSocketConfig from dictionary."""
        host = str(data.get("host", "127.0.0.1")).strip()
        port = int(data.get("port", 8765))
        return cls(host=host, port=port)

    @classmethod
    def from_json_file(cls, path: str | Path) -> WebSocketConfig:
        """Load configuration from a JSON file."""
        file_path = Path(path)
        if not file_path.is_file():
            raise FileNotFoundError(f"WebSocket config file not found: {file_path}")
        with open(file_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return cls.from_dict(data)


class WebSocketStatePublisher:
    """Production WebSocket publisher streaming versioned StateMessages to clients.

    Implements `WebSocketPublisher` protocol using synchronous websockets running
    in a dedicated background server thread.

    Key guarantees:
    - Multi-client broadcast: sends messages to all actively connected clients.
    - Zero-client safety: publish() is an immediate no-op when no clients are connected.
    - Fault tolerance: client disconnects and write errors on individual sockets are
      handled gracefully and do not disrupt streaming to surviving clients.
    - Lifecycle management: start(), stop(), and context manager support with clean
      socket termination.
    """

    def __init__(self, config: Optional[WebSocketConfig] = None) -> None:
        """Initialize WebSocketStatePublisher with configuration.

        Args:
            config: Optional WebSocketConfig. If None, default settings are used.
        """
        self._config = config or WebSocketConfig()
        self._server: Optional[Server] = None
        self._server_thread: Optional[threading.Thread] = None
        self._clients: set[ServerConnection] = set()
        self._clients_lock = threading.Lock()
        self._is_running = False
        self._actual_port: Optional[int] = None
        self._started_event = threading.Event()

    @property
    def config(self) -> WebSocketConfig:
        """Return the configuration instance."""
        return self._config

    @property
    def is_running(self) -> bool:
        """Return True if the server is actively running."""
        return self._is_running

    @property
    def client_count(self) -> int:
        """Return the number of currently connected clients."""
        with self._clients_lock:
            return len(self._clients)

    @property
    def actual_port(self) -> Optional[int]:
        """Return the bound TCP port (useful when port=0 is used in tests)."""
        return self._actual_port

    def start(self) -> None:
        """Start the WebSocket server in a background thread and begin accepting connections."""
        if self._is_running:
            logger.warning("WebSocket server is already running")
            return

        self._started_event.clear()

        # Bind the listening socket
        self._server = ws_server.serve(
            self._handle_client,
            self._config.host,
            self._config.port,
        )
        self._actual_port = self._server.socket.getsockname()[1]

        def run_server() -> None:
            self._started_event.set()
            try:
                assert self._server is not None
                self._server.serve_forever()
            except Exception as e:
                logger.debug("Server serve_forever exited: %s", e)

        self._server_thread = threading.Thread(
            target=run_server,
            daemon=True,
            name="WebSocketStatePublisherThread",
        )
        self._server_thread.start()

        # Wait until server thread enters serve_forever loop
        self._started_event.wait(timeout=5.0)
        self._is_running = True
        logger.info(
            "WebSocketStatePublisher started on ws://%s:%d",
            self._config.host,
            self._actual_port,
        )

    def stop(self) -> None:
        """Stop accepting new connections, disconnect active clients, and shut down server."""
        if not self._is_running:
            return

        self._is_running = False
        logger.info("Stopping WebSocketStatePublisher...")

        if self._server is not None:
            try:
                self._server.shutdown()
            except Exception as e:
                logger.warning("Error during WebSocket server shutdown: %s", e)

        if self._server_thread is not None and self._server_thread.is_alive():
            self._server_thread.join(timeout=3.0)

        with self._clients_lock:
            self._clients.clear()

        self._server = None
        self._server_thread = None
        self._actual_port = None
        logger.info("WebSocketStatePublisher stopped cleanly")

    def publish(self, message: StateMessage) -> None:
        """Broadcast a StateMessage to all currently connected clients.

        Safe no-op when zero clients are connected.

        Args:
            message: StateMessage to serialize and broadcast.

        Raises:
            RuntimeError: If called while the publisher is not running.
            TypeError: If message is not an instance of StateMessage.
        """
        if not self._is_running:
            raise RuntimeError(
                "Cannot publish: WebSocketStatePublisher is not running. Call start() first."
            )

        if not isinstance(message, StateMessage):
            raise TypeError(
                f"Expected StateMessage instance, got {type(message).__name__}"
            )

        with self._clients_lock:
            active_clients = list(self._clients)

        if not active_clients:
            # Safe zero-client no-op: no serialization or broadcast overhead
            return

        payload = message.to_json()

        # Broadcast payload across all active connections.
        # ws_server.broadcast skips non-open connections and isolates write errors.
        try:
            ws_server.broadcast(active_clients, payload)
        except Exception as e:
            logger.warning("Broadcast encountered exception: %s", e)

    def _handle_client(self, websocket: ServerConnection) -> None:
        """Handle an incoming client connection lifecycle.

        Registers client upon handshake, waits until client disconnects or server
        shuts down, then unregisters client.
        """
        try:
            remote_addr = str(websocket.remote_address)
        except Exception:
            remote_addr = "unknown"

        with self._clients_lock:
            self._clients.add(websocket)
            logger.info(
                "WebSocket client connected from %s (total clients: %d)",
                remote_addr,
                len(self._clients),
            )

        try:
            # Keep connection open; iterate over any incoming messages (discarding them
            # since this is a unidirectional publisher)
            for _ in websocket:
                pass
        except ConnectionClosed:
            pass
        except Exception as e:
            logger.debug("Client handler exception for %s: %s", remote_addr, e)
        finally:
            with self._clients_lock:
                self._clients.discard(websocket)
                logger.info(
                    "WebSocket client disconnected from %s (remaining clients: %d)",
                    remote_addr,
                    len(self._clients),
                )

    def __enter__(self) -> WebSocketStatePublisher:
        """Enter context manager, starting the publisher."""
        self.start()
        return self

    def __exit__(self, exc_type: object, exc_val: object, exc_tb: object) -> None:
        """Exit context manager, stopping the publisher."""
        self.stop()
