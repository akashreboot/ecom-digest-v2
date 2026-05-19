# Ecom Digest — Personalised D2C Performance Reports

A working prototype of an analytics product that generates daily and weekly performance digests for D2C ecommerce brands. The system surfaces the 3–5 metric movements that actually matter, explains them in plain English with grounded numbers, and ships in under 5 minutes of read-time per digest.

## What this project demonstrates

- **Two-stage architecture** that cleanly separates deterministic selection from generative explanation
- **Grounded LLM output** — every number in every report is pre-computed and post-validated against its source
- **Personalisation that re-orders findings** based on the customer's brand stage and priority metrics
- **Three layered failure-mode mitigations** — hallucinated causation, alert fatigue, incomplete data
- **A live, end-to-end Streamlit dashboard** that exercises the actual code from a single URL, including the LLM call, prompt caching, the grounding validator, and the template fallback

## Quick start

### Prerequisites

- Python 3.10 or higher
- An Anthropic API key (optional — without one, the dashboard renders via the deterministic template fallback)
- The dataset `metrics_159d.csv` (not committed; placed in `data/` locally or uploaded at runtime in the dashboard)

### Local setup

```bash
git clone <repo-url>
cd ecom-digest-v2
python -m venv venv

# macOS / Linux
source venv/bin/activate

# Windows PowerShell
.\venv\Scripts\Activate.ps1

pip install -r requirements.txt
```

### Configure the API key

Either:

- Create `.env` with `ANTHROPIC_API_KEY=sk-ant-...`, or
- Copy `.streamlit/secrets.toml.example` to `.streamlit/secrets.toml` and replace the placeholder

### Run the dashboard

```bash
streamlit run dashboard.py
```

Opens a 9-section walkthrough at `http://localhost:8501`. Every button executes the real code in `src/`; every action is logged to a live tail in the sidebar.

Sections:

1. **Overview** — architecture diagram, environment status
2. **Data exploration** — three EDA findings that shaped the design
3. **Ranker** — pick a date, inspect the scoring, see the fact sentences
4. **Personalisation** — same date and two profiles, side-by-side reordering
5. **Hallucination check** — the numeric grounding validator with examples
6. **Report generation** — live Claude call, watch logs stream, view output
7. **Failure modes** — three failures with a live demonstration of each mitigation
8. **Cost & scale** — interactive calculator for 100 to 10,000 customers
9. **Full logs** — every action this session, downloadable as JSON

### Run notebooks instead

For headless / batch use:

1. `notebooks/01_eda.ipynb` — data exploration and baseline statistics
2. `notebooks/02_ranker_dev.ipynb` — ranker validation, writes `ranked_findings.json`
3. `notebooks/03_generate_reports.ipynb` — generates the sample digests and reports
4. `notebooks/04_design_doc.ipynb` — generates the design and trade-offs docs

## Deploying to Streamlit Community Cloud

