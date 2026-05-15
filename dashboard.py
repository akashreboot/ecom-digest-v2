"""
dashboard.py — Interactive presentation dashboard for the Ecom Digest system.

Run with:
    streamlit run dashboard.py

Designed for the interview walkthrough: every section is a slide you can
narrate from, every "run" button executes the real code, every action
streams logs to the right-hand panel so the panel can see what is happening
under the hood. No Jupyter, no terminal — the entire system drives from
this one screen.

Sections (sidebar nav):
    1. Overview              — what we built, the central design choice
    2. Data exploration      — EDA findings that shaped the design
    3. Ranker (deterministic) — score a day, inspect the math
    4. Personalisation       — same date, growth vs scale, see the diff
    5. Hallucination check   — paste fake output, watch the validator catch it
    6. Report generation     — call the LLM, stream logs, see the report
    7. Failure modes         — three failures, three mitigations, all live
    8. Cost & scale          — interactive cost calculator, 100 vs 10k customers
    9. Live logs             — everything the system did this session
"""

from __future__ import annotations

import io
import json
import logging
import os
import sys
import time
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Optional

import altair as alt
import numpy as np
import pandas as pd
import streamlit as st

# ── Local imports ─────────────────────────────────────────────────────────────
ROOT = Path(__file__).parent.resolve()
sys.path.insert(0, str(ROOT / "src"))

# Defer heavy imports — wrap so missing data file doesn't crash the app on load
try:
    from ranker import (
        MetricRanker, Finding,
        BUSINESS_WEIGHTS, DEDUP_PAIRS, RATIO_DELTA_CAP,
        Z_ALERT_THRESHOLD, ROLLING_WINDOW,
    )
    RANKER_IMPORT_OK = True
    RANKER_IMPORT_ERR = None
except Exception as e:
    RANKER_IMPORT_OK = False
    RANKER_IMPORT_ERR = str(e)

try:
    from report_generator import (
        ReportGenerator, CustomerProfile,
        PROFILE_GROWTH, PROFILE_SCALE,
        apply_profile_reranking, validate_numeric_grounding,
        render_template_fallback,
        build_daily_prompt, build_weekly_prompt,
        SYSTEM_PROMPT, MODEL,
        DAILY_TOP_N, WEEKLY_TOP_N,
    )
    REPORT_IMPORT_OK = True
    REPORT_IMPORT_ERR = None
except Exception as e:
    REPORT_IMPORT_OK = False
    REPORT_IMPORT_ERR = str(e)


DATA_PATH = ROOT / "data" / "metrics_159d.csv"
OUTPUT_DIR = ROOT / "outputs"
REPORTS_DIR = OUTPUT_DIR / "reports"


# ──────────────────────────────────────────────────────────────────────────────
# Logging — custom handler that pushes records into st.session_state
# ──────────────────────────────────────────────────────────────────────────────

class StreamlitLogHandler(logging.Handler):
    """Captures log records into st.session_state['logs'] for display."""

    LEVEL_ICON = {
        "DEBUG":    "·",
        "INFO":     "🟢",
        "WARNING":  "🟡",
        "ERROR":    "🔴",
        "CRITICAL": "🚨",
    }

    def emit(self, record: logging.LogRecord) -> None:
        try:
            msg = self.format(record)
        except Exception:
            msg = record.getMessage()
        entry = {
            "ts":    datetime.now().strftime("%H:%M:%S"),
            "level": record.levelname,
            "name":  record.name,
            "msg":   msg,
            "icon":  self.LEVEL_ICON.get(record.levelname, "·"),
        }
        if "logs" not in st.session_state:
            st.session_state["logs"] = []
        st.session_state["logs"].append(entry)


def setup_logging() -> None:
    """Idempotent root-logger setup with our custom handler."""
    if st.session_state.get("_log_handler_attached"):
        return
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    # Strip any pre-existing handlers to avoid double output
    for h in list(root.handlers):
        root.removeHandler(h)
    handler = StreamlitLogHandler()
    handler.setFormatter(logging.Formatter("%(name)s — %(message)s"))
    root.addHandler(handler)
    st.session_state["_log_handler_attached"] = True


def log_step(msg: str, level: str = "INFO") -> None:
    """Helper: log a high-level narration step the presenter can point at."""
    getattr(logging.getLogger("dashboard"), level.lower())(msg)


# ──────────────────────────────────────────────────────────────────────────────
# Cached resource builders
# ──────────────────────────────────────────────────────────────────────────────

@st.cache_resource(show_spinner="Loading 159 days of metric data …")
def get_ranker() -> Optional[MetricRanker]:
    if not RANKER_IMPORT_OK or not DATA_PATH.exists():
        return None
    logging.getLogger("dashboard").info("Constructing MetricRanker from %s", DATA_PATH.name)
    return MetricRanker(DATA_PATH)


@st.cache_data(show_spinner=False)
def get_raw_df() -> Optional[pd.DataFrame]:
    if not DATA_PATH.exists():
        return None
    df = pd.read_csv(DATA_PATH, parse_dates=["date"])
    for col in ["channel", "campaign", "customer_type"]:
        df[col] = df[col].fillna("").astype(str)
    df["metric"] = df["metric"].str.lower()
    return df


def get_report_generator() -> Optional[ReportGenerator]:
    if not REPORT_IMPORT_OK:
        return None
    if not os.getenv("ANTHROPIC_API_KEY"):
        return None
    if "_report_gen" not in st.session_state:
        try:
            st.session_state._report_gen = ReportGenerator()
        except Exception as e:
            logging.getLogger("dashboard").error("ReportGenerator init failed: %s", e)
            return None
    return st.session_state._report_gen


# ──────────────────────────────────────────────────────────────────────────────
# Shared UI helpers
# ──────────────────────────────────────────────────────────────────────────────

def narration(text: str) -> None:
    """Renders an 'Under the hood' callout — what the code is doing right now."""
    st.info(f"**🔧 Under the hood** — {text}")


def step_header(n: int, total: int, title: str, subtitle: str = "") -> None:
    st.caption(f"Step {n} of {total}")
    st.title(title)
    if subtitle:
        st.markdown(f"*{subtitle}*")
    st.markdown("---")


def env_status_panel() -> None:
    """Show whether the system is ready to run (data, API key, imports)."""
    c1, c2, c3 = st.columns(3)
    with c1:
        ok = DATA_PATH.exists()
        st.metric("Dataset",
                  "✓ Loaded" if ok else "✗ Missing",
                  f"data/{DATA_PATH.name}" if ok else "place CSV in data/")
    with c2:
        ok = RANKER_IMPORT_OK and REPORT_IMPORT_OK
        st.metric("Code imports",
                  "✓ OK" if ok else "✗ Error",
                  "src/ranker.py + src/report_generator.py"
                  if ok else (RANKER_IMPORT_ERR or REPORT_IMPORT_ERR or "see logs"))
    with c3:
        ok = bool(os.getenv("ANTHROPIC_API_KEY"))
        st.metric("Anthropic API key",
                  "✓ Set" if ok else "✗ Missing",
                  "live LLM calls enabled"
                  if ok else "demo mode (template fallback used)")


