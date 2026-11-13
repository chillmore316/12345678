import unittest
from unittest.mock import patch
import pandas as pd

from research_center.portfolio_data import load_aligned_frames


def config(symbol):
    return dict(symbol=symbol, start="2024-01-02", end="2024-01-02", strategy="buy_hold")


def table(times):
    return pd.DataFrame([dict(datetime=pd.Timestamp(time), source_datetime=pd.Timestamp(time)+pd.Timedelta(minutes=1),
                              open=10., high=11., low=9., close=10., volume=1000., turnover=10000.) for time in times])


class PortfolioDataTest(unittest.TestCase):
    def test_scope_errors_happen_before_any_read(self):
        for configs in ([config("000001.SZSE")]*2,
                        [config("000001.SZSE"), dict(config("000002.SZSE"), start="2026-01-02",end="2026-01-02")]):
            with patch("research_center.portfolio_data._load_training_bars") as read:
                with self.assertRaises(ValueError):
                    load_aligned_frames(configs)
                read.assert_not_called()

    def test_exact_alignment_and_missing_symbol_minutes(self):
        frame=table(["2024-01-02 09:30","2024-01-02 09:31"])
        configs=[config("000001.SZSE"),config("000002.SZSE")]
        with patch("research_center.portfolio_data._load_training_bars",return_value=(frame,{})):
            frames,manifest=load_aligned_frames(configs)
        self.assertEqual(len(frames),2)
        self.assertEqual(list(frames[0]),["000001.SZSE","000002.SZSE"])
        self.assertFalse(manifest["paper_admission"])
        with patch("research_center.portfolio_data._load_training_bars",side_effect=[(frame,{}),(frame.iloc[1:],{})]):
            with self.assertRaisesRegex(ValueError,"时间轴不一致"):
                load_aligned_frames(configs)