1. Push to GitHub (this repo)
2. On [share.streamlit.io](https://share.streamlit.io), create a new app pointing to `dashboard.py`
3. Add `ANTHROPIC_API_KEY` under the app's Settings → Secrets in TOML format:

   ```toml
   ANTHROPIC_API_KEY = "sk-ant-..."
   ```

4. Handle the dataset: `data/metrics_159d.csv` is gitignored. On Cloud, either bundle an anonymised demo CSV, add S3 download logic, or use the built-in file uploader in section 1 of the dashboard to drop the CSV at runtime.

`dashboard.py:_bridge_secrets_to_env()` reads `st.secrets["ANTHROPIC_API_KEY"]` and exposes it as the `ANTHROPIC_API_KEY` environment variable before any downstream module imports, so the existing `os.getenv` lookup in `report_generator.py` continues to work without modification.

## Project structure

```
ecom-digest-v2/
├── data/                       # dataset (gitignored)
│   └── metrics_159d.csv
├── src/
│   ├── ranker.py               # deterministic scoring engine
│   └── report_generator.py     # LLM report generator + safety layer
├── notebooks/
│   ├── 01_eda.ipynb            # data exploration
│   ├── 02_ranker_dev.ipynb     # ranker validation
│   ├── 03_generate_reports.ipynb
│   └── 04_design_doc.ipynb
├── scripts/
│   └── render_docs.py          # markdown → PDF batch renderer
├── dashboard.py                # Streamlit walkthrough
├── design_doc.md               # product and system design
├── tradeoffs.md                # what's cut, what's next, where it breaks
├── OPTIMIZATION_JOURNEY.md     # v1 → v2 architectural evolution
├── docs_pdf/                   # PDF renders of the above for reading
├── requirements.txt
└── README.md                   # this file
```

## Architecture

```
        Raw CSV (metrics_159d.csv)
                  │
                  ▼
        ┌─────────────────────────┐
        │   Ranker (Python)       │   nightly batch, deterministic
        │   • z-score (same-DoW)  │   • WoW + MoM deltas
        │   • business weights    │   • ratio cap, dedup, DQ flag
        └────────────┬────────────┘
                     │
                     ▼
            ranked_findings.json
                     │
                     ▼
        ┌─────────────────────────┐
        │   Report Generator      │   online, per-report
        │   • profile re-ranking  │   • prompt caching
        │   • top-5 / top-8 slice │   • retry with backoff
        │   • Claude API call     │   • numeric grounding check
        └────────────┬────────────┘
                     │
            pass ◄───┴───► fail
             │             │
             ▼             ▼
        LLM output    Template fallback
             │             │
             └──────┬──────┘
                    ▼
         Daily / Weekly Markdown Report
```

The deterministic / generative boundary sits at `ranked_findings.json`. Everything upstream is pure Python. The LLM only writes prose connecting pre-computed facts. Every number that appears in the final report was computed before the LLM was invoked, and re-verified after.

## Key design decisions

**Deterministic ranking before the LLM.** The LLM explains findings — it never selects them. Selection is auditable, testable, and reproducible. Generation is not. Putting the ranker upstream of the LLM call means every report is traceable through Python logs.

**Z-score with day-of-week-adjusted baseline.** Raw z-score on this dataset would flag every Sunday (always 30–40% below the 28-day mean) and every Thursday (always 30–40% above). The DoW adjustment compares Sunday revenue to recent Sundays only, eliminating an entire class of false positives. The baseline mean and standard deviation are both computed from the same-weekday window, so the z-score numerator and denominator come from the same distribution.

**Ratio-metric cap at ±500%.** ROAS, CTR, CPC, and CPM are mathematically unstable when the denominator approaches zero — a campaign with INR 10 of prior-week spend can show 4,653% WoW change in ROAS. The cap eliminates the artifact tail while preserving genuine large movements; capped values are flagged `[capped]` in the fact sentence so the LLM communicates the caveat instead of treating it as real performance.

**Pre-computed fact sentences before the LLM call.** The LLM never sees raw CSV data. It sees fact sentences with the numbers already baked in. After generation, `validate_numeric_grounding()` regex-extracts every numeric token from the LLM output and verifies it appears in the input. Ungrounded numbers trigger a deterministic template fallback.

**Prompt caching on the shared system prompt.** The system prompt is identical across every report. `cache_control: ephemeral` means it's billed at full rate once per ~5-minute window and at ~10% on cache hits. For batch report generation this is the main cost lever — at 10,000 customers it brings monthly LLM spend from roughly USD 110k to roughly USD 25k.

**Profile multiplier instead of a learned model.** A 1.3× boost on `profile.primary_metrics` is the cold-start prior. Auditable, easy to explain, easy to replace with per-customer learned weights once 4–6 weeks of engagement data exists. The architecture is designed so the constant becomes a function call without disturbing anything downstream.

## Key data insights from EDA

- **r=0.12** between Meta spend and Shopify revenue at the day level — causal attribution is not supportable from this data, which justifies the prompt's banned-words list (no "because", "caused by", "due to")
- **91%** of Shopify orders are unattributed at the channel level — channel slices are directional signals only, and the unattributed slice duplicates the daily total (handled by `DEDUP_PAIRS`)
- **Strong Thursday peak, Sunday trough** with a ~40% spread — day-of-week adjustment is essential before any z-score is meaningful
- **4 anomaly days** in 160 days of data — Oct 8, Dec 10, Jan 1, Feb 7 — a reasonable false-positive rate at the |z|>2 threshold

## What's next

Three architectural upgrades, each replacing a specific structural limitation of the v1 prototype with a known-better, industry-validated alternative. Detailed analysis with implementation sketches in `OPTIMIZATION_JOURNEY.md`.

1. **STL or Prophet decomposition for the baseline.** Replaces the rolling-mean-with-DoW-adjustment baseline. Cleanly separates trend, seasonality, and residual; handles holidays explicitly via Prophet. Used in production by Netflix, Uber, LinkedIn (Luminol), DataDog Watchdog, and Booking.com.
2. **CausalImpact (Bayesian Structural Time Series) for attribution.** Replaces hedged-causation language with counterfactual estimates and credible intervals. Brodersen et al. 2015 (Google Research); standard at Walmart, Lifesight, Measured, Recast.
3. **Cross-metric story grouping.** Collapses correlated findings (e.g. CPM + CPC + CTR + ROAS all moving together) into a single "story" headlined by the common cause. Anodot's core feature; Outlier.ai (acquired by Salesforce); OutOfTheBlue's event summary; DataDog Watchdog correlated alerts.

Each upgrade requires data the v1 dataset doesn't yet have (multiple years for annual seasonality, labelled interventions for CausalImpact, larger finding volume for story grouping). The v1 architecture is intentionally shaped to absorb these upgrades without rewriting the rest of the system.

## What's NOT in the prototype (and why)

| Capability | Reason for omission |
|---|---|
| Engagement-based learned weights | No click data exists yet; learned weights against zero training data fits noise |
| Calendar / holiday awareness | Deferred in favour of the grounding validator; first thing to add post-v1 |
| Test suite (`tests/`) | Honest gap. Ranker invariants and validator behaviour both belong in pytest |
| Real-time alerting | Daily batch is the right architecture for a morning digest; not justified |
| Cross-tenant peer benchmarks | Requires multiple tenants and k-anonymous aggregation; 12-month build |
| GA4 sessions / bounce / conversion rate | Not in the source dataset |
| SKU / device / geography breakdowns | Not in the source dataset |

See `tradeoffs.md` for the full short-form discussion.

## Where this breaks at scale

| Customer count | Binding constraint | Mitigation |
|---|---|---|
| 100 | Two profiles converge across users; reports start to feel templated | Add 4–6 more cold-start profiles |
| 1,000 | Sequential batch generation is borderline (~10 min) | Parallelise the LLM calls; add a proper job queue (Celery, SQS) |
| 10,000 | LLM cost is the wall (~USD 110k/month on Sonnet without caching) | Prompt caching + Haiku for low-engagement accounts + length budgets |
| 50,000 | Per-tenant data isolation and timezone-aware data freshness | Per-tenant batches; k-anonymous peer benchmarks |

## Documentation

- `design_doc.md` — product and system design (users, scoring, personalisation, architecture, failure modes, success metrics)
- `tradeoffs.md` — what's cut, what's next, where it breaks
- `OPTIMIZATION_JOURNEY.md` — v1 architecture decisions and the v2 evolution path
- `docs_pdf/` — PDF renders of the above for reading away from the repo

Regenerate the PDFs after editing the markdown:

```bash
python scripts/render_docs.py
```

## Tech stack

- Python 3.10+
- pandas, numpy for data handling
- Anthropic Python SDK + Claude (`claude-sonnet-4-6`) for the LLM layer
- Streamlit for the interactive dashboard
- Altair for charts
- WeasyPrint + Markdown for PDF rendering
- python-dotenv for local environment configuration
