# Daily Digest & Weekly Report — Product & System Design

---

## 1. Users and Job-to-be-Done

### Daily Digest

The primary reader is a D2C brand's growth lead or founder — someone who opens Slack or email between 8–9 AM before their first standup and needs to know, in under two minutes, whether yesterday was normal. They are not doing analysis. They are doing triage. The question they are answering is: "Do I need to act on anything before my team syncs today?"

This means the Daily Digest must be opinionated. It cannot surface twelve metrics and ask the reader to form a view. It must lead with the single most anomalous signal from the prior day, state whether the data is complete or suspect, and give a directional interpretation with appropriate hedging. A growth lead at a brand doing INR 22.6L median daily revenue does not need to know that impressions were up 3%. They need to know that new customer AOV on December 10 jumped 190.5% week-over-week, and that this pattern is consistent with a sale event driving high-volume, possibly discounted acquisition — not organic demand.

The morning workflow this enables: read the digest, decide within 90 seconds whether to escalate to the paid media manager or the ops team, and walk into standup with a specific question rather than a vague sense that "the numbers looked different."

### Weekly Report

The Weekly Report is read by a different version of the same person — or a different person entirely. The CMO, the agency lead, or the founder doing their Sunday planning review. The job is not triage. The job is pattern recognition across a full week: which channels showed sustained movement, whether a single anomalous day was noise or the start of a trend, and whether budget allocation decisions need to be revisited before the next week begins.

The critical distinction is temporal compression. The Daily Digest answers "was yesterday unusual?" The Weekly Report answers "what does this week tell us about how our acquisition engine is actually performing?" A campaign that showed a ROAS of 0.000 for the week of February 1 — meaning search_google_002 spent budget and returned zero attributed orders — is a budget decision, not a morning alert. It belongs in the Weekly Report with full context, not in a Daily Digest that might have caught it mid-week when data was still accumulating.

The reader mode is slower, more deliberate, and tolerates more text. They are comfortable seeing a ranked table of the top five metric movements of the week alongside a narrative that explains which signals are sustained versus one-day spikes.

---

## 2. What Makes a Metric Movement "Important"

### Statistical Scoring

The core scoring formula is:

```
final_score = (|z|/3 × 0.4 + |WoW|/100 × 0.4 + |MoM|/100 × 0.2) × business_weight
```

Each term captures a different type of signal. The z-score term measures statistical surprise — how far a metric is from its recent baseline relative to its own volatility. The WoW delta term captures short-term directional momentum. The MoM term captures whether this is part of a longer trend or an isolated event. The weights (0.4, 0.4, 0.2) reflect the practical reality that most D2C operational decisions are made on a 7-day horizon, not a 30-day one.

The z-score uses a 28-day rolling window with day-of-week adjustment. Rather than comparing Monday's revenue against a 28-day mean that includes Sundays and Thursdays, the system computes a rolling mean and standard deviation using only the prior four Mondays (or whichever day matches). This is non-negotiable given the data: Thursday is the best-performing day of the week and Sunday is the worst, with a revenue spread across the week that would make unadjusted z-scores nearly meaningless — every Sunday would look like an anomaly and every Thursday would trigger a false positive. The EDA confirmed exactly four anomaly days at |z| > 2 across 160 days (Oct 8, Dec 10, Jan 1, Feb 7), which is a reasonable false-positive rate for a dataset this size.

Anomaly threshold for surfacing to the digest is |z| > 2.0. Signals above |z| > 3.0 are flagged as high-confidence anomalies.

### Business Weighting

Weights live in `ranker.py:BUSINESS_WEIGHTS` and are applied as a multiplier on the statistical score. The full table:

