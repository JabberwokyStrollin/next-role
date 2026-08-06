"""
geography.py — Single source of truth for location → country derivation and the
geography pre-filter gate.

Deliberately dependency-free (stdlib only — NO api keys, NO yaml, does NOT import
config) so that:
  - every Python consumer shares one implementation (config.py re-exports these,
    so `from config import derive_country` keeps working), and
  - the Node cover-letter generator can call it as a subprocess:
        python scripts/geography.py "<location string>"
    prints one of CA / IE / US / OTHER. (generate_cl.js uses this instead of a
    parallel JS copy that kept drifting — California, Galway, Toronto, ...)

`TARGET_COUNTRIES` lives here (it's a geography concern). `US_SPONSORSHIP_SCORE`
stays in config.py (it's a scoring concern).
"""

import re
import sys

# ─── Target geographies (the US toggle) ──────────────────────────────────────
#
# The single switch for which geographies the pipeline actively targets. The
# operator needs visa sponsorship for CA / IE but is a US citizen, so US roles
# need none — a reluctant remote-only stop-gap. Add/remove "US" to toggle; that
# flips three behaviors, each reading this set or ``derive_country``:
#   - pre-filter remote-only US gate          → ``location_passes`` (below)
#   - US sponsorship floor instead of company → ``config.composite_score``
#   - skip the no-sponsorship discard for US  → ``ingest.ingest_job``

TARGET_COUNTRIES: frozenset[str] = frozenset({"CA", "IE", "US"})

# ─── Country derivation ───────────────────────────────────────────────────────
#
# Detection tokens, matched against the lowercased location padded with a space
# on each side so word-tokens like " us " / " ie " don't false-positive inside
# "houston" / "erie". IE and CA are tested before US so a combined
# "Remote, Canada/US" posting resolves to the sponsorship-bearing country.
# Bare "us" is intentionally never a substring token.
#
# Canada is NOT detected by the bare "CA" code: it collides with the California
# state abbreviation ("San Francisco, CA"). Canada is reliably named ("canada")
# or carries a Canadian city / province code, so those are the signals; "CA"
# alone resolves to California (US) via the US state-code check below.
_IE_LOCATION_TOKENS: tuple[str, ...] = (
    "ireland", " ie,", " ie ", "(ie)", "dublin", "cork", "galway", "limerick",
)
_CA_LOCATION_TOKENS: tuple[str, ...] = (
    "canada", "toronto", "vancouver", "montreal",
    "ottawa", "calgary", "edmonton", "waterloo", "kitchener",
)
_US_LOCATION_TOKENS: tuple[str, ...] = (
    "united states", " usa", " us,", " us ", " us:", "(us)", "u.s.",
    "remote, us", "remote (us)", "us remote", "us-remote",
)

# Spelled-out US state names. Distinct from the two-letter codes below: ATS
# boards very often write the name with no country ("Florida", "Remote - New
# York", "Remote: New York City") and never a "City, XX" pair, so the anchored
# code check can't see them. Before this list existed only "california" was a
# token, so every other state fell through to OTHER — which then took the
# OTHER → CA cover-letter fallback in serve.py / run.py and stamped the Canadian
# work-permit paragraph onto US roles, besides skipping the US sponsorship floor
# in composite_score and the US no-sponsorship-discard exemption at ingest.
#
# Matched with (?<![a-z])name(?![a-z]) rather than as a bare substring so short
# names don't hit inside longer words ("maine" in "Germaine"). Two accepted
# trade-offs: "georgia" resolves the US state, not the country (US listings for
# Atlanta vastly outnumber Tbilisi ones in this pipeline); and "indiana" now
# resolves US before _FOREIGN_LOCATION_TOKENS' "india" substring can reject it,
# which is a latent bug this happens to fix.
_US_STATE_NAMES: frozenset[str] = frozenset({
    "alabama", "alaska", "arizona", "arkansas", "california", "colorado",
    "connecticut", "delaware", "florida", "georgia", "hawaii", "idaho",
    "illinois", "indiana", "iowa", "kansas", "kentucky", "louisiana", "maine",
    "maryland", "massachusetts", "michigan", "minnesota", "mississippi",
    "missouri", "montana", "nebraska", "nevada", "new hampshire", "new jersey",
    "new mexico", "new york", "north carolina", "north dakota", "ohio",
    "oklahoma", "oregon", "pennsylvania", "rhode island", "south carolina",
    "south dakota", "tennessee", "texas", "utah", "vermont", "virginia",
    "washington", "west virginia", "wisconsin", "wyoming",
    "district of columbia",
})
_US_STATE_NAME_RE = re.compile(
    r"(?<![a-z])("
    + "|".join(sorted((re.escape(s) for s in _US_STATE_NAMES), key=len, reverse=True))
    + r")(?![a-z])"
)

