import sys, os, math, csv
from datetime import datetime
from collections import defaultdict

import numpy as np
import pandas as pd

sys.path.insert(0, "/tmp/kronos")
from model import Kronos, KronosTokenizer, KronosPredictor

MODEL_PATH = "/tmp/kronos_models/Kronos-mini"
TOKENIZER_PATH = "/tmp/kronos_models/Kronos-Tokenizer-2k"
TICK_PATH = "/config/projects/trading/data/poly-data/poly_data/btc_polymarket_ticks.csv"

CACHE = None


class KronosFilter:
    def __init__(self, candle_secs=60, lookback=40, pred_len=3, sample_count=10):
        self.candle_secs = candle_secs
        self.lookback = lookback
        self.pred_len = pred_len
        self.sample_count = sample_count

        print(f"[KronosFilter] Loading Kronos-mini...", flush=True)
        self.tokenizer = KronosTokenizer.from_pretrained(TOKENIZER_PATH)
        self.model = Kronos.from_pretrained(MODEL_PATH)
        self.predictor = KronosPredictor(
            self.model, self.tokenizer, device="cpu", max_context=2048
        )
        print(
            f"[KronosFilter] Model loaded. candle_secs={candle_secs} lookback={lookback} pred_len={pred_len}",
            flush=True,
        )

        self._candles_df = None
        self._candles_ts = None

    def build_candles(self):
        print(
            f"[KronosFilter] Building {self.candle_secs}s OHLCV candles from ticks...",
            flush=True,
        )
        ticks = []
        with open(TICK_PATH, "r") as f:
            reader = csv.DictReader(f)
            for row in reader:
                ts = row.get("timestamp", "")
                sp = row.get("spot_price", "")
                if ts and sp:
                    try:
                        dt = datetime.fromisoformat(ts)
                        ticks.append((dt.timestamp(), float(sp)))
                    except:
                        continue
        ticks.sort(key=lambda x: x[0])
        seen = set()
        unique = []
        for ts, sp in ticks:
            if ts not in seen:
                seen.add(ts)
                unique.append((ts, sp))

        df = pd.DataFrame(unique, columns=["timestamp", "close"])
        df["timestamp"] = pd.to_datetime(df["timestamp"], unit="s")

        df = (
            df.set_index("timestamp")
            .resample(f"{self.candle_secs}s")
            .agg({"close": ["first", "max", "min", "last", "count"]})
        )
        df.columns = ["open", "high", "low", "close", "volume"]
        df = df.dropna(subset=["open"])
        df = df[df["volume"] > 0]

        if df.index.tz is None:
            df.index = df.index.tz_localize("UTC")
        self._candles_ts = pd.Series(df.index)
        self._candles_df = df.reset_index(drop=True)
        print(
            f"[KronosFilter] Built {len(self._candles_df)} candles ({self._candles_ts.iloc[0]} to {self._candles_ts.iloc[-1]})",
            flush=True,
        )
        return self._candles_df, self._candles_ts

    def get_context_for_time(self, entry_timestamp):
        if self._candles_ts is None:
            self.build_candles()
        entry_dt = (
            datetime.fromisoformat(entry_timestamp)
            if isinstance(entry_timestamp, str)
            else entry_timestamp
        )
        entry_ts = pd.Timestamp(entry_dt)
        mask = self._candles_ts <= entry_ts
        available = mask.sum()
        if available < self.lookback + 1:
            return None
        start = available - self.lookback
        end = available

        x_df = self._candles_df.iloc[start:end][
            ["open", "high", "low", "close", "volume"]
        ].copy()
        if "amount" not in x_df.columns:
            x_df["amount"] = x_df["close"] * x_df["volume"]
        x_ts = self._candles_ts.iloc[start:end]

        last_candle_time = self._candles_ts.iloc[end - 1]
        y_ts_list = []
        for i in range(1, self.pred_len + 1):
            next_t = last_candle_time + pd.Timedelta(seconds=self.candle_secs * i)
            y_ts_list.append(next_t)
        y_ts = pd.Series(y_ts_list)
        return x_df, x_ts, y_ts

    def _infer(self, x_df, x_ts, y_ts):
        return self.predictor.predict(
            df=x_df,
            x_timestamp=x_ts,
            y_timestamp=y_ts,
            pred_len=self.pred_len,
            T=0.8,
            top_p=0.9,
            sample_count=self.sample_count,
            verbose=False,
        )

    def predict_direction(self, x_df, x_ts, y_ts, fast=False):
        try:
            if fast:
                pred_df = self.predictor.predict(
                    df=x_df,
                    x_timestamp=x_ts,
                    y_timestamp=y_ts,
                    pred_len=self.pred_len,
                    T=0.8,
                    top_p=0.9,
                    sample_count=3,
                    verbose=False,
                )
            else:
                pred_df = self._infer(x_df, x_ts, y_ts)

            last_close = x_df["close"].iloc[-1]
            pred_close = pred_df["close"].iloc[-1]
            pred_high = pred_df["high"].max()
            pred_low = pred_df["low"].min()

            direction = 1 if pred_close > last_close else -1
            magnitude = abs(pred_close - last_close) / last_close * 100

            if abs(pred_close - last_close) < 0.5:
                confidence = 0.0
            elif pred_close > last_close and pred_low > last_close * 0.999:
                confidence = min(1.0, magnitude / 0.1)
            elif pred_close < last_close and pred_high < last_close * 1.001:
                confidence = min(1.0, magnitude / 0.2)
            else:
                confidence = min(0.6, magnitude / 0.2)

            return direction, confidence
        except Exception as e:
            return 0, 0.0


_FILTER_INSTANCE = None


def get_filter():
    global _FILTER_INSTANCE
    if _FILTER_INSTANCE is None:
        _FILTER_INSTANCE = KronosFilter(
            candle_secs=60, lookback=30, pred_len=2, sample_count=10
        )
        _FILTER_INSTANCE.build_candles()
    return _FILTER_INSTANCE


def kronos_confirm_signal(trade_time, strategy_direction, confidence_threshold=0.3):
    kf = get_filter()
    ctx = kf.get_context_for_time(trade_time)
    if ctx is None:
        return True, 1.0
    x_df, x_ts, y_ts = ctx
    kronos_dir, kronos_conf = kf.predict_direction(x_df, x_ts, y_ts)
    if kronos_dir == 0 or kronos_conf < confidence_threshold:
        return True, kronos_conf
    strat_dir_val = 1 if strategy_direction == "YES" else -1
    agrees = kronos_dir == strat_dir_val
    return agrees, kronos_conf
