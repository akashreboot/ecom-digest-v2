"""
report_generator.py — LLM-powered report generator
Uses ranked findings from ranker.py as grounded facts.
The LLM explains; it never selects or invents numbers.
"""

import os
import json
import anthropic
from pathlib import Path
from dotenv import load_dotenv
from dataclasses import dataclass
from typing import Optional

# Load API key
load_dotenv(Path(__file__).parent.parent / ".env")

MODEL        = "claude-sonnet-4-6"
MAX_TOKENS   = 1500  # daily digest
MAX_TOKENS_W = 2500  # weekly report (longer narrative)


# ── Customer profile ──────────────────────────────────────────────────────────

@dataclass
class CustomerProfile:
    """
    Represents what we know about a customer.
    Cold start: only brand_stage and name.
    Warm/mature: primary_metrics and known_concerns populated from
    engagement signals (clicks, follow-up questions, ignored digests).
    """
    name:             str
    brand_stage:      str   # "early", "growth", "scale"
    primary_metrics:  list  # metrics this customer cares most about
    known_concerns:   list  # e.g. ["declining ROAS", "new customer acquisition"]
    context_note:     str   # free-text note about the business


# ── Two sample customer profiles ─────────────────────────────────────────────
# Profile A: growth-stage brand, focused on acquisition and efficiency
PROFILE_GROWTH = CustomerProfile(
    name            = "Priya (Growth Brand)",
    brand_stage     = "growth",
    primary_metrics = ["revenue", "roas", "new_customer_share", "spend"],
    known_concerns  = ["ad spend efficiency", "new customer acquisition cost"],
    context_note    = (
        "D2C fashion brand, 18 months old, scaling paid acquisition. "
        "Primary concern is keeping ROAS above 3x while growing new customer base. "
        "Checks digest every morning before approving ad budgets."
    ),
)

# Profile B: scale-stage brand, focused on retention and profitability
PROFILE_SCALE = CustomerProfile(
    name            = "Rahul (Scale Brand)",
    brand_stage     = "scale",
    primary_metrics = ["revenue", "aov", "returning_orders", "new_customer_share"],
    known_concerns  = ["returning customer retention", "AOV growth"],
    context_note    = (
        "Established D2C home goods brand, 4 years old, optimising for profitability. "
        "Less focused on top-of-funnel spend, more on repeat purchase rate and AOV. "
        "Reads weekly report on Mondays to plan the week."
    ),
)


# ── System prompt ─────────────────────────────────────────────────────────────
# This is the most important part of the LLM layer.
# Every rule here prevents a specific failure mode.

SYSTEM_PROMPT = """You are an analytics assistant that writes personalized e-commerce performance digests for D2C brand owners.

STRICT RULES — violation of any rule makes the report worthless:

1. NUMBERS: Every claim must reference a specific number from the FACTS block. Never invent, round differently, or omit the source number.

2. CAUSATION: You do not have enough data to prove causation. Use only hedged language:
   - Allowed: "may indicate", "worth investigating whether", "one possible driver", "this coincided with"
   - Never use: "because", "caused by", "due to", "resulted in"

3. ATTRIBUTION: Ad platform spend and Shopify revenue are weakly correlated (r~0.12). Never say ad spend drove revenue. Say they moved together if they did, or note they diverged if they did not.

4. DATA FLAGS: If a fact contains [NOTE: possible incomplete data], mention the caveat explicitly. Do not present it as a confirmed movement.

5. CAPPED VALUES: If a fact contains [capped], note that the true % change was higher but may reflect a near-zero baseline rather than real performance, and avoid treating it as a business insight.

6. TONE: Direct, specific, no filler. No phrases like "It is important to note", "As we can see", "In conclusion". Write like a sharp analyst, not a report template.

7. FORMAT: Follow the output format exactly. Use the exact section headers provided."""


# ── Prompt builders ───────────────────────────────────────────────────────────

