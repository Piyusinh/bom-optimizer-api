"""Season Engine — derives the season bucket from an order start date.

RTM 'Algorithm Logic' item 1:

    "Ranking should also consider the season. Planned/Process order header
     start date can determine the planned start date."

The SAP mechanism behind seasonal formulations is the MKAL production-version
validity window (ADATU/BDATU) — "use this formulation in monsoon, this other
one in summer". This engine gives us the season label so ranking can prefer
paths historically run in the same season as the order we are planning.

The bucket definition below is the chemical/fertilizer default. The client has
not yet confirmed their own buckets, so it is overridable.
"""
from datetime import datetime

# Month number -> season label. Indian agri/chemical calendar default.
DEFAULT_SEASON_MAP: dict[int, str] = {
    1: "WINTER", 2: "WINTER",
    3: "SUMMER", 4: "SUMMER", 5: "SUMMER",
    6: "MONSOON", 7: "MONSOON", 8: "MONSOON", 9: "MONSOON",
    10: "POST_MONSOON", 11: "POST_MONSOON",
    12: "WINTER",
}

SEASONS = ["WINTER", "SUMMER", "MONSOON", "POST_MONSOON"]


def season_of(
    date: datetime | None, season_map: dict[int, str] | None = None
) -> str | None:
    if date is None:
        return None
    return (season_map or DEFAULT_SEASON_MAP).get(date.month)


def same_season(a: datetime | None, b: datetime | None) -> bool:
    sa, sb = season_of(a), season_of(b)
    return sa is not None and sa == sb


def season_match_score(
    candidate_dates: list[datetime], target_date: datetime | None
) -> float:
    """Fraction of a candidate's historical runs that fall in the target season.

    Returns 0.0 when there is nothing to compare, so a candidate with no dates
    is never preferred over one with a genuine seasonal match.
    """
    target = season_of(target_date)
    if target is None or not candidate_dates:
        return 0.0
    matches = sum(1 for d in candidate_dates if season_of(d) == target)
    return round(matches / len(candidate_dates), 4)
