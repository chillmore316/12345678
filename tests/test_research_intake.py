import concurrent.futures
import datetime
import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from research_center.intake import CandidateIntegrityError, CandidateStore


SYMBOLS = ["501018.SH", "161226.SZ"]


def make_rows(count=12):
    start = datetime.date(2026, 1, 1)
    rows = []
    for index in range(count):
        day = start + datetime.timedelta(days=index // 2)
        rows.append(
            {
                "ts_code": SYMBOLS[index % 2],
                "trade_date": day.strftime("%Y%m%d"),
                "open": 10.0 + index / 100,
                "high": 10.5 + index / 100,
                "low": 9.5 + index / 100,
                "close": 10.1 + index / 100,
                "pre_close": 10.0 + index / 100,
                "vol": 100.0,
                "amount": 1000.0,
            }
        )
    return rows


def write_source(directory, rows=None, *, manifest_changes=None, rows_raw=None, manifest_raw=None):
    directory.mkdir(parents=True, exist_ok=True)
    rows = make_rows() if rows is None else rows
    if rows_raw is None:
        # Emit NaN/Infinity spellings for invalid-input cases so the reader can
        # prove that the non-standard JSON constants are rejected.
        rows_raw = json.dumps(rows, ensure_ascii=False, allow_nan=True, separators=(",", ":")).encode("utf-8")
    symbols = sorted(
        {row["ts_code"] for row in rows if isinstance(row, dict) and isinstance(row.get("ts_code"), str)}
    )
    manifest = {
        "mode": "RESEARCH_ONLY",
        "status": "ISOLATED_CANDIDATE_NOT_ADMITTED",
        "row_count": len(rows),
        "symbols": symbols,
        "rows_sha256": hashlib.sha256(rows_raw).hexdigest(),
        "paper_admission": False,
        "candidate_promoted_to_runtime": False,
        "price_basis_verified": False,
        "upstream_license_verified": False,
    }
    if manifest_changes:
        manifest.update(manifest_changes)
    if manifest_raw is None:
        manifest_raw = json.dumps(
            manifest, ensure_ascii=False, allow_nan=False, separators=(",", ":")
        ).encode("utf-8")
    manifest_path = directory / "manifest.json"
    rows_path = directory / "rows.json"
    manifest_path.write_bytes(manifest_raw)
    rows_path.write_bytes(rows_raw)
    return manifest_path, rows_path, manifest_raw, rows_raw


class ResearchIntakeTests(unittest.TestCase):
    def setUp(self):
        self._temporary = tempfile.TemporaryDirectory(prefix="research-intake-test-")
        self.tmp_path = Path(self._temporary.name)

    def tearDown(self):
        self._temporary.cleanup()

    def store_at(self):
        return CandidateStore(self.tmp_path / "run-store")

    def test_register_freezes_bytes_and_duplicate_registration_is_idempotent(self):
        manifest_path, rows_path, manifest_bytes, rows_bytes = write_source(self.tmp_path / "source")
        source_before = (manifest_path.read_bytes(), rows_path.read_bytes())
        store = self.store_at()

        first = store.register(manifest_path, rows_path, "第一份候选")
        self.assertEqual((manifest_path.read_bytes(), rows_path.read_bytes()), source_before)
        candidate_id = first["record"]["candidate_id"]
        self.assertEqual(first["record"]["row_count"], 12)
        self.assertEqual(first["record"]["symbols"], sorted(SYMBOLS))
        self.assertEqual(first["record"]["manifest_sha256"], hashlib.sha256(manifest_bytes).hexdigest())
        self.assertEqual(first["record"]["rows_sha256"], hashlib.sha256(rows_bytes).hexdigest())
        self.assertEqual(first["record"]["status"], "CANDIDATE_ONLY")
        self.assertEqual(first["record"]["mode"], "RESEARCH_ONLY")
        self.assertIs(first["record"]["paper_admission"], False)
        self.assertIs(first["record"]["candidate_promoted_to_runtime"], False)
        self.assertIs(first["record"]["source_evidence_copied"], False)

        rows_path.write_text("source changed after registration", encoding="utf-8")
        self.assertEqual(store.rows_bytes(candidate_id), rows_bytes)
        self.assertEqual(store.detail(candidate_id)["record"]["rows_sha256"], hashlib.sha256(rows_bytes).hexdigest())

        replacement_manifest, replacement_rows, _, _ = write_source(self.tmp_path / "same-bytes")
        duplicate = store.register(replacement_manifest, replacement_rows, "新标签不覆盖旧记录")
        self.assertEqual(duplicate["record"]["candidate_id"], candidate_id)
        self.assertEqual(duplicate["record"]["label"], "第一份候选")
        self.assertEqual(duplicate["record"]["created_at"], first["record"]["created_at"])
        self.assertEqual(
            (manifest_path.read_bytes(), rows_path.read_bytes()),
            (source_before[0], b"source changed after registration"),
        )

    def test_detail_verifies_bytes_and_preview_is_limited_to_fifty(self):
        manifest_path, rows_path, _, _ = write_source(self.tmp_path / "source", make_rows(72))
        store = self.store_at()
        result = store.register(manifest_path, rows_path)
        candidate_id = result["record"]["candidate_id"]

        detail = store.detail(candidate_id)
        self.assertEqual(len(detail["preview_rows"]), 50)
        self.assertEqual(detail["validation"]["checked_row_count"], 72)
        self.assertIs(detail["validation"]["source_certification_independently_verified"], False)
        self.assertIs(detail["validation"]["paper_admission"], False)
        self.assertIs(detail["validation"]["candidate_promoted_to_runtime"], False)

        stored_rows = store.root / "data_candidates" / candidate_id / "rows.json"
        tampered = json.loads(stored_rows.read_text(encoding="utf-8"))
        tampered[0]["close"] = 999.0
        stored_rows.write_text(json.dumps(tampered), encoding="utf-8")
        with self.assertRaises(CandidateIntegrityError):
            store.detail(candidate_id)
        with self.assertRaises(CandidateIntegrityError):
            store.rows_bytes(candidate_id)
        listed = next(item for item in store.list()["candidates"] if item["candidate_id"] == candidate_id)
        self.assertEqual(listed["summary_validation"], "NOT_CHECKED")
        self.assertIn("未逐项校验", listed["message"])

    def test_detail_rejects_symlinked_frozen_files(self):
        manifest_path, rows_path, _, _ = write_source(self.tmp_path / "source")
        store = self.store_at()
        candidate_id = store.register(manifest_path, rows_path)["record"]["candidate_id"]
        stored_rows = store.root / "data_candidates" / candidate_id / "rows.json"
        stored_rows.unlink()
        stored_rows.symlink_to(rows_path)
        with self.assertRaisesRegex(CandidateIntegrityError, "符号链接"):
            store.detail(candidate_id)

    def test_optional_numeric_fields_are_allowed_and_unknown_fields_are_rejected(self):
        rows = make_rows()
        rows[0]["change"] = -0.04
        rows[0]["pct_chg"] = -0.39
        manifest_path, rows_path, _, _ = write_source(self.tmp_path / "source", rows)
        result = self.store_at().register(manifest_path, rows_path)
        self.assertEqual(result["preview_rows"][0]["change"], -0.04)
        self.assertEqual(result["preview_rows"][0]["pct_chg"], -0.39)

        rows = make_rows()
        rows[0]["vendor_note"] = "extra"
        manifest_path, rows_path, _, _ = write_source(self.tmp_path / "unknown", rows)
        with self.assertRaisesRegex(CandidateIntegrityError, "九字段投影"):
            self.store_at().register(manifest_path, rows_path)

    def test_rows_validation_rejects_invalid_content(self):
        cases = [
            (lambda rows: rows[0].update(trade_date="20260230"), "日期"),
            (lambda rows: rows[0].update(ts_code="ABC.SH"), "证券代码"),
            (lambda rows: rows[0].update(close=True), "有限数值"),
            (lambda rows: rows[0].update(high=1.0), "high 关系"),
            (lambda rows: rows[0].update(vol=0.0, amount=1.0), "零成交量"),
            (lambda rows: rows[2].update(trade_date=rows[0]["trade_date"]), "重复证券日期"),
            (lambda rows: rows[0].update(change=float("inf")), "UTF-8 JSON"),
            (lambda rows: rows[0].update(vol=-1.0), "不能为负数"),
            (lambda rows: rows[0].update(vol=10**500), "有限数值"),
            (lambda rows: rows[0].update(low=10.3), "low 关系"),
        ]
        for index, (mutation, message) in enumerate(cases):
            with self.subTest(case=index, message=message):
                rows = make_rows()
                mutation(rows)
                manifest_path, rows_path, _, _ = write_source(self.tmp_path / "source", rows)
                with self.assertRaisesRegex(CandidateIntegrityError, message):
                    self.store_at().register(manifest_path, rows_path)

    def test_manifest_validation_rejects_unsafe_claims(self):
        cases = [
            ({"mode": "SHADOW_ONLY"}, "mode"),
            ({"status": "ADMITTED"}, "status"),
            ({"row_count": True}, "row_count"),
            ({"paper_admission": 0}, "paper_admission"),
            ({"candidate_promoted_to_runtime": True}, "candidate_promoted"),
            ({"symbols": SYMBOLS + SYMBOLS}, "symbols"),
            ({"untrusted_admission_claim": "unknown"}, "准入"),
        ]
        for changes, message in cases:
            with self.subTest(changes=changes):
                manifest_path, rows_path, _, _ = write_source(
                    self.tmp_path / "source", manifest_changes=changes
                )
                with self.assertRaisesRegex(CandidateIntegrityError, message):
                    self.store_at().register(manifest_path, rows_path)

    def test_duplicate_json_keys_nonfinite_json_and_rows_hash_mismatch_fail_closed(self):
        rows = make_rows()
        manifest_path, rows_path, _, _ = write_source(self.tmp_path / "duplicate", rows)
        raw = manifest_path.read_bytes().replace(b'"row_count":12', b'"row_count":12,"row_count":12')
        manifest_path.write_bytes(raw)
        with self.assertRaisesRegex(CandidateIntegrityError, "UTF-8 JSON"):
            self.store_at().register(manifest_path, rows_path)

        raw_rows = json.dumps(rows, separators=(",", ":")).replace('"close":10.1', '"close":NaN').encode()
        manifest_path, rows_path, _, _ = write_source(self.tmp_path / "nonfinite", rows, rows_raw=raw_rows)
        with self.assertRaisesRegex(CandidateIntegrityError, "UTF-8 JSON"):
            self.store_at().register(manifest_path, rows_path)

        manifest_path, rows_path, _, _ = write_source(self.tmp_path / "wrong-hash", rows)
        changed_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        changed_manifest["rows_sha256"] = "0" * 64
        manifest_path.write_text(json.dumps(changed_manifest), encoding="utf-8")
        with self.assertRaisesRegex(CandidateIntegrityError, "rows_sha256"):
            self.store_at().register(manifest_path, rows_path)

    def test_symlink_fifo_size_and_source_inside_store_are_rejected(self):
        source_dir = self.tmp_path / "source"
        manifest_path, rows_path, _, _ = write_source(source_dir)
        link = source_dir / "manifest-link.json"
        link.symlink_to(manifest_path)
        with self.assertRaisesRegex(CandidateIntegrityError, "符号链接"):
            self.store_at().register(link, rows_path)

        fifo = source_dir / "pipe.json"
        os.mkfifo(fifo)
        with self.assertRaisesRegex(CandidateIntegrityError, "普通文件"):
            self.store_at().register(fifo, rows_path)

        store = self.store_at()
        store.root.mkdir(parents=True)
        internal = store.root / "source.json"
        internal.write_text("{}", encoding="utf-8")
        with self.assertRaisesRegex(CandidateIntegrityError, "自身导入"):
            store.register(internal, rows_path)

        too_large_rows = source_dir / "too-large-rows.json"
        too_large_rows.write_bytes(b" " * (8 * 1024 * 1024 + 1))
        with self.assertRaisesRegex(CandidateIntegrityError, "大小上限"):
            store.register(manifest_path, too_large_rows)

        too_large_manifest = source_dir / "too-large-manifest.json"
        too_large_manifest.write_bytes(b" " * (1024 * 1024 + 1))
        with self.assertRaisesRegex(CandidateIntegrityError, "大小上限"):
            store.register(too_large_manifest, rows_path)

        oversized_rows = make_rows(10_001)
        oversized_manifest, oversized_rows_path, _, _ = write_source(source_dir / "too-many", oversized_rows)
        with self.assertRaisesRegex(CandidateIntegrityError, "10000 行"):
            store.register(oversized_manifest, oversized_rows_path)

    def test_concurrent_registration_is_atomic_and_idempotent(self):
        manifest_path, rows_path, _, _ = write_source(self.tmp_path / "source")
        stores = [self.store_at(), self.store_at()]
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            results = list(
                pool.map(
                    lambda args: args[0].register(manifest_path, rows_path, args[1]),
                    [(stores[0], "label-a"), (stores[1], "label-b")],
                )
            )
        ids = {result["record"]["candidate_id"] for result in results}
        self.assertEqual(len(ids), 1)
        candidates = stores[0].list()["candidates"]
        self.assertEqual([item["candidate_id"] for item in candidates], list(ids))
        self.assertIn(candidates[0]["label"], {"label-a", "label-b"})
        self.assertEqual(list((stores[0].root / "data_candidates").glob(".pending-*")), [])

    def test_list_is_read_only_ignores_pending_and_marks_bad_records(self):
        store = self.store_at()
        self.assertEqual(store.list(), {"candidates": [], "truncated": False})
        self.assertFalse(store.root.exists())

        candidate_root = store.root / "data_candidates"
        candidate_root.mkdir(parents=True)
        pending = candidate_root / ".pending-crash-leftover"
        pending.mkdir()
        (pending / "partial").write_text("leave me", encoding="utf-8")
        self.assertEqual(store.list(), {"candidates": [], "truncated": False})
        self.assertTrue((pending / "partial").exists())

        candidate_id = "a" * 32
        corrupt = candidate_root / candidate_id
        corrupt.mkdir()
        (corrupt / "record.json").write_text("{bad json", encoding="utf-8")
        result = store.list()
        self.assertEqual(len(result["candidates"]), 1)
        self.assertEqual(result["candidates"][0]["candidate_id"], candidate_id)
        self.assertEqual(result["candidates"][0]["summary_validation"], "RECORD_INVALID")
        self.assertEqual(result["candidates"][0]["status"], "CHECK_REQUIRED")

    def test_list_is_capped_and_truncated_is_accurate(self):
        store = self.store_at()
        root = store.root / "data_candidates"
        root.mkdir(parents=True)
        for index in range(101):
            directory = root / f"{index:032x}"
            directory.mkdir()
            (directory / "record.json").write_text("{}", encoding="utf-8")
        with patch.object(store, '_load_record', wraps=store._load_record) as read_record:
            result = store.list()
        self.assertLessEqual(read_record.call_count, 100)
        self.assertEqual(len(result["candidates"]), 100)
        self.assertIs(result["truncated"], True)

    def test_directory_scan_has_a_bound_and_keeps_unfinished_entries(self):
        store = self.store_at()
        root = store.root / 'data_candidates'
        root.mkdir(parents=True)
        for index in range(1002):
            (root / ('.pending-' + str(index))).mkdir()
        result = store.list()
        self.assertEqual(result['candidates'], [])
        self.assertTrue(result['truncated'])
        self.assertEqual(len(list(root.iterdir())), 1002)

    def test_wide_symbol_snapshot_fits_record_and_list_preview_is_bounded(self):
        rows = make_rows(5500)
        for index, row in enumerate(rows):
            row['ts_code'] = f'{index:06d}.SH'
            row['trade_date'] = '20260101'
        manifest, data, _, _ = write_source(self.tmp_path / 'wide', rows)
        store = self.store_at()
        registered = store.register(manifest, data)
        self.assertEqual(len(registered['record']['symbols']), 5500)
        summary = store.list()['candidates'][0]
        self.assertEqual(summary['symbol_count'], 5500)
        self.assertEqual(len(summary['symbols']), 3)
        self.assertEqual(len(store.detail(registered['record']['candidate_id'])['preview_rows']), 50)

    def test_ids_cannot_escape_and_invalid_labels_fail(self):
        store = self.store_at()
        for invalid in ("../etc/passwd", "A" * 32, "0" * 31, "0" * 32 + "/x"):
            with self.subTest(candidate_id=invalid):
                with self.assertRaisesRegex(CandidateIntegrityError, "candidate_id"):
                    store.detail(invalid)
                with self.assertRaisesRegex(CandidateIntegrityError, "candidate_id"):
                    store.rows_bytes(invalid)
        with self.assertRaisesRegex(CandidateIntegrityError, "label"):
            store.register("manifest", "rows", "x" * 121)


if __name__ == "__main__":
    unittest.main()