def build_daily_prompt(findings: list, profile: CustomerProfile, date: str) -> str:
    facts_block = _format_facts_block(findings)
    return f"""DATE: {date}
CUSTOMER: {profile.name} — {profile.brand_stage} stage brand
PRIORITIES: {", ".join(profile.primary_metrics)}
KNOWN CONCERNS: {", ".join(profile.known_concerns) if profile.known_concerns else "none"}
CONTEXT: {profile.context_note}

FACTS (ranked by importance, all numbers verified from source data):
{facts_block}

Write a Daily Digest using EXACTLY this format:

# Daily Digest — {date}

## Headline
One sentence. The single most important thing that happened today. Must include a number.

## What needs attention
2-4 bullet points. Each must contain at least one number from the facts. Flag alerts (marked ***) first.
Focus on facts relevant to this customer's priorities: {", ".join(profile.primary_metrics)}

## What's holding steady
1-2 bullet points. Metrics that are within normal range — reassurance that not everything needs attention.

## One question to investigate today
A single specific, actionable question this brand owner should answer today.
Must reference a specific metric or channel from the facts."""


def build_weekly_prompt(findings: list, profile: CustomerProfile,
                        week_end: str, daily_summaries: list) -> str:
    facts_block = _format_facts_block(findings)
    daily_block = "\n".join(f"- {s}" for s in daily_summaries) if daily_summaries else "- No daily summaries available"

    return f"""WEEK ENDING: {week_end}
CUSTOMER: {profile.name} — {profile.brand_stage} stage brand
PRIORITIES: {", ".join(profile.primary_metrics)}
KNOWN CONCERNS: {", ".join(profile.known_concerns) if profile.known_concerns else "none"}
CONTEXT: {profile.context_note}

DAILY HEADLINES THIS WEEK:
{daily_block}

TOP WEEKLY FACTS (ranked by importance, sustained signals boosted):
{facts_block}

Write a Weekly Report using EXACTLY this format:

# Weekly Report — Week ending {week_end}

## The week in one paragraph
3-4 sentences. Narrative arc: what was the dominant theme? Did performance improve, decline, or shift in character?
Must reference at least 3 numbers. No causation claims.

## Sustained trends
Movements that appeared on multiple days this week. Each bullet: metric, direction, magnitude, days observed.
Skip one-off spikes.

## Bright spots
1-3 bullets. Genuine positives — metrics above baseline, improving trends.
Only include if the data supports it. Do not manufacture positivity.

## Watch list for next week
2-3 bullets. Metrics that warrant monitoring. Include the specific threshold or condition to watch for.

## Recommended action
One concrete, specific action this brand owner should take on Monday morning.
Must name a specific channel, campaign, or metric. Not generic advice."""


def _format_facts_block(findings: list) -> str:
    lines = []
    for i, f in enumerate(findings, 1):
        alert = "*** ALERT" if f.get("is_alert") else "   "
        dq    = " [DATA QUALITY — treat with caution]" if f.get("is_data_quality_flag") else ""
        lines.append(f"{i:2}. {alert} {f['fact_sentence']}{dq}")
    return "\n".join(lines)


# ── Report generator ──────────────────────────────────────────────────────────

class ReportGenerator:

    def __init__(self):
        api_key = os.getenv("ANTHROPIC_API_KEY", "")
        if not api_key:
            raise ValueError("ANTHROPIC_API_KEY not set — check your .env file")
        self.client = anthropic.Anthropic(api_key=api_key)
        print(f"ReportGenerator ready — model: {MODEL}")

    def generate_daily(self, findings: list, profile: CustomerProfile,
                       date: str) -> str:
        """Generate a daily digest markdown string."""
        prompt = build_daily_prompt(findings, profile, date)

        response = self.client.messages.create(
            model      = MODEL,
            max_tokens = MAX_TOKENS,
            system     = SYSTEM_PROMPT,
            messages   = [{"role": "user", "content": prompt}]
        )
        return response.content[0].text

    def generate_weekly(self, findings: list, profile: CustomerProfile,
                        week_end: str, daily_summaries: list = None) -> str:
        """Generate a weekly report markdown string."""
        prompt = build_weekly_prompt(
            findings, profile, week_end, daily_summaries or []
        )

        response = self.client.messages.create(
            model      = MODEL,
            max_tokens = MAX_TOKENS_W,
            system     = SYSTEM_PROMPT,
            messages   = [{"role": "user", "content": prompt}]
        )
        return response.content[0].text

    def save_report(self, content: str, path: Path) -> None:
        """Save a report to disk as markdown."""
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(content)
        print(f"Saved: {path}")
