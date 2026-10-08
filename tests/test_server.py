import http.client
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from dao.config import Config
from dao.decision import demo_payload
from dao.server import STATIC, make_server
from dao.service import Dao
from dao.store import Store


class HttpTests(unittest.TestCase):
    app_type = Dao
    static_dir = STATIC

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.temp.name) / "test.db")
        self.server = make_server(self.app_type(self.store, Config()), 0, static_dir=self.static_dir)
        self.thread = threading.Thread(target=self.server.serve_forever)
        self.thread.start()
        self.port = self.server.server_port
        self.bootstrap = self.request("GET", "/api/bootstrap")[1]

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        self.store.close()
        self.temp.cleanup()

    def request(self, method, path, body=None, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        hdr = {"Content-Type": "application/json", **(headers or {})}
        conn.request(method, path, json.dumps(body) if body is not None else None, hdr)
        response = conn.getresponse()
        raw, status = response.read(), response.status
        mime = response.getheader("Content-Type", "")
        conn.close()
        if "ndjson" in mime:
            return status, [json.loads(line) for line in raw.splitlines()], response.headers
        return status, json.loads(raw) if "json" in mime else raw, response.headers

    def auth(self):
        return {"X-Dao-CSRF": self.bootstrap["csrf"]}

    def test_csrf_origin_and_host_are_enforced(self):
        body = {"branch": "main", "expected_head": self.bootstrap["head"]["id"], "key": "x", "value": "y"}
        self.assertEqual(self.request("POST", "/api/memory", body)[0], 403)
        self.assertEqual(self.request("POST", "/api/memory", body, {**self.auth(), "Origin": "https://evil.example"})[0], 403)
        self.assertEqual(self.request("GET", "/api/bootstrap", headers={"Host": "evil.example"})[0], 403)
        self.assertEqual(self.request("GET", "/api/bootstrap", headers={"Sec-Fetch-Site": "cross-site"})[0], 403)
        self.assertEqual(self.request("POST", "/api/memory", body, self.auth())[0], 200)

    def test_conflict_and_validation_have_explicit_status(self):
        data = {"branch": "main", "expected_head": "stale", "message": "Hello"}
        self.assertEqual(self.request("POST", "/api/chat", data, self.auth())[0], 409)
        self.assertEqual(self.request("POST", "/api/memory", [], self.auth())[0], 400)
        self.assertEqual(self.request("GET", "/../../dao/config.py")[0], 404)

    def test_streaming_then_restart_keeps_memory_and_integrity(self):
        data = {"branch": "main", "expected_head": self.bootstrap["head"]["id"], "message": "/remember project=Dao"}
        status, events, _ = self.request("POST", "/api/chat", data, self.auth())
        self.assertEqual(status, 200)
        self.assertEqual([e["type"] for e in events], ["start", "delta", "done"])
        reopened = Store(Path(self.temp.name) / "test.db")
        try:
            self.assertEqual(reopened.head()["state"]["memory"]["project"], "Dao")
            self.assertTrue(reopened.verify()["ok"])
        finally:
            reopened.close()

    def test_csp_static_assets_and_export(self):
        status, page, headers = self.request("GET", "/")
        self.assertEqual(status, 200)
        self.assertIn(b"Dao", page)
        self.assertIn("frame-ancestors 'none'", headers["Content-Security-Policy"])
        export = self.request("GET", "/api/export")[1]
        self.assertEqual(export["schema"], "dao-export-v1")
        self.assertNotIn("csrf", export)
        self.assertNotIn("api_key", export["config"])

    def test_second_listener_cannot_share_workspace_port(self):
        with self.assertRaises(OSError):
            make_server(Dao(self.store, Config()), self.port)

    def test_brand_assets_are_local_allowlisted_images(self):
        for path, mime, signature in (
            ("/static/dao.svg", "image/svg+xml; charset=utf-8", b"<svg"),
            ("/static/dao.ico", "image/vnd.microsoft.icon", b"\x00\x00\x01\x00"),
        ):
            with self.subTest(path=path):
                status, image, headers = self.request("GET", path)
                self.assertEqual(status, 200)
                self.assertIn(signature, image)
                self.assertEqual(headers["Content-Type"], mime)
                self.assertEqual(headers["Content-Length"], str(len(image)))
                self.assertEqual(headers["X-Content-Type-Options"], "nosniff")
                self.assertEqual(self.request("GET", path, headers={"Host": "evil.example"})[0], 403)
        for path in ("/static/unknown.svg", "/static/../config.py", "/static/%2e%2e/config.py"):
            with self.subTest(path=path):
                self.assertEqual(self.request("GET", path)[0], 404)

    def test_oversized_decision_is_rejected_without_committing_state(self):
        head = self.bootstrap["head"]["id"]
        problem = demo_payload()
        problem["actions"] = [{}] * 65
        body = {"branch": "main", "expected_head": head, "problem": problem}
        status, result, _ = self.request("POST", "/api/decision", body, self.auth())
        self.assertEqual(status, 400)
        self.assertIn("at most 64", result["error"])
        self.assertEqual(self.store.head()["id"], head)
        self.assertTrue(self.store.verify()["ok"])


if __name__ == "__main__":
    unittest.main()
