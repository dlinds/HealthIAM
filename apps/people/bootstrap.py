"""The person types every deployment starts with.

`ensure_person_types()` only *creates* what is missing: the flags are an organization's
policy (whether a traveler must have an end date, whether a contractor needs an agency), so a
value an Admin changed on the People types page is never put back by the next deploy.
"""

from __future__ import annotations

from .models import PersonType

#: code, name, is_external, requires_end_date, requires_sponsor, requires_organization
DEFAULT_PERSON_TYPES = [
    ("employee", "Employee", False, False, False, False),
    ("provider", "Provider", False, False, False, False),
    ("student", "Student", True, True, True, True),
    ("traveler", "Traveler", True, False, True, True),
    ("contractor", "Contractor", True, False, True, False),
    ("volunteer", "Volunteer", True, False, True, False),
    ("vendor", "Vendor representative", True, True, True, True),
]

DESCRIPTIONS = {
    "employee": "Employed staff; positions and names come from the HR feed.",
    "provider": "Physicians and advanced practice providers, employed or affiliated.",
    "student": "Nursing, medical and allied-health students on rotation.",
    "traveler": "Agency (travel) clinicians on contract.",
    "contractor": "Consultants and contract workers, from a company or independent.",
    "volunteer": "Volunteers and auxiliary members.",
    "vendor": "Vendor support and implementation staff who need access.",
}


def ensure_person_types() -> list[tuple[PersonType, bool]]:
    """Create any missing default type. Returns `(type, created)` per default, in order."""
    out = []
    for i, (code, name, external, end_date, sponsor, organization) in enumerate(
        DEFAULT_PERSON_TYPES, start=1
    ):
        ptype, created = PersonType.objects.get_or_create(
            code=code,
            defaults={
                "name": name,
                "description": DESCRIPTIONS.get(code, ""),
                "is_external": external,
                "requires_end_date": end_date,
                "requires_sponsor": sponsor,
                "requires_organization": organization,
                "sort_order": i * 10,
            },
        )
        out.append((ptype, created))
    return out
