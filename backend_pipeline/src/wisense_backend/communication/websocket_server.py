"""WebSocket state publisher implementation for WiSense-Twin.

Broadcasts serialized `StateMessage` payloads or raw text payloads over standard
WebSocket connections to external consumers such as Unreal Engine 5 or monitoring dashboards.
"""

from __future__ import annotations

import json
import logging
import socket
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable, Optional

import websockets.sync.server as ws_server
from websockets.exceptions import ConnectionClosed
from websockets.sync.server import Server, ServerConnection

from wisense_backend.contracts.state_message import StateMessage
from wisense_backend.contracts.websocket_publisher import WebSocketPublisher

logger = logging.getLogger(__name__)

ALLOWED_MESSAGE_FORMATS = frozenset({"state_message_v1", "legacy_zone"})
DEFAULT_MESSAGE_FORMAT = "state_message_v1"
DEFAULT_RESYNC_INTERVAL_SECONDS = 5.0


@dataclass(frozen=True, slots=True)
class WebSocketConfig:
    """Configuration settings for the WebSocket publisher server.

    Attributes:
        host: Hostname or IP address to bind to (default "127.0.0.1").
        port: TCP port number to listen on (default 8765, or 0 for ephemeral test port).
        message_format: Wire protocol output format ("state_message_v1" or "legacy_zone",
            default "state_message_v1"). Consumed by the application wiring layer
            (Section 21) or demo scripts, not by the low-level publisher.
        legacy_resync_interval_seconds: Snapshot re-synchronization interval in seconds for
            legacy zone output (default 5.0, >= 0.0). Consumed by the application wiring
            layer (Section 21) or demo scripts.
    """

    host: str = "127.0.0.1"
    port: int = 8765
    message_format: str = DEFAULT_MESSAGE_FORMAT
    legacy_resync_interval_seconds: float = DEFAULT_RESYNC_INTERVAL_SECONDS

    def __post_init__(self) -> None:
        """Validate configuration settings."""
        if not self.host or not isinstance(self.host, str) or not self.host.strip():
            raise ValueError(f"host must be a non-empty string, got {self.host!r}")
        if not isinstance(self.port, int) or isinstance(self.port, bool):
            raise TypeError(f"port must be an integer, got {type(self.port).__name__}")
        if self.port < 0 or self.port > 65535:
            raise ValueError(f"port must be in range [0, 65535], got {self.port}")
        if not isinstance(self.message_format, str) or self.message_format not in ALLOWED_MESSAGE_FORMATS:
            raise ValueError(
                f"message_format must be one of {sorted(ALLOWED_MESSAGE_FORMATS)}, got {self.message_format!r}"
            )
        if isinstance(self.legacy_resync_interval_seconds, bool) or not isinstance(
            self.legacy_resync_interval_seconds, (int, float)
        ):
            raise TypeError(
                f"legacy_resync_interval_seconds must be a number, got {type(self.legacy_resync_interval_seconds).__name__}"
            )
        if self.legacy_resync_interval_seconds < 0.0:
            raise ValueError(
                f"legacy_resync_interval_seconds must be >= 0, got {self.legacy_resync_interval_seconds}"
            )

    def to_dict(self, include_defaults: bool = False) -> dict[str, Any]:
        """Convert configuration to dictionary.

        Args:
            include_defaults: If True, include all fields. If False (default),
                preserves backward compatibility by omitting message_format and
                legacy_resync_interval_seconds when they match their defaults.
        """
        res: dict[str, Any] = {
            "host": self.host,
            "port": self.port,
        }
        if include_defaults or self.message_format != DEFAULT_MESSAGE_FORMAT:
            res["message_format"] = self.message_format
        if include_defaults or self.legacy_resync_interval_seconds != DEFAULT_RESYNC_INTERVAL_SECONDS:
            res["legacy_resync_interval_seconds"] = self.legacy_resync_interval_seconds
        return res

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> WebSocketConfig:
        """Construct WebSocketConfig from dictionary."""
        host = str(data.get("host", "127.0.0.1")).strip()
        port = int(data.get("port", 8765))
        message_format = str(data.get("message_format", DEFAULT_MESSAGE_FORMAT)).strip()
        legacy_resync_interval_seconds = float(
            data.get("legacy_resync_interval_seconds", DEFAULT_RESYNC_INTERVAL_SECONDS)
        )
        return cls(
            host=host,
            port=port,
            message_format=message_format,
            legacy_resync_interval_seconds=legacy_resync_interval_seconds,
        )

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
    - Raw text streaming: concrete publish_text() for legacy adapters or arbitrary text.
    - On-connect hook: settable callback delivering initial frames to newly registered clients.
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
        self._connect_hook: Optional[Callable[[], Iterable[str] | str]] = None
        self._connect_lock: Optional[threading.Lock | threading.RLock] = None

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

    @property
    def connect_lock(self) -> Optional[threading.Lock | threading.RLock]:
        """Return the lock used to synchronize client connection and initial frame delivery."""
        return self._connect_lock

    @connect_lock.setter
    def connect_lock(self, lock: Optional[threading.Lock | threading.RLock]) -> None:
        """Set the lock used to synchronize client connection and initial frame delivery."""
        self._connect_lock = lock

    def set_connect_lock(self, lock: Optional[threading.Lock | threading.RLock]) -> None:
        """Set the lock used to synchronize client connection and initial frame delivery."""
        self._connect_lock = lock

    @property
    def connect_hook(self) -> Optional[Callable[[], Iterable[str] | str]]:
        """Return the registered on-connect hook."""
        return self._connect_hook

    @connect_hook.setter
    def connect_hook(self, hook: Optional[Callable[[], Iterable[str] | str]]) -> None:
        """Set or update the on-connect hook."""
        self._connect_hook = hook

    def set_connect_hook(
        self,
        hook: Optional[Callable[[], Iterable[str] | str]],
        lock: Optional[threading.Lock | threading.RLock] = None,
    ) -> None:
        """Register an on-connect hook callable returning payloads to send to newly connected clients.

        Args:
            hook: Callable returning text payloads to deliver to newly connected client.
            lock: Optional synchronization lock (e.g. from LegacyZoneBridge) to acquire
                across snapshot delivery and client registration.
        """
        self._connect_hook = hook
        if lock is not None:
            self._connect_lock = lock

    def start(self) -> None:
        """Start the WebSocket server in a background thread and begin accepting connections."""
        if self._is_running:
            logger.warning("WebSocket server is already running")
            return

        self._started_event.clear()

        # Bind the listening socket with SO_REUSEADDR
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind((self._config.host, self._config.port))
        sock.listen()

        self._server = ws_server.serve(
            self._handle_client,
            sock=sock,
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

    def publish_text(self, payload: str) -> None:
        """Broadcast an arbitrary text payload to all currently connected clients.

        Safe no-op when zero clients are connected. Write errors on individual
        sockets are caught and isolated so surviving clients remain uninterrupted.

        Args:
            payload: String payload to broadcast.

        Raises:
            RuntimeError: If called while the publisher is not running.
            TypeError: If payload is not a string.
        """
        if not self._is_running:
            raise RuntimeError(
                "Cannot publish: WebSocketStatePublisher is not running. Call start() first."
            )

        if not isinstance(payload, str):
            raise TypeError(
                f"Expected str payload, got {type(payload).__name__}"
            )

        with self._clients_lock:
            active_clients = list(self._clients)

        if not active_clients:
            # Safe zero-client no-op: no broadcast overhead
            return

        for client in active_clients:
            try:
                client.send(payload)
            except Exception as e:
                logger.debug("Broadcast encountered exception: %s", e)

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
        for client in active_clients:
            try:
                client.send(payload)
            except Exception as e:
                logger.debug("Broadcast send exception: %s", e)

    def _handle_client(self, websocket: ServerConnection) -> None:
        """Handle an incoming client connection lifecycle.

        Delivers on-connect hook payloads (if configured) and registers client
        for broadcasts as one critical section under connect_lock (if configured).
        This guarantees that initial snapshot frames precede any broadcast messages,
        and that lock ordering is identical across paths (bridge lock then clients lock).
        """
        try:
            remote_addr = str(websocket.remote_address)
        except Exception:
            remote_addr = "unknown"

        lock = self._connect_lock
        acquire_lock = lock is not None

        if acquire_lock:
            assert lock is not None
            lock.acquire()

        try:
            # 1. Deliver on-connect hook payloads to the new client BEFORE it is in the broadcast set
            hook = self._connect_hook
            if hook is not None:
                try:
                    initial_payloads = hook()
                    if isinstance(initial_payloads, str):
                        initial_payloads = [initial_payloads]
                    if initial_payloads is not None:
                        for p in initial_payloads:
                            try:
                                websocket.send(p)
                            except Exception as e:
                                logger.warning(
                                    "Error sending connect hook payload to %s: %s",
                                    remote_addr,
                                    e,
                                )
                                break
                except Exception as e:
                    logger.warning("Connect hook raised exception for %s: %s", remote_addr, e)

            # 2. Register client in broadcast set under clients_lock
            with self._clients_lock:
                self._clients.add(websocket)
                logger.info(
                    "WebSocket client connected from %s (total clients: %d)",
                    remote_addr,
                    len(self._clients),
                )
        finally:
            if acquire_lock:
                assert lock is not None
                lock.release()

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
