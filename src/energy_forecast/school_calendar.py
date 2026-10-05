"""Verified SA government term dates; other profiles use explicit calendar imports."""

from datetime import date, timedelta

SOURCE = (
    "https://www.education.sa.gov.au/parents-and-families/term-dates-south-australian-state-schools"
)
# Department page last updated 7 April 2026. Earlier issue times cannot assume this snapshot.
KNOWN_AT = "2026-04-07T00:00:00+00:00"
TERMS = {
    2024: [("01-29", "04-12"), ("04-29", "07-05"), ("07-22", "09-27"), ("10-14", "12-13")],
    2025: [("01-28", "04-11"), ("04-28", "07-04"), ("07-21", "09-26"), ("10-13", "12-12")],
    2026: [("01-27", "04-10"), ("04-27", "07-03"), ("07-20", "09-25"), ("10-12", "12-11")],
    2027: [("01-27", "04-09"), ("04-26", "07-02"), ("07-19", "09-24"), ("10-11", "12-10")],
}


def events():
    result = []
    for year, terms in TERMS.items():
        cursor = date(year, 1, 1)
        for first, last in terms:
            start, end = date.fromisoformat(f"{year}-{first}"), date.fromisoformat(f"{year}-{last}")
            result.append(
                {
                    "start": cursor.isoformat(),
                    "end": start.isoformat(),
                    "label": "SA government school holidays",
                    "kind": "school_holiday",
                    "profile": "government",
                    "source": SOURCE,
                    "version": "2026-04-07",
                    "known_at": KNOWN_AT,
                }
            )
            cursor = end + timedelta(days=1)
        result.append(
            {
                "start": cursor.isoformat(),
                "end": date(year + 1, 1, 1).isoformat(),
                "label": "SA government summer holidays",
                "kind": "school_holiday",
                "profile": "government",
                "source": SOURCE,
                "version": "2026-04-07",
                "known_at": KNOWN_AT,
            }
        )
    return result
