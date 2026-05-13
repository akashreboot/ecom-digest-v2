"""
report_generator.py — LLM-powered report generator
Uses ranked findings from ranker.py as grounded facts.
The LLM explains; it never selects or invents numbers.

This module owns four production-shaped concerns the design doc promises:
  1. Customer profile re-ranking (boost metrics the customer cares about).
  2. Numeric grounding validation (every number in output must appear in facts).
  3. Prompt caching on the shared system prompt.
  4. Retry + template fallback so a single API failure doesn't kill the batch.
"""

import os
import re
import time
import json
import logging
import anthropic
from pathlib import Path
from dotenv import load_dotenv
from dataclasses import dataclass
from typing import Optional, Tuple, List

load_dotenv(Path(__file__).parent.parent / ".env")
log = logging.getLogger(__name__)

MODEL          = "claude-sonnet-4-6"
MAX_TOKENS     = 1500   # daily digest
MAX_TOKENS_W   = 2500   # weekly report
DAILY_TOP_N    = 5      # facts surfaced to the LLM for daily
WEEKLY_TOP_N   = 8      # facts surfaced to the LLM for weekly
STEADY_N       = 3      # low-scoring "holding steady" facts surfaced as ground truth
PROFILE_BOOST  = 1.3    # multiplier applied to profile.primary_metrics findings
API_RETRIES    = 3
API_BACKOFF_S  = 2.0    # 2, 4, 8


# ── Customer profile ──────────────────────────────────────────────────────────

@dataclass
class CustomerProfile:
    """
    Cold start: only brand_stage and name available.
    Warm/mature: primary_metrics and known_concerns populated from
    engagement signals (clicks, follow-ups, ignored digests).
    """
    name:             str
    brand_stage:      str
    primary_metrics:  list
    known_concerns:   list
    context_note:     str


PROFILE_GROWTH = CustomerProfile(
    name            = "Priya (Growth Brand)",
    brand_stage     = "growth",
    primary_metrics = ["revenue", "roas", "new_customer_share", "spend",
                       "attributed_revenue", "new_orders"],
    known_concerns  = ["ad spend efficiency", "new customer acquisition cost"],
    context_note    = (
        "D2C fashion brand, 18 months old, scaling paid acquisition. "
        "Primary concern is keeping ROAS above 3x while growing new customer base. "
        "Checks digest every morning before approving ad budgets."
    ),
)

PROFILE_SCALE = CustomerProfile(
    name            = "Rahul (Scale Brand)",
    brand_stage     = "scale",
    primary_metrics = ["revenue", "aov", "returning_orders",
                       "new_customer_share", "orders"],
    known_concerns  = ["returning customer retention", "AOV growth"],
    context_note    = (
        "Established D2C home goods brand, 4 years old, optimising for profitability. "
        "Less focused on top-of-funnel spend, more on repeat purchase rate and AOV. "
        "Reads weekly report on Mondays to plan the week."
    ),
)


# ── Profile re-ranking ────────────────────────────────────────────────────────

def apply_profile_reranking(findings: list, profile: CustomerProfile,
                            boost: float = PROFILE_BOOST) -> list:
    """
    Multiply final_score by `boost` for findings whose metric is in the
    customer's primary_metrics. This is the personalisation layer the
    design doc promises — it actually re-orders the list the LLM sees.

    Returns a new list sorted by adjusted score; does not mutate input.
    """
    primary = set(profile.primary_metrics or [])
    reranked = []
    for f in findings:
        f_copy = dict(f)
        original = f_copy.get("final_score", 0.0)
        if f_copy.get("metric") in primary:
            f_copy["final_score"] = round(original * boost, 4)
            f_copy["_profile_boosted"] = True
        else:
            f_copy["_profile_boosted"] = False
        reranked.append(f_copy)

    # Preserve the DQ-bottom, alert-first invariant from the ranker
    reranked.sort(key=lambda f: (
        f.get("is_data_quality_flag", False),
        not f.get("is_alert", False),
        -f.get("final_score", 0.0),
    ))
    return reranked


# ── Numeric grounding validator ───────────────────────────────────────────────

# Match numbers with optional sign, commas, decimals, and trailing %.
# Captures cases like "INR 1,234.56", "+416.5%", "0.000", "2,927,408".
_NUM_RE = re.compile(r"[+-]?\d{1,3}(?:,\d{3})+(?:\.\d+)?%?|[+-]?\d+\.\d+%?|[+-]?\d+%?")