| Metric | Weight | Rationale |
|--------|--------|-----------|
| revenue | 1.0 | Direct P&L impact |
| orders | 0.9 | Volume proxy; leads revenue by hours |
| roas | 0.8 | Efficiency signal; informs budget decisions |
| aov | 0.75 | Cohort quality indicator |
| spend | 0.7 | Input variable; less informative than outputs |
| new_customer_share | 0.65 | Acquisition health |
| attributed_revenue | 0.6 | Platform-reported, weakly correlated with Shopify revenue |
| attributed_orders | 0.55 | Platform-reported volume |
| new_orders | 0.5 | Acquisition volume |
| returning_orders | 0.5 | Retention volume |
| cpc | 0.4 | Auction cost signal |
| cpm | 0.35 | Auction signal; contextual not decisive |
| ctr | 0.3 | Creative signal; rarely actionable same-day |
| impressions | 0.2 | Scale metric; high noise, low decision value |
| clicks | 0.2 | Scale metric; high noise, low decision value |

Revenue is weighted 1.0 because it is the only metric that directly maps to business outcomes. Impressions are weighted 0.2 because a 50% swing in impressions is often explained by auction dynamics, day-of-week, or budget pacing — and almost never requires a human decision on its own. The weight structure means that a large ROAS anomaly on a low-spend campaign will be appropriately discounted relative to a revenue anomaly at the brand's average daily scale.

### The Double-Counting Problem

During EDA, a critical data integrity issue was identified. The Shopify export emits three overlapping slices per metric per day:

1. **Daily total** (`channel = ""`, `customer_type = ""`) — one row per day, the brand's overall number.
2. **By referrer channel** — `direct`, `paid`, `organic`, `email`, `marketplace`, `other`, `unattributed`. ~91% of orders fall into `unattributed`, so its value is numerically identical to the daily total on most days.
3. **By customer type** — `new` and `returning`. These also sum to the daily total.

Scoring everything together would double- or triple-count the same revenue and let the largest single row (`unattributed`) dominate every digest. Worse, the daily total and `unattributed` would surface as two separate findings with the same number.

The fix in `ranker.py:DEDUP_PAIRS` is to **keep the daily total** as the headline revenue/orders/AOV signal and **drop the `unattributed` channel slice** before scoring, since on most days they carry the same value. The remaining attributed channel slices (`direct`, `email`, `paid`, etc.) are still scored — they're directionally useful even though attribution is thin. Customer-type slices (`new`, `returning`) are scored separately and are not dedup'd against the total because they answer a different question (cohort mix).

### The Ratio Metric Artifact Problem

Ratio metrics — ROAS, CTR, CPM, CPC — become mathematically unstable when the denominator approaches zero. In the raw data, shopping_google_003 showed a WoW ROAS change of 4,653% because its spend in the prior week was INR 340, making any positive ROAS in the current week produce an enormous percentage change. This signal is not informative. It is an artifact of near-zero base values.

The mitigation is a hard cap: WoW delta for all ratio metrics is capped at 500% before being passed into the scoring formula. The cap value is chosen to be high enough to preserve genuine large movements (a campaign doubling its ROAS from 1.0 to 2.0 is a 100% WoW change, well below the cap) while eliminating the tail of statistically meaningless artifacts. Capped values are disclosed explicitly in the report output — the system does not silently substitute the cap; it notes that the reported delta is capped and the raw value exceeded 500%.

---

## 3. Personalization

### Cold Start

A brand with fewer than 14 days of data gets a fixed default configuration. Business weights are the standard table above. The DoW adjustment is disabled (insufficient historical observations per day-of-week to compute a stable rolling mean). Z-scores are replaced with simple percentile ranks within the available window. The output leads with a disclosure: "Baseline statistics will stabilize after 4 weeks of data." The profile defaults to PROFILE_GROWTH (see below) on the assumption that a new customer onboarding to an analytics tool is more likely to be in acquisition mode than retention mode.

### Warm Customer (2–8 Weeks)

