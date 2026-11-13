from __future__ import annotations

from copy import deepcopy
import hashlib
import tempfile
from pathlib import Path
import unittest
from unittest.mock import patch

from research_center import portfolio_provenance as provenance


RUNTIME = {
    "python_implementation": "CPython",
    "python_version": "3.11.9",
    "pandas_version": "2.2.3",
    "numpy_version": "2.1.3",
    "pyarrow_version": "18.1.0",
    "platform_system": "Darwin",
    "platform_architecture": "arm64",
}


def write_source_tree(root: Path, content: bytes = b"# synthetic source\n") -> None:
    for relative_path in provenance.SOURCE_FILES:
        path = root / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)


class PortfolioProvenanceTest(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name) / "synthetic"
        self.root.mkdir()
        write_source_tree(self.root)
        self.runtime_patch = patch.object(provenance, "_runtime_metadata", return_value=dict(RUNTIME))
        self.runtime_patch.start()
        self.addCleanup(self.runtime_patch.stop)
        self.addCleanup(self.temp_dir.cleanup)

    def snapshot(self):
        return provenance.snapshot_sources(self.root)

    def test_snapshot_is_read_only_and_deterministic(self):
        before = {
            path: (path.read_bytes(), path.stat().st_mtime_ns)
            for path in self.root.rglob("*.py")
        }

        first = self.snapshot()
        second = self.snapshot()

        self.assertEqual(first, second)
        self.assertEqual(first["schema_version"], 1)
        self.assertEqual(set(first["files"]), set(provenance.SOURCE_FILES))
        self.assertEqual(first["source_version"], provenance._source_version(
            {path: entry["sha256"] for path, entry in first["files"].items()},
            RUNTIME,
        ))
        after = {
            path: (path.read_bytes(), path.stat().st_mtime_ns)
            for path in self.root.rglob("*.py")
        }
        self.assertEqual(before, after)
        provenance.validate_snapshot(first)
        provenance.verify_sources(self.root, first)

    def test_source_change_and_runtime_change_fail_closed(self):
        snapshot = self.snapshot()
        source_path = self.root / provenance.SOURCE_FILES[1]
        source_path.write_bytes(b"# changed synthetic source\n")
        with self.assertRaisesRegex(ValueError, "当前源码与快照不一致"):
            provenance.verify_sources(self.root, snapshot)

        source_path.unlink()
        with self.assertRaisesRegex(ValueError, "缺失或不可读"):
            provenance.verify_sources(self.root, snapshot)

        source_path.write_bytes(b"# synthetic source\n")
        changed_runtime = dict(RUNTIME, numpy_version="2.1.4")
        with patch.object(provenance, "_runtime_metadata", return_value=changed_runtime):
            with self.assertRaisesRegex(ValueError, "当前运行环境与快照不一致"):
                provenance.verify_sources(self.root, snapshot)
        with patch.object(provenance, "_runtime_metadata", side_effect=AssertionError("must be skipped")):
            provenance.verify_sources(self.root, snapshot, check_runtime=False)

    def test_snapshot_body_hash_version_and_runtime_tampering_are_rejected(self):
        original = self.snapshot()

        tampered = deepcopy(original)
        tampered["files"][provenance.SOURCE_FILES[0]]["source"] += "# edited\n"
        with self.assertRaisesRegex(ValueError, "正文与哈希"):
            provenance.validate_snapshot(tampered)

        tampered = deepcopy(original)
        tampered["files"][provenance.SOURCE_FILES[0]]["sha256"] = "0" * 64
        with self.assertRaisesRegex(ValueError, "正文与哈希"):
            provenance.validate_snapshot(tampered)

        tampered = deepcopy(original)
        tampered["source_version"] = "0" * 64
        with self.assertRaisesRegex(ValueError, "版本校验失败"):
            provenance.validate_snapshot(tampered)

        tampered = deepcopy(original)
        tampered["runtime"]["numpy_version"] = "2.1.4"
        with self.assertRaisesRegex(ValueError, "版本校验失败"):
            provenance.validate_snapshot(tampered)

        tampered = deepcopy(original)
        tampered["runtime"]["platform_system"] = "/Users/example"
        with self.assertRaisesRegex(ValueError, "运行环境信息无效"):
            provenance.validate_snapshot(tampered)

    def test_non_allowlisted_paths_and_root_traversal_are_rejected(self):
        snapshot = self.snapshot()
        snapshot["files"]["../../outside.py"] = {"sha256": "0" * 64, "source": ""}
        with self.assertRaisesRegex(ValueError, "文件清单无效"):
            provenance.validate_snapshot(snapshot)

        with self.assertRaisesRegex(ValueError, "源码目录无效"):
            provenance.snapshot_sources(self.root / ".." / "synthetic")

    def test_symlink_and_non_regular_source_are_rejected(self):
        link_path = self.root / provenance.SOURCE_FILES[0]
        link_path.unlink()
        outside = Path(self.temp_dir.name) / "outside.py"
        outside.write_bytes(b"# outside\n")
        link_path.symlink_to(outside)
        with self.assertRaisesRegex(ValueError, "缺失或不可读"):
            self.snapshot()

        link_path.unlink()
        link_path.mkdir()
        with self.assertRaisesRegex(ValueError, "文件类型无效"):
            self.snapshot()

    def test_invalid_utf8_and_per_file_size_are_rejected(self):
        path = self.root / provenance.SOURCE_FILES[0]
        path.write_bytes(b"\xff")
        with self.assertRaisesRegex(ValueError, "编码无效"):
            self.snapshot()

        path.write_bytes(b"x" * (provenance.MAX_SOURCE_FILE_BYTES + 1))
        with self.assertRaisesRegex(ValueError, "文件大小超限"):
            self.snapshot()

    def test_total_source_and_serialized_snapshot_limits_are_enforced(self):
        for relative_path in provenance.SOURCE_FILES[:5]:
            (self.root / relative_path).write_bytes(b"x" * 850_000)
        with self.assertRaisesRegex(ValueError, "总大小超限"):
            self.snapshot()

        write_source_tree(self.root)
        for relative_path in provenance.SOURCE_FILES[:5]:
            (self.root / relative_path).write_bytes(b"\x01" * 300_000)
        with self.assertRaisesRegex(ValueError, "序列化大小超限"):
            self.snapshot()

    def test_sha256_is_over_exact_utf8_source_bytes(self):
        snapshot = self.snapshot()
        for relative_path, entry in snapshot["files"].items():
            expected = hashlib.sha256(entry["source"].encode("utf-8")).hexdigest()
            self.assertEqual(entry["sha256"], expected, relative_path)

    def test_descriptor_exhaustion_returns_safe_error(self):
        with patch.object(provenance.os, "dup", side_effect=OSError("PRIVATE-IO-SENTINEL")):
            with self.assertRaises(provenance.PortfolioProvenanceError) as error:
                self.snapshot()
        self.assertNotIn("PRIVATE-IO-SENTINEL", str(error.exception))
        self.assertEqual(str(error.exception), "源码文件缺失或不可读")


if __name__ == "__main__":
    unittest.main()