# Two-letter region codes, matched only in an anchored "City, XX" / "(XX)" form
# (see _has_region_code) so they don't false-positive on English words or full
# country names. CA provinces are checked as part of Canada detection (before
# US), so e.g. "London, ON" → CA, not OTHER. US states omit ``in``/``de``/``co``
# (India/Germany/Colombia country-code collisions appear in remote listings far
# more often than Indiana/Delaware/Colorado).
_CA_PROVINCE_CODES: frozenset[str] = frozenset({
    "on", "bc", "ab", "mb", "sk", "qc", "ns", "nb", "nl", "pe", "yt", "nt", "nu",
})
_US_STATE_CODES: frozenset[str] = frozenset({
    "al", "ak", "az", "ar", "ca", "ct", "fl", "ga", "hi", "id", "il", "ia",
    "ks", "ky", "la", "me", "md", "ma", "mi", "mn", "ms", "mo", "mt", "ne",
    "nv", "nh", "nj", "nm", "ny", "nc", "nd", "oh", "ok", "or", "pa", "ri",
    "sc", "sd", "tn", "tx", "ut", "vt", "va", "wa", "wv", "wi", "wy", "dc",
})


def _has_region_code(padded_loc: str, codes: frozenset[str]) -> bool:
    """True if ``padded_loc`` (already lowercased + space-padded) contains one of
    ``codes`` in an anchored "City, XX" / "City, XX," / "City, XX)" / "(XX)"
    form. The leading comma/paren and trailing boundary keep full country names
    ("..., india") and embedded letters from matching."""
    for code in codes:
        if (f", {code} " in padded_loc
                or f", {code}," in padded_loc
                or f", {code})" in padded_loc
                or f"({code})" in padded_loc):
            return True
    return False


def derive_country(location: str) -> str:
    """Map a free-text job/application location to ``"CA" | "IE" | "US" |
    "OTHER"``. SSOT — IE/CA are matched before US so a combined posting
    resolves to the sponsorship-bearing country. Bare "us" is never a substring
    token (it would match "houston"); region codes are matched only in an
    anchored "City, XX" form, but spelled-out US state names are matched on a
    word boundary anywhere ("Remote - New York"). "CA" resolves to California
    (US) — Canada is detected first by name / Canadian city / province code
    (ON, BC, …), so a combined "Remote - Canada; Remote - New York" is still
    CA."""
    loc = f" {(location or '').lower()} "
    if any(t in loc for t in _IE_LOCATION_TOKENS):
        return "IE"
    if any(t in loc for t in _CA_LOCATION_TOKENS) or _has_region_code(loc, _CA_PROVINCE_CODES):
        return "CA"
    if (any(t in loc for t in _US_LOCATION_TOKENS)
            or _has_region_code(loc, _US_STATE_CODES)
            or _US_STATE_NAME_RE.search(loc)):
        return "US"
    return "OTHER"


# ─── Remote detection ─────────────────────────────────────────────────────────
#
# Boards where every listing is remote by definition, so a region-only location
# ("USA", "United States", "Worldwide") still denotes a remote role. Consumed by
# is_remote_role for the source-aware remote check.
REMOTE_ONLY_SOURCES: frozenset[str] = frozenset({"remoteok", "remotive"})

_REMOTE_LOCATION_TOKENS: tuple[str, ...] = (
    "remote", "anywhere", "worldwide", "distributed",
)


def is_remote_role(location: str, source: str | None = None) -> bool:
    """True if a role is remote — either the location text says so, or the
    listing came from a remote-only board (``REMOTE_ONLY_SOURCES``), where a
    region-only location like "USA" still denotes a remote, US-eligible role.
    SSOT for remote detection: used by ``location_passes`` (the US remote-only
    gate) and by ``ingest.ingest_job`` (the stored ``job_type``)."""
    if source in REMOTE_ONLY_SOURCES:
        return True
    loc = (location or "").lower()
    return any(t in loc for t in _REMOTE_LOCATION_TOKENS)