def findings_table(findings: list[dict]) -> pd.DataFrame:
    """Compact dataframe view of findings."""
    rows = []
    for i, f in enumerate(findings, 1):
        rows.append({
            "#":      i,
            "alert":  "🚨" if f.get("is_alert") else "",
            "DQ":     "⚠️" if f.get("is_data_quality_flag") else "",
            "boost":  "⭐" if f.get("_profile_boosted") else "",
            "metric": f["metric"],
            "where":  " / ".join(p for p in [f.get("channel", ""),
                                              f.get("campaign", ""),
                                              f.get("customer_type", "")] if p) or f.get("source", ""),
            "value":  f["value"],
            "z":      round(f["z_score"], 2),
            "WoW%":   f.get("wow_delta_pct"),
            "MoM%":   f.get("mom_delta_pct"),
            "score":  round(f["final_score"], 4),
        })
    return pd.DataFrame(rows)


# ──────────────────────────────────────────────────────────────────────────────
# SECTION 1 — Overview
# ──────────────────────────────────────────────────────────────────────────────

def section_overview() -> None:
    step_header(1, 9, "Ecom Digest",
                "A daily and weekly performance digest for D2C brands, grounded so it can't hallucinate.")
    narration(
        "When this page renders, the dashboard imports `ranker.py` and `report_generator.py` from `src/`, "
        "wires a `StreamlitLogHandler` into Python's root logger so every log line from any module flows into "
        "the live tail in the sidebar, and runs `env_status_panel()` — which checks for the dataset file, "
        "the import status of both modules, and the `ANTHROPIC_API_KEY` env var. The architecture diagram is "
        "rendered client-side from a DOT graph (no system binaries needed). Nothing else runs until you click "
        "into a later section — the ranker is built lazily via `@st.cache_resource` on first use."
    )

    env_status_panel()
    st.markdown(" ")

    c1, c2 = st.columns([3, 2])
    with c1:
        st.subheader("Architecture")
        st.graphviz_chart("""
            digraph G {
                rankdir=TB;
                node [shape=box, style="rounded,filled", fontname="Helvetica"];

                csv [label="metrics_159d.csv\\n(159 days, ~19k rows)", fillcolor="#e3f2fd"];

                ranker [label="ranker.py — DETERMINISTIC\\n• 28d rolling z (same-DoW)\\n• WoW + MoM deltas\\n• Business weights\\n• Ratio cap, dedup, DQ flag", fillcolor="#fff3e0"];

                json [label="ranked_findings.json", fillcolor="#f3e5f5"];

                profile [label="Customer profile\\nPRIORITY_BOOST = 1.3x", fillcolor="#e8f5e9"];

                rerank [label="apply_profile_reranking()", fillcolor="#e8f5e9"];

                steady [label="STEADY FACTS block\\n(3 low-z high-weight metrics)", fillcolor="#e8f5e9"];

                llm [label="Claude API — GENERATIVE\\n• Cached system prompt\\n• Retry + backoff\\n• Strict format", fillcolor="#ffebee"];

                check [label="validate_numeric_grounding()\\nregex every numeric token\\nvs facts block", fillcolor="#fffde7"];

                fallback [label="render_template_fallback()", fillcolor="#fafafa"];

                report [label="Daily / Weekly Report\\n(.md, 5 sections)", fillcolor="#e1f5fe"];

                csv -> ranker -> json -> rerank;
                profile -> rerank;
                rerank -> steady;
                steady -> llm;
                rerank -> llm;
                llm -> check;
                check -> report [label="passed"];
                check -> fallback [label="failed", color="red"];
                fallback -> report;
            }
        """)
    with c2:
        st.subheader("The 60-second pitch")
        st.markdown(
            "- **160 days, 19k rows, ~200 series** — typical D2C scale.\n"
            "- **Two-stage pipeline**: deterministic Python ranker → grounded LLM explainer.\n"
            "- **No invented numbers** — `validate_numeric_grounding()` is a post-check, not a hope.\n"
            "- **No invented causation** — the prompt bans 'because/due to', the data justifies it (r=0.12).\n"
            "- **Personalisation that actually re-orders findings** — `+30%` boost on `profile.primary_metrics`.\n"
            "- **Prompt-cached system prompt** — keeps the 10k-customer cost story credible.\n"
            "- **Template fallback** when the API fails or grounding rejects the output."
        )
        st.subheader("Where the LLM is and isn't")
        st.markdown(
            "| Operation | Python | LLM |\n"
            "|---|---|---|\n"
            "| Loading, scoring, ranking | ✓ | |\n"
            "| Choosing top-N findings | ✓ | |\n"
            "| Formatting fact sentences | ✓ | |\n"
            "| Writing the headline | | ✓ |\n"
            "| Connecting two findings into a story | | ✓ |\n"
            "| Phrasing the investigation question | | ✓ |"
        )


# ──────────────────────────────────────────────────────────────────────────────
# SECTION 2 — EDA
# ──────────────────────────────────────────────────────────────────────────────

