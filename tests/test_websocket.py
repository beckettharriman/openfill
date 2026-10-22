# SPDX-License-Identifier: Apache-2.0
"""Tests the stdlib WebSocket client against a tiny in-process server."""

import base64
import hashlib
import socket
import struct
import threading
import unittest

from openfill.websocket import OP_BINARY, OP_CLOSE, OP_PING, OP_TEXT, WebSocket, WebSocketError, ws_url

MAGIC = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


def _server_frame(opcode: int, payload: bytes) -> bytes:
    n = len(payload)
    if n < 126:
        return bytes((0x80 | opcode, n)) + payload
    if n <= 0xFFFF:
        return bytes((0x80 | opcode, 126)) + struct.pack(">H", n) + payload
    return bytes((0x80 | opcode, 127)) + struct.pack(">Q", n) + payload


def _read_client_frame(conn: socket.socket) -> tuple[int, bytes]:
    first, second = conn.recv(2)
    opcode, length = first & 0x0F, second & 0x7F
    if length == 126:
        length = struct.unpack(">H", conn.recv(2))[0]
    mask = conn.recv(4)
    payload = bytearray()
    while len(payload) < length:
        payload += conn.recv(length - len(payload))
    return opcode, bytes(b ^ mask[i % 4] for i, b in enumerate(payload))


class FakeServer:
    def __init__(self, frames: list[bytes], accept: bool = True) -> None:
        self.frames = frames
        self.accept = accept
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(1)
        self.port = self.sock.getsockname()[1]
        self.received: list[tuple[int, bytes]] = []
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def _run(self) -> None:
        conn, _ = self.sock.accept()
        self.sock.close()  # one connection per fake server
        request = b""
        while b"\r\n\r\n" not in request:
            request += conn.recv(1024)
        key = next(
            line.split(b":", 1)[1].strip()
            for line in request.split(b"\r\n")
            if line.lower().startswith(b"sec-websocket-key")
        )
        if not self.accept:
            conn.sendall(b"HTTP/1.1 403 Forbidden\r\n\r\n")
            conn.close()
            return
        accept = base64.b64encode(hashlib.sha1(key + MAGIC.encode()).digest())
        conn.sendall(
            b"HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
            b"Sec-WebSocket-Accept: " + accept + b"\r\n\r\n" + b"".join(self.frames)
        )
        try:
            while True:
                self.received.append(_read_client_frame(conn))
                if self.received[-1][0] == OP_CLOSE:
                    break
        except Exception:
            pass
        conn.close()


class WebSocketTests(unittest.TestCase):
    def test_text_binary_ping_and_close(self):
        big = b"x" * 70000
        server = FakeServer(
            [
                _server_frame(OP_TEXT, b'{"type":"progress"}'),
                _server_frame(OP_PING, b"hi"),
                _server_frame(OP_BINARY, big),
                _server_frame(OP_CLOSE, b""),
            ]
        )
        ws = WebSocket(f"ws://127.0.0.1:{server.port}/ws?clientId=abc", timeout=5)
        ws.connect()
        self.assertEqual(ws.recv(), (OP_TEXT, b'{"type":"progress"}'))
        opcode, payload = ws.recv()  # ping answered transparently, binary returned
        self.assertEqual((opcode, len(payload)), (OP_BINARY, 70000))
        with self.assertRaises(WebSocketError):
            ws.recv()
        server.thread.join(2)
        self.assertEqual(server.received[0], (0xA, b"hi"))
        self.assertFalse(ws.connected)

    def test_rejected_handshake(self):
        server = FakeServer([], accept=False)
        ws = WebSocket(f"ws://127.0.0.1:{server.port}/ws", timeout=5)
        with self.assertRaises(WebSocketError):
            ws.connect()

    def test_send_text_is_masked_and_readable(self):
        server = FakeServer([])
        ws = WebSocket(f"ws://127.0.0.1:{server.port}/ws", timeout=5)
        ws.connect()
        ws.send_text("hello")
        ws.close()
        server.thread.join(2)
        self.assertEqual(server.received[0], (OP_TEXT, b"hello"))

    def test_oversized_frame_is_refused(self):
        header = bytes((0x80 | OP_BINARY, 127)) + struct.pack(">Q", 1 << 40)  # claims 1 TiB
        server = FakeServer([header])
        ws = WebSocket(f"ws://127.0.0.1:{server.port}/ws", timeout=5)
        ws.connect()
        with self.assertRaises(WebSocketError):
            ws.recv()
        self.assertFalse(ws.connected)

    def test_ws_url_quotes_client_id(self):
        self.assertEqual(ws_url("http://127.0.0.1:8188", "a b&c"), "ws://127.0.0.1:8188/ws?clientId=a%20b%26c")

    def test_ws_url(self):
        self.assertEqual(ws_url("http://127.0.0.1:8188", "id"), "ws://127.0.0.1:8188/ws?clientId=id")
        self.assertEqual(ws_url("https://h:1/", "id"), "wss://h:1/ws?clientId=id")


if __name__ == "__main__":
    unittest.main()
