# OpenFill - generative fill for GIMP 3
# Copyright (C) 2026 Beckett Harriman
# SPDX-License-Identifier: Apache-2.0
"""Minimal RFC 6455 WebSocket client on top of the standard library.

Only what ComfyUI's ``/ws`` endpoint needs: a client handshake, text frames,
binary frames (previews, ignored), ping/pong and close. No extensions, no
fragmentation support beyond what the server sends in practice.
"""

from __future__ import annotations

import base64
import hashlib
import os
import socket
import ssl
import struct
from urllib.parse import quote, urlparse

_MAGIC = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"

# Largest frame accepted from the server. ComfyUI previews are well under 1 MB and
# full-size images (SaveImageWebsocket) stay far below this; anything bigger is a
# broken or hostile peer and would otherwise be allocated in memory.
MAX_FRAME_BYTES = 256 * 1024 * 1024

OP_TEXT = 0x1
OP_BINARY = 0x2
OP_CLOSE = 0x8
OP_PING = 0x9
OP_PONG = 0xA


class WebSocketError(ConnectionError):
    """Handshake failure or broken connection."""


class WebSocket:
    """Blocking client connection; call :meth:`connect` first."""

    def __init__(self, url: str, timeout: float = 30.0) -> None:
        self.url = url
        self.timeout = timeout
        self._sock: socket.socket | None = None

    def connect(self) -> None:
        """Open the TCP (or TLS for ``wss``) connection and perform the upgrade handshake."""
        parsed = urlparse(self.url)
        if parsed.scheme not in ("ws", "wss"):
            raise WebSocketError(f"unsupported scheme in {self.url}")
        host = parsed.hostname or "127.0.0.1"
        port = parsed.port or (443 if parsed.scheme == "wss" else 80)
        sock = socket.create_connection((host, port), timeout=self.timeout)
        if parsed.scheme == "wss":
            sock = ssl.create_default_context().wrap_socket(sock, server_hostname=host)
        key = base64.b64encode(os.urandom(16)).decode("ascii")
        path = parsed.path or "/"
        if parsed.query:
            path += "?" + parsed.query
        request = (
            f"GET {path} HTTP/1.1\r\n"
            f"Host: {host}:{port}\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            "Sec-WebSocket-Version: 13\r\n\r\n"
        ).encode("ascii")
        sock.sendall(request)
        response = self._read_headers(sock)
        accept = base64.b64encode(hashlib.sha1((key + _MAGIC).encode("ascii")).digest()).decode()
        if (
            b" 101 " not in response.split(b"\r\n", 1)[0]
            or f"sec-websocket-accept: {accept}".encode().lower() not in response.lower()
        ):
            sock.close()
            raise WebSocketError("websocket upgrade rejected by server")
        self._sock = sock

    @property
    def connected(self) -> bool:
        """True between a successful :meth:`connect` and :meth:`close`."""
        return self._sock is not None

    def settimeout(self, timeout: float | None) -> None:
        """Socket timeout for subsequent :meth:`recv` calls."""
        if self._sock is not None:
            self._sock.settimeout(timeout)

    def recv(self) -> tuple[int, bytes]:
        """Return ``(opcode, payload)``; handles ping and close internally.

        Raises ``socket.timeout`` when nothing arrives within the timeout.
        """
        while True:
            opcode, payload = self._read_frame()
            if opcode == OP_PING:
                self._send_frame(OP_PONG, payload)
                continue
            if opcode == OP_CLOSE:
                self.close(reply=True)
                raise WebSocketError("websocket closed by server")
            return opcode, payload

    def send_text(self, text: str) -> None:
        """Send one masked text frame."""
        self._send_frame(OP_TEXT, text.encode("utf-8"))

    def close(self, reply: bool = False) -> None:
        """Send a close frame (unless replying to the server's) and close the socket."""
        if self._sock is None:
            return
        try:
            if not reply:
                self._send_frame(OP_CLOSE, b"")
        except OSError:
            pass
        try:
            self._sock.close()
        finally:
            self._sock = None

    # frame helpers -------------------------------------------------------

    def _read_exact(self, length: int) -> bytes:
        if self._sock is None:
            raise WebSocketError("not connected")
        buf = bytearray()
        while len(buf) < length:
            chunk = self._sock.recv(length - len(buf))
            if not chunk:
                self._sock = None
                raise WebSocketError("connection closed")
            buf += chunk
        return bytes(buf)

    def _read_frame(self) -> tuple[int, bytes]:
        first, second = self._read_exact(2)
        opcode = first & 0x0F
        length = second & 0x7F
        if length == 126:
            length = struct.unpack(">H", self._read_exact(2))[0]
        elif length == 127:
            length = struct.unpack(">Q", self._read_exact(8))[0]
        if length > MAX_FRAME_BYTES:
            self.close(reply=True)
            raise WebSocketError(f"frame of {length} bytes exceeds the {MAX_FRAME_BYTES} byte limit")
        mask = self._read_exact(4) if second & 0x80 else b""
        payload = bytearray(self._read_exact(length))
        if mask:
            for i in range(length):
                payload[i] ^= mask[i % 4]
        return opcode, bytes(payload)

    def _send_frame(self, opcode: int, payload: bytes) -> None:
        if self._sock is None:
            raise WebSocketError("not connected")
        mask = os.urandom(4)
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        n = len(payload)
        if n < 126:
            header = bytes((0x80 | opcode, 0x80 | n))
        elif n <= 0xFFFF:
            header = bytes((0x80 | opcode, 0x80 | 126)) + struct.pack(">H", n)
        else:
            header = bytes((0x80 | opcode, 0x80 | 127)) + struct.pack(">Q", n)
        self._sock.sendall(header + mask + masked)

    @staticmethod
    def _read_headers(sock: socket.socket) -> bytes:
        # One byte at a time so we never swallow the first frame.
        data = bytearray()
        while not data.endswith(b"\r\n\r\n"):
            chunk = sock.recv(1)
            if not chunk:
                raise WebSocketError("connection closed during handshake")
            data += chunk
            if len(data) > 65536:
                raise WebSocketError("handshake response too large")
        return bytes(data)


def ws_url(http_url: str, client_id: str) -> str:
    """``http://host:port`` -> ``ws://host:port/ws?clientId=...``."""
    parsed = urlparse(http_url)
    scheme = "wss" if parsed.scheme == "https" else "ws"
    return f"{scheme}://{parsed.netloc}/ws?clientId={quote(client_id, safe='')}"
