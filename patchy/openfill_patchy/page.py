# OpenFill for Patchy
# SPDX-License-Identifier: Apache-2.0
"""The OpenFill page's server: ``openfill_photocraft.web.serve`` without its start-up wait.

Python's ``HTTPServer.server_bind`` looks up the host's name (``socket.getfqdn``) before it
serves anything. On a computer whose reverse lookups are slow that alone took 35 s (a Mac
Studio, macOS 27). The page only ever listens on 127.0.0.1 and never uses that name, so this
server skips the lookup. It can go once ``openfill_photocraft.web.serve`` does the same.
"""

from __future__ import annotations

import socketserver
import threading
from http.server import ThreadingHTTPServer

from openfill_photocraft.web import Api, make_handler


class PageServer(ThreadingHTTPServer):
    def server_bind(self) -> None:
        socketserver.TCPServer.server_bind(self)  # not HTTPServer's: no host-name lookup
        self.server_name, self.server_port = self.server_address[:2]


def serve(api: Api, port: int = 0) -> tuple[PageServer, str]:
    """Start the page on 127.0.0.1 in a background thread; returns (server, url with key)."""
    server = PageServer(("127.0.0.1", port), make_handler(api))
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, name="openfill-web", daemon=True).start()
    host, real_port = server.server_address[:2]
    return server, f"http://{host}:{real_port}/?key={api.key}"
