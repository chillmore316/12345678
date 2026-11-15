"""Local-only portfolio task API; worker launches and source reads are mocked."""
from contextlib import closing
import http.client
import json
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
import unittest
import uuid
from unittest.mock import patch

from research_center.http import make_server
from research_center.store import RunStore


class PortfolioWorkflowHttpTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = RunStore(Path(self.temp.name) / "research")
        self.server = make_server(self.store, 0)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.close)

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()

    def request(self, method, path, payload=None, **headers):
        with closing(http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=5)) as connection:
            connection.request(method, path, None if payload is None else json.dumps(payload),
                               {"Content-Type": "application/json", **headers})
            response = connection.getresponse()
            return response.status, response.read(), dict(response.getheaders())

    def test_preview_submit_retry_and_cancel_keep_identity_and_method_guards(self):
        root = "/api/research/workflows"
        self.assertEqual(self.request("GET", "/portfolio")[0], 200)
        status, script, headers = self.request("GET", "/api/research/portfolio-submit.js")
        self.assertEqual(status, 200)
        self.assertIn("javascript", headers["Content-Type"])
        self.assertIn(b"/api/research/workflows", script)
        spec = {"config": {"symbols": ["000001.SZSE", "000002.SZSE"], "start": "2024-01-02", "end": "2024-01-05"}}
        request_id = uuid.uuid4().hex
        with patch("research_center.portfolio_workflow.snapshot_sources", return_value={"source_version": "f" * 64}), patch(
                "research_center.workflow_jobs.subprocess.Popen", return_value=SimpleNamespace(pid=999999)) as spawn, patch(
                "research_center.workflow_jobs._worker_is_alive", return_value=True):
            status, data, _ = self.request("POST", root + "/preview", {"kind": "portfolio", "spec": spec})
            self.assertEqual(status, 200)
            self.assertEqual(json.loads(data)["source_version"], "f" * 64)
            self.assertFalse((self.store.root / "workflow_jobs.sqlite3").exists())
            spawn.assert_not_called()
            spec["expected_source_version"] = "0" * 64
            self.assertEqual(self.request("POST", root, {"kind": "portfolio", "spec": spec, "request_id": request_id})[0], 409)
            spawn.assert_not_called()
            spec["expected_source_version"] = "f" * 64
            payload = {"kind": "portfolio", "spec": spec, "request_id": request_id}
            status, body, _ = self.request("POST", root, payload)
            self.assertEqual(status, 202)
            first = json.loads(body)
            job_id = first["job_id"]
            self.assertEqual(json.loads(self.request("POST", root, payload)[1])["job_id"], job_id)
            spawn.assert_called_once()
            cancel = root + "/" + job_id + "/cancel"
            self.assertEqual(self.request("POST", cancel, {}, Origin="https://outside.example")[0], 403)
            self.assertEqual(self.request("POST", cancel, {"extra": True})[0], 400)
            self.assertEqual(self.request("POST", cancel, {}, Host="outside.example")[0], 403)
            status, _, headers = self.request("GET", cancel)
            self.assertEqual(status, 405)
            self.assertEqual(headers["Allow"], "POST")
            status, body, _ = self.request("POST", cancel, {})
            self.assertEqual(status, 200)
            requested = json.loads(body)
            self.assertTrue(requested["cancel_requested"])
            self.assertEqual(requested["status"], "queued")
            self.assertEqual(self.request("PUT", root)[0], 405)
            status, body, headers = self.request("HEAD", root)
            self.assertEqual(status, 405)
            self.assertEqual(body, b"")
            self.assertEqual(headers["Allow"], "GET, POST")

    def test_verification_only_rejects_cancellation_before_backend(self):
        server = make_server(self.store, 0, verification_only=True)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with patch("research_center.workflow_jobs.WorkflowJobs.request_cancel") as cancel, closing(
                    http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=5)) as connection:
                connection.request("POST", "/api/research/workflows/" + "a" * 32 + "/cancel", "{}", {"Content-Type": "application/json"})
                response = connection.getresponse()
                self.assertEqual(response.status, 403)
                response.read()
                cancel.assert_not_called()
        finally:
            server.shutdown()
            server.server_close()
            thread.join()


if __name__ == "__main__":
    unittest.main()
