#!/usr/bin/env python3
"""Phase 0 diagnostic for the Artie 3000.

Stdlib only, on purpose: run this before installing anything, and before
trusting any other code in this repo. It answers two questions:

  1. Can this machine even reach the robot?  (the WSL2 NAT question)
  2. Does it speak the Mirobot WebSocket protocol we designed against?

Usage:
    python3 scripts/probe.py                  # default 192.168.4.1 (Artie's own AP)
    python3 scripts/probe.py 192.168.0.80     # after it has joined your LAN
"""

from __future__ import annotations

import base64
import json
import os
import socket
import struct
import sys
import urllib.error
import urllib.request

DEFAULT_HOST = os.environ.get("ARTIE_HOST", "192.168.4.1")
PORT = int(os.environ.get("ARTIE_PORT", "8899"))
TIMEOUT = 5.0


# --- a minimal WebSocket client (RFC 6455), just enough to ask one question ---


def ws_connect(host: str, port: int, path: str = "/websocket") -> socket.socket:
    sock = socket.create_connection((host, port), timeout=TIMEOUT)
    key = base64.b64encode(os.urandom(16)).decode()
    handshake = (
        f"GET {path} HTTP/1.1\r\n"
        f"Host: {host}:{port}\r\n"
        "Upgrade: websocket\r\n"
        "Connection: Upgrade\r\n"
        f"Sec-WebSocket-Key: {key}\r\n"
        "Sec-WebSocket-Version: 13\r\n"
        "\r\n"
    )
    sock.sendall(handshake.encode())

    # Read until end of HTTP headers.
    buf = b""
    while b"\r\n\r\n" not in buf:
        chunk = sock.recv(4096)
        if not chunk:
            raise ConnectionError("server closed during handshake")
        buf += chunk

    status = buf.split(b"\r\n", 1)[0].decode(errors="replace")
    if "101" not in status:
        raise ConnectionError(f"expected HTTP 101 upgrade, got: {status}")
    return sock


def ws_send_text(sock: socket.socket, payload: str) -> None:
    data = payload.encode()
    header = bytearray([0x81])  # FIN + text opcode
    mask = os.urandom(4)  # clients MUST mask
    n = len(data)
    if n < 126:
        header.append(0x80 | n)
    elif n < (1 << 16):
        header.append(0x80 | 126)
        header += struct.pack(">H", n)
    else:
        header.append(0x80 | 127)
        header += struct.pack(">Q", n)
    header += mask
    masked = bytes(b ^ mask[i % 4] for i, b in enumerate(data))
    sock.sendall(bytes(header) + masked)


def _recv_exactly(sock: socket.socket, n: int) -> bytes:
    buf = b""
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise ConnectionError("server closed mid-frame")
        buf += chunk
    return buf


def ws_recv_text(sock: socket.socket) -> str:
    b0, b1 = _recv_exactly(sock, 2)
    opcode = b0 & 0x0F
    masked = bool(b1 & 0x80)
    length = b1 & 0x7F
    if length == 126:
        length = struct.unpack(">H", _recv_exactly(sock, 2))[0]
    elif length == 127:
        length = struct.unpack(">Q", _recv_exactly(sock, 8))[0]

    mask = _recv_exactly(sock, 4) if masked else None
    payload = _recv_exactly(sock, length) if length else b""
    if mask:
        payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))

    if opcode == 0x8:  # close
        raise ConnectionError("server sent close frame")
    return payload.decode(errors="replace")


# --- the checks ---


def check_websocket(host: str) -> bool:
    print(f"[1/2] WebSocket  ws://{host}:{PORT}/websocket")
    try:
        sock = ws_connect(host, PORT)
    except OSError as exc:
        print(f"      FAIL  cannot connect: {exc}")
        print()
        print("      If you are in WSL2 and Windows IS joined to Artie's hotspot,")
        print("      this is most likely WSL2's NAT. Two fixes:")
        print("        a) Add to C:\\Users\\<you>\\.wslconfig:")
        print("               [wsl2]")
        print("               networkingMode=mirrored")
        print("           then run:  wsl --shutdown")
        print("        b) Or run this probe from Windows Python instead.")
        return False

    try:
        # The protocol is async: a command can produce 'accepted' then 'complete'.
        # 'version' is a short command, so we expect 'complete' straight away --
        # but read a couple of frames rather than assuming.
        ws_send_text(sock, json.dumps({"cmd": "version", "id": "probe1"}) + "\r\n")
        for _ in range(3):
            reply = ws_recv_text(sock)
            print(f"      <-- {reply.strip()}")
            try:
                msg = json.loads(reply)
            except json.JSONDecodeError:
                continue
            if msg.get("status") == "complete":
                print(f"      OK    firmware version: {msg.get('msg')!r}")
                return True
        print("      WARN  connected, but no 'complete' reply to 'version'.")
        return False
    except (OSError, ConnectionError) as exc:
        print(f"      FAIL  {exc}")
        return False
    finally:
        sock.close()


def _http_get(url: str) -> tuple[int, str]:
    req = urllib.request.Request(url, headers={"User-Agent": "artie-probe"})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
        return resp.status, resp.read(2048).decode(errors="replace")


def check_web_ui(host: str) -> None:
    """Artie's own web interface -- where the WiFi setting actually lives.

    Note: Mirobot's admin pages (/admin/wifi.html) are NOT present on Artie,
    despite the shared protocol lineage. Confirmed 404 on real hardware. The
    WiFi config is in Artie's own UI at the root.
    """
    print(f"[2/2] Web UI     http://{host}/")
    try:
        status, _ = _http_get(f"http://{host}/")
        if status == 200:
            print("      OK    Artie's interface is up.")
            print("      To put Artie on your home WiFi: join its hotspot from a")
            print("      phone, open this URL, and set the network there.")
            return
        print(f"      HTTP {status} -- unexpected.")
    except urllib.error.HTTPError as exc:
        print(f"      HTTP {exc.code}")
    except OSError as exc:
        print(f"      unreachable: {exc}")


def main() -> int:
    host = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_HOST
    print(f"Probing Artie 3000 at {host}\n")

    reachable = check_websocket(host)
    print()
    check_web_ui(host)
    print()

    if reachable:
        print("RESULT: robot reachable and speaking the expected protocol.")
        print("        Next: calibrate. Units (mm/degrees) are inferred from the")
        print("        Mirobot docs and NOT yet confirmed on Artie -- measure a")
        print("        forward(100) with a ruler before trusting any drawing tool.")
        return 0

    print("RESULT: could not talk to the robot. Fix this before building on it.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
