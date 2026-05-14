# Trade-offs & What's Next

## What I cut and why

Several capabilities are conspicuously absent, and none of them were forgotten.

**Personalization ML** was the most significant cut. A learned model for weighting metrics by customer behavior would be more defensible than my rule-based growth/scale profiles, but it requires click-stream or engagement data that doesn't exist yet. The two profiles are a reasonable prior, not a substitute—they encode assumptions about what growth-stage versus scale-stage brands care about, and those assumptions need to be validated before training anything on top of them.

**GA4 funnel metrics** (sessions, bounce rate, conversion steps) weren't in the dataset. I could have synthesized them, but synthetic data in a prototype creates false confidence in the architecture. The ranker is designed to accept additional metric columns; the plumbing is ready when the data is.

**Email delivery, A/B testing infrastructure, and real-time alerting** were all out of scope by deliberate choice rather than time pressure. Delivery infrastructure is a solved problem and adds no signal in a prototype. A/B testing report variants requires real engagement data to measure against—running it now would generate noise, not insight. Real-time alerting adds operational complexity that isn't justified until we know daily batch isn't sufficient; for a morning digest use case, it probably is.

**Hourly granularity and SKU-level breakdowns** are dataset constraints, not design constraints. The architecture doesn't preclude them.

## What I'd build next

In priority order, and each priority is contingent on the one before it producing signal:

1. **Engagement feedback loop first.** A thumbs up/down on each finding, plus click tracking on any linked metric, is the minimum viable signal. Nothing else on this list is worth building without it. This is the closed loop that makes everything downstream defensible.

2. **Metric preference learning from engagement signals.** Once we have finding-level feedback, weight adjustments become a learning problem rather than a configuration problem. Even a simple logistic regression over "finding type × customer segment × engaged/ignored" is more honest than my current business weight constants.

3. **Calendar event awareness.** Suppressing January 1st anomalies and flagging known promotional days would immediately reduce false positive noise. This is low-complexity, high-trust-building work—the kind of thing that makes customers feel understood rather than spammed.

4. **Cross-tenant pattern library.** Statements like "D2C brands at your revenue stage typically see Thursday order volume 12% below Tuesday baseline" require enough tenants and enough engagement history to be statistically grounded. This is a 12-month build, not a 3-month one.

5. **Explanation confidence scoring.** The LLM currently hedges causation linguistically. I'd rather surface an explicit confidence tier per finding—high data support, moderate, speculative—so customers calibrate appropriately and so we can measure whether high-confidence findings drive more action.

## What I need to learn from real customers first

Before any of the above gets prioritized, I have questions that prototypes can't answer:

- **Do they read daily or skip to weekly?** The entire batch cadence assumption depends on this. If customers open Monday's report on Wednesday, daily freshness is wasted infrastructure cost.
- **Which finding types drive follow-up actions versus get ignored?** I have hypotheses about which z-score thresholds feel actionable. I don't have evidence.
- **What's the tolerance for false positives versus false negatives?** A brand that checks their dashboard obsessively wants high recall and will tolerate noise. A brand that only looks when the report flags something needs high precision. These are different products wearing the same interface.
- **Do growth-stage and scale-stage brands actually behave differently in response to findings?** My profile split is a reasonable prior. It may be wrong. If scale-stage brands respond strongly to acquisition findings too, the segmentation logic needs revisiting before it gets encoded anywhere more permanent.

## Where this breaks at scale

I want to be specific rather than vague about this.

**At 100 customers**, two problems emerge. Sequential LLM calls at roughly 3 seconds each means a two-report-per-customer batch runs approximately 10 minutes. That's manageable but not comfortable, and it's fully blocking. Parallelization is straightforward but requires async refactoring and rate limit management. The subtler problem at 100 customers is report differentiation—with rule-based profiles and a shared prompt structure, reports across similar customers will start to look nearly identical. Customers will notice, and it will correctly feel like a mail merge rather than an analyst.

**At 10,000 customers**, cost becomes the binding constraint. At roughly $0.15 per report and two reports per customer, the daily LLM spend is approximately $3,000. Prompt caching on the static system prompt and metric context structure is the first mitigation, but it requires careful prompt architecture to maximize cache hit rate. Beyond cost, nightly batch SLA risk becomes real—a slow tenant cohort or a model latency spike could push delivery past the morning window customers expect. Tenant data isolation also needs to be explicit in the architecture; right now it's implicit in how I've structured the notebook. At 10,000 tenants, "implicit" is a liability.

None of these are surprising failure modes, which is why I'm stating them plainly. The prototype is honest about what it is: a working proof of concept for the ranking and generation logic, not a production system.