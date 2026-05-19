"""
ranker.py — Deterministic metric movement scorer
v2 — fixes: dedup shopify/unattributed, caps ROAS spike artifacts,
     flags last-day incomplete data
"""

import pandas as pd
import numpy as np
import logging
from pathlib import Path
from dataclasses import dataclass, asdict
from typing import Optional, Union

log = logging.getLogger(__name__)


BUSINESS_WEIGHTS = {
    "revenue":            1.0,
    "orders":             0.9,
    "roas":               0.8,
    "aov":                0.75,
    "spend":              0.7,
    "new_customer_share": 0.65,
    "attributed_revenue": 0.6,
    "attributed_orders":  0.55,
    "new_orders":         0.5,
    "returning_orders":   0.5,
    "cpc":                0.4,
    "cpm":                0.35,
    "ctr":                0.3,
    "impressions":        0.2,
    "clicks":             0.2,
}

W_ZSCORE = 0.4
W_WOW    = 0.4
W_MOM    = 0.2

ROLLING_WINDOW    = 28
Z_ALERT_THRESHOLD = 2.0

# Cap WoW/MoM deltas for ratio metrics (ROAS, CTR, CPC, CPM)
# A 5000% WoW spike in ROAS is almost always a near-zero spend artifact
# not a real efficiency gain. Cap at 500% for scoring purposes.
RATIO_DELTA_CAP = 500.0

# Deduplicate: when shopify total and unattributed slice show same value,
# keep only the total (channel == ""). These sources to deduplicate:
DEDUP_PAIRS = {
    ("shopify", "revenue",  "unattributed"),
    ("shopify", "orders",   "unattributed"),
    ("shopify", "aov",      "unattributed"),
}


@dataclass
class Finding:
    date:          str
    source:        str
    metric:        str
    channel:       str
    campaign:      str
    customer_type: str
    value:         float
    baseline_mean: float
    baseline_std:  float
    z_score:       float
    wow_delta_pct: Optional[float]
    mom_delta_pct: Optional[float]
    stat_score:    float
    biz_weight:    float
    final_score:   float
    direction:     str
    is_alert:      bool
    is_data_quality_flag: bool
    fact_sentence: str

    def to_dict(self) -> dict:
        """JSON-safe dict — explicit casts handle numpy scalars from pandas."""
        d = asdict(self)
        for k in ("value", "baseline_mean", "baseline_std", "z_score",
                 "stat_score", "biz_weight", "final_score"):
            d[k] = float(d[k])
        for k in ("wow_delta_pct", "mom_delta_pct"):
            d[k] = float(d[k]) if d[k] is not None else None
        d["is_alert"] = bool(d["is_alert"])
        d["is_data_quality_flag"] = bool(d["is_data_quality_flag"])
        return d


