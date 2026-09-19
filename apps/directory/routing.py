"""Which application should hold a given AD group as an access level?

An AD group carries no pointer to the system it belongs to; a naming convention is all
there is to go on. `ADGroupRoute` records those conventions, and this module applies
them: first active route by priority whose pattern matches the name wins.

Routing is **advisory**. Nothing here writes, and `sync` does not import this module --
a route only pre-fills a target that a person confirms before any access level is
created. That keeps a mistyped pattern from quietly filling the catalog.
"""

from __future__ import annotations

from dataclasses import dataclass

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
    return list(ADGroupRoute.objects.filter(is_active=True).select_related("application"))


def match_in(name: str, routes: list[ADGroupRoute]) -> Match | None:
    """First route in `routes` whose pattern claims `name`. Callers that already hold the
    route list use this to avoid a query per name."""
    for route in routes:
        # A single pattern, so the "empty means all" default of `matches_patterns` -- which
        # would make every route match everything -- can never be reached from here.
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
