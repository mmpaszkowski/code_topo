"""Small subprocess peer used only by transport tests."""

import json
import sys
import time


def read():
    headers = {}
    while True:
        line = sys.stdin.buffer.readline()
        if not line:
            raise SystemExit
        if line == b"\r\n":
            break
        key, value = line.decode().split(":", 1)
        headers[key.lower()] = value.strip()
    return json.loads(sys.stdin.buffer.read(int(headers["content-length"])))


def send(message, fragment=False):
    data = json.dumps(message, ensure_ascii=False).encode()
    wire = f"Content-Length: {len(data)}\r\n\r\n".encode() + data
    if fragment:
        for byte in wire:
            sys.stdout.buffer.write(bytes([byte]))
            sys.stdout.buffer.flush()
    else:
        sys.stdout.buffer.write(wire)
        sys.stdout.buffer.flush()


if "--stall" in sys.argv:
    time.sleep(20)

while True:
    request = read()
    method = request.get("method")
    if method == "exit":
        break
    if "id" not in request:
        continue
    response = {"jsonrpc": "2.0", "id": request["id"], "result": request.get("params")}
    if method == "shutdown":
        response["result"] = None
    elif method == "interleaved":
        send({"jsonrpc": "2.0", "method": "progress", "params": {"value": "working"}})
        for server_method in ("window/workDoneProgress/create", "workspace/configuration", "unknown"):
            send({"jsonrpc": "2.0", "id": "server", "method": server_method, "params": {"items": [{}]}})
            reply = read()
            assert reply["id"] == "server"
            assert ("error" in reply) == (server_method == "unknown")
    elif method == "error":
        response.pop("result")
        response["error"] = {"code": -32603, "message": "request rejected", "data": {"detail": 1}}
    elif method == "stderr":
        sys.stderr.buffer.write(b"x" * 200000)
        sys.stderr.buffer.flush()
    elif method == "timeout":
        time.sleep(20)
    elif method == "eof":
        break
    elif method == "bad_header":
        sys.stdout.buffer.write(b"Content-Length: nope\r\n\r\n")
        sys.stdout.buffer.flush()
        continue
    elif method == "bad_json":
        sys.stdout.buffer.write(b"Content-Length: 1\r\n\r\n{")
        sys.stdout.buffer.flush()
        continue
    elif method == "wrong_id":
        response["id"] = 999
    send(response, fragment=method == "fragment")