def section_eda() -> None:
    step_header(2, 9, "Data exploration",
                "Three findings from EDA shaped the entire design.")

    narration(
        "When this section opens, `get_raw_df()` reads `data/metrics_159d.csv` via "
        "`pd.read_csv(parse_dates=['date'])` and is wrapped in `@st.cache_data` so subsequent renders skip the "
        "I/O. Each tab below performs different DataFrame operations live: the schema tab calls "
        "`df.isnull().sum()` and `value_counts()`; the DoW tab does a `groupby('date').sum()` and renders via "
        "Altair; the correlation tab merges Meta spend with Shopify revenue on date and calls `.corr()`; the "
        "triple-counting tab does a `groupby('channel').size()`. Every number is computed in real time from "
        "the actual CSV — no static fixtures."
    )

    df = get_raw_df()
    if df is None:
        st.error(f"Dataset missing. Place metrics_159d.csv in {DATA_PATH.parent}/")
        return

    log_step(f"EDA section opened — dataset has {len(df):,} rows")

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Rows", f"{len(df):,}")
    c2.metric("Days", f"{df['date'].nunique()}")
    c3.metric("Sources", df["source"].nunique())
    c4.metric("Metrics", df["metric"].nunique())

    tabs = st.tabs([
        "🔎 Schema & nulls",
        "📈 Daily revenue + DoW",
        "💔 The r=0.12 finding",
        "🪤 The triple-counting trap",
    ])

    with tabs[0]:
        st.markdown(f"**Date range:** `{df['date'].min().date()}` → `{df['date'].max().date()}`")
        st.markdown("**Null counts** (nulls encode meaning — a null channel means 'this is the total'):")
        st.dataframe(df.isnull().sum().rename("nulls").to_frame(), use_container_width=False)
        with st.expander("Unique values per categorical"):
            for col in ["source", "metric", "channel", "customer_type"]:
                vals = df[col].replace("", "(empty)").value_counts()
                st.markdown(f"**`{col}`** — {len(vals)} unique")
                st.dataframe(vals.rename("rows").to_frame(), use_container_width=False)

    with tabs[1]:
        st.markdown("Sundays are always low, Thursdays high. **This is why raw z-score fails on this dataset.**")
        daily = (df[(df["source"] == "shopify") &
                    (df["metric"] == "revenue") &
                    (df["channel"] == "") &
                    (df["customer_type"] == "")]
                 .groupby("date")["value"].sum()
                 .reset_index().rename(columns={"value": "revenue"}))
        daily["dow"] = daily["date"].dt.day_name()

        chart = alt.Chart(daily).mark_line(point=alt.OverlayMarkDef(size=20)).encode(
            x="date:T",
            y=alt.Y("revenue:Q", title="Daily revenue (INR)"),
            color=alt.Color("dow:N", title="Day of week",
                            sort=["Monday", "Tuesday", "Wednesday", "Thursday",
                                  "Friday", "Saturday", "Sunday"]),
            tooltip=["date", "dow", alt.Tooltip("revenue:Q", format=",.0f")],
        ).properties(height=320)
        st.altair_chart(chart, use_container_width=True)

        dow_avg = (daily.groupby("dow")["revenue"].mean()
                   .reindex(["Monday", "Tuesday", "Wednesday", "Thursday",
                             "Friday", "Saturday", "Sunday"])
                   .reset_index())
        st.subheader("Average revenue by day of week")
        bar = alt.Chart(dow_avg).mark_bar().encode(
            x=alt.X("dow:N", sort=None, title="Day of week"),
            y=alt.Y("revenue:Q", title="Avg revenue (INR)"),
            tooltip=[alt.Tooltip("revenue:Q", format=",.0f")],
        ).properties(height=240)
        st.altair_chart(bar, use_container_width=True)
        log_step("EDA: rendered DoW seasonality chart")

    with tabs[2]:
        meta = (df[(df["source"] == "meta_ads") & (df["metric"] == "spend")]
                .groupby("date")["value"].sum().rename("meta_spend"))
        rev = (df[(df["source"] == "shopify") & (df["metric"] == "revenue") &
                  (df["channel"] == "") & (df["customer_type"] == "")]
               .groupby("date")["value"].sum().rename("revenue"))
        merged = pd.concat([meta, rev], axis=1).dropna()
        r = merged["meta_spend"].corr(merged["revenue"])
        st.metric("Pearson correlation: Meta spend vs Shopify revenue", f"r = {r:.3f}",
                  "weak — causation cannot be inferred")
        scatter = alt.Chart(merged.reset_index()).mark_circle(size=50, opacity=0.5).encode(
            x=alt.X("meta_spend:Q", title="Meta spend (INR)"),
            y=alt.Y("revenue:Q", title="Shopify revenue (INR)"),
            tooltip=["date", "meta_spend", "revenue"],
        ).properties(height=340)
        st.altair_chart(scatter, use_container_width=True)
        st.info(
            "This finding alone is why the system prompt bans the words 'because', 'caused by', 'due to'. "
            "Causal claims at r=0.12 would be lying with confidence."
        )
        log_step(f"EDA: computed Meta spend vs revenue correlation r={r:.3f}")

    with tabs[3]:
        st.markdown(
            "Each Shopify revenue *day* shows up three times in the raw export — once as a daily total, "
            "once per attributed channel, and once per customer-type. Summing rows naively **triple-counts**."
        )
        breakdown = (df[(df["source"] == "shopify") & (df["metric"] == "revenue")]
                     .groupby(df["channel"].replace("", "(empty = daily total)")).size()
                     .rename("rows").sort_values(ascending=False))
        st.dataframe(breakdown.to_frame(), use_container_width=False)
        st.success(
            "**The fix** — `ranker.py:DEDUP_PAIRS` drops the `unattributed` channel slice (which equals the "
            "daily total on most days) and keeps the daily total as the headline number. The other attributed "
            "slices are kept as directional signals."
        )


# ──────────────────────────────────────────────────────────────────────────────
# SECTION 3 — Ranker
# ──────────────────────────────────────────────────────────────────────────────

