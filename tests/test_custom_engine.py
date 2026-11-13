from __future__ import annotations

from copy import deepcopy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import pandas as pd

from research_center import custom_engine, custom_kernel, engine
from research_center.custom_source_store import CustomSourceStore


SOURCE = b"def build_strategy(api, params):\n    return object()\n"


class FakeAdapter:
    def __init__(self, source, symbol, params, runtime, state=None, history_limit=60, api_source=None, *, events=None, close_error=None):
        self.source = source
        self.symbol = symbol
        self.params = deepcopy(params)
        self.runtime = runtime
        self.history_limit = history_limit
        self.api_source = api_source
        self._state = deepcopy(state or {})
        self.events = events if events is not None else []
        self.close_error = close_error
        self.calls = []
        self.events.append(("init", symbol))

    @property
    def state(self):
        return deepcopy(self._state)

    def on_bar(self, bar, context):
        self.calls.append((bar, context))
        self.events.append(("callback", context.datetime.isoformat()))
        return []

    def close(self):
        self.events.append(("close",))
        if self.close_error is not None:
            raise self.close_error


class CustomEngineTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / "sources"
        self.store = CustomSourceStore(self.root)
        self.record = self.store.register(
            SOURCE,
            name="Example callback",
            default_params={"fast": 4, "slow": 10, "nested": {"base": 1}},
        )

    def tearDown(self):
        self.temp.cleanup()

    def base_config(self):
        return {
            "strategy": "custom_python",
            "backend": "isolated_python",
            "source_id": self.record["registration_id"],
            "symbol": "000001.XSHE",
            "start": "2024-01-02",
            "end": "2024-01-02",
            "interval": "1m",
            "data_root": str(Path(self.temp.name) / "unused-data-root"),
        }

    @staticmethod
    def one_bar_frame():
        return pd.DataFrame(
            [
                {
                    "datetime": pd.Timestamp("2024-01-02 09:30:00"),
                    "source_datetime": pd.Timestamp("2024-01-02 09:31:00"),
                    "open": 10.0,
                    "high": 10.1,
                    "low": 9.9,
                    "close": 10.0,
                    "volume": 10000.0,
                    "turnover": 100000.0,
                }
            ]
        )

    def test_normalization_separates_custom_params_from_execution_risk(self):
        config = self.base_config()
        config["custom_params"] = {"fast": 7, "nested": {"custom": 2}}
        config["params"] = {"target_fraction": 0.7}
        config["history_limit"] = 80
        normalized = custom_engine.normalize_config(config, self.store)

        self.assertEqual(normalized["strategy"], "custom_python")
        self.assertEqual(normalized["backend"], "isolated_python")
        self.assertEqual(normalized["custom_params"], {"fast": 7, "slow": 10, "nested": {"custom": 2}})
        self.assertEqual(normalized["params"], {"target_fraction": 0.7, "max_turnover_participation": 0.01})
        self.assertEqual(normalized["history_limit"], 80)
        self.assertNotIn("fast", normalized["params"])
        execution_config = custom_engine._canonical_request_fields(normalized, 80)[1]
        self.assertEqual(execution_config["params"]["fast"], 1)
        self.assertEqual(execution_config["params"]["slow"], 79)
        self.assertEqual(normalized["symbol"], "000001.SZSE")
        self.assertEqual(normalized["start"], "2024-01-02")

    def test_unknown_runtime_source_fields_and_nonrisk_params_are_rejected(self):
        for forbidden in ("runtime", "api_source", "source_archive_root", "source_file", "source_path"):
            config = self.base_config()
            config[forbidden] = "/tmp/not-accepted"
            with self.subTest(field=forbidden), self.assertRaisesRegex(ValueError, "unsupported fields"):
                custom_engine.normalize_config(config, self.store)

        config = self.base_config()
        config["params"] = {"fast": 5}
        with self.assertRaisesRegex(ValueError, "execution-risk"):
            custom_engine.normalize_config(config, self.store)

    def test_training_symbol_date_and_history_policy_is_enforced(self):
        invalid = [
            {"symbol": "510300.XSHG"},
            {"symbol": "000001.XSHE", "start": "2023-12-31", "end": "2024-01-02"},
            {"symbol": "000001.XSHE", "start": "2026-01-01", "end": "2026-01-01"},
            {"symbol": "000001.XSHE", "start": "2024-01-01", "end": "2024-02-02"},
        ]
        for changes in invalid:
            config = self.base_config()
            config.update(changes)
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                custom_engine.normalize_config(config, self.store)

        for limit in (0, 242, True, 1.0):
            config = self.base_config()
            config["history_limit"] = limit
            with self.subTest(history_limit=limit), self.assertRaisesRegex(ValueError, "history_limit"):
                custom_engine.normalize_config(config, self.store)

    def test_raw_source_and_claimed_version_drift_fail_closed(self):
        config = self.base_config()
        normalized = custom_engine.normalize_config(config, self.store)

        class DriftStore:
            def show(inner_self, _source_id):
                return self.store.show(self.record["registration_id"])

            def read_source(inner_self, _source_id):
                return SOURCE + b"# changed after registration\n"

        with self.assertRaisesRegex(ValueError, "integrity"):
            custom_engine.normalize_config(config, DriftStore())

        stale = deepcopy(normalized)
        stale["custom_params"]["fast"] = 8
        with self.assertRaisesRegex(ValueError, "stale"):
            custom_engine.normalize_config(stale, self.store)

    def test_source_interface_initialization_fails_before_data_loader(self):
        normalized = custom_engine.normalize_config(self.base_config(), self.store)
        with patch.object(
            custom_engine,
            "GuestStrategyAdapter",
            side_effect=RuntimeError("synthetic init failure"),
        ), patch.object(engine, "_load_training_bars") as loader:
            with self.assertRaisesRegex(RuntimeError, "init failure"):
                custom_engine.run_backtest(
                    normalized,
                    source_store=self.store,
                    runtime=object(),
                )
        loader.assert_not_called()

    def test_close_failure_never_renders_or_returns_result(self):
        normalized = custom_engine.normalize_config(self.base_config(), self.store)
        events = []
        progress_values = []
        adapter = FakeAdapter(
            SOURCE,
            normalized["symbol"],
            normalized["custom_params"],
            object(),
            events=events,
            close_error=RuntimeError("synthetic close failure"),
        )
        with patch.object(custom_engine, "GuestStrategyAdapter", return_value=adapter), patch.object(
            engine,
            "_load_training_bars",
            return_value=(self.one_bar_frame(), {"synthetic": True}),
        ), patch.object(custom_kernel, "result_from_state") as render:
            with self.assertRaisesRegex(RuntimeError, "close failure"):
                custom_engine.run_backtest(
                    normalized,
                    progress=lambda value, _message: progress_values.append(value),
                    source_store=self.store,
                    runtime=object(),
                )
        render.assert_not_called()
        self.assertEqual(events[-1], ("close",))
        self.assertNotIn(100.0, progress_values)

    def test_valid_payload_runs_one_kernel_advance_and_progress_finishes_after_close(self):
        normalized = custom_engine.normalize_config(self.base_config(), self.store)
        events = []
        adapter = FakeAdapter(
            SOURCE,
            normalized["symbol"],
            normalized["custom_params"],
            object(),
            events=events,
        )
        with patch.object(custom_engine, "GuestStrategyAdapter", return_value=adapter), patch.object(
            engine,
            "_load_training_bars",
            return_value=(self.one_bar_frame(), {"synthetic": True}),
        ), patch.object(custom_kernel, "advance", wraps=custom_kernel.advance) as advance:
            result = custom_engine.run_backtest(
                normalized,
                progress=lambda value, _message: events.append(("progress", value)),
                source_store=self.store,
                runtime=object(),
            )

        self.assertEqual(advance.call_count, 1)
        self.assertEqual(len(adapter.calls), 1)
        bar, context = adapter.calls[0]
        self.assertEqual(bar.datetime, pd.Timestamp("2024-01-02 09:31:00"))
        self.assertEqual(list(bar.ohlcv), ["000001.SZSE"])
        self.assertEqual(context.datetime, pd.Timestamp("2024-01-02 09:31:00"))
        self.assertEqual(events[-2], ("close",))
        self.assertEqual(events[-1], ("progress", 100.0))
        self.assertTrue(next(index for index, item in enumerate(events) if item == ("close",)) < len(events) - 1)
        self.assertEqual(result["strategy"], "custom_python")
        self.assertEqual(result["backend"], "isolated_python")
        self.assertEqual(result["name"], "Example callback")
        self.assertEqual(result["strategy_version"], normalized["strategy_version"])
        self.assertTrue(result["source_manifest"]["runtime_interface_verified"])
        self.assertFalse(result["source_manifest"]["paper_admission"])
        self.assertEqual(result["data_manifest"]["synthetic"], True)
        self.assertEqual(
            result["source_manifest"]["sdk_source_sha256"],
            result["source_manifest"]["legacy_source_sha256"]["strategy_base"],
        )
        self.assertIn(
            "immutable_image_ref",
            result["source_manifest"]["runtime_pins"],
        )
        self.assertIn("no SMA-cross signal", result["custom_execution"]["execution_plumbing"])

    def test_cancel_before_initialization_raises_and_does_not_start_adapter(self):
        normalized = custom_engine.normalize_config(self.base_config(), self.store)
        with patch.object(custom_engine, "GuestStrategyAdapter") as adapter:
            with self.assertRaises(InterruptedError):
                custom_engine.run_backtest(
                    normalized,
                    cancelled=lambda: True,
                    source_store=self.store,
                    runtime=object(),
                )
        adapter.assert_not_called()

    def test_version_is_deterministic_and_changes_with_custom_parameters(self):
        config = self.base_config()
        first = custom_engine.strategy_version(config, self.store)
        self.assertEqual(first, custom_engine.strategy_version(config, self.store))
        changed = deepcopy(config)
        changed["custom_params"] = {"fast": 11}
        self.assertNotEqual(first, custom_engine.strategy_version(changed, self.store))

    def test_configured_runtime_returns_only_the_embedded_pins(self):
        runtime = custom_engine.configured_runtime()
        self.assertEqual(runtime.instance, "strategy")
        self.assertEqual(str(runtime.lima_home), str(Path.home() / "Library/Application Support/QuantSandbox/lima"))
        self.assertEqual(
            runtime.expected_instance_yaml_sha256,
            "1d0523837d0aafaac3b06661997ea97ad4e25dd976e13fca1d5d7a189be95abd",
        )
        self.assertEqual(
            runtime.expected_resolved_config_sha256,
            "f00339bb1c8af41e457608772c30c6b1d751a1abb78d79651b3d9bc7f5495bf3",
        )
        self.assertEqual(
            runtime.immutable_image_ref,
            "quant-strategy-worker@sha256:a5972e8f6704cfdd3cd53426edec57c2624e20de842639678cdcbbce9a2c425e",
        )
        self.assertEqual(
            runtime.worker_sha256,
            "1b9c2dd77827d41653963ce37d5ed21fbfaaaae72647754a5c0ca12aee731fa2",
        )
        self.assertEqual(
            runtime.expected_image_config_sha256,
            "4eb865cdac8520ff8ed42a68531979755560180100b20674b831e4ffa674aa88",
        )


if __name__ == "__main__":
    unittest.main()