# ─── Office-requirement detection (JD text, not location) ─────────────────────
#
# The US gate used to require an EXPLICIT remote marker in the location string.
# Measured against 14 real Greenhouse boards, that allowlist dropped 939 US
# title-passing roles, of which only ~43% actually state an office requirement:
# 14% say remote in the JD body while the location string doesn't, and 43% say
# nothing at all. An allowlist on a field most companies don't use for work model
# therefore reads "unstated" as "onsite".
#
# So the gate is INVERTED for the US branch: admit unless the JD names an office
# requirement. This is a cheap, string-only pre-screen whose job is to reject the
# obvious ~43% for free, keeping the ingest-time Claude spend down. It is NOT the
# accurate answer — ``score_jd`` returns a ``work_model`` judgment and
# ``config.classify_work_model`` owns the policy. Absence of a match here means
# "no office evidence found", never "confirmed remote".
#
# Recall matters more than precision: a missed pattern admits a role that costs
# one scoring call, while a false positive silently drops a good role. Patterns
# below are drawn from observed JD phrasing (Brex, Lyft, MongoDB, Block).
_OFFICE_NOISE_RE = re.compile(
    # Recruiting-process and travel uses of "onsite" that say nothing about the
    # work model. Stripped BEFORE matching so they can't trip the patterns.
    r"on-?site interview\w*"
    r"|onsite loop"
    r"|customer (?:site|sites|onsite|locations?)"
    r"|travel to (?:customer|client|user)\w*",
    re.I,
)

_OFFICE_REQUIREMENT_RE = re.compile(
    r"this (?:role|position) (?:will be|is|must be) (?:based|located)(?: in| out of| at)"
    r"|(?:based|located|sit) (?:in|out of|at) (?:our|the) [^.]{0,40}office"
    r"|in[- ]office[^.]{0,40}(?:schedule|per week|days|requirement|expectation|role|policy)"
    # "hybrid working model" was a real miss of an earlier draft — allow the
    # participle, and treat a bare "hybrid role/position" as office evidence too.
    r"|hybrid(?: work(?:ing)?)? (?:environment|schedule|model|policy|approach|arrangement|role|position|setup)"
    # Office-days clauses. Both the count style ("3 days" / "three days") and
    # both clause orders occur in the wild — "3 days per week in the office" and
    # "three coordinated days in the office per week" — so match on
    # <count> … days … office rather than assuming an order. The intervening
    # [^.] can't cross a sentence boundary.
    r"|(?:\d+|one|two|three|four|five)\s+(?:[a-z]+\s+){0,2}days?\b[^.]{0,45}office"
    r"|(?:expected|required|expectation) to (?:work|be)(?: on-?site| in the office| in office| at our office)"
    r"|must (?:live|reside|be (?:located|based)) (?:in|within|near)"
    r"|commut(?:able|ing) distance"
    r"|(?:relocate|relocation) to (?:our|the)"
    r"|on-?site (?:\d+|five|four|three|two) days",
    re.I,
)


def names_office_requirement(jd_text: str) -> str | None:
    """Return the matched snippet if the JD states an in-office / hybrid
    requirement, else None. Pure string logic — pre-filter-safe (no Claude).

    Used only by the US branch of ``location_passes``, as the free pre-screen
    described in the banner above. A None result means "no office evidence in the
    text", NOT "this role is remote" — the authoritative call is Claude's
    ``work_model`` resolved through ``config.classify_work_model`` at ingest."""
    if not jd_text:
        return None
    cleaned = _OFFICE_NOISE_RE.sub(" ", jd_text)
    m = _OFFICE_REQUIREMENT_RE.search(cleaned)
    return m.group(0).strip()[:80] if m else None


# ─── Foreign-pinned rejection ─────────────────────────────────────────────────
#
# Location-flexible tokens: an OTHER-classified remote role carrying one of
# these is open to the operator's region (or globally), so it passes. Checked
# BEFORE the foreign denylist so a multi-region role ("Americas, Europe, Asia")
# that includes the Americas isn't rejected for also naming Europe/Asia.
_FLEXIBLE_LOCATION_TOKENS: tuple[str, ...] = (
    "worldwide", "anywhere", "global", "americas", "north america", "namer",
)

