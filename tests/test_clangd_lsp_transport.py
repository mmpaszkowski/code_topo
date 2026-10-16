from pathlib import Path
import sys
import unittest

from clangd_lsp.transport import JsonRpcProcess, LspRequestError, LspSessionError


SERVER = Path(__file__).parent / "fixtures" / "lsp_server.py"


class TransportTests(unittest.TestCase):
    def start(self, *args, timeout=2):
        self.notifications = []
        process = JsonRpcProcess([sys.executable, str(SERVER), *args], SERVER.parent, timeout,
                                 lambda method, params: self.notifications.append((method, params)))
        self.addCleanup(process.close)
        return process

    def test_fragmented_unicode_and_interleaved_server_messages(self):
        process = self.start()
        self.assertEqual(process.request("fragment", {"value": "é😀"}), {"value": "é😀"})
        self.assertEqual(process.request("interleaved", {"ok": True}), {"ok": True})
        self.assertEqual(self.notifications, [("progress", {"value": "working"})])
        self.assertEqual(process.request("stderr", 42), 42)
        self.assertLessEqual(len(process._stderr), 65536)
        process.close()
        self.assertEqual(process._process.returncode, 0)
        self.assertTrue(process._process.stdout.closed)

    def test_server_error_keeps_session_usable(self):
        process = self.start()
        with self.assertRaises(LspRequestError) as caught:
            process.request("error")
        self.assertEqual(caught.exception.code, -32603)
        self.assertEqual(caught.exception.data, {"detail": 1})
        self.assertEqual(process.request("echo", 123), 123)

    def test_protocol_failures_invalidate_session_and_reap_process(self):
        for method in ("bad_header", "bad_json", "wrong_id", "eof", "timeout"):
            with self.subTest(method=method):
                process = self.start(timeout=0.2)
                with self.assertRaises(LspSessionError):
                    process.request(method)
                with self.assertRaisesRegex(LspSessionError, "no longer usable"):
                    process.request("echo")
                process.close()
                self.assertIsNotNone(process._process.returncode)
                self.assertTrue(process._process.stdin.closed)
                self.assertTrue(process._process.stdout.closed)
                self.assertTrue(process._process.stderr.closed)

    def test_stalled_write_times_out_and_invalidates_session(self):
        process = self.start("--stall", timeout=0.1)
        with self.assertRaises(LspSessionError):
            process.notify("large", {"text": "x" * 1000000})
        with self.assertRaises(LspSessionError):
            process.request("echo")
        process.close()
        self.assertIsNotNone(process._process.returncode)


if __name__ == "__main__":
    unittest.main()