# Tokens to ignore — these are typographic, not factual claims.
_IGNORE = {"0", "1", "2", "3", "4", "5", "6", "7", "8", "9", "10"}


def _normalize_numbers(text: str) -> set:
    """Extract numeric tokens from text, normalize for fair comparison."""
    raw = _NUM_RE.findall(text)
    out = set()
    for tok in raw:
        clean = tok.replace(",", "").rstrip("%").lstrip("+")
        if clean in _IGNORE:
            continue
        try:
            # Round to 2 decimal places to handle cosmetic precision differences
            # (e.g. "1.05" in output vs "1.051" in facts).
            v = float(clean)
            out.add(f"{abs(v):.2f}".rstrip("0").rstrip("."))
        except ValueError:
            continue
    return out


def validate_numeric_grounding(output: str, facts_text: str) -> Tuple[bool, List[str]]:
    """
    Every number that appears in the LLM output should also appear in the
    facts block the LLM was given. Numbers the LLM invents are hallucinations.

    Returns (passed, list_of_ungrounded_numbers). Single-digit numbers
    (0-10) are ignored because they're almost always typographic
    (section ordinals, "3-4 sentences" prompt residue, etc).
    """
    in_output = _normalize_numbers(output)
    in_facts  = _normalize_numbers(facts_text)
    ungrounded = sorted(in_output - in_facts)
    return (len(ungrounded) == 0, ungrounded)


# ── System prompt ─────────────────────────────────────────────────────────────

SYSTEM_PROMPT = """You are an analytics assistant that writes personalized e-commerce performance digests for D2C brand owners.

STRICT RULES — violation of any rule makes the report worthless:

1. NUMBERS: Every claim must reference a specific number from the FACTS block. Never invent, round differently, or omit the source number. Do not introduce numbers that are not in the facts block.

2. CAUSATION: You do not have enough data to prove causation. Use only hedged language:
   - Allowed: "may indicate", "worth investigating whether", "one possible driver", "this coincided with"
   - Never use: "because", "caused by", "due to", "resulted in"

3. ATTRIBUTION: Ad platform spend and Shopify revenue are weakly correlated (r~0.12). Never say ad spend drove revenue. Say they moved together if they did, or note they diverged if they did not.

4. DATA FLAGS: If a fact contains [NOTE: possible incomplete data], mention the caveat explicitly. Do not present it as a confirmed movement.

5. CAPPED VALUES: If a fact contains [capped], note that the true % change was higher but may reflect a near-zero baseline rather than real performance, and avoid treating it as a business insight.

6. TONE: Direct, specific, no filler. No phrases like "It is important to note", "As we can see", "In conclusion". Two short sentences max per bullet. Write like a sharp analyst, not a report template.

7. HOLDING-STEADY SECTION: Only reference metrics listed in the STEADY FACTS block. Do not invent metrics that are "stable".

8. FORMAT: Follow the output format exactly. Use the exact section headers provided."""


# ── Prompt builders ───────────────────────────────────────────────────────────

def build_daily_prompt(findings: list, steady: list,
                       profile: CustomerProfile, date: str) -> Tuple[str, str]:
    """Returns (full_user_prompt, facts_block_for_validation)."""
    facts_block  = _format_facts_block(findings)
    steady_block = _format_steady_block(steady)
    user = f"""DATE: {date}
CUSTOMER: {profile.name} — {profile.brand_stage} stage brand
PRIORITIES: {", ".join(profile.primary_metrics)}
KNOWN CONCERNS: {", ".join(profile.known_concerns) if profile.known_concerns else "none"}
CONTEXT: {profile.context_note}

FACTS (ranked by importance, all numbers verified from source data):
{facts_block}

STEADY FACTS (metrics within normal range — use ONLY these for "What's holding steady"):
{steady_block}

Write a Daily Digest using EXACTLY this format:

# Daily Digest — {date}

## Headline
One sentence. The single most important thing that happened today. Must include a number.

## What needs attention
2-4 bullet points. Each must contain at least one number from the FACTS block. Flag alerts (marked ***) first.
Focus on facts relevant to this customer's priorities: {", ".join(profile.primary_metrics)}

## What's holding steady
1-2 bullet points. Reference ONLY metrics listed in the STEADY FACTS block above. Do not invent stability claims.

## One question to investigate today
A single specific, actionable question this brand owner should answer today.
Must reference a specific metric or channel from the FACTS block."""
    # facts_block + steady_block is the universe of numbers the LLM may reference
    return user, facts_block + "\n" + steady_block


