import re
from dataclasses import dataclass

_US_STATES = {
    "AL", "AK", "AZ", "AR", "CA", "CO", "CT", "DE", "FL", "GA",
    "HI", "ID", "IL", "IN", "IA", "KS", "KY", "LA", "ME", "MD",
    "MA", "MI", "MN", "MS", "MO", "MT", "NE", "NV", "NH", "NJ",
    "NM", "NY", "NC", "ND", "OH", "OK", "OR", "PA", "RI", "SC",
    "SD", "TN", "TX", "UT", "VT", "VA", "WA", "WV", "WI", "WY",
    "DC", "GU", "PR", "VI",
}

_US_STATE_NAMES = {
    "Alabama": "AL", "Alaska": "AK", "Arizona": "AZ", "Arkansas": "AR",
    "California": "CA", "Colorado": "CO", "Connecticut": "CT", "Delaware": "DE",
    "Florida": "FL", "Georgia": "GA", "Hawaii": "HI", "Idaho": "ID",
    "Illinois": "IL", "Indiana": "IN", "Iowa": "IA", "Kansas": "KS",
    "Kentucky": "KY", "Louisiana": "LA", "Maine": "ME", "Maryland": "MD",
    "Massachusetts": "MA", "Michigan": "MI", "Minnesota": "MN", "Mississippi": "MS",
    "Missouri": "MO", "Montana": "MT", "Nebraska": "NE", "Nevada": "NV",
    "New Hampshire": "NH", "New Jersey": "NJ", "New Mexico": "NM", "New York": "NY",
    "North Carolina": "NC", "North Dakota": "ND", "Ohio": "OH", "Oklahoma": "OK",
    "Oregon": "OR", "Pennsylvania": "PA", "Rhode Island": "RI", "South Carolina": "SC",
    "South Dakota": "SD", "Tennessee": "TN", "Texas": "TX", "Utah": "UT",
    "Vermont": "VT", "Virginia": "VA", "Washington": "WA", "West Virginia": "WV",
    "Wisconsin": "WI", "Wyoming": "WY", "District Of Columbia": "DC",
    "District of Columbia": "DC", "Puerto Rico": "PR", "Guam": "GU",
}

_COUNTRY_TOKENS = {"UNITED STATES", "UNITED STATES OF AMERICA", "USA", "US"}

# JobBoardly matches city names against a gazetteer of canonical US place names and
# silently drops any city it cannot resolve, keeping only the state. Measured on
# 2026-09-18 against live job pages: "St. Petersburg" and "Saint Paul" are kept,
# while "St Augustine" (no period) and "Mt. Juliet" are dropped - the canonical
# forms are "St. Augustine" and "Mount Juliet". These rewrites are USPS/Census
# conventions, not guesses about which city was meant.
_ABBREVIATIONS = (
    (re.compile(r"^St\.?\s+(?=[A-Z])"), "St. "),
    (re.compile(r"^Mt\.?\s+(?=[A-Z])"), "Mount "),
    (re.compile(r"^Ft\.?\s+(?=[A-Z])"), "Fort "),
)

# Misspellings in employer postings, corrected only where the misspelling is not
# itself a real US place and the correct city exists in the state given. Each entry
# must be verified before being added - this is normalization of a known-bad value,
# never a guess at an unknown one.
#   "Pheonix, AZ" (Hillpointe): no such place; Phoenix, AZ is unambiguous, and
#   JobBoardly drops "Pheonix" entirely, so the page currently shows no city.
_CITY_CORRECTIONS = {
    ("PHEONIX", "AZ"): "Phoenix",
}


