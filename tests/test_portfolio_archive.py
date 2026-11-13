"""Synthetic archive fixtures: no prices, credentials or live services."""
import hashlib
import http.client
import json
from pathlib import Path
import tempfile
import threading
import unittest

from research_center.http import make_server
from research_center.portfolio_archive import PortfolioArchive, PortfolioArtifactChanged, archive_root
from research_center.store import RunStore


RUN = "a" * 32


def fixture(root):
    directory = root / RUN
    directory.mkdir(parents=True)
    content = {"config.json": {"symbols": ["000001.SZSE", "000002.SZSE"], "initial_cash": 100000, "data_root": "/private/local-market"},
               "state.json": {"synthetic": True}, "data_manifest.json": {"frames": 1},
               "result.json": {"mode": "RESEARCH_ONLY", "paper_admission": False, "equity": [], "positions": {}}}
    hashes = {}
    for name, value in content.items():
        raw = json.dumps(value).encode()
        (directory / name).write_bytes(raw)
        hashes[name] = hashlib.sha256(raw).hexdigest()
    receipt = {"run_id": RUN, "status": "completed", "mode": "RESEARCH_ONLY", "paper_admission": False,
               "created_at": "2024-01-02T00:00:00+00:00", "artifact_sha256": hashes, "artifact_directory": str(directory)}
    (directory / "receipt.json").write_text(json.dumps(receipt))
    return directory, receipt


class PortfolioArchiveTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "archive"
        self.archive = PortfolioArchive(self.root)

    def test_absent_archive_is_read_only(self):
        self.assertEqual(self.archive.list()["runs"], [])
        self.assertFalse(self.root.exists())

    def test_completed_result_checks_all_four_hashes_and_hides_local_paths(self):
        fixture(self.root)
        value = self.archive.get(RUN)
        self.assertNotIn("data_root", value["config"])
        self.assertNotIn("artifact_directory", value["receipt"])
        self.assertFalse(value["result"]["paper_admission"])
        self.assertEqual(value["receipt"]["artifact_integrity"], "verified")
        self.assertEqual(len(self.archive.list()["runs"]), 1)
        self.assertEqual(self.archive.list()["runs"][0]["receipt"]["artifact_integrity"], "not_checked")
        (self.root / RUN / "state.json").write_text("{}")
        self.assertEqual(self.archive.list()["runs"][0]["receipt"]["artifact_integrity"], "not_checked")
        with self.assertRaises(PortfolioArtifactChanged):
            self.archive.get(RUN)

    def test_missing_hash_and_wrong_identity_fail_closed(self):
        directory, receipt = fixture(self.root)
        del receipt["artifact_sha256"]["state.json"]
        (directory / "receipt.json").write_text(json.dumps(receipt))
        with self.assertRaises(PortfolioArtifactChanged):
            self.archive.get(RUN)
        self.assertEqual(self.archive.list()["runs"][0]["receipt"]["status"], "invalid")
        receipt["run_id"] = "b" * 32
        (directory / "receipt.json").write_text(json.dumps(receipt))
        with self.assertRaises(ValueError):
            self.archive.get(RUN)

    def test_versioned_snapshot_verified_without_exposing_or_requiring_current_sources(self):
        from research_center.portfolio_provenance import SOURCE_FILES, snapshot_sources
        source_root = Path(self.temp.name) / "synthetic-code"
        for name in SOURCE_FILES:
            path = source_root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("# PRIVATE-SOURCE-SNAPSHOT-SENTINEL\n")
        snapshot = snapshot_sources(source_root)
        directory, receipt = fixture(self.root)
        raw = json.dumps(snapshot).encode()
        (directory / "source_snapshot.json").write_bytes(raw)
        receipt.update(schema_version=2, code_version=snapshot["source_version"])
        receipt["artifact_sha256"]["source_snapshot.json"] = hashlib.sha256(raw).hexdigest()
        result = json.loads((directory / "result.json").read_text())
        result["provenance"] = {"source_version": snapshot["source_version"], "runtime": snapshot["runtime"]}
        raw = json.dumps(result).encode()
        (directory / "result.json").write_bytes(raw)
        receipt["artifact_sha256"]["result.json"] = hashlib.sha256(raw).hexdigest()
        (directory / "receipt.json").write_text(json.dumps(receipt))
        (source_root / SOURCE_FILES[0]).write_text("# newer current source\n")
        value = self.archive.get(RUN)
        self.assertEqual(value["receipt"]["source_snapshot_status"], "archived_and_verified")
        self.assertEqual(value["receipt"]["code_version"], snapshot["source_version"])
        self.assertNotIn("PRIVATE-SOURCE-SNAPSHOT-SENTINEL", json.dumps(value))
        receipt["code_version"] = "0" * 64
        (directory / "receipt.json").write_text(json.dumps(receipt))
        with self.assertRaises(PortfolioArtifactChanged):
            self.archive.get(RUN)

    def test_running_and_failed_records_do_not_read_partial_result(self):
        directory, receipt = fixture(self.root)
        for status in ("running", "failed"):
            receipt["status"] = status
            (directory / "receipt.json").write_text(json.dumps(receipt))
            (directory / "result.json").write_text("partial invalid JSON")
            self.assertIsNone(self.archive.get(RUN)["result"])

    def test_symlinks_and_traversal_rejected(self):
        directory, _ = fixture(self.root)
        (directory / "state.json").unlink()
        (directory / "state.json").symlink_to(directory / "config.json")
        with self.assertRaises(OSError):
            self.archive.get(RUN)
        with self.assertRaises(ValueError):
            self.archive.get("../" + RUN)

    def test_inventory_bounds_do_not_modify_unknown_entries(self):
        fixture(self.root)
        for index in range(1002):
            (self.root / ("pending-" + str(index))).touch()
        self.assertTrue(self.archive.list()["truncated"])
        self.assertEqual(len(list(self.root.glob("pending-*"))), 1002)


class PortfolioArchiveHttpTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = RunStore(Path(self.temp.name) / "research")
        self.directory, _ = fixture(archive_root(self.store))
        self.server = make_server(self.store, 0)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.close)

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()

    def request(self, path, method="GET", host=None, *, return_headers=False):
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port)
        connection.request(method, path, headers={} if host is None else {"Host": host})
        response = connection.getresponse()
        data = response.read()
        headers = dict(response.getheaders())
        connection.close()
        return (response.status, data, headers) if return_headers else (response.status, data)

    def test_reads_integrity_conflict_and_local_only_method_guard(self):
        status, data = self.request("/api/research/portfolios")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(data)["runs"][0]["receipt"]["run_id"], RUN)
        self.assertEqual(self.request("/api/research/portfolios/" + RUN)[0], 200)
        (self.directory / "result.json").write_text("{}")
        self.assertEqual(self.request("/api/research/portfolios/" + RUN)[0], 409)
        self.assertEqual(self.request("/api/research/portfolios", "POST")[0], 405)
        for method in ("POST", "PUT", "DELETE", "PATCH", "OPTIONS", "HEAD"):
            status, _, headers = self.request("/api/research/portfolios", method, return_headers=True)
            self.assertEqual(status, 405)
            self.assertEqual(headers["Allow"], "GET")
        self.assertEqual(self.request("/api/research/portfolios", "HEAD")[1], b"")
        self.assertEqual(self.request("/api/research/portfolios", host="outside.example")[0], 403)


if __name__ == "__main__":
    unittest.main()
