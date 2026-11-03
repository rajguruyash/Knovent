"""MALWARE SCANNING of uploaded files, behind one small interface. The engine never executes an uploaded file and refuses types it does not read (sources.SUPPORTED), refuses XML that declares entities and
caps what an office file may unpack to; scanning adds a signature check for files that are later opened by people (a Word or PDF file a customer downloads from the knowledge page).

    NullScanner     scans nothing (the default: no scanner is configured, and the service says so in each source's warnings)
    ClamdScanner    a clamd daemon over TCP (INSTREAM): the file is streamed to it, nothing is stored there

`ENGINE_SCANNER=clamav` with `CLAMAV_HOST` / `CLAMAV_PORT` turns it on; `ENGINE_SCAN_REQUIRED=1` makes the service refuse a file it could NOT scan (fail closed) instead of reading it unscanned.
Standard library only."""
from __future__ import annotations

import socket
import struct

CHUNK = 64 * 1024


class ScanError(Exception):
    """The scanner could not give a verdict (unreachable, timed out, refused)."""


class Infected(Exception):
    def __init__(self, signature: str):
        super().__init__(signature)
        self.signature = signature


class NullScanner:
    name = "none"

    def scan(self, name: str, data: bytes) -> None:
        return None


class ClamdScanner:
    name = "clamav"

    def __init__(self, host: str, port: int = 3310, timeout: float = 30.0):
        self.host, self.port, self.timeout = host, port, timeout

    def scan(self, name: str, data: bytes) -> None:
        """Returns None for a clean file, raises Infected with the signature's name, or ScanError when no verdict was possible."""
        try:
            with socket.create_connection((self.host, self.port), timeout=self.timeout) as s:
                s.sendall(b"zINSTREAM\0")
                for i in range(0, len(data), CHUNK):
                    piece = data[i:i + CHUNK]
                    s.sendall(struct.pack("!I", len(piece)) + piece)
                s.sendall(struct.pack("!I", 0))
                reply = b""
                while not reply.endswith(b"\0") and len(reply) < 4096:
                    got = s.recv(1024)
                    if not got:
                        break
                    reply += got
        except OSError as e:
            raise ScanError(f"the scanner could not be reached ({type(e).__name__})") from None
        text = reply.rstrip(b"\0").decode("utf-8", "replace").strip()
        if text.endswith("FOUND"):
            raise Infected(text.removeprefix("stream:").removesuffix("FOUND").strip() or "unknown")
        if text.endswith("OK"):
            return None
        raise ScanError("the scanner gave no usable verdict")
