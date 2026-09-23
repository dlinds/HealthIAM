"""Which application should hold a given cloud group as an access level?

The Entra ID counterpart of `apps.directory.routing`, and deliberately the same rules: a route's
pattern is a case-insensitive glob on the group's **display name**, and resolution puts
application-kind targets first, then priority, then pk. Matching is shared with the AD module --
only the route table differs.

Only groups that can back an `entra_group` level are ever routed. A group synced from Active
Directory is an AD group, which the AD group routes place; callers skip those before asking.
"""

from __future__ import annotations

from apps.directory.routing import Match, in_resolution_order, match_in, matches_in

from .models import EntraGroupRoute

__all__ = ["Match", "active_routes", "match_in", "matches_in", "route_for", "routes_for"]


def active_routes() -> list[EntraGroupRoute]:
    """Active routes in resolution order, with their target loaded."""
    return in_resolution_order(EntraGroupRoute.objects.filter(is_active=True))


def route_for(name: str) -> Match | None:
    """The route claiming the display name `name`, or None. Prefer `routes_for` for a list."""
    if not name:
        return None
    return match_in(name, active_routes())


def routes_for(names) -> dict[str, Match | None]:
    """`{name: Match | None}` for every display name given, loading the routes once."""
    names = list(names)
    if not names:
        return {}
    routes = active_routes()
    return {name: match_in(name, routes) for name in names}
