"""GitHub source adapter.

Public repos are excellent participant evidence for defense
hackathons — the code submission itself is the artifact. Examples
the user surfaced for xTech National Security Hackathon:

    https://github.com/Kingkali69/Army_Xtech
    https://github.com/jeremyloseycesi/K9-Sentinel
    https://github.com/sachin-crispai/xtech-natsec-cv
    https://github.com/PoggyBobby/EMCON-Sentinel

This adapter is structured as an abstract backend (GitHubBackend)
with two concrete implementations, matching the pattern in
`sources/search_backend.py`:

  - GitHubAPIBackend       — live, hits api.github.com (requires
                             GITHUB_TOKEN for auth; 60 req/h
                             unauthenticated, 5000 req/h auth'd).
  - GitHubFixtureBackend   — reads tests/fixtures/github_repos.json,
                             no network.

Selection via env var GITHUB_BACKEND (default: fixture). Use the
fixture by default so dev/test runs don't burn API quota.

The public entry point is `fetch_participations(event_id, queries)`
which returns the number of Participation rows written. Unlike the
DIU/AFWERX/xTech adapters, this module does NOT auto-create events:
GitHub-discovered repos must attribute to an Event that already
exists in the store (typically created by sources.xtech). The
mapping from search query to event_id lives in
`config/github_events.yaml` (simple key:value text format —
event_id: list of queries — same hand-rolled YAML approach as
config/recap_sources.yaml).

Public API
----------
    fetch_events() -> list[Event]
        Runs fetch_participations() for every event_id in the config
        file. Returns the (existing) Events that received new
        participations. No new Events are created.

    fetch_participations(event_id, queries) -> int
        Lower-level: run `queries` against the configured backend,
        write Participation rows for each repo found, return count.
"""

from __future__ import annotations

import logging
import os
import re
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Optional
from urllib.parse import quote_plus

import httpx

from schema.event import Event
from schema.participation import Participation, make_participation_id
from store import cache as store

log = logging.getLogger(__name__)


# ---- Data class ----

@dataclass
class GitHubRepo:
    """One repository hit — the unit we turn into a Participation."""
    full_name: str          # "owner/repo"
    owner: str              # repo owner (user or org)
    name: str               # repo name
    description: str        # repo description (NER input)
    url: str                # html_url
    topics: list[str] = field(default_factory=list)
    owner_type: str = "User"   # "User" or "Organization"
    owner_company: Optional[str] = None  # owner.company field, if set
    owner_blog: Optional[str] = None     # owner.blog field, if set
    owner_html_url: Optional[str] = None # https://github.com/<owner>
    repo_homepage: Optional[str] = None  # repo.homepage field, if set
    created_at: Optional[date] = None
    pushed_at: Optional[date] = None


# ---- Abstract backend ----

class GitHubBackend(ABC):
    name: str = "base"

    @abstractmethod
    def search_repos(self, query: str, *, limit: int = 30) -> list[GitHubRepo]:
        """Return up to `limit` repo hits matching `query`."""
        raise NotImplementedError

    @abstractmethod
    def get_repo(self, full_name: str) -> Optional[GitHubRepo]:
        """Return a single repo by 'owner/repo', or None if missing."""
        raise NotImplementedError


# ---- Live backend ----

