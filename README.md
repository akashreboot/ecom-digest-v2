# Daily Digest & Weekly Report — Take-Home Assignment

## What this builds
A system that generates personalized daily digests and weekly reports
for D2C e-commerce brands, surfacing only the metric movements that
genuinely matter — grounded in data, never hallucinated.

## How to run

### Setup
    cd ecom-digest-v2
    python -m venv venv
    # macOS / Linux:
    source venv/bin/activate
    # Windows:
    venv\Scripts\activate
    pip install -r requirements.txt

### Add your API key
Create a `.env` file in the project root:
    ANTHROPIC_API_KEY=sk-ant-...

### Place the dataset
Copy `metrics_159d.csv` into the `data/` folder.

### Run the interactive dashboard (presentation mode)

    streamlit run dashboard.py

Opens a 9-section walkthrough on http://localhost:8501. Every button
runs the real code in `src/`, every action streams logs to the
sidebar, and the LLM call is gated on `ANTHROPIC_API_KEY` (without
a key the system shows the template fallback in action — useful for
demoing the failure path too).

Sections:
1. Overview + architecture diagram
2. EDA — DoW seasonality, the r=0.12 finding, the triple-counting trap
3. Ranker — pick a date, inspect the math, see the fact sentences
4. Personalisation — same date / two profiles / different leading findings
5. Hallucination check — paste fake output, watch the validator catch it
6. Report generation — run Claude, watch the logs, read the report
7. Failure modes — three failures with a live demo of each mitigation
8. Cost & scale — interactive calculator for 100 → 10k customers
9. Full logs — every action this session, downloadable as JSON

### Or run the notebooks directly
1. `notebooks/01_eda.ipynb` — data exploration and baseline stats
2. `notebooks/02_ranker_dev.ipynb` — ranking engine validation, writes `ranked_findings.json`
3. `notebooks/03_generate_reports.ipynb` — generates the sample reports
4. `notebooks/04_design_doc.ipynb` — generates the design doc and tradeoffs

## Deploying to Streamlit Community Cloud

The dashboard is deployable on [share.streamlit.io](https://share.streamlit.io) in five steps.

1. **Push this repo to GitHub** if it isn't already public-or-accessible to your Streamlit Cloud account.
2. **Configure secrets locally** for testing:
   - Copy `.streamlit/secrets.toml.example` to `.streamlit/secrets.toml`
   - Replace `your_api_key_here` with your real Anthropic API key
   - `.streamlit/secrets.toml` is gitignored — it will never be committed
3. **Create the app** on Streamlit Cloud: New app → repo → branch → `dashboard.py` as the main file
4. **Paste secrets** under the app's **Settings → Secrets** panel. Use the exact same TOML contents you used locally — Streamlit Cloud exposes them via both `st.secrets["ANTHROPIC_API_KEY"]` and the `ANTHROPIC_API_KEY` env var.
5. **Handle the dataset**. `data/metrics_159d.csv` is gitignored (the dataset notice forbids redistribution) and Streamlit Cloud doesn't auto-upload it. Three options, in order of preference:
   - Upload a copy to a private S3 / GCS bucket and add download-on-startup logic
   - Use a Streamlit file_uploader in section 1 so the panel can drop the CSV at runtime
   - Bundle a small anonymised demo CSV that's safe to commit publicly

The secrets bridge in `dashboard.py:_bridge_secrets_to_env()` reads `st.secrets["ANTHROPIC_API_KEY"]` and copies it into the `ANTHROPIC_API_KEY` environment variable before any code that needs it loads, so `report_generator.py`'s existing `os.getenv` path keeps working without modification.

## Project structure

    ecom-digest/
    ├── data/
    │   └── metrics_159d.csv          # dataset (not committed)
    ├── src/
    │   ├── ranker.py                 # deterministic scoring engine
    │   └── report_generator.py      # LLM report generator
    ├── notebooks/
    │   ├── 01_eda.ipynb
    │   ├── 02_ranker_dev.ipynb
    │   ├── 03_generate_reports.ipynb
    │   └── 04_design_doc.ipynb
    ├── outputs/
    │   ├── eda/                      # saved EDA charts + summary
    │   └── reports/                  # generated markdown reports
    ├── design_doc.md                 # Deliverable 1
    ├── tradeoffs.md                  # Deliverable 4
    └── requirements.txt

## Key design decisions

**Why deterministic ranking before the LLM?**
The LLM explains findings — it never selects them. Selection is
auditable, testable, and consistent. Generation is not.

**Why z-score with DoW adjustment?**
Raw z-score fires false alerts every Sunday (always lower than
28d mean). DoW adjustment compares Monday to recent Mondays only,
eliminating a whole class of noise. The baseline mean AND std are
both computed from the same-weekday window so the z numerator and
denominator come from the same distribution.

**Why cap ROAS/ratio metric deltas at 500%?**
A campaign spending INR 10 one week and INR 0 the next shows
infinite WoW change. This is a math artifact, not a business
signal. Cap at 500% and flag [capped] in the fact sentence so
the LLM handles it correctly.

**Why pre-compute fact sentences before the LLM call?**
Hallucination prevention. Every number the LLM sees is already
computed and formatted. After generation, `validate_numeric_grounding()`
re-extracts every numeric token from the LLM output and verifies it
exists in the input facts; ungrounded numbers trigger a deterministic
template fallback.

**Why prompt caching?**
The system prompt is identical across every report. `cache_control:
ephemeral` on the system block means it's billed at full rate once per
~5 minute window and at ~10% after that, which is the main cost lever
for the 10k-customer scenario.

**Why a profile multiplier instead of a learned model?**
No engagement data exists yet. A +30% boost on `profile.primary_metrics`
is the cold-start prior — auditable, easy to override, and ready to be
replaced by learned per-customer weights once 4–6 weeks of click data
exists.

## Key data insights from EDA
- Ad spend and revenue weakly correlated (Meta r=0.12, Google r=0.15)
  — causal attribution not supportable from this data
- 91% of Shopify orders unattributed at channel level
  — channel slices are directional signals only
- Strong Thursday peak, Sunday trough — DoW adjustment is essential
- 4 anomaly days in 160 days — Oct 8, Dec 10, Jan 1, Feb 7

## How I used AI tools
- **Claude (claude-sonnet-4-6):** LLM layer in report_generator.py,
  design doc drafting, tradeoffs analysis
- **All ranking logic (ranker.py):** deterministic Python, no LLM
- **All report numbers:** pre-computed facts — LLM cannot invent values
- **EDA insights:** discovered through manual notebook exploration