def section_ranker() -> None:
    step_header(3, 9, "The deterministic ranker",
                "Picks the 3–5 things that matter, with no LLM in sight.")

    narration(
        "Clicking **Rank this day** calls `ranker.rank_day(target_date, top_n)`. That iterates "
        "`self._series` (~200 entries built once at construction time and cached), and for each series "
        "calls `_score_series_on_date()`. Inside the scorer: same-DoW mean and std are looked up, "
        "z = (value − baseline) / std is computed, WoW and MoM deltas are computed by index lookup at "
        "`date − 7d` and `date − 28d`, ratio metrics are clipped at ±500% via `np.clip`, the three signals "
        "are weighted into `stat_score`, multiplied by the business weight from `BUSINESS_WEIGHTS[metric]`, "
        "and packaged into a `Finding` dataclass. The list is then sorted with the 3-level tuple key "
        "`(is_data_quality_flag, not is_alert, -final_score)` and sliced to `top_n`. No LLM. Same input → "
        "same output, every time. **v2 upgrade path:** replace the rolling-mean baseline with STL or "
        "Prophet decomposition (Netflix, Uber, DataDog Watchdog do this) — full reasoning in "
        "`OPTIMIZATION_JOURNEY.md`."
    )

    ranker = get_ranker()
    if ranker is None:
        st.error("Ranker unavailable — check dataset and src/ranker.py")
        return

    dates = ranker.available_dates()

    c1, c2, c3 = st.columns([2, 1, 1])
    with c1:
        target_date = st.selectbox(
            "Target date",
            options=dates,
            index=dates.index("2025-12-10") if "2025-12-10" in dates else len(dates) - 30,
            help="Dec 10 is the known anomaly day. The last date triggers the DQ-flag mitigation.",
        )
    with c2:
        top_n = st.slider("Top N", 3, 20, 10)
    with c3:
        st.write(" ")
        st.write(" ")
        run = st.button("▶ Rank this day", type="primary", use_container_width=True)

    if run or st.session_state.get("_last_ranked_date") == target_date:
        st.session_state["_last_ranked_date"] = target_date
        log_step(f"Ranker: scoring all series on {target_date} (top_n={top_n})")
        with st.spinner("Computing z, WoW, MoM, business weights …"):
            findings = ranker.rank_day(target_date, top_n=top_n)
        log_step(f"Ranker: {len(findings)} findings returned, "
                 f"top score={findings[0].final_score if findings else 0:.4f}, "
                 f"alerts={sum(1 for f in findings if f.is_alert)}")

        findings_dicts = [f.to_dict() for f in findings]
        st.session_state["_last_findings"] = findings_dicts

        if not findings:
            st.warning(
                f"No findings on **{target_date}**. This usually means the date is too early in the "
                "dataset for the rolling baselines to be populated. The ranker needs ~3 weeks of prior "
                "history per series before z-scores become valid (28-day rolling mean with min_periods=14, "
                "plus same-day-of-week std needs 3+ same-weekday observations). "
                "**Try a date after roughly Sep 22 2025.**"
            )
            log_step(f"Ranker: {target_date} returned 0 findings — likely insufficient history",
                     level="WARNING")
            return

        st.subheader(f"Top {len(findings)} findings — {target_date}")
        df_f = findings_table(findings_dicts)
        st.dataframe(df_f, use_container_width=True, hide_index=True)

        st.subheader("Fact sentences (this is what the LLM will see)")
        for i, f in enumerate(findings, 1):
            prefix = "🚨" if f.is_alert else "  "
            dq = "  ⚠️ DQ" if f.is_data_quality_flag else ""
            st.code(f"{i:2}. {prefix} {f.fact_sentence}{dq}", language="text")

        # Score breakdown for the #1 finding
        st.subheader("Score breakdown — top finding")
        top = findings[0]
        bc1, bc2, bc3, bc4 = st.columns(4)
        bc1.metric("z-score", f"{top.z_score:+.2f}", "vs same-DoW baseline")
        bc2.metric("WoW", f"{(top.wow_delta_pct or 0):+.1f}%", "week-over-week")
        bc3.metric("MoM", f"{(top.mom_delta_pct or 0):+.1f}%", "month-over-month")
        bc4.metric("Final score", f"{top.final_score:.4f}",
                   f"stat {top.stat_score:.3f} × weight {top.biz_weight}")

        with st.expander("How the score was computed"):
            st.code(
                "stat_score = 0.4 * min(|z|/3, 1) "
                "+ 0.4 * min(|WoW|/100, 1) "
                "+ 0.2 * min(|MoM|/100, 1)\n"
                f"           = 0.4 * {min(abs(top.z_score)/3, 1):.3f} "
                f"+ 0.4 * {min(abs(top.wow_delta_pct or 0)/100, 1):.3f} "
                f"+ 0.2 * {min(abs(top.mom_delta_pct or 0)/100, 1):.3f}\n"
                f"           = {top.stat_score:.4f}\n\n"
                f"final_score = stat_score * business_weight[{top.metric!r}]\n"
                f"           = {top.stat_score:.4f} * {top.biz_weight}\n"
                f"           = {top.final_score:.4f}",
                language="text",
            )

        with st.expander("Why this isn't just `value.pct_change()`"):
            st.markdown(
                "- **Pure percentage change is fooled by scale.** A metric that normally swings ±50% showing "
                "a 30% drop is nothing; a normally rock-steady metric showing 30% is a major event.\n"
                "- **z-score normalises by each metric's own volatility.** A z of +2 is rare for any metric, "
                "regardless of its natural noisiness.\n"
                "- **DoW adjustment** prevents Sunday-naturally-low from flagging every Sunday."
            )


# ──────────────────────────────────────────────────────────────────────────────
# SECTION 4 — Personalisation
# ──────────────────────────────────────────────────────────────────────────────

