from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import stat
import tempfile
import unittest
from unittest.mock import patch

from research_center import custom_source_store as store_module
from research_center.custom_source_store import (
    INTERFACE_CONTRACT,
    MAX_PARAMS_BYTES,
    MAX_PARAMS_DEPTH,
    MAX_PARAMS_NODES,
    MAX_PARAM_STRING_CHARS,
    MAX_SOURCE_BYTES,
    CustomSourceError,
    CustomSourceStore,
)


SOURCE = b"def build_strategy(params):\n    return None\n"


def error_code(test: unittest.TestCase, expected: str, operation) -> None:
    with test.assertRaises(CustomSourceError) as caught:
        operation()
    test.assertEqual(caught.exception.code, expected)


class CustomSourceStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.base = Path(self.tempdir.name)
        self.root = self.base / "custom_sources"
        self.store = CustomSourceStore(self.root)
        self.addCleanup(self.tempdir.cleanup)

    def test_fifo_corruption_is_rejected_without_blocking(self) -> None:
        record = self.store.register(SOURCE, name="fifo")
        path = self.root / record['registration_id'] / 'source.py'
        path.unlink()
        os.mkfifo(path, 0o600)
        error_code(self, 'record_corrupt', lambda: self.store.read_source(record['registration_id']))
        self.assertEqual(self.store.list()['corrupt_count'], 1)

    def test_nested_credential_variants_are_rejected(self) -> None:
        for key in ('Client_Secret', 'REFRESH_TOKEN', 'private_key', 'PassWd',
                    'apiKey', 'clientSecret', 'refreshToken', 'private-key'):
            with self.subTest(key=key):
                error_code(self, 'credential_param', lambda: self.store.register(
                    SOURCE, name='credential', default_params={'nested': {key: 'owned-test-only'}}))

    def test_timestamp_and_schema_tampering_is_rejected(self) -> None:
        record = self.store.register(SOURCE, name='integrity')
        path = self.root / record['registration_id'] / 'record.json'
        for key, value in [('created_at', '2030-01-01T00:00:00+00:00'), ('schema_version', True), ('schema_version', 1.0)]:
            changed = dict(record)
            changed[key] = value
            # Also re-seal schema variants to verify strict type independently.
            if key == 'schema_version':
                unsigned = {k: v for k, v in changed.items() if k != 'record_sha256'}
                changed['record_sha256'] = hashlib.sha256(store_module._canonical_json_bytes(unsigned)).hexdigest()
            path.write_text(json.dumps(changed))
            error_code(self, 'record_corrupt', lambda: self.store.show(record['registration_id']))

    def test_register_only_parses_source_and_preserves_original_bytes(self) -> None:
        canary = self.base / "source_was_executed"
        source = (
            "from pathlib import Path\n"
            f"Path({str(canary)!r}).write_text('executed')\n"
            "def build_strategy(params):\n"
            "    return None\n"
        ).encode("utf-8")

        record = self.store.register(source, name="语法检查样例")

        self.assertFalse(canary.exists())
        self.assertEqual(self.store.read_source(record["registration_id"]), source)
        self.assertEqual(record["source_sha256"], hashlib.sha256(source).hexdigest())
        self.assertEqual(record["code_version"], record["source_sha256"])
        self.assertEqual(record["declared_import_roots"], ["pathlib"])
        self.assertEqual(record["import_declaration_status"], "DECLARED_NOT_VERIFIED")
        self.assertEqual(record["interface_contract"], INTERFACE_CONTRACT)
        self.assertFalse(record["runtime_interface_verified"])
        self.assertFalse(record["dependencies_verified"])
        self.assertFalse(record["paper_admission"])
        self.assertEqual(record["mode"], "RESEARCH_ONLY")
        public_record = self.store.show(record["registration_id"])
        self.assertNotIn("source", public_record)
        self.assertNotIn("source.py", json.dumps(public_record))

        entry = self.root / record["registration_id"]
        self.assertEqual(stat.S_IMODE(self.root.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(entry.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE((entry / "record.json").stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE((entry / "source.py").stat().st_mode), 0o600)

    def test_bom_syntax_error_and_source_limits_are_safe(self) -> None:
        source = b"\xef\xbb\xbfdef build_strategy(params):\n    return None\n"
        record = self.store.register(source, name="BOM")
        self.assertEqual(self.store.read_source(record["registration_id"]), source)

        secret = "SOURCE_SECRET_MUST_NOT_LEAK"
        invalid = f"def build_strategy(:  # {secret}\n    return None\n".encode()
        with self.assertRaises(CustomSourceError) as caught:
            self.store.register(invalid, name="bad")
        self.assertEqual(caught.exception.code, "source_syntax")
        self.assertEqual(caught.exception.lineno, 1)
        self.assertIsNone(caught.exception.__context__)
        self.assertNotIn(secret, str(caught.exception))
        self.assertNotIn(secret, repr(caught.exception))

        error_code(self, "source_type", lambda: self.store.register(bytearray(SOURCE), name="bad"))
        error_code(
            self,
            "source_too_large",
            lambda: self.store.register(b"#" * (MAX_SOURCE_BYTES + 1), name="bad"),
        )
        error_code(
            self,
            "source_entrypoint",
            lambda: self.store.register(b"async def build_strategy():\n    return None\n", name="bad"),
        )

    def test_registration_is_idempotent_and_hashes_metadata_separately_from_code(self) -> None:
        caller_params = {"nested": {"period": 5}}
        first = self.store.register(
            SOURCE,
            name="first",
            description="原始记录",
            default_params={"period": 5, "token": "this-key-name-is-allowed", **caller_params},
        )
        caller_params["nested"]["period"] = 99
        repeated = self.store.register(
            SOURCE,
            name="first",
            description="原始记录",
            default_params={"period": 5, "token": "this-key-name-is-allowed", "nested": {"period": 5}},
        )
        changed_metadata = self.store.register(SOURCE, name="renamed", description="另一份注册")
        child = self.store.register(SOURCE, name="child", parent_id=first["registration_id"])

        self.assertEqual(repeated, first)
        self.assertEqual(first["default_params"]["nested"]["period"], 5)
        self.assertNotEqual(first["registration_id"], first["code_version"])
        self.assertNotEqual(changed_metadata["registration_id"], first["registration_id"])
        self.assertEqual(changed_metadata["code_version"], first["code_version"])
        self.assertEqual(child["parent_id"], first["registration_id"])
        self.assertEqual(self.store.show(first["registration_id"]), first)

        error_code(
            self,
            "parent_not_found",
            lambda: self.store.register(SOURCE, name="orphan", parent_id="f" * 64),
        )
        self.assertFalse(self.store.list()["empty"])

    def test_params_are_strict_bounded_finite_and_credential_free(self) -> None:
        valid = self.store.register(
            SOURCE,
            name="params",
            default_params={"token": "ordinary field", "enabled": True, "value": [1, 2.5, None]},
        )
        self.assertEqual(valid["default_params"]["token"], "ordinary field")

        cases = (
            ("credential_param", {"nested": {"API_KEY": "do-not-store"}}),
            ("credential_param", {"access_token": "do-not-store"}),
            ("params_invalid", {"value": float("nan")}),
            ("params_invalid", {"value": float("inf")}),
            ("params_too_large", {"value": "x" * (MAX_PARAM_STRING_CHARS + 1)}),
            ("params_too_large", {"value": "\x01" * 3000}),
            ("params_invalid", {"value": object()}),
        )
        for expected, params in cases:
            with self.subTest(expected=expected, keys=tuple(params)):
                error_code(
                    self,
                    expected,
                    lambda params=params: self.store.register(SOURCE, name="bad", default_params=params),
                )

        nested = None
        for _ in range(MAX_PARAMS_DEPTH + 1):
            nested = [nested]
        error_code(
            self,
            "params_invalid",
            lambda: self.store.register(SOURCE, name="deep", default_params={"value": nested}),
        )
        error_code(
            self,
            "params_invalid",
            lambda: self.store.register(
                SOURCE,
                name="many",
                default_params={"values": [None] * MAX_PARAMS_NODES},
            ),
        )
        error_code(
            self,
            "params_too_large",
            lambda: self.store.register(SOURCE, name="bytes", default_params={"value": "é" * MAX_PARAMS_BYTES}),
        )

    def test_metadata_control_characters_and_absolute_name_are_not_retained(self) -> None:
        error_code(self, "metadata_invalid", lambda: self.store.register(SOURCE, name="bad\nname"))
        error_code(self, "metadata_invalid", lambda: self.store.register(SOURCE, name=""))
        error_code(
            self,
            "metadata_invalid",
            lambda: self.store.register(SOURCE, name=str(self.base / "uploaded.py")),
        )
        self.assertTrue(self.store.register(SOURCE, name="good", description="")["registration_id"])

    def test_tampering_symlinks_and_traversal_fail_closed(self) -> None:
        record = self.store.register(SOURCE, name="protected")
        entry = self.root / record["registration_id"]
        outside = self.base / "outside.py"
        outside.write_bytes(SOURCE)

        source_path = entry / "source.py"
        source_path.unlink()
        source_path.symlink_to(outside)
        error_code(self, "record_corrupt", lambda: self.store.show(record["registration_id"]))
        error_code(self, "invalid_id", lambda: self.store.read_source("../outside.py"))

        metadata_record = self.store.register(SOURCE, name="metadata-protected")
        metadata_path = self.root / metadata_record["registration_id"] / "record.json"
        tampered = json.loads(metadata_path.read_text(encoding="utf-8"))
        tampered["name"] = "changed"
        metadata_path.write_text(json.dumps(tampered), encoding="utf-8")
        error_code(self, "record_corrupt", lambda: self.store.show(metadata_record["registration_id"]))

    def test_list_is_read_only_and_empty_root_is_not_created(self) -> None:
        listing = self.store.list()
        self.assertEqual(listing["sources"], [])
        self.assertEqual(listing["status"], "empty")
        self.assertTrue(listing["complete"])
        self.assertTrue(listing["empty"])
        self.assertFalse(self.root.exists())

    def test_list_reports_corruption_and_bounded_truncation(self) -> None:
        records = [self.store.register(SOURCE, name=f"source-{index}") for index in range(4)]
        corrupt_id = "0" * 64
        (self.root / corrupt_id).symlink_to(self.base, target_is_directory=True)

        with (
            patch.object(store_module, "MAX_DIRECTORY_ENTRIES", 100),
            patch.object(store_module, "MAX_LIST_RECORDS", 2),
        ):
            limited = self.store.list()
        self.assertLessEqual(limited["returned_count"], 2)
        self.assertEqual(limited["scanned_valid_count"], len(records))
        self.assertEqual(limited["corrupt_count"], 1)
        self.assertEqual(limited["status"], "truncated")
        self.assertFalse(limited["complete"])
        self.assertFalse(limited["empty"])
        self.assertTrue(limited["records_truncated"])

        with (
            patch.object(store_module, "MAX_DIRECTORY_ENTRIES", 3),
            patch.object(store_module, "MAX_LIST_RECORDS", 2),
        ):
            scan_limited = self.store.list()
        self.assertLessEqual(scan_limited["scanned_entries"], 3)
        self.assertTrue(scan_limited["scan_limit_reached"])
        self.assertFalse(scan_limited["complete"])
        self.assertNotEqual(scan_limited["status"], "empty")


if __name__ == "__main__":
    unittest.main()
