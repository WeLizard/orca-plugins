import builtins
import http.server
import pathlib
import threading
import unittest
from unittest import mock

import upload_transport


class _CaptureHandler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_POST(self):
        length = int(self.headers["Content-Length"])
        self.server.requests.append({
            "path": self.path,
            "headers": dict(self.headers.items()),
            "transfer_encoding": self.headers.get("Transfer-Encoding"),
            "body": self.rfile.read(length),
        })
        if self.path == "/redirect":
            self.send_response(307)
            self.send_header("Location", "/must-not-follow")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        self.send_response(201)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, _format, *_args):
        pass


class _ReadSpy:
    def __init__(self, wrapped):
        self._wrapped = wrapped
        self.read_sizes = []

    def fileno(self):
        return self._wrapped.fileno()

    def read(self, size=-1):
        self.read_sizes.append(size)
        if size <= 0:
            raise AssertionError("file transport must only perform bounded reads")
        return self._wrapped.read(size)

    def __enter__(self):
        self._wrapped.__enter__()
        return self

    def __exit__(self, *args):
        return self._wrapped.__exit__(*args)


class MultipartUploadTransportTest(unittest.TestCase):
    def setUp(self):
        self.server = http.server.HTTPServer(("127.0.0.1", 0), _CaptureHandler)
        self.server.requests = []
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = "http://127.0.0.1:%d" % self.server.server_port
        self.addCleanup(self._stop_server)

    def _stop_server(self):
        self.server.shutdown()
        self.thread.join(timeout=2)
        self.server.server_close()

    def test_posts_exact_multipart_body_with_bounded_file_reads(self):
        path = pathlib.Path(__file__).with_name("printers_plugin.py")
        payload = path.read_bytes()
        original_open = builtins.open
        spies = []

        def open_with_spy(*args, **kwargs):
            spy = _ReadSpy(original_open(*args, **kwargs))
            spies.append(spy)
            return spy

        with mock.patch("builtins.open", side_effect=open_with_spy):
            status = upload_transport.post_multipart_file(
                self.url + "/upload",
                path,
                fields={"select": "true", "print": "false"},
                headers={"X-Api-Key": "secret-token"},
                chunk_size=8192,
                timeout=2,
            )

        self.assertEqual(status, 201)
        self.assertEqual(len(spies), 1)
        self.assertTrue(spies[0].read_sizes)
        self.assertLessEqual(max(spies[0].read_sizes), 8192)
        request = self.server.requests[0]
        self.assertEqual(request["transfer_encoding"], None)
        self.assertEqual(int(request["headers"]["Content-Length"]), len(request["body"]))
        self.assertEqual(request["headers"]["X-Api-Key"], "secret-token")
        self.assertIn(b'name="select"\r\n\r\ntrue\r\n', request["body"])
        self.assertIn(b'name="print"\r\n\r\nfalse\r\n', request["body"])
        self.assertIn(b'filename="printers_plugin.py"', request["body"])
        self.assertTrue(request["body"].endswith(
            payload + b"\r\n" + b"------PrintFarmUpload7MA4YWxkTrZu0gW--\r\n"
        ))

    def test_preserves_auth_header_and_returns_redirect_without_following(self):
        path = pathlib.Path(__file__).with_name("printers_plugin.py")

        status = upload_transport.post_multipart_file(
            self.url + "/redirect",
            path,
            headers={"Authorization": "Bearer private"},
            timeout=2,
        )

        self.assertEqual(status, 307)
        self.assertEqual([item["path"] for item in self.server.requests], ["/redirect"])
        self.assertEqual(self.server.requests[0]["headers"]["Authorization"], "Bearer private")

    def test_rejects_redirect_sensitive_or_invalid_request_options(self):
        path = pathlib.Path(__file__).with_name("printers_plugin.py")

        cases = [
            ("ftp://127.0.0.1/file", {}),
            ("http://user:pass@127.0.0.1/file", {}),
            (self.url + "/file", {"headers": {"Transfer-Encoding": "chunked"}}),
            (self.url + "/file", {"headers": {"X-Test": "ok\r\nInjected: yes"}}),
            (self.url + "/file", {"chunk_size": 0}),
        ]
        for url, options in cases:
            with self.subTest(url=url, options=options):
                with self.assertRaises(ValueError):
                    upload_transport.post_multipart_file(url, path, **options)


if __name__ == "__main__":
    unittest.main()
