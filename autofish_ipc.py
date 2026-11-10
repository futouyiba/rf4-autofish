"""Local loopback IPC for the autofish runtime.

The server only listens on 127.0.0.1 (random port), authenticates every request
with a per-session random token, and never performs game actions itself.  Its
only side effects are updating :class:`autofish_control.ControlSession` and
queueing commands for the main thread.

Wire format: one UTF-8 JSON object per line, one request per TCP connection.
Both requests and responses are limited to 64 KiB.
"""

from __future__ import annotations

import copy
import hmac
import json
import math
import os
import secrets
import socket
import tempfile
import threading
import time
from collections import OrderedDict
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from autofish_control import COMMAND_QUEUE_MAX, ControlSession
from autofish_snapshot import SnapshotStore

PROTOCOL_VERSION = 1
MAX_PACKET_BYTES = 64 * 1024
DEFAULT_ENDPOINT_PATH = Path(__file__).resolve().parent / "autofish.endpoint.json"
LOOPBACK_HOST = "127.0.0.1"
COMMANDS = frozenset(
    {
        "stop",
        "stop_after_fish",
        "resume",
        "observe",
        "confirm_ready",
        "confirm_net_retracted",
    }
)
_ACK_STATUSES = frozenset({"accepted", "executed", "rejected"})
_DEDUP_MAX = 1024
# The UI polls ``status`` and resolves pending commands from the final ack.
# Only a bounded, non-sensitive projection is exposed (no token/session/payload).
STATUS_ACK_LIMIT = 64
STATUS_ACK_ID_MAX = 128
STATUS_ACK_MESSAGE_MAX = 512
_STATUS_PACKET_MARGIN = 256


class _PacketTooLarge(Exception):
    pass


