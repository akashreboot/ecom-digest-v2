# Trade-offs & What's Next

## What I cut and why

Several capabilities were deliberately left out, and I want to be direct about the reasoning rather than framing every omission as a principled architectural choice.

**Personalization ML model.** Without clickstream or engagement data, a learned model would be fitting noise. Rule-based growth vs. scale-stage profiles are a reasonable first approximation, but they're essentially educated guesses about what different customers care about. The profiles need validation before they deserve a more sophisticated implementation.

**GA4 sessions, bounce rate, and conversion funnel data.** Not in the dataset. The ranker operates entirely on revenue and order-side signals, which means it's blind to top-of-funnel dynamics. A D2C brand optimizing acquisition without session data is working with one hand tied behind its back—this is a meaningful gap, not a cosmetic one.

**Calendar event awareness.** The current system will flag a January 1st revenue drop as anomalous. It isn't. Hardcoding known sale days and suppressing obvious calendar artifacts should have been in scope for v1; I deprioritized it in favor of the LLM grounding layer, which was the wrong call.

**Hourly granularity, SKU-level breakdowns, real-time alerting, email delivery, and A/B testing.** These were genuinely out of scope or blocked by data availability, not just deferred. Daily batch is the right architecture for a daily digest product. The rest requires infrastructure that would have produced a deployment prototype rather than a product prototype.

---

## What I'd build next

In priority order, based on what would generate the most learning per unit of effort:

1. **Engagement feedback loop.** A thumbs up/down on each finding, plus click tracking on any recommended action. Without this, metric weight tuning is opinion-driven. Everything downstream depends on closing this loop first.

2. **Metric preference learning.** Once engagement signals exist, adjust per-tenant business weights based on which finding types drive follow-up actions. The z-score ranker architecture already supports pluggable weights—this is an incremental addition, not a rewrite.

3. **Calendar event awareness.** Suppress known artifacts (holidays, prior sale days) and flag annotated events positively ("revenue up 40% WoW—your flash sale ran Tuesday"). This is the highest-confidence improvement I can name without customer data: false positives from calendar effects will erode trust faster than almost anything else.

4. **Cross-tenant pattern library.** Aggregate anonymized patterns across similar brands to add benchmarking context—"brands at your stage typically see Thursday softness of 8–12%; yours is 22%." This makes individual findings meaningfully more actionable.

5. **Explanation confidence scoring.** The LLM currently hedges causation in language but doesn't expose a structured confidence signal. Scoring each causal hypothesis by data support (sample size, consistency across periods, statistical significance) would let the UI surface high-confidence findings differently and help customers calibrate trust over time.

---

## What I need to learn from real customers first

Several design assumptions baked into this prototype could be completely wrong, and I'd rather know before building on top of them.

**Reading cadence.** Do customers read the daily digest daily, or do they accumulate and skim weekly? If the latter, the daily freshness optimization in the ranker is wasted and weekly summaries should be the primary artifact.

**Which findings drive action vs. which get ignored.** The ranker scores by statistical unusualness and business weight. Customers may actually act on a narrower set of finding types than the model surfaces. Without click data, I'm guessing at what "useful" means.

**False positive tolerance.** A risk-averse operator who ignores noisy alerts is a different calibration target than a growth-stage founder who wants everything flagged. The current thresholds reflect a judgment call I made without data.

**Whether the two customer profiles actually behave differently in practice.** The growth vs. scale-stage segmentation is a hypothesis. It's possible the within-segment variance swamps the between-segment differences, in which case the profiling adds complexity without adding value.

---

## Where this breaks at scale

The current architecture is sequential and naive, which is fine for a prototype and a problem at production volume.

**At 100 customers**, LLM latency becomes the binding constraint. Sequential API calls at roughly 3 seconds each means a 5-minute batch for 100 tenants—borderline acceptable until it isn't. More concerning: with a fixed prompt structure and similar input data shapes, reports start converging on the same phrasing and sentence patterns. Differentiation degrades before cost does.

**At 10,000 customers**, cost is the hard wall. Two report variants per customer at ~$0.15 each is $3,000 per day, or roughly $90,000 per month, on LLM spend alone before any other infrastructure. Prompt caching on shared system prompt components would recover a meaningful fraction of that, but the architecture needs a fundamentally different approach: pre-computed narrative templates for common finding patterns, LLM reserved for genuinely novel or high-stakes findings, and tiered generation based on customer plan. Tenant data isolation also becomes a compliance concern that needs explicit design, not assumption. And a nightly batch that takes 8+ hours to complete for 10,000 tenants is no longer a nightly batch—it's a problem.

The honest summary: this prototype demonstrates the product concept and surfaces the right research questions. It is not a foundation you'd scale without meaningful rearchitecting of the generation pipeline.