def build_weekly_prompt(findings: list, steady: list, profile: CustomerProfile,
                        week_end: str, daily_summaries: list) -> Tuple[str, str]:
    facts_block  = _format_facts_block(findings)
    steady_block = _format_steady_block(steady)
    daily_block  = ("\n".join(f"- {s}" for s in daily_summaries)
                    if daily_summaries else "- No daily summaries available")

    user = f"""WEEK ENDING: {week_end}
CUSTOMER: {profile.name} — {profile.brand_stage} stage brand
PRIORITIES: {", ".join(profile.primary_metrics)}
KNOWN CONCERNS: {", ".join(profile.known_concerns) if profile.known_concerns else "none"}
CONTEXT: {profile.context_note}

DAILY HEADLINES THIS WEEK:
{daily_block}

TOP WEEKLY FACTS (ranked by importance, sustained signals boosted):
{facts_block}

STEADY FACTS (metrics within normal range this week — use ONLY these for "Bright spots" stability claims):
{steady_block}

Write a Weekly Report using EXACTLY this format:

# Weekly Report — Week ending {week_end}

## The week in one paragraph
3-4 sentences. Narrative arc: what was the dominant theme? Did performance improve, decline, or shift in character?
Must reference at least 3 numbers from the FACTS block. No causation claims.

## Sustained trends
Movements that appeared on multiple days this week. Each bullet: metric, direction, magnitude, days observed.
Skip one-off spikes.

## Bright spots
1-3 bullets. Genuine positives — metrics above baseline or stable per STEADY FACTS.
Only include if the data supports it. Do not manufacture positivity.

## Watch list for next week
2-3 bullets. Metrics that warrant monitoring. Include the specific threshold or condition to watch for.

## Recommended action
One concrete, specific action this brand owner should take on Monday morning.
Must name a specific channel, campaign, or metric from the FACTS block. Not generic advice."""
    return user, facts_block + "\n" + steady_block + "\n" + daily_block


def _format_facts_block(findings: list) -> str:
    lines = []
    for i, f in enumerate(findings, 1):
        alert = "*** ALERT" if f.get("is_alert") else "   "
        dq    = " [DATA QUALITY — treat with caution]" if f.get("is_data_quality_flag") else ""
        boost = " [priority metric]" if f.get("_profile_boosted") else ""
        lines.append(f"{i:2}. {alert} {f['fact_sentence']}{dq}{boost}")
    return "\n".join(lines) if lines else "(no findings)"


def _format_steady_block(steady: list) -> str:
    if not steady:
        return "(none surfaced)"
    lines = []
    for f in steady:
        # Reuse the fact_sentence so numbers in steady are in the same vocabulary
        lines.append(f" - {f['fact_sentence']}")
    return "\n".join(lines)


# ── Template fallback ─────────────────────────────────────────────────────────

def render_template_fallback(findings: list, steady: list,
                             profile: CustomerProfile, date: str,
                             report_type: str = "daily") -> str:
    """
    Deterministic markdown rendering of the facts when the LLM call fails
    or output fails numeric grounding. Ugly but always correct.
    """
    title = ("Daily Digest" if report_type == "daily" else "Weekly Report")
    header = f"# {title} — {date}\n\n*Generated via template fallback — LLM call unavailable or grounding failed.*\n"
    parts = [header, f"\n**Customer:** {profile.name} ({profile.brand_stage})\n",
             "\n## What needs attention\n"]
    for f in findings:
        flag = " 🚨" if f.get("is_alert") else ""
        parts.append(f"- {f['fact_sentence']}{flag}\n")
    if steady:
        parts.append("\n## What's holding steady\n")
        for f in steady:
            parts.append(f"- {f['fact_sentence']}\n")
    return "".join(parts)


# ── Report generator ──────────────────────────────────────────────────────────