class MetricRanker:

    def __init__(self, csv_path):
        self.csv_path   = Path(csv_path)
        self._df        = None
        self._series    = None
        self._last_date = None
        print(f"Loading {self.csv_path.name}...")
        self._load()
        print(f"  {len(self._df):,} rows loaded")
        print(f"  {self._df['date'].min().date()} -> {self._df['date'].max().date()}")
        self._build_series()
        print(f"  {len(self._series)} unique metric series built")

    def _load(self):
        df = pd.read_csv(self.csv_path, parse_dates=["date"])
        for col in ["channel", "campaign", "customer_type"]:
            df[col] = df[col].fillna("").astype(str).str.strip()
        df["metric"]    = df["metric"].str.lower().str.strip()
        self._df        = df.sort_values("date").reset_index(drop=True)
        self._last_date = self._df["date"].max()

    def _build_series(self):
        group_keys  = ["source", "metric", "channel", "campaign", "customer_type"]
        series_list = []
        for keys, grp in self._df.groupby(group_keys, sort=False):
            s = grp.set_index("date")["value"].sort_index()
            if len(s) < 14:
                continue
            roll_mean = s.rolling(ROLLING_WINDOW, min_periods=14).mean()
            roll_std  = s.rolling(ROLLING_WINDOW, min_periods=14).std()
            dow_mean, dow_std = self._dow_adjusted_stats(s)
            series_list.append({
                "keys":      dict(zip(group_keys, keys)),
                "series":    s,
                "roll_mean": roll_mean,
                "roll_std":  roll_std,
                "dow_mean":  dow_mean,
                "dow_std":   dow_std,
            })
        self._series = series_list

    def _dow_adjusted_stats(self, s):
        """
        Same-weekday baseline mean AND std so the z-score numerator and denominator
        come from the same distribution. Using overall 28d std with a same-DoW mean
        understates variance and inflates z on stable metrics.

        Uses up to 8 prior same-weekday observations for std (need n>=3 to be stable)
        and the last 4 for mean (recent enough to track level changes).
        """
        dow_means = pd.Series(index=s.index, dtype=float)
        dow_stds  = pd.Series(index=s.index, dtype=float)
        dates     = s.index.sort_values()
        for i, date in enumerate(dates):
            same_dow = [d for d in dates[:i] if d.dayofweek == date.dayofweek]
            if len(same_dow) >= 2:
                dow_means[date] = s[same_dow[-4:]].mean()
            if len(same_dow) >= 3:
                dow_stds[date] = s[same_dow[-8:]].std()
        return dow_means, dow_stds

    def _is_ratio_metric(self, metric):
        return metric in {"roas", "ctr", "cpc", "cpm", "aov", "new_customer_share"}

    def _score_series_on_date(self, series_entry, target_date):
        keys      = series_entry["keys"]
        s         = series_entry["series"]
        roll_mean = series_entry["roll_mean"]
        roll_std  = series_entry["roll_std"]
        dow_mean  = series_entry["dow_mean"]
        dow_std   = series_entry["dow_std"]

        if target_date not in s.index:
            return None

        # ── Skip dedup pairs (unattributed duplicates shopify total) ──────────
        dedup_key = (keys["source"], keys["metric"], keys["channel"])
        if dedup_key in DEDUP_PAIRS:
            return None

        value = s[target_date]

        # Use same-DoW baseline+std when available so numerator & denominator
        # come from the same distribution. Fall back to overall 28d stats
        # for short series.
        use_dow = (target_date in dow_mean.index and
                   not pd.isna(dow_mean[target_date]) and
                   not pd.isna(dow_std.get(target_date, np.nan)))
        if use_dow:
            baseline = dow_mean[target_date]
            std      = dow_std[target_date]
        else:
            baseline = roll_mean.get(target_date, np.nan)
            std      = roll_std.get(target_date, np.nan)

        if pd.isna(baseline) or pd.isna(std) or std == 0:
            return None

        z_score = (value - baseline) / std

        prev_week  = target_date - pd.Timedelta(days=7)
        prev_month = target_date - pd.Timedelta(days=28)

        wow_delta = (
            (value - s[prev_week]) / s[prev_week] * 100
            if prev_week in s.index and s[prev_week] != 0
            else np.nan
        )
        mom_delta = (
            (value - s[prev_month]) / s[prev_month] * 100
            if prev_month in s.index and s[prev_month] != 0
            else np.nan
        )

        # ── Cap ratio metric deltas to prevent spend=0 artifacts ──────────────
        if self._is_ratio_metric(keys["metric"]):
            if not pd.isna(wow_delta):
                wow_delta = np.clip(wow_delta, -RATIO_DELTA_CAP, RATIO_DELTA_CAP)
            if not pd.isna(mom_delta):
                mom_delta = np.clip(mom_delta, -RATIO_DELTA_CAP, RATIO_DELTA_CAP)

        wow_norm = min(abs(wow_delta) / 100, 1.0) if not pd.isna(wow_delta) else 0.0
        mom_norm = min(abs(mom_delta) / 100, 1.0) if not pd.isna(mom_delta) else 0.0

        stat_score  = (
            W_ZSCORE * min(abs(z_score) / 3, 1.0) +
            W_WOW    * wow_norm +
            W_MOM    * mom_norm
        )
        biz_weight  = BUSINESS_WEIGHTS.get(keys["metric"], 0.3)
        final_score = stat_score * biz_weight
        direction   = "up" if value > baseline else "down"
        is_alert    = abs(z_score) > Z_ALERT_THRESHOLD

        # ── Flag last day of dataset as potential incomplete data ──────────────
        is_dq_flag = (target_date == self._last_date)

        fact = self._format_fact(
            keys, keys["metric"], value, baseline, z_score,
            wow_delta, mom_delta, direction, target_date, is_dq_flag
        )

        return Finding(
            date                 = str(target_date.date()),
            source               = keys["source"],
            metric               = keys["metric"],
            channel              = keys["channel"],
            campaign             = keys["campaign"],
            customer_type        = keys["customer_type"],
            value                = round(value, 2),
            baseline_mean        = round(baseline, 2),
            baseline_std         = round(std, 2),
            z_score              = round(z_score, 3),
            wow_delta_pct        = round(wow_delta, 1) if not pd.isna(wow_delta) else None,
            mom_delta_pct        = round(mom_delta, 1) if not pd.isna(mom_delta) else None,
            stat_score           = round(stat_score, 4),
            biz_weight           = biz_weight,
            final_score          = round(final_score, 4),
            direction            = direction,
            is_alert             = is_alert,
            is_data_quality_flag = is_dq_flag,
            fact_sentence        = fact,
        )

    def _format_fact(self, keys, metric, value, baseline, z_score,
                     wow_delta, mom_delta, direction, date, is_dq_flag=False):
        context_parts = []
        if keys["channel"]:
            context_parts.append(keys["channel"])
        if keys["campaign"]:
            context_parts.append(keys["campaign"])
        if keys["customer_type"]:
            context_parts.append(f"{keys['customer_type']} customers")
        context = " / ".join(context_parts) if context_parts else keys["source"]

        money_metrics = {"revenue", "attributed_revenue", "spend", "aov", "cpm", "cpc"}
        if metric in money_metrics:
            val_str      = f"INR {value:,.0f}"
            baseline_str = f"INR {baseline:,.0f}"
        elif metric in {"ctr", "new_customer_share", "roas"}:
            val_str      = f"{value:.3f}"
            baseline_str = f"{baseline:.3f}"
        else:
            val_str      = f"{value:,.0f}"
            baseline_str = f"{baseline:,.0f}"

        arrow  = "rose" if direction == "up" else "fell"
        deltas = []
        if wow_delta is not None and not np.isnan(wow_delta):
            cap_note = " [capped]" if abs(wow_delta) >= RATIO_DELTA_CAP else ""
            deltas.append(f"{abs(wow_delta):.1f}% WoW{cap_note}")
        if mom_delta is not None and not np.isnan(mom_delta):
            cap_note = " [capped]" if abs(mom_delta) >= RATIO_DELTA_CAP else ""
            deltas.append(f"{abs(mom_delta):.1f}% MoM{cap_note}")
        delta_str = " / ".join(deltas) if deltas else f"z={z_score:+.1f}"

        dq_note = " [NOTE: possible incomplete data — last day of dataset]" if is_dq_flag else ""

        return (
            f"[{context}] {metric} {arrow} {delta_str} "
            f"(value: {val_str}, DoW-adjusted baseline: {baseline_str}, "
            f"z={z_score:+.2f}){dq_note}"
        )

    def rank_day(self, target_date, top_n=10):
        ts       = pd.Timestamp(target_date)
        findings = []
        for entry in self._series:
            f = self._score_series_on_date(entry, ts)
            if f is not None:
                findings.append(f)
        # Sort: data quality flags last, then alerts first, then by score
        findings.sort(key=lambda f: (
            f.is_data_quality_flag,
            not f.is_alert,
            -f.final_score
        ))
        return findings[:top_n]

    def rank_week(self, week_end_date, top_n=20):
        end   = pd.Timestamp(week_end_date)
        start = end - pd.Timedelta(days=6)
        dates = pd.date_range(start, end)

        all_findings = {}
        for date in dates:
            for f in self.rank_day(str(date.date()), top_n=50):
                key = (f"{f.source}|{f.metric}|{f.channel}"
                       f"|{f.campaign}|{f.customer_type}")
                all_findings.setdefault(key, []).append(f)

        weekly_findings = []
        for key, day_findings in all_findings.items():
            best           = max(day_findings, key=lambda f: f.final_score)
            sustained_days = len(day_findings)
            if sustained_days >= 3:
                boost = 1.0 + 0.1 * (sustained_days - 2)
                object.__setattr__(best, "final_score",
                                   round(best.final_score * boost, 4))
            weekly_findings.append(best)

        weekly_findings.sort(key=lambda f: (
            f.is_data_quality_flag,
            not f.is_alert,
            -f.final_score
        ))
        return weekly_findings[:top_n]

    def available_dates(self):
        return [str(d.date()) for d in sorted(self._df["date"].unique())]

    def date_range(self):
        dates = sorted(self._df["date"].unique())
        return str(pd.Timestamp(dates[0]).date()), str(pd.Timestamp(dates[-1]).date())