class GitHubAPIBackend(GitHubBackend):
    """Live GitHub REST API client.

    Docs: https://docs.github.com/en/rest

    Authentication is strongly recommended — unauthenticated calls
    are rate-limited to 60/hour, which exhausts in a single run.
    Set GITHUB_TOKEN to a fine-grained personal-access-token with
    `public_repo` read scope.
    """
    name = "api"
    BASE = "https://api.github.com"

    def __init__(self, token: Optional[str] = None, timeout: float = 30.0):
        self.token = token or os.getenv("GITHUB_TOKEN", "").strip()
        headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "defense-aggregator/1.0",
        }
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        else:
            log.warning(
                "[github] no GITHUB_TOKEN set — rate-limited to 60/h. "
                "Get a token at https://github.com/settings/tokens?type=beta"
            )
        self.client = httpx.Client(timeout=timeout, headers=headers)
        self._last_request_at = 0.0
        # Respect a polite 1 req/sec floor even if quota allows more
        self._min_interval = 1.0

    def _get(self, path: str, params: Optional[dict] = None) -> dict | None:
        elapsed = time.time() - self._last_request_at
        if elapsed < self._min_interval:
            time.sleep(self._min_interval - elapsed)
        url = f"{self.BASE}{path}"
        try:
            r = self.client.get(url, params=params)
            self._last_request_at = time.time()
            if r.status_code == 403 and "rate limit" in r.text.lower():
                log.warning("[github] rate-limited — back off and retry later")
                return None
            r.raise_for_status()
        except httpx.HTTPError as exc:
            log.warning("[github] GET %s failed: %s", url, exc)
            return None
        return r.json()

    def search_repos(self, query: str, *, limit: int = 30) -> list[GitHubRepo]:
        data = self._get("/search/repositories", params={
            "q": query, "per_page": min(limit, 100), "sort": "updated",
        })
        if not data:
            return []
        out: list[GitHubRepo] = []
        for item in (data.get("items") or [])[:limit]:
            owner = item.get("owner") or {}
            out.append(GitHubRepo(
                full_name=item.get("full_name", ""),
                owner=owner.get("login", ""),
                name=item.get("name", ""),
                description=item.get("description") or "",
                url=item.get("html_url", ""),
                topics=item.get("topics") or [],
                owner_type=owner.get("type", "User"),
                owner_html_url=owner.get("html_url"),
                repo_homepage=item.get("homepage") or None,
                created_at=_parse_iso(item.get("created_at")),
                pushed_at=_parse_iso(item.get("pushed_at")),
            ))
        return out

    def get_repo(self, full_name: str) -> Optional[GitHubRepo]:
        data = self._get(f"/repos/{full_name}")
        if not data:
            return None
        owner = data.get("owner") or {}
        # Owner profile may carry "company" + "blog" fields — useful
        # for resolving repo author to a real company name and
        # contact route.
        owner_company = None
        owner_blog = None
        if owner.get("login"):
            user_data = self._get(f"/users/{owner['login']}")
            if user_data:
                owner_company = (user_data.get("company") or "").lstrip("@") or None
                blog = (user_data.get("blog") or "").strip()
                if blog and not blog.startswith(("http://", "https://")):
                    blog = "https://" + blog
                owner_blog = blog or None
        return GitHubRepo(
            full_name=data.get("full_name", ""),
            owner=owner.get("login", ""),
            name=data.get("name", ""),
            description=data.get("description") or "",
            url=data.get("html_url", ""),
            topics=data.get("topics") or [],
            owner_type=owner.get("type", "User"),
            owner_company=owner_company,
            owner_blog=owner_blog,
            owner_html_url=owner.get("html_url"),
            repo_homepage=data.get("homepage") or None,
            created_at=_parse_iso(data.get("created_at")),
            pushed_at=_parse_iso(data.get("pushed_at")),
        )


# ---- Fixture backend ----

class GitHubFixtureBackend(GitHubBackend):
    """Reads from tests/fixtures/github_repos.json.

    Fixture format:
      {
        "queries": {
          "<normalized query>": ["owner/repo1", "owner/repo2", ...]
        },
        "repos": {
          "owner/repo": {
              "full_name": "owner/repo",
              "owner": "owner",
              "name": "repo",
              "description": "...",
              "url": "https://github.com/owner/repo",
              "topics": ["xtech-hackathon"],
              "owner_type": "User",
              "owner_company": null,
              "created_at": "2026-05-26",
              "pushed_at": "2026-05-27"
          }
        }
      }
    """
    name = "fixture"
    DEFAULT_PATH = (
        Path(__file__).parent.parent / "tests" / "fixtures" / "github_repos.json"
    )

    def __init__(self, path: Path | None = None):
        import json
        path = path or self.DEFAULT_PATH
        self._data: dict = {"queries": {}, "repos": {}}
        if path.exists():
            with open(path, "r", encoding="utf-8") as f:
                self._data = json.load(f)
        else:
            log.warning("[github] fixture not found at %s", path)

    def _hydrate(self, full_name: str) -> Optional[GitHubRepo]:
        d = self._data.get("repos", {}).get(full_name)
        if not d:
            return None
        return GitHubRepo(
            full_name=d.get("full_name", full_name),
            owner=d.get("owner", ""),
            name=d.get("name", ""),
            description=d.get("description") or "",
            url=d.get("url", f"https://github.com/{full_name}"),
            topics=d.get("topics") or [],
            owner_type=d.get("owner_type", "User"),
            owner_company=d.get("owner_company"),
            owner_blog=d.get("owner_blog"),
            owner_html_url=d.get(
                "owner_html_url",
                f"https://github.com/{d.get('owner', '')}",
            ),
            repo_homepage=d.get("repo_homepage"),
            created_at=_parse_iso(d.get("created_at")),
            pushed_at=_parse_iso(d.get("pushed_at")),
        )

    def search_repos(self, query: str, *, limit: int = 30) -> list[GitHubRepo]:
        key = " ".join(query.lower().split())
        full_names = (self._data.get("queries") or {}).get(key, [])
        out = [self._hydrate(n) for n in full_names[:limit]]
        return [r for r in out if r is not None]

    def get_repo(self, full_name: str) -> Optional[GitHubRepo]:
        return self._hydrate(full_name)