def _zip3_to_state(prefix: str) -> str:
    try:
        p = int(prefix)
    except ValueError:
        return ""

    if p == 5:                return "NY"
    if 6 <= p <= 9:           return "PR"
    if 10 <= p <= 27:         return "MA"
    if 28 <= p <= 29:         return "RI"
    if 30 <= p <= 38:         return "NH"
    if 39 <= p <= 49:         return "ME"
    if 50 <= p <= 59:         return "VT"
    if 60 <= p <= 69:         return "CT"
    if 70 <= p <= 89:         return "NJ"
    if 100 <= p <= 149:       return "NY"
    if 150 <= p <= 196:       return "PA"
    if 197 <= p <= 199:       return "DE"
    if 200 <= p <= 205:       return "DC"
    if 206 <= p <= 219:       return "MD"
    if 220 <= p <= 246:       return "VA"
    if 247 <= p <= 268:       return "WV"
    if 270 <= p <= 289:       return "NC"
    if 290 <= p <= 299:       return "SC"
    if 300 <= p <= 319:       return "GA"
    if 320 <= p <= 349:       return "FL"
    if 350 <= p <= 369:       return "AL"
    if 370 <= p <= 385:       return "TN"
    if 386 <= p <= 397:       return "MS"
    if 398 <= p <= 399:       return "GA"
    if 400 <= p <= 427:       return "KY"
    if 430 <= p <= 458:       return "OH"
    if 460 <= p <= 479:       return "IN"
    if 480 <= p <= 499:       return "MI"
    if 500 <= p <= 528:       return "IA"
    if 530 <= p <= 549:       return "WI"
    if 550 <= p <= 567:       return "MN"
    if 570 <= p <= 577:       return "SD"
    if 580 <= p <= 588:       return "ND"
    if 590 <= p <= 599:       return "MT"
    if 600 <= p <= 629:       return "IL"
    if 630 <= p <= 658:       return "MO"
    if 660 <= p <= 679:       return "KS"
    if 680 <= p <= 693:       return "NE"
    if 700 <= p <= 714:       return "LA"
    if 716 <= p <= 729:       return "AR"
    if p == 733:              return "TX"
    if 730 <= p <= 749:       return "OK"
    if 750 <= p <= 799:       return "TX"
    if p == 885:              return "TX"
    if 800 <= p <= 816:       return "CO"
    if 820 <= p <= 831:       return "WY"
    if 832 <= p <= 838:       return "ID"
    if 840 <= p <= 847:       return "UT"
    if 850 <= p <= 865:       return "AZ"
    if 870 <= p <= 884:       return "NM"
    if 889 <= p <= 898:       return "NV"
    if 900 <= p <= 961:       return "CA"
    if 967 <= p <= 968:       return "HI"
    if p == 969:              return "GU"
    if 970 <= p <= 979:       return "OR"
    if 980 <= p <= 994:       return "WA"
    if 995 <= p <= 999:       return "AK"
    return ""


def _extract_city_from_segment(seg: str) -> str:
    """If segment contains dash-separated parts, return the last meaningful one as city."""
    if " - " in seg:
        return seg.split(" - ")[-1].strip().title()
    return seg.strip().title()


def _normalize_city(city: str, state: str) -> str:
    """Canonicalise a city name so JobBoardly's gazetteer can resolve it."""
    city = city.strip().strip(".,;-").strip()
    if not city:
        return ""
    for pattern, replacement in _ABBREVIATIONS:
        city = pattern.sub(replacement, city)
    return _CITY_CORRECTIONS.get((city.upper(), state.upper()), city)


def _match_state(candidate: str) -> str:
    """Return state abbreviation if candidate is a recognizable state token, else ''."""
    upper = candidate.upper().strip()

    if upper in _US_STATES:
        return upper

    # "ST ZIP" or "ST - ZIP" (e.g. "NH 03766", "SC - 29414")
    m = re.match(r"^([A-Z]{2})\s*-?\s*\d{5}", upper)
    if m and m.group(1) in _US_STATES:
        return m.group(1)

    # Full state name
    abbr = _US_STATE_NAMES.get(candidate.strip().title())
    if abbr:
        return abbr

    return ""


