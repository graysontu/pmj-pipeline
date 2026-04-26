import re

_US_STATES = {
    "AL", "AK", "AZ", "AR", "CA", "CO", "CT", "DE", "FL", "GA",
    "HI", "ID", "IL", "IN", "IA", "KS", "KY", "LA", "ME", "MD",
    "MA", "MI", "MN", "MS", "MO", "MT", "NE", "NV", "NH", "NJ",
    "NM", "NY", "NC", "ND", "OH", "OK", "OR", "PA", "RI", "SC",
    "SD", "TN", "TX", "UT", "VT", "VA", "WA", "WV", "WI", "WY",
    "DC", "GU", "PR", "VI",
}


def _zip3_to_state(prefix: str) -> str:
    """Map a 3-digit ZIP prefix to a US state abbreviation using USPS zone ranges."""
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


def parse_location(location: str) -> tuple[str, str]:
    """Split a location string into (city, state_abbr).

    Handles formats: 'City, ST', 'City, ZIP', 'City, ST ZIP', 'City, ST, Country'.
    Returns ('', '') for single-token or unrecognized strings.
    """
    if not location:
        return "", ""

    parts = [p.strip() for p in location.split(",")]
    if len(parts) < 2:
        return location.strip().title(), ""

    city = parts[0].strip().title()

    # Walk parts from the end looking for a recognizable state token
    for candidate in reversed(parts[1:]):
        upper = candidate.upper().strip()

        # Direct 2-letter state abbreviation
        if upper in _US_STATES:
            return city, upper

        # "ST ZIP" packed into one segment (e.g. "TX 78701")
        st_zip = re.match(r"^([A-Z]{2})\s+\d{5}", upper)
        if st_zip and st_zip.group(1) in _US_STATES:
            return city, st_zip.group(1)

        # Pure 5-digit (or 5+4) ZIP code
        zip_match = re.match(r"^(\d{5})(?:-\d{4})?$", candidate.strip())
        if zip_match:
            state = _zip3_to_state(zip_match.group(1)[:3])
            return city, state

    return city, parts[-1]