# ---- Factory ----

def get_backend() -> GitHubBackend:
    name = os.getenv("GITHUB_BACKEND", "fixture").strip().lower()
    if name == "api":
        return GitHubAPIBackend()
    if name == "fixture":
        return GitHubFixtureBackend()
    raise ValueError(f"unknown GITHUB_BACKEND: {name}")


# ---- Public API ----

CONFIG_PATH = Path(__file__).parent.parent / "config" / "github_events.yaml"


def _load_event_queries(path: Path = CONFIG_PATH) -> dict[str, list[str]]:
    """Parse the event-id → list-of-queries config.

    Format (hand-rolled YAML, same approach as recap_sources.yaml):

        events:
          <event_id_1>:
            - query: xtech natsec hackathon
            - query: army hackathon
            - topic: xtech-hackathon
          <event_id_2>:
            - query: ...

    Each entry is either {query: <text>} or {topic: <topic>}. Topic
    entries become `topic:<topic>` GitHub search syntax.
    """
    if not path.exists():
        log.warning("[github] config not found at %s", path)
        return {}

    out: dict[str, list[str]] = {}
    current_event: str | None = None
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.rstrip()
        if not line or line.lstrip().startswith("#"):
            continue
        # Top-level key
        if not line.startswith(" "):
            if line.strip() == "events:":
                continue
            continue
        # Event id (4-space indent)
        if line.startswith("  ") and not line.startswith("    "):
            current_event = line.strip().rstrip(":")
            out.setdefault(current_event, [])
            continue
        # Query / topic entry (6+ space indent)
        if current_event is None:
            continue
        s = line.lstrip()
        if s.startswith("- query:"):
            out[current_event].append(s.split(":", 1)[1].strip().strip('"\''))
        elif s.startswith("- topic:"):
            topic = s.split(":", 1)[1].strip().strip('"\'')
            out[current_event].append(f"topic:{topic}")
    return out


# Heuristic patterns for inferring a usable company / project name
# from a repo when the owner is a personal user account. README NER
# would be ideal but adds a heavy dep; for now we use the repo name
# (CamelCase-or-hyphen split → display name) when nothing else fits.
_CAMEL_RE = re.compile(r"(?<=[a-z])(?=[A-Z])")


def _project_display_name(repo: GitHubRepo) -> str:
    """Return the best-available human-readable project name for a repo.

    Priority:
      1. owner.company (set by org / user in profile) — strongest.
      2. owner login if owner is an Organization (likely a real org).
      3. Repo name with simple unCamelCase + hyphen → space.
    """
    if repo.owner_company and len(repo.owner_company) >= 2:
        return repo.owner_company
    if repo.owner_type == "Organization" and repo.owner:
        return repo.owner
    name = repo.name.replace("_", "-")
    name = _CAMEL_RE.sub(" ", name)
    return name.replace("-", " ").strip()


# Generic / non-company strings the project-name fallback should
# never resolve to. If a repo's derived display name normalizes to
# one of these (or to the event name itself), skip it — the repo
# is real evidence but doesn't identify a distinct participant.
_GENERIC_PROJECT_NAMES = frozenset({
    "hackathon", "submission", "project", "demo", "test",
    "untitled", "main", "code", "app",
})


def _normalize_for_match(s: str) -> str:
    """Lowercase, strip punctuation, collapse whitespace."""
    out = re.sub(r"[^a-z0-9]+", " ", (s or "").lower())
    return " ".join(out.split()).strip()