# Non-target country / region tokens. An OTHER-classified remote role pinned to
# one of these wants a candidate based there (e.g. "Remote - India" wants an
# India-based hire), which the operator can't take — so it's rejected. CA / IE /
# US are resolved earlier by derive_country and never reach this list.
# Operator-editable: add/remove regions to taste.
_FOREIGN_LOCATION_TOKENS: tuple[str, ...] = (
    # Europe (operator targets IE via sponsorship, not EU-wide work authorization)
    "united kingdom", " uk ", " uk,", "(uk)", "england", "scotland", "wales",
    "european union", "european economic area", "europe", "emea", " eea ", "(eea)",
    "germany", "france", "spain", "portugal", "italy", "netherlands", "belgium",
    "switzerland", "austria", "poland", "romania", "bulgaria", "hungary",
    "czech", "ukraine", "sweden", "norway", "denmark", "finland", "greece",
    "turkey", "israel",
    # Asia / APAC
    "india", "pakistan", "bangladesh", "sri lanka", "china", "hong kong",
    "japan", "korea", "taiwan", "singapore", "malaysia", "thailand",
    "indonesia", "vietnam", "philippines", "apac", "asia pacific", "asia-pacific",
    " asia ", "(asia)",
    # Latin America
    "mexico", "brazil", "argentina", "colombia", "chile", "peru", "latam",
    "latin america", "south america", "central america",
    # Africa / Middle East / Oceania
    "nigeria", "kenya", "egypt", "south africa", "africa", "middle east",
    "united arab emirates", " uae", "saudi arabia", "qatar",
    "australia", "new zealand", "oceania",
)


def names_foreign_location(location: str) -> bool:
    """True if ``location`` is pinned to a non-target country/region the operator
    can't work in. Flexible/global tokens win first, so a multi-region role that
    includes the Americas is kept. Only meaningful for OTHER-classified
    locations (CA/IE/US are resolved by derive_country before this is reached)."""
    loc = f" {(location or '').lower()} "
    if any(t in loc for t in _FLEXIBLE_LOCATION_TOKENS):
        return False
    return any(t in loc for t in _FOREIGN_LOCATION_TOKENS)


def location_passes(location: str,
                    enabled_countries: frozenset[str] | None = None,
                    source: str | None = None,
                    jd_text: str | None = None) -> bool:
    """Pre-filter-safe geography gate (pure string logic — never calls a
    composite, so it's safe to import from ``crawl.pre_filter`` /
    ``prefilter_staged.pre_filter_relaxed``). Removes rows the operator can't
    take:
      - **US**: kept only if US is enabled AND the role is not office-bound.
        A remote marker in the location passes immediately (``is_remote_role``;
        source-aware — a region-only "USA" counts as remote from a remote-only
        board). Otherwise the decision moves to the JD body: with ``jd_text``
        supplied, the role is admitted unless ``names_office_requirement``
        matches; with no usable ``jd_text``, the stricter "explicit marker
        required" rule applies, so callers opt in to the wider gate.
      - **CA / IE**: always kept (sponsorship-target markets, incl. Canadian
        province codes like "London, ON").
      - **OTHER**: kept only if NOT pinned to a foreign region
        (``names_foreign_location``) — so "Worldwide" / "Americas" / bare
        "Remote" pass, but "Remote - India" / "European Union (Remote)" don't.

    ``enabled_countries`` defaults to the live ``TARGET_COUNTRIES`` module global
    — read at call time (NOT captured as a default-arg value), so it tracks the
    configured set exactly like ``config.composite_score`` does.

    SUBTRACTIVE gate layered AFTER the YAML ``location_allow`` allowlist — it
    only removes rows the allowlist would otherwise admit (via bare ``remote``);
    it never admits a row the allowlist rejected. The positive allowlist in
    profile/stack_keywords.yaml remains the pre-filter SSOT."""
    countries = enabled_countries if enabled_countries is not None else TARGET_COUNTRIES
    country = derive_country(location)
    if country == "US":
        if "US" not in countries:
            return False
        if is_remote_role(location, source):
            return True
        # No remote marker in the location. Fall back to the JD body when the
        # caller supplied one: admit unless it names an office requirement.
        # When jd_text is None/blank the caller has no usable body, so we keep
        # the original strict rule — a caller must opt in to the wider gate by
        # passing text, and no existing call site widens silently.
        if not (jd_text or "").strip():
            return False
        return names_office_requirement(jd_text) is None
    if country in ("CA", "IE"):
        return True
    return not names_foreign_location(location)


if __name__ == "__main__":
    # CLI for non-Python callers (e.g. generate_cl.js): prints the country code
    # for the location passed as argv[1]. No API key / config import required.
    print(derive_country(sys.argv[1] if len(sys.argv) > 1 else ""))
