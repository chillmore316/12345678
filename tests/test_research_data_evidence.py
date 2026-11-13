from __future__ import annotations

import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import pyarrow as pa
import pyarrow.parquet as pq

from research_center.data_evidence import assess_data


SYMBOLS = ("000001.SZ", "000002.SZ", "600000.SH")


class ResearchDataEvidenceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name) / "raw_daily_probe"
        self.root.mkdir()
        self._write_cache({symbol: 8.13 for symbol in SYMBOLS})
        project_patch = patch("research_center.data_evidence.PROJECT_ROOT", Path(self.tempdir.name))
        self.cache_patch = patch("research_center.data_evidence.RAW_DAILY_PROBE_ROOT", self.root)
        project_patch.start()
        self.addCleanup(project_patch.stop)
        self.cache_patch.start()
        self.addCleanup(self.cache_patch.stop)
        self.addCleanup(self.tempdir.cleanup)

    def _write_cache(self, closes: dict[str, float]) -> None:
        symbol_rows = []
        for symbol in SYMBOLS:
            path = self.root / f"{symbol}.parquet"
            table = pa.table({
                "ts_code": [symbol, symbol],
                "trade_date": ["20240102", "20240103"],
                "close": [closes[symbol], closes[symbol]],
            })
            pq.write_table(table, path)
            symbol_rows.append({
                "symbol": symbol,
                "status": "passed",
                "parquet_file": path.name,
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                "sha256_verified": True,
            })
        manifest = {
            "schema_version": 1,
            "status": "passed",
            "provider": "synthetic_test_fixture",
            "price_basis": "unadjusted",
            "price_basis_verified": False,
            "request_window": {"start_date": "20230101", "end_date": "20251231"},
            "expected_symbols": list(SYMBOLS),
            "symbols": symbol_rows,
            "paper_admission": False,
        }
        (self.root / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")

    @staticmethod
    def _inputs(close: float = 8.13, include_15h: bool = True):
        config = {
            "symbol": "000001.SZSE",
            "start": "2024-01-02",
            "end": "2024-01-03",
        }
        bars = [
            {
                "symbol": "000001.SZSE",
                "source_datetime": "2024-01-02T14:59:00",
                "close": close,
            },
        ]
        if include_15h:
            bars += [
                {
                    "symbol": "000001.SZSE",
                    "source_datetime": "2024-01-02T15:00:00",
                    "close": close,
                },
                {
                    "symbol": "000001.SZSE",
                    "source_datetime": "2024-01-03T15:00:00",
                    "close": close,
                },
            ]
        manifest = {
            "read_only": True,
            "research_only": True,
            "requested_symbol": "000001.SZSE",
            "requested_range_inclusive": ["2024-01-02", "2024-01-03"],
            "price_adjustment": "vendor_qfq_vintage_unresolved",
            "volume_semantics": "not_verified_as_raw_shares",
            "turnover_capacity_semantics": "units_not_certified",
            "calendar_completeness_verified": False,
            "corporate_actions_verified": False,
            "reference_matcher_equivalence": "not_asserted",
        }
        return config, {"bars": bars, "data_manifest": manifest}

    @staticmethod
    def _batch_inputs(symbol: str = "600010.SH", close: float = 8.13):
        config, result = ResearchDataEvidenceTests._inputs(close=close)
        config["symbol"] = symbol
        result["data_manifest"]["requested_symbol"] = symbol
        for bar in result["bars"]:
            bar["symbol"] = symbol
        return config, result

    def _write_batch_cache(
        self,
        symbols: tuple[str, ...] = ("600010.SH",),
        rows_by_symbol: dict[str, list[tuple[str, float]]] | None = None,
    ) -> tuple[Path, str]:
        root = Path(self.tempdir.name) / "raw_daily_batch"
        root.mkdir(exist_ok=True)
        rows_by_symbol = rows_by_symbol or {
            symbol: [("20240102", 8.13), ("20240103", 8.13)] for symbol in symbols
        }
        symbol_rows = []
        for symbol in symbols:
            target = root / f"{symbol}.parquet"
            observations = rows_by_symbol[symbol]
            pq.write_table(pa.table({
                "ts_code": [symbol for _ in observations],
                "trade_date": [trade_date for trade_date, _ in observations],
                "close": [close for _, close in observations],
            }), target)
            symbol_rows.append({
                "checks": {name: True for name in (
                    "amount_numeric_finite_nonnegative",
                    "non_empty",
                    "ohlc_boundaries",
                    "ohlc_numeric_finite_positive",
                    "required_columns",
                    "row_count_below_6000",
                    "symbol_matches",
                    "trade_date_in_range",
                    "trade_date_yyyymmdd",
                    "unique_key",
                    "vol_numeric_finite_nonnegative",
                )},
                "error_type": None,
                "parquet_file": target.name,
                "raw_rows": len(observations),
                "request": {
                    "api_name": "daily",
                    "fields": "ts_code,trade_date,open,high,low,close,pre_close,vol,amount",
                    "parameters": {
                        "end_date": "20251231",
                        "start_date": "20230101",
                        "ts_code": symbol,
                    },
                },
                "sha256": hashlib.sha256(target.read_bytes()).hexdigest(),
                "sha256_verified": True,
                "status": "passed",
                "symbol": symbol,
                "validation_errors": [],
            })
        manifest = {
            "schema_version": 1,
            "created_at_utc": "2026-10-04T00:00:00+00:00",
            "status": "passed",
            "mode": "RESEARCH_ONLY",
            "provider": "official_tushare",
            "price_basis": "unadjusted",
            "price_basis_verified": False,
            "vol_unit": "手",
            "amount_unit": "千元",
            "request_window": {"start_date": "20230101", "end_date": "20251231"},
            "fields": "ts_code,trade_date,open,high,low,close,pre_close,vol,amount",
            "expected_symbols": list(symbols),
            "symbols": symbol_rows,
            "calendar_completeness_verified": False,
            "corporate_actions_verified": False,
            "paper_admission": False,
        }
        manifest_bytes = json.dumps(manifest, sort_keys=True).encode("utf-8")
        (root / "manifest.json").write_bytes(manifest_bytes)
        return root, hashlib.sha256(manifest_bytes).hexdigest()

    def _batch_patch(self, root: Path, manifest_sha256: str, count: int = 1):
        return patch(
            "research_center.data_evidence._RAW_DAILY_BATCH_REFERENCES",
            (("synthetic_batch", root, manifest_sha256, count),),
        )

    @staticmethod
    def _check(assessment, check_id):
        return next(item for item in assessment["checks"] if item["id"] == check_id)

    def test_matching_15h_closes_are_observations_not_certification(self):
        config, result = self._inputs()
        assessment = assess_data(config, result)

        self.assertEqual(assessment["status"], "unverified")
        self.assertFalse(assessment["paper_admission"])
        self.assertEqual(self._check(assessment, "raw_daily_cache_integrity")["status"], "observed")
        comparison = self._check(assessment, "daily_reference")
        self.assertEqual(comparison["status"], "observed")
        self.assertEqual(comparison["evidence"]["paired_days"], 2)
        self.assertFalse(comparison["evidence"]["close_ratio_inversion_used"])
        self.assertEqual(self._check(assessment, "daily_cache_price_basis")["status"], "unverified")
        self.assertEqual(self._check(assessment, "minute_price_basis")["status"], "unverified")

    def test_difference_over_threshold_is_explicit_mismatch(self):
        config, result = self._inputs(close=8.15)
        assessment = assess_data(config, result)

        comparison = self._check(assessment, "daily_reference")
        self.assertEqual(comparison["status"], "mismatch")
        self.assertEqual(assessment["status"], "mismatch")
        self.assertEqual(comparison["evidence"]["mismatch_days"], 2)

    def test_missing_15h_bar_does_not_fall_back_to_intraday_close(self):
        config, result = self._inputs(include_15h=False)
        assessment = assess_data(config, result)

        comparison = self._check(assessment, "daily_reference")
        self.assertEqual(comparison["status"], "unverified")
        self.assertEqual(comparison["evidence"]["paired_days"], 0)
        self.assertEqual(comparison["evidence"]["run_15h_days"], 0)

    def test_partial_date_pairing_remains_unverified(self):
        config, result = self._inputs()
        result["bars"] = result["bars"][:-1]
        assessment = assess_data(config, result)

        comparison = self._check(assessment, "daily_reference")
        self.assertEqual(comparison["status"], "unverified")
        self.assertEqual(comparison["evidence"]["paired_days"], 1)
        self.assertEqual(comparison["evidence"]["missing_15h_for_unpaired_cache_days"], ["20240103"])

    def test_unknown_symbol_returns_unknown_without_cache_access(self):
        config, result = self._inputs()
        config["symbol"] = "688888.SSE"
        result["data_manifest"]["requested_symbol"] = "688888.SSE"
        assessment = assess_data(config, result)

        self.assertEqual(assessment["status"], "unverified")
        self.assertFalse(self._check(assessment, "raw_daily_cache_integrity")["evidence"]["read_performed"])
        self.assertFalse(self._check(assessment, "daily_reference")["evidence"]["read_performed"])

    def test_hash_mismatch_fails_closed(self):
        config, result = self._inputs()
        target = self.root / "000001.SZ.parquet"
        target.write_bytes(target.read_bytes() + b"changed")
        with patch(
            "research_center.data_evidence._read_batch_reference",
            side_effect=AssertionError("legacy cache failure must not fall through"),
        ):
            assessment = assess_data(config, result)

        integrity = self._check(assessment, "raw_daily_cache_integrity")
        self.assertEqual(integrity["status"], "mismatch")
        self.assertEqual(integrity["evidence"]["issue"], "cache_file_sha256_mismatch")
        self.assertEqual(self._check(assessment, "daily_reference")["status"], "unverified")

    def test_symlink_cache_file_is_rejected(self):
        config, result = self._inputs()
        target = self.root / "000001.SZ.parquet"
        outside = self.root.parent / "outside.parquet"
        outside.write_bytes(target.read_bytes())
        target.unlink()
        target.symlink_to(outside)
        assessment = assess_data(config, result)

        integrity = self._check(assessment, "raw_daily_cache_integrity")
        self.assertEqual(integrity["status"], "mismatch")
        self.assertEqual(integrity["evidence"]["issue"], "cache_file_not_regular")

    def test_config_outside_training_window_is_rejected_before_cache_read(self):
        config, result = self._inputs()
        config["start"] = "2026-01-02"
        config["end"] = "2026-01-03"
        assessment = assess_data(config, result)

        self.assertEqual(assessment["status"], "mismatch")
        self.assertEqual(self._check(assessment, "input_scope")["status"], "mismatch")
        self.assertFalse(self._check(assessment, "daily_reference")["evidence"]["read_performed"])

    def test_batch_member_close_match_is_observation_only(self):
        root, manifest_hash = self._write_batch_cache()
        config, result = self._batch_inputs()
        with self._batch_patch(root, manifest_hash):
            assessment = assess_data(config, result)

        integrity = self._check(assessment, "raw_daily_cache_integrity")
        comparison = self._check(assessment, "daily_reference")
        self.assertEqual(integrity["status"], "observed")
        self.assertEqual(integrity["evidence"]["reference_cache"], "synthetic_batch")
        self.assertTrue(integrity["evidence"]["manifest_sha256_pinned"])
        self.assertEqual(integrity["evidence"]["target_file"], "600010.SH.parquet")
        self.assertEqual(comparison["status"], "observed")
        self.assertEqual(comparison["evidence"]["paired_days"], 2)
        self.assertEqual(self._check(assessment, "minute_price_basis")["status"], "unverified")
        self.assertEqual(self._check(assessment, "daily_cache_price_basis")["status"], "unverified")
        self.assertEqual(self._check(assessment, "volume_units")["status"], "unverified")
        self.assertEqual(self._check(assessment, "turnover_units")["status"], "unverified")
        self.assertEqual(self._check(assessment, "trading_calendar")["status"], "unverified")
        self.assertEqual(self._check(assessment, "corporate_actions")["status"], "unverified")
        self.assertFalse(assessment["paper_admission"])

    def test_batch_member_price_mismatch_is_explicit(self):
        root, manifest_hash = self._write_batch_cache()
        config, result = self._batch_inputs(close=8.15)
        with self._batch_patch(root, manifest_hash):
            assessment = assess_data(config, result)

        comparison = self._check(assessment, "daily_reference")
        self.assertEqual(comparison["status"], "mismatch")
        self.assertEqual(comparison["evidence"]["mismatch_days"], 2)
        self.assertFalse(assessment["paper_admission"])

    def test_batch_nonmember_reads_manifests_but_not_target_file(self):
        root, manifest_hash = self._write_batch_cache()
        config, result = self._batch_inputs(symbol="600011.SH")
        read_paths = []
        from research_center import data_evidence
        stable_read = data_evidence._stable_read

        def recording_read(path, max_bytes):
            read_paths.append(Path(path))
            if Path(path).name == "600011.SH.parquet":
                raise AssertionError("nonmember target file must not be read")
            return stable_read(Path(path), max_bytes)

        with self._batch_patch(root, manifest_hash), patch(
            "research_center.data_evidence._stable_read", side_effect=recording_read,
        ):
            assessment = assess_data(config, result)

        integrity = self._check(assessment, "raw_daily_cache_integrity")
        reference = self._check(assessment, "daily_reference")
        self.assertEqual(integrity["status"], "unverified")
        self.assertEqual(integrity["evidence"]["membership"], "not_found_in_pinned_manifests")
        self.assertFalse(integrity["evidence"]["target_file_read_performed"])
        self.assertEqual(reference["status"], "unverified")
        self.assertNotIn(root / "600011.SH.parquet", read_paths)
        self.assertFalse(assessment["paper_admission"])

    def test_batch_manifest_bad_filename_fails_before_target_read(self):
        root, manifest_hash = self._write_batch_cache()
        manifest_path = root / "manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["symbols"][0]["parquet_file"] = "../600010.SH.parquet"
        manifest_bytes = json.dumps(manifest, sort_keys=True).encode("utf-8")
        manifest_path.write_bytes(manifest_bytes)
        manifest_hash = hashlib.sha256(manifest_bytes).hexdigest()
        config, result = self._batch_inputs()
        read_paths = []
        from research_center import data_evidence
        stable_read = data_evidence._stable_read

        def recording_read(path, max_bytes):
            read_paths.append(Path(path))
            return stable_read(Path(path), max_bytes)

        with self._batch_patch(root, manifest_hash), patch(
            "research_center.data_evidence._stable_read", side_effect=recording_read,
        ):
            assessment = assess_data(config, result)

        integrity = self._check(assessment, "raw_daily_cache_integrity")
        self.assertEqual(integrity["status"], "mismatch")
        self.assertEqual(integrity["evidence"]["issue"], "batch_manifest_filename_invalid")
        self.assertNotIn(root / "600010.SH.parquet", read_paths)

    def test_batch_manifest_pin_mismatch_fails_before_target_read(self):
        root, pinned_hash = self._write_batch_cache()
        manifest_path = root / "manifest.json"
        manifest_path.write_bytes(manifest_path.read_bytes() + b" ")
        config, result = self._batch_inputs()
        read_paths = []
        from research_center import data_evidence
        stable_read = data_evidence._stable_read

        def recording_read(path, max_bytes):
            read_paths.append(Path(path))
            return stable_read(Path(path), max_bytes)

        with self._batch_patch(root, pinned_hash), patch(
            "research_center.data_evidence._stable_read", side_effect=recording_read,
        ):
            assessment = assess_data(config, result)

        integrity = self._check(assessment, "raw_daily_cache_integrity")
        self.assertEqual(integrity["status"], "mismatch")
        self.assertEqual(integrity["evidence"]["issue"], "batch_manifest_sha256_mismatch")
        self.assertNotIn(root / "600010.SH.parquet", read_paths)

    def test_batch_target_hash_mismatch_fails_closed(self):
        root, manifest_hash = self._write_batch_cache()
        target = root / "600010.SH.parquet"
        target.write_bytes(target.read_bytes() + b"changed")
        config, result = self._batch_inputs()
        with self._batch_patch(root, manifest_hash):
            assessment = assess_data(config, result)

        integrity = self._check(assessment, "raw_daily_cache_integrity")
        self.assertEqual(integrity["status"], "mismatch")
        self.assertEqual(integrity["evidence"]["issue"], "batch_target_sha256_mismatch")
        self.assertEqual(self._check(assessment, "daily_reference")["status"], "unverified")

    def test_batch_target_symlink_is_rejected(self):
        root, manifest_hash = self._write_batch_cache()
        target = root / "600010.SH.parquet"
        outside = Path(self.tempdir.name) / "outside.parquet"
        outside.write_bytes(target.read_bytes())
        target.unlink()
        target.symlink_to(outside)
        config, result = self._batch_inputs()
        with self._batch_patch(root, manifest_hash):
            assessment = assess_data(config, result)

        integrity = self._check(assessment, "raw_daily_cache_integrity")
        self.assertEqual(integrity["status"], "mismatch")
        self.assertEqual(integrity["evidence"]["issue"], "cache_file_not_regular")

    def test_fixed_batch_manifest_hashes_are_pinned(self):
        from research_center import data_evidence

        descriptors = data_evidence._RAW_DAILY_BATCH_REFERENCES
        self.assertEqual(
            [(item[0], item[2], item[3]) for item in descriptors],
            [
                (
                    "raw_daily_batch50_20261004",
                    "c57da75b481f0b13e430ea975f4c87a24271baeabe1620e316dae15e22ab0f97",
                    50,
                ),
                (
                    "raw_daily_batch100_20261004",
                    "39f226127fe42c80c66d171859de28cff223bf36fd0b7ba923c2f66943026539",
                    100,
                ),
            ],
        )

    def test_batch_filter_uses_configured_dates_and_holdout_is_not_read(self):
        root, manifest_hash = self._write_batch_cache(rows_by_symbol={
            "600010.SH": [
                ("20231229", 7.91),
                ("20240102", 8.13),
                ("20240103", 8.14),
                ("20251231", 9.12),
            ],
        })
        config, result = self._batch_inputs()
        original_read_table = pq.read_table
        with self._batch_patch(root, manifest_hash), patch(
            "pyarrow.parquet.read_table", wraps=original_read_table,
        ) as read_table:
            assessment = assess_data(config, result)
        comparison = self._check(assessment, "daily_reference")
        self.assertEqual(comparison["evidence"]["paired_days"], 2)
        self.assertEqual(
            self._check(assessment, "raw_daily_cache_integrity")["evidence"]["target_daily_closes"],
            {"20240102": 8.13, "20240103": 8.14},
        )
        filters = read_table.call_args.kwargs["filters"]
        self.assertIn(("trade_date", ">=", "20240102"), filters)
        self.assertIn(("trade_date", "<=", "20240103"), filters)

        config["start"] = "2026-01-02"
        config["end"] = "2026-01-03"
        opened_paths = []
        from research_center import data_evidence
        stable_read = data_evidence._stable_read

        def recording_read(path, max_bytes):
            opened_paths.append(Path(path))
            return stable_read(Path(path), max_bytes)

        with self._batch_patch(root, manifest_hash), patch(
            "research_center.data_evidence._stable_read", side_effect=recording_read,
        ):
            holdout_assessment = assess_data(config, result)
        self.assertEqual(self._check(holdout_assessment, "input_scope")["status"], "mismatch")
        self.assertEqual(opened_paths, [])
        self.assertFalse(holdout_assessment["paper_admission"])


if __name__ == "__main__":
    unittest.main()
