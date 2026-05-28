"""Tests for the defense-relevance gate.

The test cases here are drawn directly from the noise patterns
observed in report_2026-05-26.pdf — keep them in sync with the
real failures we're trying to prevent.
"""

from __future__ import annotations

from extract.defense_relevance import is_defense_relevant, score_event


# ---- Should PASS ----

PASSES = [
    # Real defense engagements — host allowlist
    {"name": "xTechDisrupt",
     "host": "U.S. Army FUZE xTech Program",
     "source_url": "https://xtech.army.mil/competition/xtechdisrupt/"},
    {"name": "Proposers Day: DICE",
     "host": "DARPA",
     "source_url": "https://www.darpa.mil/events/2026/dice-proposers-day"},
    {"name": "STO Industry Day",
     "host": "DARPA",
     "source_url": "https://www.darpa.mil/events/2026/sto-industry-day"},
    # Discovered events with defense vocab in title
    {"name": "National Security Hackathon (by Army xTech)",
     "host": "discovered:cerebralvalley.ai",
     "source_url": "https://cerebralvalley.ai/e/3rd-annual-natsec-hackathon"},
    {"name": "Four Companies Selected To Provide Long-Range, One-Way Unmanned Platforms Prototypes for Evaluation",
     "host": "discovered:www.diu.mil",
     "source_url": "https://www.diu.mil/latest/four-companies-selected"},
    {"name": "Hackathon at Indo-Pacific Command's new AI battle lab open to all US citizens",
     "host": "discovered:defensescoop.com",
     "source_url": "https://defensescoop.com/2023/12/07/hackathon-at-indo-pacific-commands-new-ai-battle-lab"},
    {"name": "Air Force hackathon puts real data on open source code",
     "host": "discovered:defensescoop.com",
     "source_url": "https://defensescoop.com/2022/02/10/air-force-hackathon"},
]


# ---- Should DROP ----

DROPS = [
    # Commercial / dev hackathons
    {"name": "Hospitality 2030: A Rosewood Sand Hill Hackathon",
     "host": "discovered:cerebralvalley.ai",
     "source_url": "https://cerebralvalley.ai/e/rosewood-hospitality-2030"},
    {"name": "AI Fintech Hackathon",
     "host": "discovered:cerebralvalley.ai",
     "source_url": "https://cerebralvalley.ai/e/ai-finance-hackathon"},
    {"name": "Health & Benefits 2025 AI Hackathon",
     "host": "discovered:medium.com",
     "source_url": "https://medium.com/@wextechblogs/health-benefits-2025-ai-hackathon-a8819b314947"},
    {"name": "Built with Opus 4.6: a Claude Code hackathon",
     "host": "discovered:cerebralvalley.ai",
     "source_url": "https://cerebralvalley.ai/e/claude-code-hackathon"},
    {"name": "Google I/O Hackathon",
     "host": "discovered:cerebralvalley.ai",
     "source_url": "https://cerebralvalley.ai/e/google-io-hackathon"},
    {"name": "Gemini 3 NYC Hackathon",
     "host": "discovered:cerebralvalley.ai",
     "source_url": "https://cerebralvalley.ai/e/gemini-3-nyc-hackathon"},
    {"name": "Llama 4 Hackathon Seattle",
     "host": "discovered:cerebralvalley.ai",
     "source_url": "https://cerebralvalley.ai/e/llama-4-hackathon-seattle"},
    {"name": "OpenAI Codex Hackathon",
     "host": "discovered:cerebralvalley.ai",
     "source_url": "https://cerebralvalley.ai/e/openai-codex-hackathon"},
    {"name": "Mistral AI MCP Hackathon",
     "host": "discovered:cerebralvalley.ai",
     "source_url": "https://cerebralvalley.ai/e/mistral-mcp-hackathon"},
    {"name": "GPT-5 Startup Hackathon NYC",
     "host": "discovered:cerebralvalley.ai",
     "source_url": "https://cerebralvalley.ai/e/gpt5-nyc"},
    {"name": "W25 Demo Day After Party",
     "host": "discovered:cerebralvalley.ai",
     "source_url": "https://cerebralvalley.ai/e/w25-demo-day-after-party"},
    # Medium explainer / personal
    {"name": "What's a \"hackathon\", exactly?",
     "host": "discovered:medium.com",
     "source_url": "https://medium.com/lahacks/whats-a-hackathon-exactly"},
    {"name": "My First Hackathon — Hack This Fall",
     "host": "discovered:medium.com",
     "source_url": "https://medium.com/@manik23265/my-first-hackathon"},
    {"name": "We entered our first team hackathon - Introducing Ecoswap",
     "host": "discovered:medium.com",
     "source_url": "https://medium.com/multiverse-tech/we-entered-our-first-team-hackathon-introducing-ecoswap"},
    {"name": "Unveiling Tech Alchemy's Inaugural Tech Alchathon 2024",
     "host": "discovered:medium.com",
     "source_url": "https://medium.com/tech-alchemy/unveiling-tech-alchemys-inaugural-tech-alchathon-2024"},
    {"name": "Siemens MakeIT Real Hackathon 2017",
     "host": "discovered:medium.com",
     "source_url": "https://medium.com/@krishit/experience-siemens-makeit-real-hackathon-2017"},
    {"name": "MachineHack Airline Price Hackathon",
     "host": "discovered:medium.com",
     "source_url": "https://medium.com/@jaswinder9051998/machinehack-airline-price-hackathon"},
    # Crypto / DeFi
    {"name": "XDC GDCE DeFi Hackathon: A Smashing Success",
     "host": "discovered:medium.com",
     "source_url": "https://medium.com/@xdcnetworknews/xdc-gdce-defi-hackathon"},
    # SOFWERX outreach (these come from a real defense host but
    # aren't engagements)
    {"name": "SOFWERX STEM Showcase 2024",
     "host": "discovered:events.sofwerx.org",
     "source_url": "https://events.sofwerx.org/sofwerx-stem-showcase-2024"},
    {"name": "SOFWERX at Wharton High School Career Fair",
     "host": "discovered:events.sofwerx.org",
     "source_url": "https://events.sofwerx.org/discover/wharton-high-school-career-fair"},
    {"name": "SOFWERX Welding Workshop",
     "host": "discovered:events.sofwerx.org",
     "source_url": "https://events.sofwerx.org/discover/sofwerx-introduction-to-welding-workshop"},
    {"name": "SOFWERX Women Ambassadors Forum",
     "host": "discovered:events.sofwerx.org",
     "source_url": "https://events.sofwerx.org/discover/women-ambassadors-forum"},
    {"name": "Spring 2025 Florida College Career Fair",
     "host": "discovered:events.sofwerx.org",
     "source_url": "https://events.sofwerx.org/discover/spring-2025-florida-college-career-fair"},
]


