from __future__ import annotations

import pandas as pd
import unittest

from data.joinquant_minute import canonicalize_frame, jq_to_vt, vt_to_jq


def raw_frame() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "time": [pd.Timestamp("2024-01-02 09:31:00")],
            "code": ["000001.XSHE"],
            "open": [10.0],
            "high": [10.2],
            "low": [9.9],
            "close": [10.1],
            "volume": [1000.0],
            "money": [10050.0],
        }
    )


class JoinQuantMinuteTest(unittest.TestCase):
    def test_symbol_mapping_round_trip(self) -> None:
        self.assertEqual(jq_to_vt("000001.XSHE"), "000001.SZSE")
        self.assertEqual(jq_to_vt("600000.XSHG"), "600000.SSE")
        self.assertEqual(vt_to_jq("000001.SZSE"), "000001.XSHE")
        self.assertEqual(vt_to_jq("600000.SSE"), "600000.XSHG")

    def test_bar_end_is_shifted_to_vnpy_bar_start(self) -> None:
        result = canonicalize_frame(raw_frame())
        self.assertEqual(
            result.loc[0, "source_datetime"], pd.Timestamp("2024-01-02 09:31")
        )
        self.assertEqual(result.loc[0, "datetime"], pd.Timestamp("2024-01-02 09:30"))
        self.assertEqual(result.loc[0, "vt_symbol"], "000001.SZSE")
        self.assertEqual(result.loc[0, "turnover"], 10050.0)

    def test_invalid_ohlc_fails_closed(self) -> None:
        frame = raw_frame()
        frame.loc[0, "high"] = 9.8
        with self.assertRaisesRegex(ValueError, "invalid bars"):
            canonicalize_frame(frame)

    def test_all_nan_unavailable_bar_is_omitted(self) -> None:
        frame = raw_frame()
        frame.loc[0, ["open", "high", "low", "close", "volume", "money"]] = float("nan")
        result = canonicalize_frame(frame)
        self.assertTrue(result.empty)


if __name__ == "__main__":
    unittest.main()