def section_personalisation() -> None:
    step_header(4, 9, "Personalisation in action",
                "Same date, same data — two profiles, two different leading findings.")

    narration(
        "Clicking the button calls `ranker.rank_day(date, top_n=15)` once, then runs "
        "`apply_profile_reranking(findings, profile)` twice — once for `PROFILE_GROWTH`, once for "
        "`PROFILE_SCALE`. Inside the reranker: a list comprehension iterates `findings`, copies each dict, and "
        "if `f['metric'] in profile.primary_metrics` it multiplies `final_score × 1.3` and tags "
        "`_profile_boosted = True`. The list is then re-sorted with the same 3-level tuple key the ranker uses. "
        "Both result lists are shown side by side; the diff panel below compares positions 1–5 cell by cell. "
        "The boost is a constant `PROFILE_BOOST = 1.3` in `report_generator.py` — easy to tune, easy to "
        "replace with a learned weight later. **v2 upgrade path:** add a clustering step between rerank and "
        "the LLM prompt so correlated findings (Meta CPM + CPC + CTR moving together) collapse into one "
        "story — Anodot and OutOfTheBlue both ship this pattern. See `OPTIMIZATION_JOURNEY.md`."
    )

    ranker = get_ranker()
    if ranker is None or not REPORT_IMPORT_OK:
        st.error("Ranker or report_generator unavailable.")
        return

    dates = ranker.available_dates()
    # Default to a mid-dataset normal day, NOT Dec 10. The Dec 10 revenue spike
    # is so dominant that revenue leads both profiles and the diff disappears —
    # bad for demos. A normal day lets each profile's priorities surface.
    default_idx = max(0, len(dates) - 30)
    target_date = st.selectbox(
        "Target date",
        options=dates,
        index=default_idx,
        help=(
            "Pick a normal weekday for the clearest diff. The Dec 10 revenue mega-spike "
            "is so dominant that revenue leads both profiles regardless of priorities."
        ),
    )

    if st.button("▶ Re-rank for both profiles", type="primary"):
        log_step(f"Personalisation: re-ranking {target_date} for both profiles")
        findings = [f.to_dict() for f in ranker.rank_day(target_date, top_n=15)]

        if not findings:
            st.warning(
                f"No findings on **{target_date}** — likely too early in the dataset for rolling baselines. "
                "Pick a later date (after roughly Sep 22 2025)."
            )
            log_step(f"Personalisation: {target_date} returned 0 findings", level="WARNING")
            return

        growth = apply_profile_reranking(findings, PROFILE_GROWTH)
        scale  = apply_profile_reranking(findings, PROFILE_SCALE)

        st.session_state["_pers_growth"] = growth
        st.session_state["_pers_scale"] = scale
        log_step(f"Growth top metric: {growth[0]['metric']} ({growth[0]['final_score']:.4f})")
        log_step(f"Scale  top metric: {scale[0]['metric']} ({scale[0]['final_score']:.4f})")

    if "_pers_growth" not in st.session_state:
        st.info("Click the button above to run the re-ranker.")
        return

    growth = st.session_state["_pers_growth"]
    scale  = st.session_state["_pers_scale"]

    st.subheader(f"Side-by-side: same date `{target_date}`")
    c1, c2 = st.columns(2)
    with c1:
        st.markdown(f"### 🌱 {PROFILE_GROWTH.name}")
        st.caption(f"Priorities: {', '.join(PROFILE_GROWTH.primary_metrics)}")
        st.dataframe(findings_table(growth[:7]), use_container_width=True, hide_index=True)
    with c2:
        st.markdown(f"### 🏛 {PROFILE_SCALE.name}")
        st.caption(f"Priorities: {', '.join(PROFILE_SCALE.primary_metrics)}")
        st.dataframe(findings_table(scale[:7]), use_container_width=True, hide_index=True)

    # ── Positions 2-5 diff panel ─────────────────────────────────────────────
    # The lead finding often matches because revenue dominates on big days.
    # The interesting personalisation signal lives in positions 2-5.
    st.subheader("Positions 1–5: where the profiles diverge")
    st.caption("Lead is often shared — the personalisation signal usually lives below it.")

    def _where(f: dict) -> str:
        parts = [p for p in [f.get("channel", ""), f.get("campaign", ""),
                              f.get("customer_type", "")] if p]
        return " / ".join(parts) if parts else f.get("source", "")

    diff_rows = []
    for pos in range(5):
        g = growth[pos] if pos < len(growth) else None
        s = scale[pos]  if pos < len(scale)  else None
        same = (g is not None and s is not None
                and g["metric"] == s["metric"]
                and g.get("channel") == s.get("channel")
                and g.get("campaign") == s.get("campaign")
                and g.get("customer_type") == s.get("customer_type"))
        diff_rows.append({
            "#":              f"#{pos + 1}",
            "Growth metric":  g["metric"] if g else "—",
            "Growth where":   _where(g) if g else "—",
            "Growth boost":   "⭐" if g and g.get("_profile_boosted") else "",
            "Scale metric":   s["metric"] if s else "—",
            "Scale where":    _where(s) if s else "—",
            "Scale boost":    "⭐" if s and s.get("_profile_boosted") else "",
            "Match?":         "✓ same" if same else "✗ different",
        })
    st.dataframe(pd.DataFrame(diff_rows), use_container_width=True, hide_index=True)

    if not growth or not scale:
        st.info("No findings to compare on this date.")
        return

    n_different = sum(1 for r in diff_rows if r["Match?"].startswith("✗"))
    g_lead, s_lead = growth[0]["metric"], scale[0]["metric"]
    if g_lead != s_lead:
        st.success(
            f"✅ The lead itself diverges: growth leads with **{g_lead}**, scale with **{s_lead}**. "
            f"Across positions 1–5, **{n_different}/5 rows differ**. This is the cold-start prior in action."
        )
    elif n_different > 0:
        st.success(
            f"✅ Lead matches (**{g_lead}**) because it dominates the score, but **{n_different}/5 positions "
            "below the lead differ** — that's the visible personalisation signal. In production, learned "
            "weights from engagement data would amplify this."
        )
    else:
        st.warning(
            "Top-5 happen to match exactly on this date. Try a normal weekday (Dec 10 is dominated by "
            "the revenue spike) — Jan 29 or any recent non-anomaly date should show divergence."
        )


# ──────────────────────────────────────────────────────────────────────────────
# SECTION 5 — Hallucination prevention
# ──────────────────────────────────────────────────────────────────────────────

def section_grounding() -> None:
    step_header(5, 9, "Hallucination prevention — the numeric grounding check",
                "Watch the validator catch an invented number.")

    narration(
        "Clicking **Validate** calls `validate_numeric_grounding(output, facts, user_prompt, SYSTEM_PROMPT)`. "
        "Inside: `_normalize_numbers(text)` runs the regex `[+-]?\\d{1,3}(?:,\\d{3})+(?:\\.\\d+)?%?|...` against "
        "each input, strips commas and `%`, casts to float, drops 4-digit years (1900–2100) and small ordinals "
        "(0–10), and rounds to 2dp. The output set is compared against the union of allowed sources via "
        "`set difference`. Any number in the output but not in any input is returned in the `ungrounded` list "
        "and fails the check. In the live pipeline this rejection triggers `render_template_fallback()` instead "
        "of returning the LLM text. The expander below shows the actual extracted token sets. "
        "**v2 upgrade path:** the system currently hedges causation at r=0.12; v2 adds CausalImpact "
        "(Bayesian Structural Time Series, Google 2015) so attribution questions get a counterfactual "
        "estimate with credible intervals instead of a disclaimer. See `OPTIMIZATION_JOURNEY.md`."
    )

    if not REPORT_IMPORT_OK:
        st.error("report_generator unavailable.")
        return

    facts_default = (
        "1. *** [shopify] revenue rose 167.6% WoW (value: INR 3,915,420, baseline: INR 1,462,518, z=+12.30)\n"
        "2.     [paid_search / shopping_google_003] roas rose 500.0% WoW [capped] "
        "(value: 6.184, baseline: 1.051, z=+4.64)\n"
        "3.     [new customers] revenue rose 416.5% WoW (value: INR 2,927,408, baseline: INR 567,041, z=+5.12)"
    )

    examples = {
        "✓ Grounded output (should pass)": (
            "## Headline\n"
            "Total Shopify revenue is INR 3,915,420 (+167.6% WoW), with new-customer revenue at INR 2,927,408.\n\n"
            "## What needs attention\n"
            "- ROAS on shopping_google_003 reached 6.184 vs baseline 1.051 — the 500.0% figure is capped."
        ),
        "✗ Hallucinated number (should fail)": (
            "## Headline\n"
            "Revenue is INR 3,915,420 (+167.6% WoW). But conversion rate fell 23.4% — investigate today."
        ),
        "✗ Hallucinated causation (passes numeric, fails the prompt)": (
            "## Headline\n"
            "Revenue rose 167.6% **because** Meta CPM dropped, driving incremental Shopify orders."
        ),
    }
    pick = st.selectbox("Pre-fill example", list(examples.keys()))
    facts = st.text_area("FACTS block (this is what the LLM was given)",
                         facts_default, height=140)
    output = st.text_area("LLM output (paste, or use the example)", examples[pick], height=180)

    if st.button("▶ Validate", type="primary"):
        log_step("Grounding check: running validate_numeric_grounding()")
        ok, ungrounded = validate_numeric_grounding(output, facts)
        if ok:
            st.success("✅ All numbers in the output trace back to the FACTS block. Report would ship as-is.")
            log_step("Grounding: PASSED")
        else:
            st.error(f"❌ {len(ungrounded)} ungrounded number(s) found: `{', '.join(ungrounded)}`")
            st.warning("In production this triggers `render_template_fallback()` and the LLM output is rejected.")
            log_step(f"Grounding: FAILED — ungrounded={ungrounded}", level="WARNING")

        with st.expander("Show what the validator extracted"):
            from report_generator import _normalize_numbers
            in_out = _normalize_numbers(output)
            in_fct = _normalize_numbers(facts)
            c1, c2 = st.columns(2)
            with c1:
                st.markdown("**Numbers in output**")
                st.write(sorted(in_out))
            with c2:
                st.markdown("**Numbers in facts**")
                st.write(sorted(in_fct))