def test_defense_relevance_passes():
    """Every PASSES entry must clear the gate."""
    failures = []
    for ev in PASSES:
        result = score_event(ev)
        if not result.passes:
            failures.append((ev["name"], result.score, result.reasons))
    assert not failures, (
        "Expected PASS but gated:\n" +
        "\n".join(f"  {n!r} score={s} reasons={r}" for n, s, r in failures)
    )


def test_defense_relevance_drops():
    """Every DROPS entry must fail the gate."""
    leaks = []
    for ev in DROPS:
        result = score_event(ev)
        if result.passes:
            leaks.append((ev["name"], result.score, result.reasons))
    assert not leaks, (
        "Expected DROP but admitted:\n" +
        "\n".join(f"  {n!r} score={s} reasons={r}" for n, s, r in leaks)
    )


def test_is_defense_relevant_matches_score_event():
    """The bool wrapper must agree with the full result."""
    for ev in PASSES + DROPS:
        result = score_event(ev)
        assert is_defense_relevant(ev) == result.passes


if __name__ == "__main__":
    # When run directly, print a summary instead of a pytest dump.
    print(f"PASSES (expect all PASS): {len(PASSES)}")
    pass_fails = 0
    for ev in PASSES:
        r = score_event(ev)
        verdict = "PASS" if r.passes else "FAIL"
        if not r.passes:
            pass_fails += 1
        print(f"  {verdict:4} {r.score:+.2f}  {ev['name'][:70]}")
    print()
    print(f"DROPS (expect all DROP): {len(DROPS)}")
    drop_leaks = 0
    for ev in DROPS:
        r = score_event(ev)
        verdict = "DROP" if not r.passes else "LEAK"
        if r.passes:
            drop_leaks += 1
        print(f"  {verdict:4} {r.score:+.2f}  {ev['name'][:70]}")
    print()
    print(f"Summary: PASS fails={pass_fails}, DROP leaks={drop_leaks}")
    raise SystemExit(1 if (pass_fails + drop_leaks) else 0)
