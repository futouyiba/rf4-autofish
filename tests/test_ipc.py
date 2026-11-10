"""Loopback IPC tests for autofish_ipc.

The tests use real 127.0.0.1 sockets but never touch the game, capture the
screen, send input, or install anything.
"""

from __future__ import annotations

import json
import pathlib
import socket
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from typing import Any, Dict, Optional

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from autofish_control import ControlSession  # noqa: E402
from autofish_ipc import (  # noqa: E402
    MAX_PACKET_BYTES,
    PROTOCOL_VERSION,
    STATUS_ACK_LIMIT,
    STATUS_ACK_MESSAGE_MAX,
    RuntimeClient,
    RuntimeServer,
)


class IPCIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.endpoint_path = Path(self.temp_dir.name) / "autofish.endpoint.json"
        self.control = ControlSession("session-a")
        self.server = RuntimeServer(self.control, self.endpoint_path)
        self.server.start()
        self.addCleanup(self.server.close)
        self.client = RuntimeClient(self.endpoint_path)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    def _endpoint(self) -> Dict[str, Any]:
        with open(self.endpoint_path, "r", encoding="utf-8") as handle:
            return json.load(handle)

    def _request(
        self,
        name: str = "observe",
        *,
        command_id: Optional[str] = None,
        session: Optional[str] = None,
        token: Optional[str] = None,
        expires_at: Optional[float] = None,
        payload: Optional[Dict[str, Any]] = None,
        include_protocol: bool = True,
        include_version: bool = False,
    ) -> Dict[str, Any]:
        endpoint = self._endpoint()
        request: Dict[str, Any] = {
            "session": endpoint["session"] if session is None else session,
            "token": endpoint["token"] if token is None else token,
            "name": name,
            "payload": {} if payload is None else payload,
            "expires_at": (
                time.monotonic() + 5.0 if expires_at is None else expires_at
            ),
        }
        if command_id is not None:
            request["id"] = command_id
        if include_protocol:
            request["protocol"] = PROTOCOL_VERSION
        if include_version:
            request["version"] = PROTOCOL_VERSION
        return request

    def _send_raw(self, raw: bytes, timeout: float = 2.0) -> bytes:
        endpoint = self._endpoint()
        with socket.create_connection(
            (endpoint["host"], endpoint["port"]), timeout=timeout
        ) as sock:
            sock.settimeout(timeout)
            sock.sendall(raw)
            data = bytearray()
            while b"\n" not in data and len(data) <= MAX_PACKET_BYTES + 1:
                chunk = sock.recv(4096)
                if not chunk:
                    break
                data.extend(chunk)
        return bytes(data).split(b"\n", 1)[0]

    def _send_json(self, request: Dict[str, Any], timeout: float = 2.0) -> Dict[str, Any]:
        line = self._send_raw(
            json.dumps(request, ensure_ascii=False).encode("utf-8") + b"\n",
            timeout=timeout,
        )
        return json.loads(line.decode("utf-8"))

    def _fill_queue(self) -> None:
        for _ in range(128):
            ack = self.client.command("observe")
            self.assertTrue(ack["ok"], ack)

    # ------------------------------------------------------------------
    # Snapshot / status
    # ------------------------------------------------------------------
    def test_status_contains_snapshot_and_session(self) -> None:
        self.server.publish({"state": "idle", "nested": {"a": [1, 2]}})
        status = self.client.status()
        self.assertTrue(status["ok"])
        self.assertEqual(status["session"], "session-a")
        self.assertEqual(status["snapshot"], {"state": "idle", "nested": {"a": [1, 2]}})
        self.assertFalse(status["stopped"])
        self.assertFalse(status["stop_after_fish"])
        self.assertEqual(self.server.drain_commands(), [])

    def test_snapshot_publish_and_read_are_deep_copies(self) -> None:
        source = {"nested": {"values": [1, 2]}}
        self.server.publish(source)
        source["nested"]["values"].append(99)

        status = self.client.status()
        self.assertEqual(status["snapshot"]["nested"]["values"], [1, 2])
        status["snapshot"]["nested"]["values"].append(7)

        second = self.client.status()
        self.assertEqual(second["snapshot"]["nested"]["values"], [1, 2])

    def test_publish_rejects_non_dict(self) -> None:
        with self.assertRaises(TypeError):
            self.server.publish(["not", "a", "dict"])  # type: ignore[arg-type]

    # ------------------------------------------------------------------
    # Commands / acknowledgements
    # ------------------------------------------------------------------
    def test_command_ack_and_queue_roundtrip(self) -> None:
        ack = self.client.command("observe", epoch=7, fish_id="fish-1")
        self.assertTrue(ack["ok"])
        self.assertEqual(ack["status"], "accepted")
        self.assertTrue(ack["id"])

        commands = self.server.drain_commands()
        self.assertEqual(len(commands), 1)
        self.assertEqual(commands[0]["id"], ack["id"])
        self.assertEqual(commands[0]["name"], "observe")
        self.assertEqual(commands[0]["payload"], {"epoch": 7, "fish_id": "fish-1"})
        self.assertEqual(commands[0]["session"], "session-a")
        self.assertTrue(self.server.acknowledge(ack["id"], "executed", "done"))

    def test_duplicate_id_returns_stored_ack_without_requeue(self) -> None:
        first = self.client.command("observe")
        self.server.drain_commands()
        self.assertTrue(self.server.acknowledge(first["id"], "executed", "done"))

        duplicate = self._send_json(
            self._request("observe", command_id=first["id"])
        )
        self.assertEqual(duplicate["status"], "executed")
        self.assertEqual(duplicate["message"], "done")
        self.assertEqual(self.server.drain_commands(), [])

    # ------------------------------------------------------------------
    # Status -> UI final acks
    # ------------------------------------------------------------------
    def test_status_exposes_final_ack_list_for_ui(self) -> None:
        ack = self.client.command("observe")
        self.assertEqual(ack["status"], "accepted")
        self.server.drain_commands()
        self.assertTrue(self.server.acknowledge(ack["id"], "executed", "done"))

        status = self.client.status()
        self.assertIsInstance(status["acks"], list)
        by_id = {item["id"]: item for item in status["acks"]}
        self.assertEqual(by_id[ack["id"]]["status"], "executed")
        self.assertEqual(by_id[ack["id"]]["message"], "done")
        for item in status["acks"]:
            self.assertEqual(set(item), {"id", "status", "message"})

    def test_status_acks_do_not_leak_token_payload_or_session(self) -> None:
        ack = self.client.command("observe", secret="hunter2", token="payload-token")
        self.server.drain_commands()
        self.assertTrue(self.server.acknowledge(ack["id"], "executed", "done"))

        status = self.client.status()
        text = json.dumps(status, ensure_ascii=False)
        self.assertNotIn(self._endpoint()["token"], text)
        self.assertNotIn("hunter2", text)
        self.assertNotIn("payload-token", text)
        self.assertNotIn("payload", text)
        self.assertNotIn("session-a", json.dumps(status["acks"], ensure_ascii=False))

    def test_status_acks_are_bounded_and_messages_truncated(self) -> None:
        command_ids = []
        for _ in range(80):
            ack = self.client.command("observe")
            self.assertTrue(ack["ok"], ack)
            command_ids.append(ack["id"])
        self.server.drain_commands()
        self.assertTrue(
            self.server.acknowledge(
                command_ids[-1], "executed", "x" * (STATUS_ACK_MESSAGE_MAX + 64)
            )
        )

        status = self.client.status()
        self.assertLessEqual(len(status["acks"]), STATUS_ACK_LIMIT)
        visible = [item["id"] for item in status["acks"]]
        self.assertNotIn(command_ids[0], visible)
        self.assertIn(command_ids[-1], visible)

        messages = {item["id"]: item["message"] for item in status["acks"]}
        self.assertEqual(len(messages[command_ids[-1]]), STATUS_ACK_MESSAGE_MAX)

    def test_status_packet_stays_under_limit_with_many_acks(self) -> None:
        for _ in range(STATUS_ACK_LIMIT):
            ack = self.client.command("observe")
            self.assertTrue(ack["ok"], ack)
            self.server.acknowledge(ack["id"], "accepted", "y" * 4096)
        self.server.drain_commands()
        self.server.publish({"blob": "z" * 30000})

        status = self.client.status()
        self.assertTrue(status["ok"], status)
        payload = json.dumps(
            status, ensure_ascii=False, separators=(",", ":")
        ).encode("utf-8")
        self.assertLessEqual(len(payload), MAX_PACKET_BYTES)

    def test_unknown_command_rejected_and_not_queued(self) -> None:
        ack = self.client.command("dance")
        self.assertFalse(ack["ok"])
        self.assertEqual(ack["status"], "rejected")
        self.assertIn("unknown", ack["message"])
        self.assertEqual(self.server.drain_commands(), [])

    def test_stop_is_queued_when_there_is_room(self) -> None:
        ack = self.client.command("stop", reason="manual")
        self.assertTrue(ack["ok"])
        self.assertTrue(self.control.stopped)
        self.assertEqual(self.control.stop_reason, "manual")
        commands = self.server.drain_commands()
        self.assertEqual([command["name"] for command in commands], ["stop"])

    def test_stop_after_fish_and_resume(self) -> None:
        ack = self.client.command("stop_after_fish")
        self.assertTrue(ack["ok"])
        self.assertTrue(self.control.stop_after_fish)
        self.assertEqual(len(self.server.drain_commands()), 1)

        ack = self.client.command("resume")
        self.assertTrue(ack["ok"])
        self.assertFalse(self.control.stop_after_fish)
        self.assertEqual([command["name"] for command in self.server.drain_commands()], ["resume"])

    def test_observe_after_stop_is_rejected(self) -> None:
        self.assertTrue(self.client.command("stop")["ok"])
        self.server.drain_commands()
        ack = self.client.command("observe")
        self.assertFalse(ack["ok"])
        self.assertEqual(ack["status"], "rejected")
        self.assertIn("stopped", ack["message"])
        self.assertEqual(self.server.drain_commands(), [])

    # ------------------------------------------------------------------
    # Queue bound / stop priority
    # ------------------------------------------------------------------
    def test_queue_full_rejects_but_stop_still_wins(self) -> None:
        self._fill_queue()
        self.assertEqual(self.control.command_count, 128)

        overflow = self.client.command("observe")
        self.assertFalse(overflow["ok"])
        self.assertEqual(overflow["status"], "rejected")
        self.assertIn("queue full", overflow["message"])

        ack = self.client.command("stop", reason="priority")
        self.assertTrue(ack["ok"])
        self.assertTrue(self.control.stopped)
        self.assertEqual(self.control.stop_reason, "priority")

        commands = self.server.drain_commands()
        self.assertEqual(len(commands), 128)
        self.assertTrue(all(command["name"] == "observe" for command in commands))

    def test_stop_after_fish_not_blocked_by_full_queue(self) -> None:
        self._fill_queue()
        ack = self.client.command("stop_after_fish")
        self.assertTrue(ack["ok"])
        self.assertTrue(self.control.stop_after_fish)
        self.assertEqual(self.control.command_count, 128)

        # resume needs a queue slot, so it is rejected and must not clear the flag.
        resume = self.client.command("resume")
        self.assertFalse(resume["ok"])
        self.assertIn("queue full", resume["message"])
        self.assertTrue(self.control.stop_after_fish)

    def test_rejected_duplicate_is_not_requeued_after_queue_drains(self) -> None:
        self._fill_queue()
        overflow = self.client.command("observe")
        self.assertEqual(overflow["status"], "rejected")

        self.server.drain_commands()
        duplicate = self._send_json(
            self._request("observe", command_id=overflow["id"])
        )
        self.assertEqual(duplicate["status"], "rejected")
        self.assertIn("queue full", duplicate["message"])
        self.assertEqual(self.server.drain_commands(), [])

    # ------------------------------------------------------------------
    # Authentication / framing
    # ------------------------------------------------------------------
    def test_old_session_is_rejected(self) -> None:
        response = self._send_json(
            self._request("stop", command_id="old-1", session="old-session")
        )
        self.assertEqual(response["status"], "rejected")
        self.assertEqual(response["message"], "bad session")
        self.assertFalse(self.control.stopped)
        self.assertEqual(self.server.drain_commands(), [])

    def test_expired_command_is_rejected(self) -> None:
        response = self._send_json(
            self._request(
                "stop",
                command_id="expired-1",
                expires_at=time.monotonic() - 1.0,
            )
        )
        self.assertEqual(response["status"], "rejected")
        self.assertEqual(response["message"], "expired")
        self.assertFalse(self.control.stopped)

    def test_old_token_is_rejected_after_restart(self) -> None:
        old_token = self._endpoint()["token"]
        self.server.close()

        replacement = RuntimeServer(self.control, self.endpoint_path)
        replacement.start()
        self.addCleanup(replacement.close)
        new_token = self._endpoint()["token"]
        self.assertNotEqual(old_token, new_token)

        response = self._send_json(
            self._request("stop", command_id="stale-token-1", token=old_token)
        )
        self.assertEqual(response["status"], "rejected")
        self.assertEqual(response["message"], "unauthorized")
        self.assertFalse(self.control.stopped)

        self.assertTrue(self.client.status()["ok"])

    def test_bad_token_is_rejected_without_echo(self) -> None:
        endpoint = self._endpoint()
        raw = self._send_raw(
            json.dumps(
                self._request(
                    "stop",
                    command_id="bad-token-1",
                    token="wrong-token-value",
                )
            ).encode("utf-8")
            + b"\n"
        )
        text = raw.decode("utf-8")
        response = json.loads(text)
        self.assertEqual(response["status"], "rejected")
        self.assertEqual(response["message"], "unauthorized")
        self.assertNotIn("wrong-token-value", text)
        self.assertNotIn(endpoint["token"], text)
        self.assertFalse(self.control.stopped)

    def test_version_field_alias_is_accepted(self) -> None:
        response = self._send_json(
            self._request(
                "observe",
                command_id="version-1",
                include_protocol=False,
                include_version=True,
            )
        )
        self.assertEqual(response["status"], "accepted")

    def test_malformed_json_does_not_kill_server(self) -> None:
        raw = self._send_raw(b"{not json\n")
        response = json.loads(raw.decode("utf-8"))
        self.assertEqual(response["status"], "rejected")
        self.assertEqual(response["message"], "bad json")
        self.assertTrue(self.client.status()["ok"])

    def test_oversized_packet_is_rejected(self) -> None:
        raw = self._send_raw(b"x" * (MAX_PACKET_BYTES + 16) + b"\n", timeout=3.0)
        response = json.loads(raw.decode("utf-8"))
        self.assertEqual(response["status"], "rejected")
        self.assertEqual(response["message"], "packet too large")
        self.assertTrue(self.client.status()["ok"])

    def test_client_rejects_oversized_payload_locally(self) -> None:
        ack = self.client.command(
            "observe", blob="x" * (MAX_PACKET_BYTES + 16)
        )
        self.assertFalse(ack["ok"])
        self.assertEqual(ack["status"], "rejected")
        self.assertTrue(ack["id"])
        self.assertIn("packet too large", ack["message"])

    # ------------------------------------------------------------------
    # Endpoint lifecycle
    # ------------------------------------------------------------------
    def test_close_removes_own_endpoint(self) -> None:
        self.assertTrue(self.endpoint_path.exists())
        self.server.close()
        self.assertFalse(self.endpoint_path.exists())

    def test_close_does_not_delete_new_session_endpoint(self) -> None:
        other = RuntimeServer(ControlSession("session-b"), self.endpoint_path)
        other.start()
        self.addCleanup(other.close)

        self.server.close()

        self.assertTrue(self.endpoint_path.exists())
        self.assertEqual(self._endpoint()["session"], "session-b")
        self.assertEqual(self.client.status()["session"], "session-b")

    def test_close_is_bounded_and_leaves_no_server_threads(self) -> None:
        held = socket.create_connection(("127.0.0.1", self._endpoint()["port"]), timeout=1.0)
        self.addCleanup(held.close)

        started = time.monotonic()
        self.server.close()
        elapsed = time.monotonic() - started

        self.assertLess(elapsed, 3.0)
        if self.server._accept_thread is not None:
            self.assertFalse(self.server._accept_thread.is_alive())
        time.sleep(0.1)
        alive = [
            thread
            for thread in threading.enumerate()
            if thread.name.startswith("autofish-ipc-")
        ]
        self.assertEqual(alive, [])

    def test_client_without_endpoint_returns_errors(self) -> None:
        missing = RuntimeClient(Path(self.temp_dir.name) / "missing.json")
        status = missing.status()
        self.assertFalse(status["ok"])
        self.assertIsNone(status["snapshot"])
        ack = missing.command("observe")
        self.assertFalse(ack["ok"])
        self.assertEqual(ack["status"], "rejected")
        self.assertTrue(ack["id"])


if __name__ == "__main__":
    unittest.main()