# ──────────────────────────────────────────────────────────────────────────────
# SECTION 6 — Report generation
# ──────────────────────────────────────────────────────────────────────────────

def section_report_generation() -> None:
    step_header(6, 9, "LLM report generation",
                "Click the button. Watch the logs. Read the report.")

    narration(
        "Clicking **Generate** runs the full online pipeline: `ranker.rank_day(date)` → "
        "`apply_profile_reranking(findings, profile)` → slice top 5 (or 8 weekly) → "
        "`steady_findings_for_date()` picks 3 low-z high-business-weight findings → `build_daily_prompt()` "
        "assembles DATE + CUSTOMER + FACTS + STEADY + format spec → `client.messages.create()` is called "
        "with `system=[{'text': SYSTEM_PROMPT, 'cache_control': {'type': 'ephemeral'}}]` for prompt caching → "
        "three retries with exponential backoff on transient failures → `validate_numeric_grounding()` runs "
        "on the response. Pass: return LLM markdown. Fail: return `render_template_fallback()` output. The "
        "log lines from `report_generator` (latency, in/out tokens, cache_read, cache_write) appear live in "
        "the sidebar tail."
    )

    ranker = get_ranker()
    gen    = get_report_generator()
    if ranker is None:
        st.error("Ranker unavailable.")
        return

    dates = ranker.available_dates()
    c1, c2, c3 = st.columns(3)
    with c1:
        target_date = st.selectbox(
            "Date", dates,
            index=dates.index("2025-12-10") if "2025-12-10" in dates else len(dates) - 30,
            key="rg_date",
        )
    with c2:
        profile_name = st.radio("Customer profile",
                                [PROFILE_GROWTH.name, PROFILE_SCALE.name],
                                key="rg_profile")
        profile = PROFILE_GROWTH if profile_name == PROFILE_GROWTH.name else PROFILE_SCALE
    with c3:
        report_type = st.radio("Report type", ["Daily digest", "Weekly report"], key="rg_type")

    if not os.getenv("ANTHROPIC_API_KEY"):
        st.warning(
            "⚠️ No `ANTHROPIC_API_KEY` set — clicking generate will go straight to the **template fallback**. "
            "That's also useful for the panel to see, but the real Claude output is what you want to demo. "
            "Set the env var or add `.env` and refresh the page."
        )

    if st.button("▶ Generate report", type="primary"):
        log_step(f"Report gen: type={report_type}, profile={profile.name}, date={target_date}")

        # Compute findings + steady
        if report_type == "Daily digest":
            findings = [f.to_dict() for f in ranker.rank_day(target_date, top_n=10)]
        else:
            findings = [f.to_dict() for f in ranker.rank_week(target_date, top_n=20)]

        steady_raw = ranker.rank_day(target_date, top_n=200)
        stable_metrics = {"revenue", "orders", "aov", "roas", "spend", "new_customer_share"}
        steady = [f.to_dict() for f in steady_raw
                  if not f.is_alert and not f.is_data_quality_flag
                  and f.metric in stable_metrics and abs(f.z_score) < 1.0][:3]

        st.markdown("### What gets sent to the LLM")
        with st.expander("System prompt (cached)"):
            st.code(SYSTEM_PROMPT, language="text")
        with st.expander("User prompt — facts + steady + format spec"):
            if report_type == "Daily digest":
                user_prompt, _ = build_daily_prompt(findings[:DAILY_TOP_N], steady, profile, target_date)
            else:
                user_prompt, _ = build_weekly_prompt(findings[:WEEKLY_TOP_N], steady, profile,
                                                     target_date, [])
            st.code(user_prompt, language="text")

        st.markdown("### Output")
        if gen is None:
            log_step("API key missing — invoking template fallback", level="WARNING")
            content = render_template_fallback(findings[:DAILY_TOP_N], steady, profile,
                                               target_date,
                                               "daily" if report_type == "Daily digest" else "weekly")
        else:
            try:
                with st.status("Calling Claude…", expanded=True) as status:
                    t0 = time.time()
                    if report_type == "Daily digest":
                        content = gen.generate_daily(findings, profile, target_date, steady=steady)
                    else:
                        content = gen.generate_weekly(findings, profile, target_date,
                                                       daily_summaries=[], steady=steady)
                    status.update(label=f"Done in {time.time()-t0:.2f}s", state="complete")
            except Exception as e:
                log_step(f"Generation failed: {e}", level="ERROR")
                content = render_template_fallback(findings[:DAILY_TOP_N], steady, profile,
                                                   target_date, "daily")

        st.markdown(content)

        REPORTS_DIR.mkdir(parents=True, exist_ok=True)
        slug = profile.name.split()[0].lower()
        outpath = REPORTS_DIR / f"{'daily' if report_type=='Daily digest' else 'weekly'}_{target_date}_{slug}.md"
        outpath.write_text(content, encoding="utf-8")
        log_step(f"Report saved to {outpath.relative_to(ROOT)}")
        st.caption(f"📄 Saved to `{outpath.relative_to(ROOT)}`")


# ──────────────────────────────────────────────────────────────────────────────
# SECTION 7 — Failure modes & mitigations
# ──────────────────────────────────────────────────────────────────────────────