def _is_event_self_reference(display_name: str, event: dict) -> bool:
    """True if `display_name` is the event's own name (e.g., the
    Army_Xtech repo whose name == the competition name itself) or
    one of its aliases. Such repos should not become participants.
    """
    norm = _normalize_for_match(display_name)
    if not norm or norm in _GENERIC_PROJECT_NAMES:
        return True
    candidates = [event.get("name") or ""]
    aliases = event.get("aliases") or []
    if isinstance(aliases, str):
        try:
            import json
            aliases = json.loads(aliases)
        except (ValueError, TypeError):
            aliases = []
    candidates.extend(aliases)
    # Also include common short forms of the host so e.g. "xtech"
    # matches "xTech National Security Hackathon".
    host = event.get("host") or ""
    if host:
        candidates.append(host.split(":", 1)[-1])
    for cand in candidates:
        cand_norm = _normalize_for_match(cand)
        if not cand_norm:
            continue
        # Match if either is a substring of the other — handles
        # "Army Xtech" vs "xTech National Security Hackathon".
        if norm in cand_norm or cand_norm in norm:
            return True
        # Also match if all the words of display overlap with the
        # event-side string — handles word-order differences.
        norm_words = set(norm.split())
        cand_words = set(cand_norm.split())
        if norm_words and norm_words.issubset(cand_words):
            return True
    return False


def _confidence_for(repo: GitHubRepo, event_topics: list[str]) -> str:
    """Decide the Participation confidence tier for a repo hit.

    `event_topics` is the list of GitHub topic tags configured for
    this event in github_events.yaml. If the repo opts into one of
    them, that's a strong primary-source signal: confirmed.
    """
    repo_topics = {t.lower() for t in (repo.topics or [])}
    event_topic_set = {t.lower() for t in event_topics}
    if repo_topics & event_topic_set:
        return "confirmed"
    return "highly_likely"


def _resolve_website(repo: GitHubRepo) -> str:
    """Best contact route for a company derived from a repo.

    Priority:
      1. Owner profile `blog` field — analyst-set, most trustworthy
         (this is how crisp-ai.com would land if CrispAI's GitHub
         user/org page lists it).
      2. Repo `homepage` field — sometimes points at the project page.
      3. Owner profile URL (https://github.com/<owner>) — falls back
         to a clickable contact route for stealth / student / one-off
         repos that have no separate website.
      4. Empty string if nothing usable.
    """
    if repo.owner_blog:
        return repo.owner_blog
    if repo.repo_homepage:
        return repo.repo_homepage
    if repo.owner_html_url:
        return repo.owner_html_url
    return ""


def _backfill_company_contact(company_id: str, repo: GitHubRepo) -> None:
    """Populate website + GitHub-URL alias on a freshly-matched
    company so the report can render a contact route.

    Only updates fields that are empty — never overwrites real data.
    """
    row = store.find_company_by_id(company_id) if hasattr(
        store, "find_company_by_id"
    ) else None
    # find_company_by_id doesn't exist; fall back to scanning loads.
    if row is None:
        for c in store.load_companies():
            if c["id"] == company_id:
                row = c
                break
    if row is None:
        return
    changed = False
    if not (row.get("website") or "").strip():
        w = _resolve_website(repo)
        if w:
            row["website"] = w
            changed = True
    # Keep the owner GitHub URL in aliases too so the report's
    # _website_display() can fall back to it if website is later
    # cleared by enrichment.
    aliases = row.get("aliases") or []
    if isinstance(aliases, str):
        try:
            import json
            aliases = json.loads(aliases)
        except (ValueError, TypeError):
            aliases = []
    if repo.owner_html_url and repo.owner_html_url not in aliases:
        aliases.append(repo.owner_html_url)
        row["aliases"] = aliases
        changed = True
    if changed:
        store.upsert_company(row)