def _json_bytes(obj: Any) -> bytes:
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def _atomic_write_json(path: Path, payload: Dict[str, Any]) -> None:
    """Write JSON with an fsync + atomic replace in the destination directory."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(
        dir=str(path.parent), prefix=path.name + ".", suffix=".tmp"
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(payload, handle, ensure_ascii=False, separators=(",", ":"))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, path)
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
    finally:
        try:
            if os.path.exists(temp_name):
                os.unlink(temp_name)
        except OSError:
            pass


class RuntimeServer:
    """Authenticated loopback command/snapshot endpoint."""

    def __init__(
        self,
        control: ControlSession,
        endpoint_path: Optional[Path] = None,
        *,
        connection_timeout: float = 1.0,
    ) -> None:
        self._control = control
        self._endpoint_path = Path(endpoint_path or DEFAULT_ENDPOINT_PATH)
        self._session = control.session_id
        self._token = secrets.token_hex(32)
        self._connection_timeout = max(0.05, min(float(connection_timeout), 1.0))

        self._start_lock = threading.Lock()
        self._close_lock = threading.Lock()
        self._state_lock = threading.RLock()
        self._snapshot_store = SnapshotStore()
        self._acks: "OrderedDict[str, Dict[str, Any]]" = OrderedDict()

        self._closing = threading.Event()
        self._closed = False
        self._started = False
        self._endpoint_written = False
        self._sock: Optional[socket.socket] = None
        self._port: Optional[int] = None
        self._accept_thread: Optional[threading.Thread] = None
        self._handlers: set[threading.Thread] = set()
        self._conns: set[socket.socket] = set()
        self._conn_seq = 0

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------
    def start(self) -> "RuntimeServer":
        with self._start_lock:
            if self._started:
                return self
            if self._closed:
                raise RuntimeError("server already closed")

            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                sock.bind((LOOPBACK_HOST, 0))
                sock.listen(16)
                sock.settimeout(0.2)
            except OSError:
                sock.close()
                raise

            self._sock = sock
            self._port = int(sock.getsockname()[1])
            self._closing.clear()
            try:
                self._write_endpoint()
                self._endpoint_written = True
            except BaseException:
                try:
                    sock.close()
                finally:
                    self._sock = None
                    self._port = None
                raise

            thread = threading.Thread(
                target=self._accept_loop,
                name="autofish-ipc-accept-%s" % self._session[:8],
                daemon=True,
            )
            self._accept_thread = thread
            self._started = True
            thread.start()
            return self

    def close(self) -> None:
        """Stop the listener and remove the endpoint only if it is ours.

        Joining is bounded; handler threads are daemons and their sockets are
        closed here so they cannot keep the process alive.
        """
        with self._close_lock:
            if self._closed:
                return
            self._closed = True
            self._closing.set()

            sock = self._sock
            self._sock = None
            if sock is not None:
                self._close_socket(sock)

            with self._state_lock:
                conns = list(self._conns)
            for conn in conns:
                self._close_socket(conn)

            deadline = time.monotonic() + 2.0
            accept_thread = self._accept_thread
            if (
                accept_thread is not None
                and accept_thread.is_alive()
                and accept_thread is not threading.current_thread()
            ):
                accept_thread.join(max(0.0, deadline - time.monotonic()))

            # Accept may have queued one last handler between the closing flag
            # and the join above; loop until dead or the deadline expires.
            while time.monotonic() < deadline:
                with self._state_lock:
                    alive = [
                        thread
                        for thread in self._handlers
                        if thread.is_alive()
                        and thread is not threading.current_thread()
                    ]
                if not alive:
                    break
                alive[0].join(max(0.0, min(0.1, deadline - time.monotonic())))

            self._started = False
            if self._endpoint_written:
                self._remove_endpoint_if_ours()

    def __enter__(self) -> "RuntimeServer":
        return self.start()

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        self.close()

    @property
    def session_id(self) -> str:
        return self._session

    @property
    def port(self) -> Optional[int]:
        return self._port

    @property
    def endpoint_path(self) -> Path:
        return self._endpoint_path

    def _close_socket(self, sock: socket.socket) -> None:
        try:
            sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            sock.close()
        except OSError:
            pass

    # ------------------------------------------------------------------
    # Endpoint file
    # ------------------------------------------------------------------
    def _write_endpoint(self) -> None:
        payload = {
            "protocol": PROTOCOL_VERSION,
            "version": PROTOCOL_VERSION,
            "session": self._session,
            "host": LOOPBACK_HOST,
            "port": self._port,
            "token": self._token,
            "pid": os.getpid(),
            "created_at": time.time(),
        }
        _atomic_write_json(self._endpoint_path, payload)

    def _remove_endpoint_if_ours(self) -> None:
        try:
            with open(self._endpoint_path, "r", encoding="utf-8") as handle:
                data = json.load(handle)
        except (OSError, ValueError, TypeError):
            return
        if not isinstance(data, dict):
            return
        if data.get("session") != self._session:
            return
        # A new server normally has a new session.  If a caller deliberately
        # reuses the session id, do not delete the newer server's endpoint.
        token = data.get("token")
        if token is not None and token != self._token:
            return
        try:
            os.unlink(self._endpoint_path)
        except OSError:
            pass

    # ------------------------------------------------------------------
    # Snapshot and command APIs used by the main thread
    # ------------------------------------------------------------------
    def publish(self, snapshot_dict: Dict[str, Any]) -> None:
        """Publish a snapshot; both write and read are deep copies."""
        self._snapshot_store.publish(snapshot_dict)

    def snapshot(self) -> Dict[str, Any]:
        return self._snapshot_store.snapshot()

    def drain_commands(self) -> List[Dict[str, Any]]:
        """Return queued commands (deep copies), oldest first."""
        return self._control.consume_commands()

    def acknowledge(self, command_id: str, status: str, message: str = "") -> bool:
        """Update the stored acknowledgement for a previously seen command."""
        if status not in _ACK_STATUSES:
            raise ValueError("invalid ack status: %r" % (status,))
        if not isinstance(command_id, str) or not command_id:
            return False
        with self._state_lock:
            ack = self._acks.get(command_id)
            if ack is None:
                return False
            ack["status"] = status
            ack["message"] = str(message or "")[:STATUS_ACK_MESSAGE_MAX]
            ack["ok"] = status != "rejected"
            self._acks.move_to_end(command_id)
            return True

    # ------------------------------------------------------------------
    # Listener loop
    # ------------------------------------------------------------------
    def _accept_loop(self) -> None:
        sock = self._sock
        while not self._closing.is_set() and sock is not None:
            try:
                conn, _addr = sock.accept()
            except socket.timeout:
                continue
            except OSError:
                break

            if self._closing.is_set():
                self._close_socket(conn)
                break

            with self._state_lock:
                self._conns.add(conn)
                self._conn_seq += 1
                seq = self._conn_seq
                thread = threading.Thread(
                    target=self._serve_connection,
                    args=(conn,),
                    name="autofish-ipc-conn-%s-%d" % (self._session[:8], seq),
                    daemon=True,
                )
                self._handlers.add(thread)
            try:
                thread.start()
            except RuntimeError:
                with self._state_lock:
                    self._handlers.discard(thread)
                    self._conns.discard(conn)
                self._close_socket(conn)

    def _serve_connection(self, conn: socket.socket) -> None:
        try:
            if self._closing.is_set():
                return
            conn.settimeout(self._connection_timeout)
            line, error = self._read_packet(conn)
            if line is None:
                if error:
                    self._safe_send(conn, self._reject(None, error))
                return

            try:
                response = self._handle_packet(line)
            except Exception:
                # Never let a malformed request kill the server thread or leak
                # packet contents (which may include the token) to stderr.
                response = self._reject(None, "internal error")
            if response is None:
                return
            try:
                self._send_json(conn, response)
            except _PacketTooLarge:
                self._send_json(
                    conn,
                    {
                        "ok": False,
                        "id": None,
                        "status": "rejected",
                        "message": "response too large",
                        "session": self._session,
                        "protocol": PROTOCOL_VERSION,
                        "version": PROTOCOL_VERSION,
                    },
                )
            except (TypeError, ValueError):
                self._safe_send(
                    conn,
                    {
                        "ok": False,
                        "id": None,
                        "status": "rejected",
                        "message": "snapshot not serializable",
                        "session": self._session,
                        "protocol": PROTOCOL_VERSION,
                        "version": PROTOCOL_VERSION,
                    },
                )
        except OSError:
            pass
        finally:
            with self._state_lock:
                self._conns.discard(conn)
                self._handlers.discard(threading.current_thread())
            self._close_socket(conn)

    def _read_packet(self, conn: socket.socket) -> Tuple[Optional[bytes], Optional[str]]:
        buffer = bytearray()
        while True:
            newline = buffer.find(b"\n")
            if newline >= 0:
                if newline > MAX_PACKET_BYTES:
                    return None, "packet too large"
                return bytes(buffer[:newline]), None
            if len(buffer) > MAX_PACKET_BYTES:
                return None, "packet too large"
            try:
                chunk = conn.recv(8192)
            except socket.timeout:
                return None, "timeout"
            except OSError:
                return None, None
            if not chunk:
                if not buffer:
                    return None, None
                # Be lenient with clients that close without a trailing LF.
                return bytes(buffer), None
            buffer.extend(chunk)

    def _send_json(self, conn: socket.socket, obj: Dict[str, Any]) -> None:
        payload = _json_bytes(obj)
        if len(payload) > MAX_PACKET_BYTES:
            raise _PacketTooLarge()
        conn.sendall(payload + b"\n")

    def _safe_send(self, conn: socket.socket, obj: Dict[str, Any]) -> None:
        try:
            self._send_json(conn, obj)
        except (OSError, _PacketTooLarge):
            pass

    # ------------------------------------------------------------------
    # Request handling
    # ------------------------------------------------------------------
    def _handle_packet(self, line: bytes) -> Dict[str, Any]:
        try:
            request = json.loads(line.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            return self._reject(None, "bad json")
        if not isinstance(request, dict):
            return self._reject(None, "bad request")

        command_id = request.get("id")
        if command_id is None:
            command_id = request.get("command_id")
        if not isinstance(command_id, str):
            command_id = None

        protocol = request.get("protocol")
        if protocol is None:
            protocol = request.get("version")
        if protocol != PROTOCOL_VERSION:
            return self._reject(command_id, "bad protocol")

        session = request.get("session")
        if not isinstance(session, str) or not hmac.compare_digest(
            session.encode("utf-8", "ignore"), self._session.encode("utf-8")
        ):
            return self._reject(command_id, "bad session")

        token = request.get("token")
        if not isinstance(token, str) or not hmac.compare_digest(
            token.encode("utf-8", "ignore"), self._token.encode("utf-8")
        ):
            return self._reject(command_id, "unauthorized")

        expires_at = request.get("expires_at")
        if not self._valid_expiry(expires_at):
            return self._reject(command_id, "expired")

        name = request.get("name")
        if name == "status":
            return self._status_response()

        if not isinstance(name, str) or name not in COMMANDS:
            return self._reject(command_id, "unknown command")

        if not command_id or len(command_id) > 128:
            return self._reject(None, "bad command id")

        payload = request.get("payload", {})
        if payload is None:
            payload = {}
        if not isinstance(payload, dict):
            return self._reject(command_id, "bad payload")

        # Deduplicate after authentication so unauthenticated clients cannot
        # fill the bounded cache.  A replay returns the last known ack.
        with self._state_lock:
            stored = self._acks.get(command_id)
            if stored is not None:
                return copy.deepcopy(stored)

        command = {
            "id": command_id,
            "name": name,
            "payload": copy.deepcopy(payload),
            "session": self._session,
            "expires_at": expires_at,
            "received_at": time.monotonic(),
        }

        if name == "stop":
            reason = payload.get("reason")
            if not isinstance(reason, str):
                reason = "ipc stop"
            reason = reason[:200]
            self._control.request_stop(reason)
            # Best effort: the stop event is authoritative, so a full queue must
            # not delay or reject a stop.
            self._control.enqueue_command(command)
            return self._remember_ack(self._accepted(command_id))

        if name == "stop_after_fish":
            self._control.request_stop_after_fish()
            self._control.enqueue_command(command)
            return self._remember_ack(self._accepted(command_id))

        if self._control.stopped:
            return self._remember_ack(
                self._rejected(command_id, "session stopped")
            )

        if name == "resume":
            if not self._control.enqueue_command(command):
                return self._remember_ack(
                    self._rejected(command_id, "queue full")
                )
            self._control.resume()
            return self._remember_ack(self._accepted(command_id))

        if not self._control.enqueue_command(command):
            return self._remember_ack(self._rejected(command_id, "queue full"))

        return self._remember_ack(self._accepted(command_id))

    def _valid_expiry(self, expires_at: Any) -> bool:
        if isinstance(expires_at, bool) or not isinstance(expires_at, (int, float)):
            return False
        value = float(expires_at)
        if not math.isfinite(value):
            return False
        return time.monotonic() <= value

    def _status_response(self) -> Dict[str, Any]:
        snapshot = self.snapshot()
        acks = self._status_acks()
        response: Dict[str, Any] = {
            "ok": True,
            "protocol": PROTOCOL_VERSION,
            "version": PROTOCOL_VERSION,
            "session": self._session,
            "snapshot": snapshot,
            "acks": acks,
            "stopped": bool(self._control.stopped),
            "stop_after_fish": bool(self._control.stop_after_fish),
            "server_time": time.monotonic(),
        }
        # Keep the whole status packet below the wire limit.  The snapshot is
        # owned by the runtime and may be large, so drop the oldest acks first
        # rather than making the UI lose the entire status response.
        budget = MAX_PACKET_BYTES - _STATUS_PACKET_MARGIN
        try:
            while acks and len(_json_bytes(response)) > budget:
                acks.pop(0)
        except (TypeError, ValueError):
            # Non-serializable snapshot: the sender reports it as before.
            pass
        return response

    def _status_acks(self) -> List[Dict[str, Any]]:
        """Copy the newest acks under the lock, stripped to UI-safe fields."""
        with self._state_lock:
            recent = list(self._acks.items())[-STATUS_ACK_LIMIT:]
            return [
                {
                    "id": str(command_id)[:STATUS_ACK_ID_MAX],
                    "status": str(ack.get("status") or "accepted"),
                    "message": str(ack.get("message") or "")[
                        :STATUS_ACK_MESSAGE_MAX
                    ],
                }
                for command_id, ack in recent
            ]

    def _accepted(self, command_id: str) -> Dict[str, Any]:
        return {
            "ok": True,
            "protocol": PROTOCOL_VERSION,
            "version": PROTOCOL_VERSION,
            "id": command_id,
            "status": "accepted",
            "message": "",
            "session": self._session,
        }

    def _rejected(self, command_id: Optional[str], message: str) -> Dict[str, Any]:
        return {
            "ok": False,
            "protocol": PROTOCOL_VERSION,
            "version": PROTOCOL_VERSION,
            "id": command_id,
            "status": "rejected",
            "message": message,
            "session": self._session,
        }

    def _reject(self, command_id: Optional[str], message: str) -> Dict[str, Any]:
        return self._rejected(command_id, message)

    def _remember_ack(self, ack: Dict[str, Any]) -> Dict[str, Any]:
        command_id = ack.get("id")
        if isinstance(command_id, str) and command_id:
            with self._state_lock:
                self._acks[command_id] = copy.deepcopy(ack)
                self._acks.move_to_end(command_id)
                while len(self._acks) > _DEDUP_MAX:
                    self._acks.popitem(last=False)
        return ack


class RuntimeClient:
    """Small client for :class:`RuntimeServer`.

    The endpoint file is read for every request so a client automatically picks
    up a new session/token after the server restarts.  Each request uses a fresh
    TCP connection and a single newline-terminated JSON packet.
    """

    def __init__(
        self,
        endpoint_path: Optional[Path] = None,
        *,
        timeout: float = 1.0,
        ttl: float = 5.0,
    ) -> None:
        self._endpoint_path = Path(endpoint_path or DEFAULT_ENDPOINT_PATH)
        self._timeout = max(0.05, min(float(timeout), 1.0))
        self._ttl = max(0.01, float(ttl))

    def status(self) -> Dict[str, Any]:
        endpoint, error = self._load_endpoint()
        if error:
            return self._status_error(error)
        response, error = self._request(endpoint, "status", None, {})
        if error:
            return self._status_error(error)
        if not isinstance(response, dict):
            return self._status_error("bad response")
        response.setdefault("ok", True)
        response.setdefault("session", endpoint["session"])
        response.setdefault("snapshot", None)
        return response

    def command(self, name: str, **payload: Any) -> Dict[str, Any]:
        command_id = secrets.token_hex(8)
        endpoint, error = self._load_endpoint()
        if error:
            return self._ack_error(command_id, error)
        response, error = self._request(endpoint, name, command_id, payload)
        if error:
            return self._ack_error(command_id, error)
        if not isinstance(response, dict):
            return self._ack_error(command_id, "bad response")
        response.setdefault("id", command_id)
        response.setdefault("session", endpoint["session"])
        if "status" not in response:
            response["status"] = "accepted" if response.get("ok") else "rejected"
        response.setdefault("ok", response.get("status") != "rejected")
        response.setdefault("message", "")
        return response

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    def _load_endpoint(self) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
        try:
            with open(self._endpoint_path, "r", encoding="utf-8") as handle:
                data = json.load(handle)
        except (OSError, ValueError, TypeError):
            return None, "endpoint unavailable"
        if not isinstance(data, dict):
            return None, "bad endpoint"
        version = data.get("protocol")
        if version is None:
            version = data.get("version")
        if version != PROTOCOL_VERSION:
            return None, "bad protocol"
        host = data.get("host")
        port = data.get("port")
        token = data.get("token")
        session = data.get("session")
        if not isinstance(host, str) or not host:
            return None, "bad endpoint"
        if isinstance(port, bool) or not isinstance(port, int) or not (0 < port < 65536):
            return None, "bad endpoint"
        if not isinstance(token, str) or not token:
            return None, "bad endpoint"
        if not isinstance(session, str) or not session:
            return None, "bad endpoint"
        return (
            {"host": host, "port": port, "token": token, "session": session},
            None,
        )

    def _request(
        self,
        endpoint: Dict[str, Any],
        name: str,
        command_id: Optional[str],
        payload: Dict[str, Any],
    ) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
        request: Dict[str, Any] = {
            "protocol": PROTOCOL_VERSION,
            "version": PROTOCOL_VERSION,
            "session": endpoint["session"],
            "token": endpoint["token"],
            "name": name,
            "payload": payload,
            "expires_at": time.monotonic() + self._ttl,
        }
        if command_id is not None:
            request["id"] = command_id

        try:
            packet = _json_bytes(request)
        except (TypeError, ValueError):
            return None, "bad payload"
        if len(packet) > MAX_PACKET_BYTES:
            return None, "packet too large"
        try:
            with socket.create_connection(
                (endpoint["host"], endpoint["port"]), timeout=self._timeout
            ) as sock:
                sock.settimeout(self._timeout)
                sock.sendall(packet + b"\n")
                line, read_error = self._read_response(sock)
        except OSError:
            return None, "transport error"

        if line is None:
            return None, read_error or "empty response"
        try:
            response = json.loads(line.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            return None, "bad response"
        if not isinstance(response, dict):
            return None, "bad response"
        return response, None

    def _read_response(
        self, sock: socket.socket
    ) -> Tuple[Optional[bytes], Optional[str]]:
        buffer = bytearray()
        while True:
            newline = buffer.find(b"\n")
            if newline >= 0:
                if newline > MAX_PACKET_BYTES:
                    return None, "packet too large"
                return bytes(buffer[:newline]), None
            if len(buffer) > MAX_PACKET_BYTES:
                return None, "packet too large"
            try:
                chunk = sock.recv(8192)
            except socket.timeout:
                return None, "timeout"
            except OSError:
                return None, "transport error"
            if not chunk:
                if not buffer:
                    return None, "empty response"
                return bytes(buffer), None
            buffer.extend(chunk)

    def _ack_error(self, command_id: str, message: str) -> Dict[str, Any]:
        return {
            "ok": False,
            "protocol": PROTOCOL_VERSION,
            "version": PROTOCOL_VERSION,
            "id": command_id,
            "status": "rejected",
            "message": message,
            "session": None,
        }

    def _status_error(self, message: str) -> Dict[str, Any]:
        return {
            "ok": False,
            "protocol": PROTOCOL_VERSION,
            "version": PROTOCOL_VERSION,
            "session": None,
            "snapshot": None,
            "status": "rejected",
            "message": message,
        }


__all__ = [
    "ControlSession",
    "RuntimeServer",
    "RuntimeClient",
    "PROTOCOL_VERSION",
    "MAX_PACKET_BYTES",
    "DEFAULT_ENDPOINT_PATH",
    "COMMAND_QUEUE_MAX",
    "STATUS_ACK_LIMIT",
    "STATUS_ACK_MESSAGE_MAX",
]
