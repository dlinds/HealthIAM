"""Which person a directory account belongs to, by the keys the account carries.

Both account mirrors -- `apps.directory` for Active Directory, `apps.entra` for Entra ID --
link their accounts through `PeopleIndex.match` and `apply_match`, so one set of rules
decides on both sides:

1. **Strong keys** name a person outright: the employee ID, then the network username (the
   account's sAMAccountName or UPN against `Person.network_username`). A username does not
   count for a person who left before the account was created: names get reused, and a new
   account must never be tied to somebody who has gone.
2. Strong keys naming **two or more people** are a conflict. Nothing is linked on their
   strength, a link to one of those people is left as it is, and the run log says which key
   names whom: a person has to decide which system is wrong.
3. Strong keys naming **exactly one** person link the account to them. The link records the
   strongest key that agreed and is left alone while that key still does.
4. With no strong key, **e-mail** is tried where the mirror allows it: an address exactly one
   person has. An ambiguous address links nobody, since a wrong link would hand one person's
   worklist entries to another.

A link or unlink made by hand is never touched: the mirrors leave `link_method=manual` rows
out of the pass altogether.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime

from django.utils import timezone

from .keys import normalize_username
from .models import Person

#: Automatic link methods, strongest first: the values `DirectoryAccount.LinkMethod` and
#: `EntraAccount.LinkMethod` store.
EMPLOYEE_ID = "employee_id"
USERNAME = "username"
EMAIL = "email"

#: What a link or an unlink writes on an account row.
LINK_FIELDS = ["person", "link_method", "linked_at", "updated_at"]


@dataclass(frozen=True)
class AccountKeys:
    """What an account says about whose it is. `usernames` are its names in the forms a
    network username takes (sAMAccountName, UPN); `emails` are tried only when no strong key
    names anybody, and only when the mirror passes them."""

    employee_id: str = ""
    usernames: tuple[str, ...] = ()
    emails: tuple[str, ...] = ()
    created_at: datetime | None = None


@dataclass(frozen=True)
class Match:
    """Whom the keys name: `person`, with every key that agreed in `methods` (strongest
    first); or the `candidates` strong keys disagree about; or nobody, with `note` saying why
    when there is more to say than "no key matches"."""

    person: Person | None = None
    methods: tuple[str, ...] = ()
    reasons: dict[str, str] = field(default_factory=dict)
    candidates: tuple[Person, ...] = ()
    note: str = ""
    #: The account carries a key that ought to name somebody, and it names nobody.
    unmatched: bool = False

    @property
    def conflict(self) -> bool:
        return len(self.candidates) > 1

    @property
    def method(self) -> str:
        return self.methods[0] if self.methods else ""

    @property
    def reason(self) -> str:
        """The audit reason of a link made on this match."""
        return self.reasons.get(self.method, "")


def left_before(person: Person, created_at: datetime | None) -> bool:
    """`person` had left before the account existed, so a username they held has been
    given to somebody else since."""
    if person.separation_date is None or created_at is None:
        return False
    if timezone.is_naive(created_at):
        created = created_at.date()
    else:
        created = timezone.localdate(created_at)
    return created > person.separation_date


class PeopleIndex:
    """Every person by each key an account can carry, built once per link pass."""

    def __init__(self, people: Iterable[Person]):
        self.by_employee_id: dict[str, Person] = {}
        self.by_username: dict[str, Person] = {}
        self.by_email: dict[str, list[Person]] = {}
        for person in people:
            if person.employee_id:
                self.by_employee_id[person.employee_id] = person
            if person.network_username:
                self.by_username[normalize_username(person.network_username)] = person
            email = (person.email or "").strip().lower()
            if email:
                self.by_email.setdefault(email, []).append(person)

    @classmethod
    def build(cls) -> PeopleIndex:
        return cls(Person.objects.all())

    def match(self, keys: AccountKeys) -> Match:
        hits = self._strong_hits(keys)
        people = {person.pk: person for _, person, _, _ in hits}
        if len(people) > 1:
            named = "; ".join(f"{what} names {person.display_name}" for _, person, _, what in hits)
            return Match(
                candidates=tuple(people.values()),
                note=f"its keys name different people: {named}",
            )
        if people:
            (person,) = people.values()
            reasons: dict[str, str] = {}
            for method, _, reason, _ in hits:
                reasons.setdefault(method, reason)
            return Match(person=person, methods=tuple(reasons), reasons=reasons)
        note = ""
        for email in dict.fromkeys(e.strip().lower() for e in keys.emails if e and e.strip()):
            found = self.by_email.get(email, [])
            if len(found) == 1:
                return Match(
                    person=found[0], methods=(EMAIL,), reasons={EMAIL: "E-mail address matches"}
                )
            if len(found) > 1:
                note = f"{len(found)} people have the e-mail {email}"
                break
        return Match(note=note, unmatched=bool((keys.employee_id or "").strip() or note))

    def _strong_hits(self, keys: AccountKeys) -> list[tuple[str, Person, str, str]]:
        """`(method, person, audit reason, the key in words)` for every strong key that names
        somebody, strongest first."""
        hits = []
        employee_id = (keys.employee_id or "").strip()
        if employee_id:
            person = self.by_employee_id.get(employee_id)
            if person is not None:
                hits.append(
                    (
                        EMPLOYEE_ID,
                        person,
                        f"Employee ID {employee_id} matches",
                        f"employee ID {employee_id}",
                    )
                )
        for username in dict.fromkeys(normalize_username(u) for u in keys.usernames):
            person = self.by_username.get(username) if username else None
            if person is not None and not left_before(person, keys.created_at):
                hits.append(
                    (USERNAME, person, f"Username {username} matches", f"username {username}")
                )
        return hits


# --- Applying a match to an account row --------------------------------------------------------


def link_message(account, previous: Person | None = None) -> str:
    """ "linked to Alice Anders by employee ID", or "re-linked to ... (was ...)": the run-log
    line for the link `account` holds now. Shared with the demo's fabricated runs."""
    how = type(account).LinkMethod(account.link_method).label
    message = f"linked to {account.person.display_name} {how[:1].lower()}{how[1:]}"
    if previous is not None and previous.pk != account.person_id:
        message = f"re-{message} (was {previous.display_name})"
    return message


