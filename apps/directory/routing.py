"""Which application should hold a given AD group as an access level?

An AD group carries no pointer to the system it belongs to; a naming convention is all
there is to go on. `ADGroupRoute` records those conventions, and this module applies
them.

Resolution order is **application-kind targets first, then priority, then pk**. A group a
real application claims by name is that application's to hold, however broad the pattern
that claims it; the numeric priority only orders routes within one kind. `matches_in`
returns the whole ordered list, because a caller that can only use a *dynamic* target has
to walk past the ones that are not.

Routing itself never writes, and it cannot change what the directory mirror holds: the
reconciler that acts on a route runs strictly after the mirror is committed, never on a
dry run, and only ever writes catalog and access rows. For an ordinary application a route
is still purely advisory -- it pre-fills a target that a person confirms. It creates and
retires levels by itself only for an application whose `dynamic_ad_groups` is on.
"""

from __future__ import annotations

from dataclasses import dataclass

from django.db.models import Case, IntegerField, When

from apps.catalog.models import Application

from .matching import matches_patterns
from .models import ADGroupRoute


@dataclass(frozen=True)
class Match:
    """The route that claimed a name, and the application it points at."""

    route: ADGroupRoute
    application: Application

    @property
    def pattern(self) -> str:
        return self.route.pattern


def active_routes() -> list[ADGroupRoute]:
    """Active routes in resolution order, with their target loaded."""
    return list(
        ADGroupRoute.objects.filter(is_active=True)
        .select_related("application")
        # Ranked explicitly rather than by ordering on `application__kind`, which happens
        # to sort "application" before "service" alphabetically -- luck, not a rule. A
        # future third kind lands after applications through the `default` arm, which is
        # the safe side to err on.
        .annotate(
            kind_rank=Case(
                When(application__kind=Application.Kind.APPLICATION, then=0),
                default=1,
                output_field=IntegerField(),
            )
        )
        .order_by("kind_rank", "priority", "pk")
    )


def matches_in(name: str, routes: list[ADGroupRoute]) -> list[Match]:
    """Every route in `routes` claiming `name`, in resolution order.

    Callers that already hold the route list use this to avoid a query per name. It is a
    linear scan of patterns rather than a database query on purpose: a few thousand groups
    against a few dozen routes is a fraction of a second, and it is *one* query instead of
    one per group.
    """
    if not name:
        return []
    return [
        Match(route=route, application=route.application)
        for route in routes
        # A single pattern, so the "empty means all" default of `matches_patterns` -- which
        # would make every route match everything -- can never be reached from here.
        if matches_patterns(name, [route.pattern])
    ]


def match_in(name: str, routes: list[ADGroupRoute]) -> Match | None:
    """The top route in `routes` claiming `name`: what the catalog suggests as its home."""
    if not name:
        return None
    for route in routes:
        if matches_patterns(name, [route.pattern]):
            return Match(route=route, application=route.application)
    return None


def route_for(name: str) -> Match | None:
    """The route claiming `name`, or None. One query; prefer `routes_for` for a list."""
    if not name:
        return None
    return match_in(name, active_routes())


def routes_for(names) -> dict[str, Match | None]:
    """`{name: Match | None}` for every name given, loading the routes once."""
    names = list(names)
    if not names:
        return {}
    routes = active_routes()
    return {name: match_in(name, routes) for name in names}