class ReportGenerator:

    def __init__(self):
        api_key = os.getenv("ANTHROPIC_API_KEY", "")
        if not api_key:
            raise ValueError("ANTHROPIC_API_KEY not set — check your .env file")
        self.client = anthropic.Anthropic(api_key=api_key)
        log.info("ReportGenerator ready — model=%s", MODEL)

    def _call_llm(self, user_prompt: str, max_tokens: int) -> str:
        """
        Single API call with prompt caching on the shared system prompt
        and exponential-backoff retry on transient failures.
        """
        last_err = None
        for attempt in range(API_RETRIES):
            try:
                t0 = time.time()
                response = self.client.messages.create(
                    model      = MODEL,
                    max_tokens = max_tokens,
                    # System as a list with cache_control means the system prompt
                    # is cached server-side and only billed at full rate once per
                    # ~5 min window. Big cost win for batch report generation.
                    system     = [{
                        "type": "text",
                        "text": SYSTEM_PROMPT,
                        "cache_control": {"type": "ephemeral"},
                    }],
                    messages   = [{"role": "user", "content": user_prompt}],
                )
                elapsed = time.time() - t0
                usage = response.usage
                cache_read = getattr(usage, "cache_read_input_tokens", 0) or 0
                cache_write = getattr(usage, "cache_creation_input_tokens", 0) or 0
                log.info(
                    "LLM call ok: latency=%.2fs in=%d out=%d cache_read=%d cache_write=%d",
                    elapsed, usage.input_tokens, usage.output_tokens,
                    cache_read, cache_write,
                )
                return response.content[0].text
            except (anthropic.APIError, anthropic.APIConnectionError) as e:
                last_err = e
                wait = API_BACKOFF_S * (2 ** attempt)
                log.warning("LLM call attempt %d failed (%s), retry in %.0fs",
                            attempt + 1, type(e).__name__, wait)
                time.sleep(wait)
        raise RuntimeError(f"LLM call failed after {API_RETRIES} attempts: {last_err}")

    def generate_daily(self, findings: list, profile: CustomerProfile,
                       date: str, steady: Optional[list] = None) -> str:
        """Generate a daily digest. `findings` may be the full ranked list;
        re-ranking and top-N slicing happen here."""
        steady = steady or []
        reranked = apply_profile_reranking(findings, profile)
        top      = reranked[:DAILY_TOP_N]
        steady   = steady[:STEADY_N]

        log.info("generate_daily: customer=%s date=%s n_facts=%d n_steady=%d",
                 profile.name, date, len(top), len(steady))

        prompt, grounding_text = build_daily_prompt(top, steady, profile, date)
        try:
            output = self._call_llm(prompt, MAX_TOKENS)
        except RuntimeError as e:
            log.error("Falling back to template: %s", e)
            return render_template_fallback(top, steady, profile, date, "daily")

        ok, ungrounded = validate_numeric_grounding(output, grounding_text)
        if not ok:
            log.warning("Numeric grounding FAILED — ungrounded numbers: %s. "
                        "Falling back to template.", ungrounded)
            return render_template_fallback(top, steady, profile, date, "daily")
        log.info("Numeric grounding passed.")
        return output

    def generate_weekly(self, findings: list, profile: CustomerProfile,
                        week_end: str, daily_summaries: Optional[list] = None,
                        steady: Optional[list] = None) -> str:
        steady = steady or []
        reranked = apply_profile_reranking(findings, profile)
        top      = reranked[:WEEKLY_TOP_N]
        steady   = steady[:STEADY_N]

        log.info("generate_weekly: customer=%s week_end=%s n_facts=%d",
                 profile.name, week_end, len(top))

        prompt, grounding_text = build_weekly_prompt(
            top, steady, profile, week_end, daily_summaries or []
        )
        try:
            output = self._call_llm(prompt, MAX_TOKENS_W)
        except RuntimeError as e:
            log.error("Falling back to template: %s", e)
            return render_template_fallback(top, steady, profile, week_end, "weekly")

        ok, ungrounded = validate_numeric_grounding(output, grounding_text)
        if not ok:
            log.warning("Numeric grounding FAILED — ungrounded: %s. Falling back.",
                        ungrounded)
            return render_template_fallback(top, steady, profile, week_end, "weekly")
        log.info("Numeric grounding passed.")
        return output

    def save_report(self, content: str, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(content)
        log.info("Saved report: %s", path)