def fetch_participations(
    event_id: str,
    queries: list[str],
    *,
    backend: GitHubBackend | None = None,
) -> int:
    """Run `queries` against the backend, write Participations to the
    given `event_id`. Returns the count of Participations written.

    Caller is responsible for ensuring the Event exists in the store;
    this adapter does not create Events.
    """
    backend = backend or get_backend()
    log.info("[github] event=%s queries=%d backend=%s",
             event_id, len(queries), backend.name)

    # Topics extracted from queries (anything matching topic:<x>).
    topic_queries = [q for q in queries if q.lower().startswith("topic:")]
    event_topics = [q.split(":", 1)[1].strip() for q in topic_queries]

    # Make sure the event exists; bail if not.
    ev = store.load_event(event_id)
    if not ev:
        log.warning("[github] event_id not in store: %s", event_id)
        return 0

    # Deduplicate across queries
    seen: dict[str, GitHubRepo] = {}
    for q in queries:
        for repo in backend.search_repos(q, limit=50):
            if repo.full_name not in seen:
                seen[repo.full_name] = repo
    log.info("[github] %d unique repos for event %s", len(seen), event_id)

    now = datetime.utcnow()
    written = 0
    skipped_self_ref = 0
    for repo in seen.values():
        company_name = _project_display_name(repo)
        if not company_name:
            continue
        # Guard: repos named after the event itself (e.g. Army_Xtech
        # for the xTech National Security Hackathon) are real
        # evidence that someone participated, but the derived name
        # is the competition name, not the participant. Drop these
        # rather than create a misleading row. Analyst can recover
        # the participant by visiting the repo and reading the
        # README (workflow lives in the inline analyst playbook).
        if _is_event_self_reference(company_name, ev):
            log.info(
                "[github] skip self-referential repo: %s "
                "(derived name %r matches event %r)",
                repo.full_name, company_name, ev["name"],
            )
            skipped_self_ref += 1
            continue
        # Use the existing matcher to bind name → company_id. The
        # matcher respects the gazetteer, auto-merges near-exact
        # matches, queues fuzzy matches for review, and creates a
        # new (stealth) company for unrecognized names.
        from extract.company_match import match_or_queue
        result = match_or_queue(
            candidate_name=company_name,
            event_id=event_id,
            evidence_url=repo.url,
            evidence_excerpt=(repo.description or "")[:300],
        )
        if result.company is None:
            # Went to review queue — analyst will resolve later.
            continue
        # Backfill website + GitHub URL alias so the report has a
        # contact route even for stealth / student-run repos.
        _backfill_company_contact(result.company["id"], repo)
        role = "participant"  # GitHub submissions are participation evidence
        confidence = _confidence_for(repo, event_topics)
        p = Participation(
            id=make_participation_id(
                result.company["id"], event_id, role, repo.url
            ),
            company_id=result.company["id"],
            event_id=event_id,
            role=role,
            confidence=confidence,
            evidence_url=repo.url,
            evidence_excerpt=(repo.description or repo.full_name)[:500],
            extracted_by="github_adapter",
            extracted_at=now,
            notes=f"topics={','.join(repo.topics)}" if repo.topics else "",
        )
        store.upsert_participation(p.to_dict())
        written += 1
    log.info(
        "[github] %d participations written, %d self-ref repos "
        "skipped for event %s",
        written, skipped_self_ref, event_id,
    )
    return written


def fetch_events() -> list[Event]:
    """Run discovery for every event in github_events.yaml.

    Returns the list of (existing, store-resolved) Events that
    received new participations from this run. No new Events are
    created — GitHub is participant-evidence-only.
    """
    queries_by_event = _load_event_queries()
    if not queries_by_event:
        log.info("[github] no events configured; skipping")
        return []
    backend = get_backend()
    updated: list[Event] = []
    for event_id, queries in queries_by_event.items():
        if not queries:
            continue
        written = fetch_participations(event_id, queries, backend=backend)
        if written > 0:
            ev = store.load_event(event_id)
            if ev:
                updated.append(Event(
                    id=ev["id"], name=ev["name"], aliases=ev.get("aliases") or [],
                    host=ev.get("host", ""),
                    dates_start=date.fromisoformat(ev["dates_start"]),
                    dates_end=(
                        date.fromisoformat(ev["dates_end"])
                        if ev.get("dates_end") else None
                    ),
                    location=ev.get("location", ""),
                    source_url=ev.get("source_url", ""),
                ))
    return updated


# ---- Helpers ----

def _parse_iso(s: Optional[str]) -> Optional[date]:
    if not s:
        return None
    try:
        return date.fromisoformat(s[:10])
    except ValueError:
        return None


if __name__ == "__main__":
    logging.basicConfig(level="INFO", format="%(levelname)s %(message)s")
    print(f"Backend: {get_backend().name}")
    for e in fetch_events():
        print(f"  {e.name}")