@dataclass(frozen=True)
class Location:
    """A resolved job location. `needs_review` marks a posting whose location could
    not be established, so it can be reported rather than silently guessed at."""
    city: str
    state: str
    needs_review: bool = False
    reason: str = ""

    @property
    def resolved(self) -> bool:
        return bool(self.city and self.state)


def _parse_segment(segment: str) -> tuple[str, str]:
    """Parse one location string into (city, state). Returns ('', '') when no US
    state can be identified - see resolve_location for why that matters."""
    parts = [p.strip() for p in segment.split(",")]

    while parts and parts[-1].upper() in _COUNTRY_TOKENS:
        parts.pop()
    if not parts:
        return "", ""

    if len(parts) >= 2:
        # Walk from the right looking for a state token; city is the part before it.
        for i in range(len(parts) - 1, 0, -1):
            state = _match_state(parts[i])
            if state:
                return _extract_city_from_segment(parts[i - 1]), state

        zip_match = re.match(r"^(\d{5})(?:-\d{4})?$", parts[-1])
        if zip_match:
            state = _zip3_to_state(zip_match.group(1)[:3])
            if state:
                return _extract_city_from_segment(parts[-2]), state
        return "", ""

    seg = parts[0].strip()

    # "City ST" or "City ST 12345"
    m = re.match(r"^(.+?)\s+([A-Z]{2})(?:\s+\d{5})?$", seg)
    if m and m.group(2) in _US_STATES:
        return m.group(1).strip().title(), m.group(2)

    # "State - City"
    if " - " in seg:
        dash_parts = [p.strip() for p in seg.split(" - ")]
        state = _match_state(dash_parts[0])
        if state and len(dash_parts) >= 2:
            return dash_parts[-1].title(), state

    return "", ""


def resolve_location(location: str) -> Location:
    """Resolve an ATS location string into a city and state.

    A location is only trusted when a US state can be identified. Employers
    routinely put a property name ("Trellis House"), a corporate office
    ("Corporate - CloudTen") or their own company name ("Redstone Residential") in
    this field, and an earlier version returned those as the city - which put
    property names into <city> and, because JobBoardly drops cities it cannot
    resolve, produced job pages with no location at all.

    Nothing is inferred from the job description. Descriptions routinely name the
    employer's headquarters ("Headquartered in Provo, Utah" on a Redstone posting
    for a property elsewhere), so mining them would attach the wrong city to real
    jobs. An unresolved location is reported, not guessed.
    """
    if not location or not location.strip():
        return Location("", "", True, "no location provided by the employer")

    raw = location.strip()
    if raw.upper() in _COUNTRY_TOKENS:
        return Location("", "", True, f"only a country was given ({raw!r})")

    # Some employers list several locations for one requisition, separated by
    # semicolons ("Reno, NV; Sparks, NV"). Splitting on commas alone turned that
    # into the city "Nv; Sparks". Take the first segment that resolves.
    segments = [seg.strip() for seg in raw.split(";") if seg.strip()]
    multi = len(segments) > 1

    for segment in segments:
        city, state = _parse_segment(segment)
        if city and state:
            city = _normalize_city(city, state)
            if not city:
                continue
            reason = (
                f"first of {len(segments)} locations listed for this requisition"
                if multi else ""
            )
            return Location(city, state, False, reason)

    return Location(
        "", "", True,
        f"no US state could be identified in {raw!r}; "
        "the value looks like a property or company name rather than a place",
    )


def parse_location(location: str) -> tuple[str, str]:
    """Split a location string into (city, state_abbr), or ('', '') if unresolved.

    Handles 'City, ST', 'City, State', 'City, ST ZIP', 'City, State, Country',
    'Property Name, City, State, Country', 'State - City', 'City ST',
    'Property - Street - City, ST - ZIP', and semicolon-separated multi-location
    strings. Kept as a thin wrapper so callers that only need the pair are
    unaffected; use resolve_location when the review flag matters.
    """
    result = resolve_location(location)
    return result.city, result.state
