"""Unit tests for v2 extraction modules.

Covers:
  - schema.company.normalize_name + alias collapse
  - extract.ner.extract_mentions against gazetteer + heuristic
  - extract.confidence.assign_confidence routing logic
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from schema.company import normalize_name, make_company_id
from extract.ner import extract_mentions, reload_gazetteer
from extract.confidence import assign_confidence


class TestNormalizeName(unittest.TestCase):
    def test_strips_legal_suffix(self):
        self.assertEqual(normalize_name("Anduril Industries, Inc."), "anduril industries")

    def test_strips_punctuation(self):
        self.assertEqual(normalize_name("L3Harris Technologies"), "l3harris technologies")

    def test_collapses_whitespace(self):
        self.assertEqual(normalize_name("  Shield   Capital  "), "shield capital")

    def test_inc_variations_collapse(self):
        self.assertEqual(normalize_name("Saronic"),  normalize_name("Saronic, Inc."))
        self.assertEqual(normalize_name("Saronic"),  normalize_name("Saronic Inc"))

    def test_stable_id(self):
        self.assertEqual(make_company_id("Saronic"), make_company_id("Saronic Inc"))


class TestNERExtraction(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        reload_gazetteer()

    def test_gazetteer_match_simple(self):
        text = "OpenAI and Scale AI sponsored the event alongside Palantir."
        mentions = extract_mentions(text)
        canonicals = {m.canonical for m in mentions}
        self.assertIn("Palantir Technologies", canonicals)
        # Scale AI may be picked up as either Scale AI or via alias

    def test_gazetteer_alias_resolved_to_canonical(self):
        text = "Hadrian announced their automation platform at the event."
        mentions = extract_mentions(text)
        canonicals = {m.canonical for m in mentions}
        self.assertIn("Hadrian Automation", canonicals)

    def test_longer_variant_wins(self):
        # When both L3Harris and L3Harris Technologies are gazetteer
        # entries, the longer variant should take precedence at the
        # same position.
        text = "L3Harris Technologies delivered the radio."
        mentions = extract_mentions(text)
        canonicals = {m.canonical for m in mentions}
        self.assertIn("L3Harris Technologies", canonicals)

    def test_heuristic_catches_obvious_inc(self):
        text = "A stealth team called Project Ironhide Inc presented onsite."
        mentions = extract_mentions(text)
        # Heuristic should pick up "Project Ironhide Inc"
        self.assertTrue(
            any("Ironhide" in m.text for m in mentions),
            f"expected Ironhide in {[m.text for m in mentions]}",
        )

    def test_filters_out_obvious_non_companies(self):
        # The heuristic must NOT pick up "U.S. Army" or "Department of War"
        text = "U.S. Army FUZE xTech Program partnered with the Department of War."
        mentions = extract_mentions(text)
        # Anything matching should not be a service / department
        for m in mentions:
            self.assertNotIn("Army", m.text)


class TestArticleDiscovery(unittest.TestCase):
    """Cover the URL-shape filter, engagement-keyword filter, and
    title extraction helpers in sources.discover."""

    def test_url_shape_filter_rejects_author_pages(self):
        from sources.discover import _is_non_article_url
        self.assertTrue(_is_non_article_url(
            "https://insidedefense.com/authors/Dan-Schere"
        ))
        self.assertTrue(_is_non_article_url(
            "https://insidedefense.com/insider/insider-daily-digest-jan-28-2024"
        ))
        self.assertTrue(_is_non_article_url(
            "https://example.com/tag/defense/"
        ))
        self.assertTrue(_is_non_article_url(
            "https://example.com/category/news/"
        ))
        self.assertTrue(_is_non_article_url(
            "https://example.com/feed.xml"
        ))

    def test_url_shape_filter_keeps_real_article_urls(self):
        from sources.discover import _is_non_article_url
        self.assertFalse(_is_non_article_url(
            "https://defensescoop.com/2026/05/06/army-right-to-integrate-defense-industry-hackathon/"
        ))
        self.assertFalse(_is_non_article_url(
            "https://www.diu.mil/latest/finalists-selected-for-the-diu-blue-object-management-challenge"
        ))
        self.assertFalse(_is_non_article_url(
            "https://xtech.army.mil/competition/xtech-hackathon/"
        ))

    def test_extract_article_title_prefers_main_h1_over_chrome(self):
        from sources.discover import _extract_article_title
        html = (
            "<html><head><title>Real Event Title | SOFWERX</title>"
            "<meta property='og:title' content='Real Event Title'></head>"
            "<body><h1>contact us</h1>"
            "<main><h1>Should Not Win Because Outer h1 Tried First</h1></main>"
            "</body></html>"
        )
        # Chrome 'contact us' is blacklisted, so we fall through to og:title.
        title = _extract_article_title(html)
        self.assertEqual(title, "Should Not Win Because Outer h1 Tried First")

    def test_extract_article_title_falls_back_to_og_title_when_h1_is_chrome(self):
        from sources.discover import _extract_article_title
        html = (
            "<html><head>"
            "<meta property='og:title' content='USSOCOM Innovation Foundry IF16 Event'>"
            "<title>USSOCOM IF16 | SOFWERX</title>"
            "</head><body><h1>contact us</h1></body></html>"
        )
        title = _extract_article_title(html)
        self.assertEqual(title, "USSOCOM Innovation Foundry IF16 Event")

    def test_extract_article_title_strips_site_name_suffix(self):
        from sources.discover import _extract_article_title
        html = (
            "<html><head><title>DIU Blue Object Challenge | Defense Innovation Unit</title>"
            "</head><body></body></html>"
        )
        title = _extract_article_title(html)
        self.assertEqual(title, "DIU Blue Object Challenge")

    def test_engagement_keyword_matches_for_real_event_titles(self):
        from sources.discover import _ENGAGEMENT_KEYWORDS_RE
        # Positive cases
        for title in (
            "xTech National Security Hackathon",
            "DARPA STO Industry Day",
            "Proposers Day: DICE",
            "Finalists Selected for the DIU Blue Object Management Challenge",
            "Two Companies Selected to Support DIU Counter UAS",
            "Two Contracts Awarded To Modernize Decision-Making",
            "Spark Tank Competition",
            "DIU Sources Sought: Geothermal",
            "USSOCOM Innovation Foundry Event",
            "AIM G-NOMES Assessment Event",
        ):
            self.assertIsNotNone(
                _ENGAGEMENT_KEYWORDS_RE.search(title),
                f"expected engagement-keyword match in: {title!r}",
            )

    def test_engagement_keyword_rejects_non_event_titles(self):
        from sources.discover import _ENGAGEMENT_KEYWORDS_RE
        for title in (
            "Defense Innovation Unit Announces New Director",
            "Cassowary VEX Mission Success",
            "Garry Haase Joins Board",
            "DIU's Blue UAS List to Transition to DCMA",
        ):
            self.assertIsNone(
                _ENGAGEMENT_KEYWORDS_RE.search(title),
                f"unexpected engagement-keyword match in: {title!r}",
            )


class TestTargetAudienceFilter(unittest.TestCase):
    """Cover the prime/integrator + sponsor-role filter that powers
    the outbound-focused report sections."""

    def test_excludes_known_primes_by_normalized_name(self):
        from reports.build_markdown import _is_excluded_company
        for norm in (
            "boeing", "lockheed martin", "northrop grumman",
            "rtx",  # 'RTX Corporation' normalizes to 'rtx'
            "bae systems", "leidos", "deloitte",
            "palantir technologies", "scale ai",
        ):
            self.assertTrue(
                _is_excluded_company({"normalized_name": norm}),
                f"{norm!r} should be excluded",
            )

    def test_keeps_real_outbound_target_companies(self):
        from reports.build_markdown import _is_excluded_company
        for norm in (
            "anduril industries", "shield ai", "mach industries",
            "saronic technologies", "vannevar labs",
            "hadrian automation", "skydio",
        ):
            self.assertFalse(
                _is_excluded_company({"normalized_name": norm}),
                f"{norm!r} should NOT be excluded",
            )

    def test_filter_drops_sponsor_judge_mentor_participations(self):
        from reports.build_markdown import _filter_data_for_target_audience
        # Synthetic data set: one company present only as a sponsor.
        data = {
            "events": [{"id": "e1", "name": "Test"}],
            "companies": [{"id": "c1", "name": "Co A", "normalized_name": "co a", "type": "startup"}],
            "participations": [
                {"company_id": "c1", "event_id": "e1", "role": "sponsor"},
            ],
            "by_company": {"c1": [{"company_id": "c1", "event_id": "e1", "role": "sponsor"}]},
            "by_event": {"e1": [{"company_id": "c1", "event_id": "e1", "role": "sponsor"}]},
            "company_by_id": {"c1": {"id": "c1", "name": "Co A", "normalized_name": "co a"}},
            "event_by_id": {"e1": {"id": "e1", "name": "Test"}},
            "review": [],
        }
        filt = _filter_data_for_target_audience(data)
        self.assertEqual(len(filt["companies"]), 0)
        self.assertEqual(len(filt["participations"]), 0)

    def test_filter_keeps_company_with_any_target_role(self):
        from reports.build_markdown import _filter_data_for_target_audience
        # Company is judge at one event but participant at another → keep.
        data = {
            "events": [
                {"id": "e1", "name": "Event 1"},
                {"id": "e2", "name": "Event 2"},
            ],
            "companies": [
                {"id": "c1", "name": "Co A", "normalized_name": "co a", "type": "startup"},
            ],
            "participations": [
                {"company_id": "c1", "event_id": "e1", "role": "judge"},
                {"company_id": "c1", "event_id": "e2", "role": "participant"},
            ],
            "by_company": {"c1": [
                {"company_id": "c1", "event_id": "e1", "role": "judge"},
                {"company_id": "c1", "event_id": "e2", "role": "participant"},
            ]},
            "by_event": {
                "e1": [{"company_id": "c1", "event_id": "e1", "role": "judge"}],
                "e2": [{"company_id": "c1", "event_id": "e2", "role": "participant"}],
            },
            "company_by_id": {"c1": {"id": "c1", "name": "Co A", "normalized_name": "co a"}},
            "event_by_id": {
                "e1": {"id": "e1", "name": "Event 1"},
                "e2": {"id": "e2", "name": "Event 2"},
            },
            "review": [],
        }
        filt = _filter_data_for_target_audience(data)
        self.assertEqual(len(filt["companies"]), 1)
        # Only the participant row survives — the judge row was filtered out.
        self.assertEqual(len(filt["participations"]), 1)
        self.assertEqual(filt["participations"][0]["role"], "participant")


class TestPerSourceQueryCap(unittest.TestCase):
    """The per-source query cap stops Brave usage from running away
    once a source has been queried N times in a single run.

    Uses a temporary SQLite store so test data doesn't leak into the
    user's actual events.sqlite. (Previous version polluted the
    production DB with a `Cap Test` event; the daily discovery loop
    then ran Brave queries against it on the next monthly run.)
    """

    def setUp(self):
        import tempfile
        from pathlib import Path
        from store import cache as store_mod
        self._tmp = tempfile.NamedTemporaryFile(
            suffix=".sqlite", delete=False
        )
        self._tmp.close()
        self._original_db = store_mod.DB_PATH
        store_mod.DB_PATH = Path(self._tmp.name)

    def tearDown(self):
        from pathlib import Path
        from store import cache as store_mod
        store_mod.DB_PATH = self._original_db
        Path(self._tmp.name).unlink(missing_ok=True)

    def test_cap_skips_further_queries_after_threshold(self):
        from collections import Counter
        from sources.discover import discover_for_event
        from sources.search_backend import SearchBackend
        from store import cache as store
        from schema.event import Event, make_event_id
        from datetime import date

        # Fake backend that records every call and never makes HTTP.
        class _RecordingBackend(SearchBackend):
            name = "recording"
            def __init__(self):
                self.calls: list[tuple[str, str | None]] = []
            def search(self, query, *, site=None, limit=10):
                self.calls.append((query, site))
                return []  # no hits -> no fetches/scraping

        backend = _RecordingBackend()

        # Seed a synthetic event with a single alias (-> two terms).
        e = Event(
            id=make_event_id("test_host", date(2026, 5, 1), "Cap Test"),
            name="Cap Test", aliases=["Cap"],
            host="test_host",
            dates_start=date(2026, 5, 1), dates_end=date(2026, 5, 2),
        )
        store.upsert_event(e.to_dict())

        # Shared counter; cap at 1 query per source. With 2 terms and
        # 6 sources we'd normally do 12 queries; cap-at-1 means
        # 6 (one per source).
        shared: dict[str, int] = {}
        summary = discover_for_event(
            e.id, backend=backend,
            queries_by_source=shared,
            max_queries_per_source=1,
        )
        per_source = Counter(c[1] or "<no-site>" for c in backend.calls)
        for cnt in per_source.values():
            self.assertLessEqual(cnt, 1, "no source should exceed the cap")
        self.assertEqual(summary["queries_run"], len(backend.calls))
        # Skipped count = (sources * terms) - queries_run when cap hits.
        # Sources = 6 (from recap_sources.yaml), terms = 2.
        self.assertEqual(
            summary["queries_run"] + summary["queries_skipped_capped"],
            6 * 2,
            f"every (source, term) combo should be either run or capped; "
            f"got run={summary['queries_run']} capped={summary['queries_skipped_capped']}",
        )


class TestXtechSlugMatching(unittest.TestCase):
    """Cover the normalized-substring slug matcher used to derive
    (competition, company) pairs from xTech participant URLs."""

    def test_normalize_slug_strips_hyphens(self):
        from sources.xtech import _norm_slug
        self.assertEqual(_norm_slug("xtech-edge-strike"), "xtechedgestrike")
        self.assertEqual(_norm_slug("xtechedgestrikeground"), "xtechedgestrikeground")
        # Both should collide on the canonical form
        self.assertTrue(_norm_slug("xtech-edge-strike-foo").startswith(
            _norm_slug("xtechedgestrike")
        ))

    def test_slug_to_company_name_preserves_acronyms(self):
        from sources.xtech import _slug_to_company_name
        self.assertEqual(_slug_to_company_name("adranos-inc"), "Adranos Inc")
        self.assertEqual(_slug_to_company_name("wildspark-technologies-llc"),
                         "Wildspark Technologies LLC")
        self.assertEqual(_slug_to_company_name("imsar-llc"), "Imsar LLC")


class TestConfidenceAssignment(unittest.TestCase):
    def test_official_domain_is_confirmed(self):
        c = assign_confidence(
            "https://xtech.army.mil/competition/xtech-hackathon/",
            "The Army FUZE xTech Program announced winners.",
        )
        self.assertEqual(c, "confirmed")

    def test_editorial_domain_is_highly_likely(self):
        c = assign_confidence(
            "https://defensescoop.com/2026/05/04/xtech-recap/",
            "Hadrian Automation took first place.",
            has_named_author=True,
        )
        self.assertEqual(c, "highly_likely")

    def test_first_person_post_is_highly_likely(self):
        c = assign_confidence(
            "https://maggiegray.us/p/2026-national-security-hackathon",
            "I am excited to announce that registration is now open.",
        )
        self.assertEqual(c, "highly_likely")

    def test_unknown_third_party_is_ecosystem(self):
        c = assign_confidence(
            "https://some-random-blog.com/saw-some-startups",
            "Lots of companies were there.",
        )
        self.assertEqual(c, "ecosystem_associated")

    def test_www_prefix_handled(self):
        c = assign_confidence(
            "https://www.defensescoop.com/article/",
            "Some text here.",
        )
        self.assertEqual(c, "highly_likely")


if __name__ == "__main__":
    unittest.main()