After two weeks, the DoW adjustment activates — there are now at least two observations per day of the week. After four weeks, the full 28-day rolling window is populated and z-scores become statistically valid. During this window, the system begins accumulating profile signal: what metrics does the user click on or ask follow-up questions about? Do their campaigns skew toward acquisition spend (high Meta and Google spend relative to revenue) or retention investment (email and loyalty spend)? The ranker begins soft-weighting metrics that have historically produced user engagement, adjusting the effective top-N cutoff to favor those metric categories.

### Mature Customer (2+ Months)

By 60+ days, the system has enough history to do three things it cannot do earlier: detect seasonality patterns (e.g., this brand's revenue spikes every Thursday, which should not be surfaced as an anomaly), identify which campaigns are persistent versus ephemeral, and weight the report toward the metric categories that the user has demonstrably acted on. A mature customer's digest is shorter than a cold-start customer's, because the ranker has learned which signals are baseline noise for this specific brand and suppresses them. The Jan 29 direct channel anomaly — revenue +370.7% MoM, AOV INR 33,679 against a baseline of INR 12,312 — would be surfaced prominently for a mature customer because it represents a genuine cohort-level departure, not a seasonal artifact.

### The Two Profiles Built

Both profiles carry the same shape: `primary_metrics`, `known_concerns`, and a free-text `context_note`. Personalisation operates in two places:

1. **Re-ranking at the online layer.** `apply_profile_reranking()` in `report_generator.py` multiplies `final_score` by 1.3 for any finding whose metric is in the customer's `primary_metrics`. This is what actually re-orders the facts the LLM sees — the same Dec 10 dataset produces a digest led by `new_customer_share` for a growth profile vs `aov` for a scale profile.

2. **Prompt injection.** The profile's priorities and known concerns are interpolated into the user message so the LLM weights narrative emphasis the same direction the re-ranker already did.

**PROFILE_GROWTH** — primary metrics: revenue, ROAS, new_customer_share, spend, attributed_revenue, new_orders. Configured for brands where the question is "are we acquiring customers efficiently?" The Dec 10 signal — new customer revenue +416.5% WoW — boosted to the top of the digest by the priority multiplier.

**PROFILE_SCALE** — primary metrics: revenue, AOV, returning_orders, new_customer_share, orders. Configured for brands where acquisition is mature and the question is "what is the quality of the customers we are acquiring, and are we retaining them?" AOV and returning-order anomalies are promoted ahead of ROAS movements in the same ranked list.

The multiplier is a simple, auditable mechanism. It is not a learned model — it is the cold-start prior. With engagement data the multiplier can be replaced by per-customer learned weights without changing the architecture.

---

## 4. System Architecture

### Batch Layer (Nightly)

A scheduled job runs at 2 AM IST, after Shopify and ad platform exports have settled. It ingests three raw CSVs (meta_ads, google_ads, shopify), applies the deduplication rules, computes rolling z-scores for all metrics across all channel-date combinations, calculates WoW and MoM deltas with ratio-metric capping, applies business weights, and produces a `ranked_findings.json` file. This file is the single source of truth for all downstream report generation. The batch layer is entirely deterministic — no LLM is involved. Runtime target: under 4 minutes for 160 days of data at current scale.

The batch layer also writes a `data_quality.json` sidecar that flags: last-day completeness (is the export timestamp within 6 hours of midnight?), any channel with zero spend and non-zero attributed revenue (possible tracking error), and any day where total Shopify revenue deviates more than 60% from the prior same-day-of-week (candidate incomplete export).

### Online Layer (Per-Report)

When a report is requested — either on a schedule or on-demand — the report generator reads `ranked_findings.json`, applies the customer's profile re-ranker (`apply_profile_reranking`, +30% on priority metrics), selects the top-N signals (N=5 for Daily, N=8 for Weekly), converts each finding into a pre-computed fact sentence, and passes the structured payload to the LLM with prompt caching on the shared system prompt. The online layer adds latency for the LLM call. Target end-to-end latency: under 12 seconds at p95. On API failure or numeric-grounding failure, `render_template_fallback()` emits a deterministic markdown rendering of the fact sentences — ugly but always correct.

### Deterministic vs Generative Boundary

The boundary is precisely at the `ranked_findings.json` output. Everything upstream — data ingestion, anomaly detection, scoring, ranking, deduplication, data quality flagging — is deterministic and auditable. Every number that appears in the final report was computed before the LLM was invoked. The LLM's job is exclusively to write coherent, hedged prose connecting pre-computed facts. It cannot query the database, compute a ratio, or invent a metric value.

This boundary exists because LLMs are unreliable calculators and reliable writers. Asking an LLM to determine whether a ROAS movement is significant requires it to implicitly reason about baselines, volatility, and business context — tasks it will get wrong in unpredictable ways. Asking it to write a sentence that says "search_google_002 recorded a ROAS of 0.000 for the week of February 1, meaning spend occurred with zero attributed conversions in the available data" is a task it will perform consistently and well.

### Data Flow Diagram

```
Raw CSVs (meta_ads, google_ads, shopify)
        │
        ▼
[ Ingestion & Dedup Layer ]
  - Exclude shopify_total (rollup)
  - Exclude shopify_unattributed (from ranking)
  - Apply last-day completeness check
        │
        ▼
[ Scoring Engine ] ← profile weights
  - 28-day rolling z-score (DoW-adjusted)
  - WoW delta (capped at 500% for ratios)
  - MoM delta
  - final_score formula applied
  - Sustained signal boost (+10%/day for 3+ day streaks)
        │
        ▼
  ranked_findings.json + data_quality.json
        │
        ▼
[ Report Generator ]
  - Apply customer profile re-ranking
  - Select top-N findings
  - Convert to fact sentences
  - Inject data quality flags
        │
        ▼
[ LLM: claude-sonnet-4-6 ]
  - System prompt: hedged causation, cap disclosures, no invention
  - Input: structured fact sentences only
  - Output: narrative prose sections
        │
        ▼
  Daily Digest (.md, 5 sections)
  Weekly Report (.md, 5 sections)
```

---

## 5. Top 3 Failure Modes and Mitigations

### Failure 1: Hallucinated Causation

**Risk:** The LLM attributes causation it cannot verify. "Revenue increased because the Meta campaign reached a new audience segment" sounds plausible and is entirely fabricated if the LLM was not given evidence of that mechanism. For a D2C brand making budget decisions based on these reports, a confident but wrong causal claim is worse than no claim at all.

**Mitigation:** The system prompt contains three explicit rules. First: the LLM may only reference numbers present in the fact sentences passed to it; it may not compute, estimate, or extrapolate. Second: causal language is hedged by default — "consistent with," "may indicate," "warrants investigation" are the approved framings; "caused by," "due to," and "because" are prohibited. Third: `validate_numeric_grounding()` (in `report_generator.py`) regex-extracts every numeric token from the LLM output, normalises (strip commas, drop trailing `%`, round to 2dp, ignore single-digit ordinals), and verifies each appears in the FACTS + STEADY blocks. If any number is ungrounded, the output is rejected and `render_template_fallback()` produces a deterministic markdown rendering of the verified facts. The check is logged with the specific ungrounded tokens so the failure mode is debuggable, not silent.

### Failure 2: Alert Fatigue

**Risk:** If every metric movement above a z-score threshold is surfaced, a brand with 15 active campaigns across two ad platforms will receive a digest with 40+ findings every morning. Users stop reading. The product becomes noise.

**Mitigation:** Four compounding mechanisms prevent this. Business weights filter out low-signal metric categories before ranking — impressions and CTR rarely reach the top-N. The DoW adjustment prevents recurrent day-of-week patterns from being scored as anomalies; Thursday revenue being high is not a finding. The top-N cap (5 for Daily, 8 for Weekly) is a hard limit regardless of how many signals exceed the threshold. Finally, sustained signal boosting — where signals persisting for 3+ consecutive days in the same direction receive a +