def section_failures() -> None:
    step_header(7, 9, "Three failure modes, three live mitigations",
                "The brief asks for the top three. Here they are, with a demonstrable defence each.")

    narration(
        "Each tab below runs different code that demonstrates one of the three mitigations. The Sunday tab "
        "filters `ranker.available_dates()` to dates where `pd.Timestamp(d).dayofweek == 6`, takes the last 4, "
        "and calls `rank_day(d, top_n=20)` on each — picking out the revenue finding and reading "
        "`f.z_score` and `f.is_alert` to show that DoW adjustment kept them near zero. The DQ tab calls "
        "`rank_day(last_date, top_n=10)` and reads `f.is_data_quality_flag` on each Finding — true findings "
        "appear sorted to the bottom by the 3-level tuple key. The hallucinated-causation tab is informational "
        "(the live demo for that lives in section 5)."
    )

    ranker = get_ranker()
    if ranker is None:
        st.error("Ranker unavailable.")
        return

    tabs = st.tabs([
        "1. Hallucinated causation",
        "2. Alert fatigue (Sunday problem)",
        "3. Incomplete last-day data",
    ])

    with tabs[0]:
        st.markdown(
            "**Risk** — The LLM writes 'revenue dropped because Meta CPM rose'. The brand owner cuts Meta. "
            "Revenue keeps dropping (real cause was a checkout bug). Trust gone.\n\n"
            "**Three mitigations layered**:\n"
            "1. System prompt explicitly bans `because`, `caused by`, `due to`.\n"
            "2. Prompt cites the actual constraint — r=0.12 between spend and revenue.\n"
            "3. Facts arrive as independent sentences; the LLM has to actively construct any cross-finding "
            "claim, and the only safe construction is hedged.\n\n"
            "**Try it live**: paste a causation-laden output in section 5 ('Hallucinated causation' example). "
            "The numeric check will pass, but in production we'd add a second check for banned words."
        )

    with tabs[1]:
        st.markdown(
            "**Risk** — naive z-score on this dataset would flag every Sunday (always below the 28-day mean). "
            "Within two weeks users stop opening the email."
        )
        dates = ranker.available_dates()
        sample = [d for d in dates if pd.Timestamp(d).dayofweek == 6][-4:]
        st.markdown(f"Let's check the last 4 Sundays in the data: {sample}")
        rows = []
        for d in sample:
            findings = ranker.rank_day(d, top_n=20)
            revenue_finding = next((f for f in findings
                                    if f.metric == "revenue" and f.source == "shopify"
                                    and not f.channel and not f.customer_type), None)
            if revenue_finding:
                rows.append({
                    "Sunday":         d,
                    "value":          revenue_finding.value,
                    "DoW baseline":   revenue_finding.baseline_mean,
                    "z (DoW-adj)":    round(revenue_finding.z_score, 2),
                    "is_alert":       "🚨" if revenue_finding.is_alert else "—",
                })
        if rows:
            st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)
            st.success(
                "Most Sundays show **z near zero** because the baseline is the average of recent Sundays, "
                "not the average of all days. A real Sunday anomaly (z>2) still flags. False positives gone."
            )
        else:
            st.info("No revenue findings on those days.")

    with tabs[2]:
        last = ranker.available_dates()[-1]
        st.markdown(f"**Risk** — `{last}` is the last day in the dataset. A naive system might flag it as a "
                    f"92% revenue crash because the export was generated mid-day and is incomplete.")
        if st.button("▶ Rank the last day"):
            findings = ranker.rank_day(last, top_n=10)
            df_f = findings_table([f.to_dict() for f in findings])
            st.dataframe(df_f, use_container_width=True, hide_index=True)
            dq = [f for f in findings if f.is_data_quality_flag]
            if dq:
                st.success(
                    f"✅ {len(dq)} findings carried the **`is_data_quality_flag = True`** marker. "
                    "The sort order pushed them to the bottom and the fact sentence appends "
                    "`[NOTE: possible incomplete data — last day of dataset]` so the LLM "
                    "communicates the caveat instead of leading with 'revenue crashed 92%'."
                )
                with st.expander("Look at one DQ-flagged fact sentence"):
                    st.code(dq[0].fact_sentence, language="text")


# ──────────────────────────────────────────────────────────────────────────────
# SECTION 8 — Cost & scale calculator
# ──────────────────────────────────────────────────────────────────────────────

def section_scale() -> None:
    step_header(8, 9, "Cost and scale",
                "Where this breaks at 100 customers, 1k, 10k.")

    narration(
        "This page makes no API calls — every number is a pure Python calculation against constants. "
        "`INPUT_TOK = 2000`, `OUTPUT_TOK = 1500` are typical per-report token counts. Sonnet pricing "
        "(USD 3 in / 15 out per million tokens) and Haiku pricing (USD 0.80 in / 4 out) are the published "
        "Anthropic rates. The caching toggle reduces the effective input price by `1 − 0.8 + 0.8 × 0.1` "
        "(80% of input is cached system prompt, billed at 10% on cache hits). The tiered-model toggle assumes "
        "70% Haiku / 30% Sonnet split. The cost-curve chart sweeps the same formula across customer counts via "
        "a Python list comprehension and renders log-log via Altair."
    )

    c1, c2 = st.columns([1, 1])
    with c1:
        n_customers = st.slider("Number of customers", 10, 20_000, 1_000, step=100)
        reports_per_day = st.slider("Reports per customer per day", 1, 3, 2)
        prompt_caching = st.toggle("Prompt caching on", value=True)
        model_choice = st.radio("Default model",
                                ["Sonnet (premium)", "Haiku for low-engagement, Sonnet for high"])

    # Pricing (approximate, USD per million tokens) — Sonnet ~$3 in / $15 out, Haiku ~$0.80 in / $4 out
    INPUT_TOK   = 2000
    OUTPUT_TOK  = 1500
    USD_INR     = 84.0

    if "Sonnet (premium)" in model_choice:
        in_price_usd_per_m  = 3.0
        out_price_usd_per_m = 15.0
    else:
        # Assume 70% Haiku / 30% Sonnet
        in_price_usd_per_m  = 0.7 * 0.80 + 0.3 * 3.0
        out_price_usd_per_m = 0.7 * 4.0 + 0.3 * 15.0

    if prompt_caching:
        # Roughly 80% of system prompt is shared and cache-read price is ~10% of base
        cache_fraction      = 0.8
        in_price_effective  = in_price_usd_per_m * (1 - cache_fraction + cache_fraction * 0.1)
    else:
        in_price_effective = in_price_usd_per_m

    per_report_usd = (INPUT_TOK  / 1_000_000) * in_price_effective \
                   + (OUTPUT_TOK / 1_000_000) * out_price_usd_per_m
    per_report_inr = per_report_usd * USD_INR

    daily_inr   = per_report_inr * reports_per_day * n_customers
    monthly_inr = daily_inr * 30
    monthly_usd = monthly_inr / USD_INR

    with c2:
        st.metric("Cost per report", f"INR {per_report_inr:.2f}")
        st.metric("Daily LLM cost",  f"INR {daily_inr:,.0f}")
        st.metric("Monthly LLM cost",
                  f"INR {monthly_inr:,.0f}",
                  f"≈ USD {monthly_usd:,.0f}/mo")

    st.subheader("Cost curve across customer counts")
    sweep = pd.DataFrame([
        {"customers": n, "monthly_inr": per_report_inr * reports_per_day * n * 30}
        for n in [50, 100, 500, 1_000, 2_500, 5_000, 10_000, 20_000]
    ])
    chart = alt.Chart(sweep).mark_line(point=True).encode(
        x=alt.X("customers:Q", title="Customers", scale=alt.Scale(type="log")),
        y=alt.Y("monthly_inr:Q", title="Monthly LLM cost (INR)",
                scale=alt.Scale(type="log")),
        tooltip=["customers", alt.Tooltip("monthly_inr:Q", format=",.0f")],
    ).properties(height=300)
    st.altair_chart(chart, use_container_width=True)

    st.subheader("Where each tier breaks")
    st.markdown(
        "| Customers | Binding constraint | Mitigation |\n"
        "|---|---|---|\n"
        "| 100 | Two profiles look samey across users | Add 4 more cold-start profiles |\n"
        "| 1,000 | Sequential batch latency | Parallel LLM calls, job queue |\n"
        "| 10,000 | Cost wall (~USD 100k/mo on Sonnet without caching) | Caching + Haiku tier + length budgets |\n"
        "| 50,000 | Cross-tenant isolation, data freshness SLA across timezones | Per-tenant batches, peer-benchmark aggregations with k-anonymity |"
    )


