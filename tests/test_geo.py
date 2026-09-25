"""Location parsing. Every employer format we have encountered is pinned here -
add a case when a new company shows wrong locations rather than patching blind.

The central rule under test: a location is only trusted when a US state can be
identified. Employers put property names, corporate offices and their own company
name in this field, and treating those as cities put garbage into <city>.
"""

import pytest

from pipeline.geo import parse_location, resolve_location


# --- formats that must keep working -----------------------------------------

@pytest.mark.parametrize("raw,expected", [
    ("Dallas, TX", ("Dallas", "TX")),
    ("Austin, Texas", ("Austin", "TX")),
    ("SEATTLE, WA, 98103", ("Seattle", "WA")),
    ("Atlanta, Georgia, United States", ("Atlanta", "GA")),
    ("Boring, Oregon", ("Boring", "OR")),
    # Scion Group: property name prefixed
    ("The Quarters, Champaign, Illinois, United States", ("Champaign", "IL")),
    # Cottonwood Residential: reversed, dash separated
    ("Florida - St. Petersburg", ("St. Petersburg", "FL")),
    # Hawthorne: no comma
    ("Charlotte NC", ("Charlotte", "NC")),
    # Hawthorne verbose
    ("Hawthorne Tower - 500 Elm - Charlotte, NC - 28202", ("Charlotte", "NC")),
    # ZIP-only state resolution
    ("Cambridge, 02139", ("Cambridge", "MA")),
])
def test_known_employer_formats(raw, expected):
    assert parse_location(raw) == expected


# --- the "anything becomes a city" bug --------------------------------------

@pytest.mark.parametrize("raw", [
    "Trellis House",            # Berkshire property
    "Metro on Main",            # CloudTen property
    "Las Brisas",               # Sunrise property
    "Redstone Residential",     # the employer's own name
    "Corporate - CloudTen",     # a corporate office
    "Berkshire Medical Dist",
    "445 W. University",        # a street address
    "United States",            # country only
    "",
    "   ",
])
def test_non_geographic_values_resolve_to_nothing(raw):
    """These used to be emitted as the city. JobBoardly drops cities it cannot
    resolve, so the result was a job page with no location at all."""
    location = resolve_location(raw)

    assert location.city == ""
    assert location.state == ""
    assert location.needs_review is True
    assert location.resolved is False


def test_unresolved_location_explains_itself():
    location = resolve_location("Tinsley on the Park")

    assert "no US state could be identified" in location.reason
    assert "Tinsley on the Park" in location.reason


def test_a_real_city_without_a_state_is_still_rejected():
    """Birgo posts 'Greensburg'. It is a real city, but Greensburg exists in PA,
    KS, IN, KY and LA, so the state cannot be inferred - flag it, don't guess."""
    location = resolve_location("Greensburg")

    assert location.resolved is False
    assert location.needs_review is True


# --- multi-location strings --------------------------------------------------

@pytest.mark.parametrize("raw,expected", [
    ("Reno, NV; Sparks, NV", ("Reno", "NV")),
    ("Colorado Springs, CO; Denver, CO", ("Colorado Springs", "CO")),
    ("Anaheim, California, United States; San Diego, California, United States",
     ("Anaheim", "CA")),
])
def test_semicolon_multi_location_takes_the_first(raw, expected):
    """Splitting on commas alone turned 'Reno, NV; Sparks, NV' into the city
    'Nv; Sparks'."""
    assert parse_location(raw) == expected


def test_multi_location_is_noted_but_not_flagged():
    location = resolve_location("Reno, NV; Sparks, NV")

    assert location.needs_review is False
    assert "first of 2" in location.reason


def test_semicolon_list_of_property_names_resolves_to_nothing():
    assert parse_location("Linden; Patton Apartments; Wedekind") == ("", "")


def test_multi_location_falls_through_to_a_resolvable_segment():
    assert parse_location("Corporate Office; Tempe, AZ") == ("Tempe", "AZ")


# --- canonical city names ----------------------------------------------------

@pytest.mark.parametrize("raw,expected_city", [
    ("St Augustine, FL", "St. Augustine"),   # JobBoardly drops the period-less form
    ("St Augustine. FL", "St. Augustine"),   # employer typo: period instead of comma
    ("St. Petersburg, FL", "St. Petersburg"),
    ("Mt. Juliet, TN", "Mount Juliet"),      # canonical is Mount Juliet
    ("Mt Juliet, TN", "Mount Juliet"),
    ("Ft Worth, TX", "Fort Worth"),
    ("Saint Paul, Minnesota, United States", "Saint Paul"),
])
def test_city_names_are_canonicalised(raw, expected_city):
    assert parse_location(raw)[0] == expected_city


def test_verified_misspelling_is_corrected():
    """Hillpointe posts 'Pheonix, AZ'. No such place exists; JobBoardly drops it,
    so the page shows no city at all."""
    assert parse_location("Pheonix, AZ") == ("Phoenix", "AZ")


def test_correction_is_scoped_to_the_state_it_was_verified_in():
    """The correction table is keyed on (city, state) so a misspelling is never
    rewritten in a state where it was not checked."""
    assert parse_location("Pheonix, TX")[0] == "Pheonix"


# --- employer headquarters must never leak in --------------------------------

def test_nothing_is_inferred_when_the_field_names_the_employer():
    """A Redstone posting for a property elsewhere says 'Headquartered in Provo,
    Utah' in its description. Resolution looks only at the location field, so the
    HQ cannot become the job's city."""
    location = resolve_location("Redstone Residential")

    assert (location.city, location.state) == ("", "")


def test_parse_location_returns_a_plain_pair():
    assert parse_location("Dallas, TX") == ("Dallas", "TX")
    assert parse_location("Trellis House") == ("", "")


@pytest.mark.parametrize("text", [
    "Concord, NC (Charlotte area)",
    "Charlotte North Carolina",
    "Washington Square",
    "Richmond, VA (Henrico/West End)",
])
def test_mentions_us_state_detects_named_states(text):
    from pipeline.geo import mentions_us_state
    assert mentions_us_state(text)


@pytest.mark.parametrize("text", [
    "Northpointe",
    "Peninsula Park & Alta Torre",
    "Acorn",
    "Casa Sueños",
    "Villa Loma Apartments",
    "",
])
def test_mentions_us_state_ignores_property_names(text):
    from pipeline.geo import mentions_us_state
    assert not mentions_us_state(text)
