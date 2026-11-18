"""Tiny loopback-to-Unix-socket relay used *inside* a GitBot sandbox.

The sandbox has no route to the host except a single bind-mounted Unix socket.
This script exposes that socket on a loopback TCP port and then runs the fixed
child argv.  It is deliberately standalone (stdlib only) because it is mounted
read-only into a minimal bubblewrap environment.

Nothing here parses HTTP or commands.  It is a byte relay; the host endpoint
authenticates and validates every request.
"""

from __future__ import annotations

import argparse
import contextlib
import select
import socket
import subprocess
import sys
import threading
from collections.abc import Sequence

MAX_CHUNK = 64 * 1024
CONNECT_TIMEOUT_S = 10.0


def _parse_listen(raw: str) -> tuple[str, int]:
    host, sep, port_text = raw.rpartition(":")
    if not sep or not host:
        raise argparse.ArgumentTypeError("listen address must be HOST:PORT")
    if host not in {"127.0.0.1", "::1", "localhost"}:
        raise argparse.ArgumentTypeError("relay may listen only on loopback")
    try:
        port = int(port_text)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("relay port must be an integer") from exc
    if not 1 <= port <= 65535:
        raise argparse.ArgumentTypeError("relay port must be 1-65535")
    return host, port


def _pump(left: socket.socket, right: socket.socket, stop: threading.Event) -> None:
    sockets = [left, right]
    try:
        while not stop.is_set():
            readable, _, _ = select.select(sockets, [], [], 0.5)
            if not readable:
                continue
            for source in readable:
                try:
                    chunk = source.recv(MAX_CHUNK)
                except OSError:
                    return
                if not chunk:
                    return
                target = right if source is left else left
                try:
                    target.sendall(chunk)
                except OSError:
                    return
    finally:
        for sock in sockets:
            with contextlib.suppress(OSError):
                sock.close()


def _serve(listener: socket.socket, unix_path: str, stop: threading.Event) -> None:
    while not stop.is_set():
        try:
            client, _ = listener.accept()
        except OSError:
            return
        upstream = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        upstream.settimeout(CONNECT_TIMEOUT_S)
        try:
            upstream.connect(unix_path)
        except OSError:
            client.close()
            upstream.close()
            continue
        upstream.settimeout(None)
        client.settimeout(None)
        threading.Thread(
            target=_pump,
            args=(client, upstream, stop),
            name="gitbot-relay-connection",
            daemon=True,
        ).start()


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--listen", required=True, type=_parse_listen)
    parser.add_argument("--unix", required=True)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)

    command = list(args.command)
    if command and command[0] == "--":
        command = command[1:]
    if not command:
        print("relay: missing child command", file=sys.stderr)
        return 2
    unix_path = str(args.unix)
    if not unix_path.startswith("/"):
        print("relay: --unix must be an absolute path", file=sys.stderr)
        return 2

    host, port = args.listen
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        listener.bind((host, port))
        listener.listen(64)
    except OSError as exc:
        print(f"relay: could not listen on {host}:{port}: {exc}", file=sys.stderr)
        listener.close()
        return 2

    stop = threading.Event()
    server = threading.Thread(
        target=_serve,
        args=(listener, unix_path, stop),
        name="gitbot-relay-accept",
        daemon=True,
    )
    server.start()

    try:
        child = subprocess.Popen(  # noqa: S603 - fixed argv, never a shell string
            command, stdin=subprocess.DEVNULL
        )
        return child.wait()
    except OSError as exc:
        print(f"relay: could not start child: {exc}", file=sys.stderr)
        return 2
    finally:
        stop.set()
        with contextlib.suppress(OSError):
            listener.close()
        server.join(timeout=1.0)


if __name__ == "__main__":  # pragma: no cover - exercised through the sandbox
    raise SystemExit(main())