# ──────────────────────────────────────────────────────────────────────────────
# SECTION 9 — Full logs
# ──────────────────────────────────────────────────────────────────────────────

def section_logs() -> None:
    step_header(9, 9, "Full session logs",
                "Every action taken on this dashboard since you opened it.")

    narration(
        "All log records on this page come from a single `StreamlitLogHandler` (a `logging.Handler` subclass) "
        "that's attached to Python's root logger by `setup_logging()` in this file. Every `log.info(...)` call "
        "from any module — `dashboard`, `ranker`, `report_generator`, even `httpx` — flows through that "
        "handler and is appended to `st.session_state['logs']`. The dataframe below filters and renders that "
        "list. Clearing fires `st.rerun()` after wiping the session-state list. Download serialises the same "
        "list to JSON. In production this same handler interface would point at Datadog, Honeycomb, or "
        "CloudWatch instead of session state."
    )

    logs = st.session_state.get("logs", [])
    c1, c2, c3 = st.columns([1, 1, 1])
    c1.metric("Total log records", len(logs))
    c2.metric("Warnings", sum(1 for l in logs if l["level"] == "WARNING"))
    c3.metric("Errors", sum(1 for l in logs if l["level"] in {"ERROR", "CRITICAL"}))

    f1, f2 = st.columns([1, 1])
    with f1:
        levels = st.multiselect("Level filter",
                                ["INFO", "WARNING", "ERROR", "CRITICAL", "DEBUG"],
                                default=["INFO", "WARNING", "ERROR", "CRITICAL"])
    with f2:
        name_filter = st.text_input("Logger-name filter (substring)", "")

    filtered = [l for l in logs
                if l["level"] in levels and (name_filter.lower() in l["name"].lower())]

    df = pd.DataFrame(filtered)
    if not df.empty:
        st.dataframe(df[["ts", "icon", "level", "name", "msg"]],
                     use_container_width=True, hide_index=True)
    else:
        st.info("No matching log records yet — go run something in the earlier sections.")

    bc1, bc2 = st.columns([1, 1])
    with bc1:
        if st.button("Clear logs"):
            st.session_state["logs"] = []
            st.rerun()
    with bc2:
        st.download_button(
            "Download logs as JSON",
            data=json.dumps(logs, indent=2),
            file_name=f"ecom-digest-session-{datetime.now():%Y%m%d-%H%M%S}.json",
            mime="application/json",
        )


# ──────────────────────────────────────────────────────────────────────────────
# Main
# ──────────────────────────────────────────────────────────────────────────────

SECTIONS = {
    "1. Overview":                 section_overview,
    "2. Data exploration (EDA)":   section_eda,
    "3. Ranker (deterministic)":   section_ranker,
    "4. Personalisation":          section_personalisation,
    "5. Hallucination check":      section_grounding,
    "6. Report generation":        section_report_generation,
    "7. Failure modes":            section_failures,
    "8. Cost & scale":             section_scale,
    "9. Full logs":                section_logs,
}


def render_sidebar_log_tail() -> None:
    """Tail of the log stream, always visible in the sidebar."""
    st.sidebar.markdown("---")
    st.sidebar.markdown("### 📜 Live log tail")
    logs = st.session_state.get("logs", [])
    if not logs:
        st.sidebar.caption("No log records yet.")
        return
    for entry in logs[-8:]:
        st.sidebar.markdown(
            f"<code style='font-size:11px'>{entry['icon']} {entry['ts']} "
            f"<b>{entry['name']}</b> {entry['msg']}</code>",
            unsafe_allow_html=True,
        )


def _bridge_secrets_to_env() -> None:
    """
    Bridge Streamlit secrets to environment variables so report_generator.py
    (which reads via os.getenv) picks them up on Streamlit Cloud and locally.
    Precedence: existing env var > st.secrets > nothing (demo mode).

    Wrapped in try/except because st.secrets raises FileNotFoundError when
    no .streamlit/secrets.toml exists locally and the app is running
    without Streamlit Cloud secrets configured — that's a valid mode.
    """
    if os.getenv("ANTHROPIC_API_KEY"):
        return
    try:
        key = st.secrets.get("ANTHROPIC_API_KEY")
        if key and key != "your_api_key_here":
            os.environ["ANTHROPIC_API_KEY"] = key
    except (FileNotFoundError, KeyError, AttributeError):
        # No secrets.toml and no Streamlit Cloud secrets — fine, dashboard
        # falls through to the .env loader in report_generator.py.
        pass


def main() -> None:
    st.set_page_config(
        page_title="Ecom Digest — Live Walkthrough",
        page_icon="📊",
        layout="wide",
        initial_sidebar_state="expanded",
    )

    _bridge_secrets_to_env()
    setup_logging()

    st.sidebar.title("📊 Ecom Digest")
    st.sidebar.caption("Senior AI Product Engineer take-home")
    st.sidebar.markdown(
        "Use the sections in order during the live walkthrough. "
        "Every button runs the **actual** ranker / report code in `src/`."
    )

    section_name = st.sidebar.radio("Navigate",
                                    list(SECTIONS.keys()),
                                    label_visibility="collapsed")

    SECTIONS[section_name]()

    render_sidebar_log_tail()

    st.sidebar.markdown("---")
    st.sidebar.caption(
        f"Model: `{MODEL}`  ·  "
        f"Top-N daily: {DAILY_TOP_N}  ·  "
        f"Top-N weekly: {WEEKLY_TOP_N}"
    )


if __name__ == "__main__":
    main()
