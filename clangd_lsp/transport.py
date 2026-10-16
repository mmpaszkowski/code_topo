"""Sequential JSON-RPC over POSIX pipes, with deadlines and no worker threads."""

import json
import os
from pathlib import Path
import select
import subprocess
import time
from collections.abc import Callable, Sequence


class LspSessionError(RuntimeError):
    """The transport cannot safely be reused."""


class LspRequestError(RuntimeError):
    """The server rejected one request; the session is still usable."""

    def __init__(self, method: str, error: dict):
        self.method = method
        self.code = error["code"]
        self.data = error.get("data")
        super().__init__(f"{method}: {error['message']} ({self.code})")


class JsonRpcProcess:
    """One request at a time. Any timeout or framing failure ends the session."""

    MAX_MESSAGE = 64 * 1024 * 1024

    def __init__(self, command: Sequence[str], cwd: Path, timeout: float,
                 notification: Callable[[str, dict], None]):
        self._process = subprocess.Popen(command, cwd=cwd, stdin=subprocess.PIPE,
                                         stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=0)
        self._timeout = timeout
        self._notification = notification
        self._buffer = bytearray()
        self._stderr = bytearray()
        self._next_id = 0
        self._broken = False
        self._closed = False
        self._readers = [self._process.stdout, self._process.stderr]
        try:
            for stream in (*self._readers, self._process.stdin):
                os.set_blocking(stream.fileno(), False)
        except BaseException:
            self.close()
            raise

    def _pump(self, deadline: float, writing: bool = False) -> bool:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise LspSessionError("clangd communication timed out")
        writers = [self._process.stdin] if writing else []
        ready, writable, _ = select.select(self._readers, writers, [], remaining)
        if not ready and not writable:
            raise LspSessionError("clangd communication timed out")
        for stream in ready:
            chunk = os.read(stream.fileno(), 65536)
            if not chunk:
                self._readers.remove(stream)
            elif stream is self._process.stdout:
                self._buffer.extend(chunk)
                if len(self._buffer) > self.MAX_MESSAGE + 65536:
                    raise LspSessionError("clangd message exceeds the size limit")
            else:
                self._stderr.extend(chunk)
                del self._stderr[:-65536]
        return bool(writable)

    def _send(self, message: dict, deadline: float) -> None:
        data = json.dumps(message, ensure_ascii=False, allow_nan=False).encode("utf-8")
        if len(data) > self.MAX_MESSAGE:
            raise LspSessionError("Outgoing message exceeds the size limit")
        wire = memoryview(f"Content-Length: {len(data)}\r\n\r\n".encode() + data)
        while wire:
            if self._pump(deadline, writing=True):
                try:
                    size = os.write(self._process.stdin.fileno(), wire)
                except BlockingIOError:
                    continue
                if size == 0:
                    raise LspSessionError("clangd input closed during a write")
                wire = wire[size:]

    def _read(self, deadline: float) -> dict:
        while True:
            boundary = self._buffer.find(b"\r\n\r\n")
            if boundary > 65536 or (boundary < 0 and len(self._buffer) > 65536):
                raise LspSessionError("Invalid LSP header")
            if boundary >= 0:
                headers = {}
                for line in self._buffer[:boundary].decode("ascii").split("\r\n"):
                    name, separator, value = line.partition(":")
                    name = name.lower()
                    if not separator or name in headers:
                        raise LspSessionError("Invalid or duplicate LSP header")
                    headers[name] = value.strip()
                length = headers.get("content-length", "")
                if not length.isascii() or not length.isdecimal():
                    raise LspSessionError("Invalid LSP Content-Length")
                length = int(length)
                if length > self.MAX_MESSAGE:
                    raise LspSessionError("clangd message exceeds the size limit")
                end = boundary + 4 + length
                if len(self._buffer) >= end:
                    payload = bytes(self._buffer[boundary + 4:end])
                    del self._buffer[:end]
                    message = json.loads(payload.decode("utf-8"))
                    if not isinstance(message, dict) or message.get("jsonrpc") != "2.0":
                        raise LspSessionError("Invalid JSON-RPC message")
                    return message
            if self._process.stdout not in self._readers:
                tail = self._stderr.decode("utf-8", errors="replace").strip()
                raise LspSessionError(f"clangd output closed: {tail}")
            self._pump(deadline)

    def _dispatch(self, message: dict, deadline: float) -> bool:
        """Handle server notifications/requests; return False for responses."""
        if "method" not in message:
            return False
        method, params = message["method"], message.get("params", {})
        if not isinstance(method, str) or not isinstance(params, dict):
            raise LspSessionError("Invalid server request or notification")
        if "id" in message:
            response = {"jsonrpc": "2.0", "id": message["id"]}
            if method in ("window/workDoneProgress/create", "window/showMessageRequest"):
                response["result"] = None
            elif method == "workspace/configuration":
                response["result"] = [None for _ in params.get("items", [])]
            else:
                response["error"] = {"code": -32601, "message": "Method not supported"}
            self._send(response, deadline)
        else:
            self._notification(method, params)
        return True

    def request(self, method: str, params=None, *, timeout: float | None = None):
        self._check_open()
        deadline = time.monotonic() + (self._timeout if timeout is None else timeout)
        self._next_id += 1
        request_id = self._next_id
        try:
            self._send({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params}, deadline)
            while True:
                message = self._read(deadline)
                if self._dispatch(message, deadline):
                    continue
                if type(message.get("id")) is not int or message["id"] != request_id:
                    raise LspSessionError("Unexpected JSON-RPC response ID")
                if ("error" in message) == ("result" in message):
                    raise LspSessionError("Expected either a JSON-RPC result or error")
                if "error" in message:
                    error = message["error"]
                    if (not isinstance(error, dict) or type(error.get("code")) is not int
                            or not isinstance(error.get("message"), str)):
                        raise LspSessionError("Invalid JSON-RPC error")
                    raise LspRequestError(method, error)
                return message["result"]
        except LspRequestError:
            raise
        except (OSError, ValueError, LspSessionError) as error:
            self._broken = True
            raise LspSessionError(str(error)) from error
        except BaseException:
            self._broken = True
            raise

    def notify(self, method: str, params=None) -> None:
        self._check_open()
        try:
            self._send({"jsonrpc": "2.0", "method": method, "params": params},
                       time.monotonic() + self._timeout)
        except (OSError, ValueError, LspSessionError) as error:
            self._broken = True
            raise LspSessionError(str(error)) from error
        except BaseException:
            self._broken = True
            raise

    def wait_until(self, predicate: Callable[[], bool]) -> None:
        self._check_open()
        deadline = time.monotonic() + self._timeout
        try:
            while not predicate():
                if not self._dispatch(self._read(deadline), deadline):
                    raise LspSessionError("Unexpected response while waiting for diagnostics")
        except (OSError, ValueError, LspSessionError) as error:
            self._broken = True
            raise LspSessionError(str(error)) from error
        except BaseException:
            self._broken = True
            raise

    def _check_open(self) -> None:
        if self._broken or self._closed:
            raise LspSessionError("clangd session is no longer usable")

    def close(self) -> None:
        if self._closed:
            return
        try:
            if not self._broken and self._process.poll() is None:
                try:
                    self.request("shutdown", timeout=min(self._timeout, 1))
                    self.notify("exit")
                except (LspSessionError, LspRequestError):
                    pass
            try:
                self._process.wait(timeout=1)
            except subprocess.TimeoutExpired:
                self._process.kill()
                self._process.wait()
        finally:
            if self._process.poll() is None:
                self._process.kill()
                self._process.wait()
            for stream in (self._process.stdin, self._process.stdout, self._process.stderr):
                stream.close()
            self._closed = True