def apply_match(
    account, match: Match, *, now, lost: str, result=None, code: str = "", dn: str = ""
) -> str:
    """Bring one account's automatic link in line with `match`.

    Returns what the caller counts: "linked" (to a person it was not linked to),
    "unlinked", "unmatched", "conflict", or "" when nothing changed -- or only which key the
    link rests on, which is audited but is not news. `lost` says why a link went away when
    `match` has nothing more specific; `result`, when given, receives the run-log rows under
    `code` and `dn`.
    """
    if match.conflict:
        if result is not None:
            result.record(0, code, "conflict", match.note, dn=dn)
        if account.person_id is None or account.person_id in {p.pk for p in match.candidates}:
            return "conflict"
        _unlink(account, match.note, result=result, code=code, dn=dn)
        return "unlinked"
    person = match.person
    if person is not None:
        if account.person_id == person.pk and account.link_method in match.methods:
            return ""
        previous = account.person if account.person_id is not None else None
        account.person = person
        account.link_method = match.method
        account.linked_at = now
        account._audit_reason = match.reason
        account.save(update_fields=LINK_FIELDS)
        if previous is not None and previous.pk == person.pk:
            return ""
        if result is not None:
            result.record(0, code, "linked", link_message(account, previous), dn=dn)
        return "linked"
    if account.person_id is not None:
        _unlink(account, match.note or lost, result=result, code=code, dn=dn)
        return "unlinked"
    return "unmatched" if match.unmatched else ""


def _unlink(account, why: str, *, result, code: str, dn: str) -> None:
    previous = account.person
    account.person = None
    account.link_method = ""
    account.linked_at = None
    account._audit_reason = why[:1].upper() + why[1:]
    account.save(update_fields=LINK_FIELDS)
    if result is not None:
        result.record(0, code, "unlinked", f"unlinked from {previous.display_name}: {why}", dn=dn)
