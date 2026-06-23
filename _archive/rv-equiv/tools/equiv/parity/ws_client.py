"""Minimal stdlib WebSocket client for RV-60 parity capture.

The parity environment uses local plain ``ws://127.0.0.1:<port>`` endpoints.
Keeping this client dependency-free avoids making RV-60 depend on an optional
third-party package just to capture ordered message-stream events.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import socket
import struct
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


class WebSocketError(RuntimeError):
    pass


@dataclass
class WebSocketEvent:
    """One received WS frame normalized for Capture.ws_events."""

    type: str
    payload: Any | None = None
    text: str | None = None
    close_code: int | None = None
    close_reason: str | None = None

    def comparable(self) -> dict[str, Any]:
        out: dict[str, Any] = {"type": self.type}
        if self.payload is not None:
            out["payload"] = self.payload
            if isinstance(self.payload, dict):
                if "project_id" in self.payload:
                    out["project_id"] = self.payload["project_id"]
                data = self.payload.get("data")
                if isinstance(data, dict) and "room_id" in data:
                    out["room_id"] = data["room_id"]
                if "cursor" in self.payload:
                    out["cursor"] = self.payload["cursor"]
        if self.text is not None:
            out["text"] = self.text
        if self.close_code is not None:
            out["close_code"] = self.close_code
        if self.close_reason is not None:
            out["close_reason"] = self.close_reason
        return out


class WebSocketClient:
    """Small RFC 6455 client sufficient for local text-frame capture."""

    def __init__(self, url: str, headers: dict[str, str] | None = None, timeout: float = 5.0) -> None:
        parsed = urlsplit(url)
        if parsed.scheme != "ws":
            raise WebSocketError(f"Only ws:// URLs are supported: {url}")
        self.host = parsed.hostname or "127.0.0.1"
        self.port = parsed.port or 80
        path = parsed.path or "/"
        if parsed.query:
            path = f"{path}?{parsed.query}"
        self.path = path
        self.headers = headers or {}
        self.timeout = timeout
        self.sock: socket.socket | None = None

    def __enter__(self) -> "WebSocketClient":
        self.connect()
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def connect(self) -> None:
        key = base64.b64encode(os.urandom(16)).decode("ascii")
        sock = socket.create_connection((self.host, self.port), timeout=self.timeout)
        sock.settimeout(self.timeout)
        lines = [
            f"GET {self.path} HTTP/1.1",
            f"Host: {self.host}:{self.port}",
            "Upgrade: websocket",
            "Connection: Upgrade",
            f"Sec-WebSocket-Key: {key}",
            "Sec-WebSocket-Version: 13",
        ]
        for name, value in self.headers.items():
            lines.append(f"{name}: {value}")
        req = "\r\n".join(lines) + "\r\n\r\n"
        sock.sendall(req.encode("ascii"))

        raw = self._read_until(sock, b"\r\n\r\n")
        head = raw.decode("iso-8859-1", "replace")
        status_line = head.split("\r\n", 1)[0]
        if " 101 " not in status_line:
            raise WebSocketError(f"WebSocket upgrade failed: {status_line}")
        headers: dict[str, str] = {}
        for line in head.split("\r\n")[1:]:
            if ":" in line:
                k, v = line.split(":", 1)
                headers[k.lower()] = v.strip()
        expected = base64.b64encode(hashlib.sha1((key + GUID).encode("ascii")).digest()).decode("ascii")
        if headers.get("sec-websocket-accept") != expected:
            raise WebSocketError("Invalid Sec-WebSocket-Accept")
        self.sock = sock

    def recv_event(self) -> WebSocketEvent:
        opcode, payload = self._recv_frame()
        if opcode == 0x1:
            text = payload.decode("utf-8", "replace")
            try:
                obj = json.loads(text)
            except ValueError:
                return WebSocketEvent(type="text", text=text)
            event_type = obj.get("type") if isinstance(obj, dict) else "json"
            return WebSocketEvent(type=str(event_type or "json"), payload=obj)
        if opcode == 0x8:
            code = None
            reason = ""
            if len(payload) >= 2:
                code = struct.unpack("!H", payload[:2])[0]
                reason = payload[2:].decode("utf-8", "replace")
            return WebSocketEvent(type="close", close_code=code, close_reason=reason)
        if opcode == 0x9:
            self._send_frame(0xA, payload)
            return WebSocketEvent(type="ping")
        if opcode == 0xA:
            return WebSocketEvent(type="pong")
        return WebSocketEvent(type=f"opcode:{opcode}", text=payload.decode("utf-8", "replace"))

    def send_json(self, payload: Any) -> None:
        self.send_text(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))

    def send_text(self, text: str) -> None:
        self._send_frame(0x1, text.encode("utf-8"))

    def close(self) -> None:
        if self.sock is None:
            return
        try:
            self._send_frame(0x8, b"")
        except OSError:
            pass
        try:
            self.sock.close()
        finally:
            self.sock = None

    @staticmethod
    def _read_until(sock: socket.socket, marker: bytes) -> bytes:
        buf = bytearray()
        while marker not in buf:
            chunk = sock.recv(4096)
            if not chunk:
                break
            buf.extend(chunk)
        return bytes(buf)

    def _recv_exact(self, n: int) -> bytes:
        if self.sock is None:
            raise WebSocketError("WebSocket is not connected")
        buf = bytearray()
        while len(buf) < n:
            chunk = self.sock.recv(n - len(buf))
            if not chunk:
                raise WebSocketError("WebSocket closed while reading frame")
            buf.extend(chunk)
        return bytes(buf)

    def _recv_frame(self) -> tuple[int, bytes]:
        head = self._recv_exact(2)
        opcode = head[0] & 0x0F
        masked = bool(head[1] & 0x80)
        length = head[1] & 0x7F
        if length == 126:
            length = struct.unpack("!H", self._recv_exact(2))[0]
        elif length == 127:
            length = struct.unpack("!Q", self._recv_exact(8))[0]
        mask = self._recv_exact(4) if masked else b""
        payload = self._recv_exact(length) if length else b""
        if masked:
            payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        return opcode, payload

    def _send_frame(self, opcode: int, payload: bytes) -> None:
        if self.sock is None:
            raise WebSocketError("WebSocket is not connected")
        mask = os.urandom(4)
        first = 0x80 | opcode
        length = len(payload)
        if length < 126:
            header = struct.pack("!BB", first, 0x80 | length)
        elif length < (1 << 16):
            header = struct.pack("!BBH", first, 0x80 | 126, length)
        else:
            header = struct.pack("!BBQ", first, 0x80 | 127, length)
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        self.sock.sendall(header + mask + masked)


def collect_ws_events(
    url: str,
    headers: dict[str, str] | None = None,
    *,
    count: int = 1,
    timeout: float = 5.0,
) -> list[dict[str, Any]]:
    """Connect and capture up to ``count`` ordered events."""

    events: list[dict[str, Any]] = []
    with WebSocketClient(url, headers=headers, timeout=timeout) as client:
        while len(events) < count:
            events.append(client.recv_event().comparable())
    return events
