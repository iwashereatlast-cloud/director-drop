#!/usr/bin/env python3
"""Mock mod-playerbots command server for the rtt-engine proof harness.

Speaks the stock line protocol (PlayerbotCommandServer.cpp): the client sends
"<verb>,<guid>\\n"; this mock answers from cmdserver-responses.json (key
"<verb>,<guid>" -> reply line; missing key or empty string -> connection
closed with no reply, like the real server for a non-live bot). Listens on
127.0.0.1 ONLY. Not part of the service - harness fixture.

  python3 mock-cmdserver.py <port> <responses.json>
"""
import json
import socket
import sys


def main():
    port = int(sys.argv[1])
    script = json.load(open(sys.argv[2]))
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", port))
    srv.listen(8)
    sys.stderr.write("mock-cmdserver: 127.0.0.1:%d\n" % port)
    sys.stderr.flush()
    while True:
        conn, _ = srv.accept()
        f = None
        try:
            f = conn.makefile("rwb")
            while True:
                line = f.readline()
                if not line:
                    break
                key = line.decode("utf-8", "replace").strip()
                reply = script.get(key, "")
                if reply:
                    f.write((reply + "\n").encode("utf-8"))
                    f.flush()
                else:
                    break  # no reply: close, like the real server for offline bots
        finally:
            if f is not None:
                f.close()
            conn.close()


if __name__ == "__main__":
    main()
