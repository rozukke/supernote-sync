from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread
import tempfile
import unittest

from supernote_sync import SupernoteConnection, discover
from supernote_sync.sync import SyncConfig, sync_folder


class BrowseHandler(BaseHTTPRequestHandler):
    uploaded: bytes = b""
    request_path = ""

    def do_GET(self) -> None:  # noqa: N802
        body = b"<script>const json = '{\"fileList\":[]}'</script>"
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers["Content-Length"])
        type(self).uploaded = self.rfile.read(length)
        type(self).request_path = self.path
        self.send_response(201)
        self.end_headers()

    def log_message(self, *_: object) -> None:
        pass


class ClientTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), BrowseHandler)
        cls.thread = Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.thread.join()

    def setUp(self) -> None:
        BrowseHandler.uploaded = b""
        BrowseHandler.request_path = ""
        self.client = SupernoteConnection("127.0.0.1", self.server.server_port)

    def test_discover_identifies_browse_page(self) -> None:
        endpoints = discover(hosts=["127.0.0.1"], port=self.server.server_port, timeout=1)
        self.assertEqual([item.host for item in endpoints], ["127.0.0.1"])

    def test_upload_file_streams_multipart(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "book.pdf"
            source.write_bytes(b"pdf bytes")
            result = self.client.upload_file(source, "/INBOX/Books")

        self.assertEqual(result.status, 201)
        self.assertEqual(result.remote_path, "/INBOX/Books/book.pdf")
        self.assertEqual(BrowseHandler.request_path, "/INBOX/Books/")
        self.assertIn(b'name="file"; filename="book.pdf"', BrowseHandler.uploaded)
        self.assertIn(b"pdf bytes", BrowseHandler.uploaded)

    def test_upload_rejects_parent_directory(self) -> None:
        with self.assertRaisesRegex(ValueError, "must not contain"):
            self.client.upload_bytes(b"x", "test.txt", "/INBOX/../Documents")

    def test_sync_uses_manifest_to_avoid_repeat_uploads(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory)
            (source / "book.pdf").write_bytes(b"book")
            config = SyncConfig(
                source_directory=source,
                remote_directory="/INBOX",
                state_file=source / ".state.json",
                cache_directory=source / ".cache",
                calibre_command="ebook-convert",
                calibre_arguments=(),
            )
            first = sync_folder(self.client, config)
            second = sync_folder(self.client, config)

        self.assertEqual((first.uploaded, first.skipped), (1, 0))
        self.assertEqual((second.uploaded, second.skipped), (0, 1))


if __name__ == "__main__":
    unittest.main()
