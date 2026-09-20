"""The synthetic Active Directory a demo install pretends to have synced from.

There is no fake LDAP server anywhere in this project. `build_client()` stays honest, so
**Test connection, Sync now and `manage.py sync_ad` fail** against `dc1.demo.local`, which
does not resolve -- that is the demo, not a defect. What `seed_demo` and `demo_ad` do instead
is write the *mirror* those commands would have filled: `ADGroup` rows, AD-managed logins and
`DirectorySyncRun` records. Everything downstream of the mirror -- reference badges, routes,
route-managed levels, adoption, the broken-reference report -- is then the real code path.

This module holds **data only**. `config.settings.dev` imports it to point `AD_BASE_DN` and
the group search base at the same DNs the seed writes, and settings are read before the app
registry exists, so importing a model here would break every `manage.py` invocation. The
writers that need Django live next door in `mirror.py`.

The inventory is deliberately built so that all four reference statuses in
`apps.directory.references` are reachable from a plain `make seed`, *and* so that they come
out the same under `config/settings/test.py`, whose group filters are narrower. The trick is
that `status_for_levels` checks `in_scope()` before it looks for an inactive row: a group
outside the filters reads "Outside sync filter" whatever the mirror holds. So the group that
demonstrates *inactive* has to be one both filter sets admit (an `APP_*` name), and the one
that demonstrates *out of scope* has to be excluded by both (`LIC_RETIRED_*`).
"""

from __future__ import annotations

import datetime as dt
import uuid
from dataclasses import dataclass, field

# --- The domain -------------------------------------------------------------------------

DOMAIN = "demo.local"
# uuid5 over this namespace: every seed run produces the same GUIDs, so re-seeding changes
# nothing and a real sync (which keys on objectGUID) simply deactivates the lot. The
# namespace and the "group:"/"user:" key scheme predate this module -- do not change them, or
# an already-seeded database grows a second copy of every row.
NAMESPACE = uuid.uuid5(uuid.NAMESPACE_DNS, DOMAIN)

BASE_DN = "DC=demo,DC=local"
GROUPS_OU = f"OU=Groups,{BASE_DN}"
# A child of the search base, so groups no application owns are visibly filed apart without
# needing a second search base.
INFRA_OU = f"OU=Infrastructure,{GROUPS_OU}"
STAFF_OU = f"OU=Staff,{BASE_DN}"
SERVICE_OU = f"OU=Service Accounts,{BASE_DN}"

SERVER = "dc1.demo.local"
# `demo_ad` records its runs against the second DC. Nothing depends on the hostname, but it
# keeps drift runs distinguishable from the seeded four at a glance and, more importantly,
# stops `record_run` ever matching a seeded run when it looks for one of its own.
DRIFT_SERVER = "dc2.demo.local"
SERVER_URI = f"ldaps://{SERVER}"

BIND_DN = f"CN=svc-healthiam,{SERVICE_OU}"
# A placeholder, exactly as `config/settings/test.py` carries one. Without it `directory.W003`
# shouts about missing bind credentials on the admin page and crowds out `directory.W008`,
# which is the one warning that explains something real about this demo: why a synced login
# cannot sign in. Nothing ever binds with it -- there is no host to bind to.
BIND_PASSWORD = "demo-only-not-a-real-secret"

USER_GROUP = "IAM-Users"
USER_GROUP_DN = f"CN={USER_GROUP},{GROUPS_OU}"
IAM_TEAM_DN = f"CN=IAM Team,{STAFF_OU}"
NETWORK_TEAM_DN = f"CN=Network Team,{STAFF_OU}"
STORAGE_TEAM_DN = f"CN=Storage Team,{STAFF_OU}"

# Empty name patterns are the documented recommendation in `.env.example`: a group outside the
# patterns can never be checked, so an access level naming it reads "outside sync filter"
# forever instead of telling you the group is gone. The search base plus the excludes do the
# filtering instead.
NAME_PATTERNS: list[str] = []
EXCLUDE_PATTERNS = ["IAM-*", "Domain *", "Enterprise *", "LIC_RETIRED_*"]

# groupType as Active Directory stores it: a signed 32-bit field whose security bit makes the
# value negative. See `apps.directory.matching.decode_group_type`.
GLOBAL_SECURITY = -2147483646
UNIVERSAL_SECURITY = -2147483640
GLOBAL_DISTRIBUTION = 2

# The mirror timestamps are fixed rather than relative to `now`, so a re-seed is a no-op and
# the run history does not shuffle every time the demo is reloaded.
SEEDED_WHEN_CHANGED = dt.datetime(2025, 9, 1, 8, 0, tzinfo=dt.UTC)


def group_guid(name: str) -> uuid.UUID:
    return uuid.uuid5(NAMESPACE, f"group:{name}")


def user_guid(sam: str) -> uuid.UUID:
    return uuid.uuid5(NAMESPACE, f"user:{sam}")


# --- Groups -----------------------------------------------------------------------------


class State:
    """What the mirror should hold for a group.

    `ABSENT` is not an oversight: a group an access level names but the directory does not
    return is what produces the "Not found in AD" badge, the dashboard tile and the
    broken-reference report, so the demo needs some on purpose.
    """

    ACTIVE = "active"
    INACTIVE = "inactive"
    ABSENT = "absent"


@dataclass(frozen=True)
class GroupSpec:
    name: str
    description: str
    ou: str = GROUPS_OU
    group_type: int = GLOBAL_SECURITY
    managed_by: str = IAM_TEAM_DN
    state: str = State.ACTIVE
    #: Why this group is in the demo. Printed by `demo_ad status`; never stored.
    demonstrates: str = ""

    @property
    def dn(self) -> str:
        return f"CN={self.name},{self.ou}"


GROUPS: tuple[GroupSpec, ...] = (
    # -- Application groups the seeded access levels reference -------------------------
    GroupSpec(
        "APP_PACS_RADIOLOGIST",
        "Sectra PACS - full diagnostic read",
        demonstrates="In AD: the healthy case",
    ),
    GroupSpec("APP_PACS_TECH", "Sectra PACS - acquire and QA images"),
    GroupSpec("APP_PACS_VIEW", "Sectra PACS - view images and reports"),
    GroupSpec("APP_3M_CODER", "3M 360 Encompass - code encounters"),
    GroupSpec(
        "APP_3M_CDI",
        "3M 360 Encompass - documentation queries",
        demonstrates="the group `demo_ad drift` deactivates",
    ),
    GroupSpec("APP_SNOW_USER", "ServiceNow - submit and track tickets"),
    GroupSpec(
        "LIC_M365_E3",
        "Microsoft 365 E3 licence",
        group_type=UNIVERSAL_SECURITY,
        demonstrates="a universal group, so the Type column is not uniform",
    ),
    GroupSpec("LIC_M365_F3", "Microsoft 365 F3 licence", group_type=UNIVERSAL_SECURITY),
    GroupSpec(
        "APP_EPIC_RESEARCH",
        "Epic - chart access for IRB-approved studies",
        state=State.INACTIVE,
        demonstrates="Not returned by the last sync: the group was deactivated by a sync",
    ),
    GroupSpec(
        "APP_UKG_EMPLOYEE",
        "UKG Dimensions - clock in/out, view schedule",
        state=State.ABSENT,
        demonstrates="Not found in AD: broken-reference report and dashboard tile",
    ),
    GroupSpec(
        "LIC_RETIRED_VISIO_2013",
        "Retired Visio 2013 licence group",
        state=State.ABSENT,
        demonstrates="Outside sync filter: excluded by LIC_RETIRED_*, so it is never checked",
    ),
    # -- Groups no application owns: the adoption worklist ------------------------------
    GroupSpec(
        "VPN_CLINICAL_REMOTE",
        "Remote access for clinical staff on call",
        ou=INFRA_OU,
        managed_by=NETWORK_TEAM_DN,
        demonstrates="route-managed level on a dynamic service; carries a position default",
    ),
    GroupSpec(
        "VPN_IS_ONCALL",
        "Remote access for Information Services on-call",
        ou=INFRA_OU,
        managed_by=NETWORK_TEAM_DN,
    ),
    GroupSpec(
        "VPN_VENDOR_SUPPORT",
        "Remote access for supervised vendor support sessions",
        ou=INFRA_OU,
        managed_by=NETWORK_TEAM_DN,
        demonstrates="adopted out of its route: the Taken over badge",
    ),
    GroupSpec(
        "FS_HIM_SCANNING",
        "File share: HIM scanning queue",
        ou=INFRA_OU,
        managed_by=STORAGE_TEAM_DN,
        demonstrates="unreferenced, with a route suggesting File Shares: the adoption worklist",
    ),
    GroupSpec(
        "FS_PHARMACY_POLICIES",
        "File share: pharmacy policies and procedures",
        ou=INFRA_OU,
        managed_by=STORAGE_TEAM_DN,
    ),
    GroupSpec(
        "FS_RADIOLOGY_TEACHING",
        "File share: radiology teaching files",
        ou=INFRA_OU,
        managed_by=STORAGE_TEAM_DN,
        demonstrates="two routes claim it; the application-kind target wins over the service",
    ),
    GroupSpec("PRINT_NURSING_3RD", "Printers: 3rd floor nursing units", ou=INFRA_OU),
    GroupSpec("PRINT_PHARMACY_LABELS", "Printers: pharmacy label printers", ou=INFRA_OU),
    GroupSpec("BADGE_OR_SUITE", "Badge access: operating room suite", ou=INFRA_OU),
    GroupSpec(
        "BADGE_PHARMACY_VAULT", "Badge access: pharmacy controlled-substance vault", ou=INFRA_OU
    ),
    GroupSpec(
        "APP_DEMO_UNUSED",
        "Pilot group from 2023; no access level references it.",
        demonstrates="unreferenced and unrouted: on the worklist with nothing to suggest",
    ),
    GroupSpec(
        "DL_NURSING_ALLSTAFF",
        "Distribution list: all nursing staff",
        group_type=GLOBAL_DISTRIBUTION,
        demonstrates="a distribution group, so the category filter has something to filter",
    ),
)

GROUPS_BY_NAME = {spec.name: spec for spec in GROUPS}

# Groups `demo_ad` introduces. They are never seeded, but the seed has to know their names:
# once drift has renamed a group or added one, an access level points at it, and the seed's
# "is this group in the inventory?" check would otherwise report a group of the demo's own
# making as an unknown reference.
RENAMED_GROUP = "VPN_CLINICAL_REMOTE"
RENAMED_GROUP_TO = f"{RENAMED_GROUP}_V2"
ADDED_GROUP = "VPN_RESEARCH_REMOTE"
ADDED_GROUP_DESCRIPTION = "Remote access for the research informatics team"
DRIFT_GROUP_NAMES = frozenset({RENAMED_GROUP_TO, ADDED_GROUP})

#: Names a seeded access level may reference without the seed complaining.
KNOWN_GROUP_NAMES = frozenset(GROUPS_BY_NAME) | DRIFT_GROUP_NAMES
#: What a healthy mirror holds after `seed_demo`.
SEEDED_ACTIVE_NAMES = frozenset(s.name for s in GROUPS if s.state == State.ACTIVE)
SEEDED_INACTIVE_NAMES = frozenset(s.name for s in GROUPS if s.state == State.INACTIVE)
ABSENT_NAMES = frozenset(s.name for s in GROUPS if s.state == State.ABSENT)
#: Written to the mirror at all (active or deactivated).
MIRRORED_NAMES = SEEDED_ACTIVE_NAMES | SEEDED_INACTIVE_NAMES

# `IAM-Users` itself is deliberately not mirrored: EXCLUDE_PATTERNS carries `IAM-*`, so a real
# sync would never import the group that grants access to HealthIAM itself. Its DN appears
# only as `DirectorySyncRun.group_dn`, which is where a real run records it too.


# --- People -----------------------------------------------------------------------------


@dataclass(frozen=True)
class StaffSpec:
    """One member of `IAM-Users`, written the way `sync._UserSync.create` would write it."""

    sam: str
    first_name: str
    last_name: str
    job_title: str
    department_name: str
    enabled: bool = True
    demonstrates: str = ""

    @property
    def upn(self) -> str:
        return f"{self.sam}@{DOMAIN}"

    @property
    def username(self) -> str:
        # The sync names a login after the userPrincipalName, which is why these look
        # different from the seven local demo accounts.
        return self.upn

    @property
    def cn(self) -> str:
        return f"{self.first_name} {self.last_name}"

    @property
    def dn(self) -> str:
        return f"CN={self.cn},{STAFF_OU}"


STAFF: tuple[StaffSpec, ...] = (
    StaffSpec("rnorton", "Rachel", "Norton", "Registered Nurse", "Nursing"),
    StaffSpec(
        "dpatel",
        "Dev",
        "Patel",
        "Pharmacy Technician",
        "Pharmacy",
        demonstrates="the login `demo_ad drift` disables",
    ),
    StaffSpec("mchen", "Mei", "Chen", "Radiologic Technologist", "Radiology"),
    StaffSpec("tokafor", "Tunde", "Okafor", "Coding Specialist", "Health Information Management"),
    StaffSpec("sgrant", "Sam", "Grant", "Patient Access Representative", "Patient Access"),
    StaffSpec("lbeaumont", "Luc", "Beaumont", "Systems Analyst", "Information Services"),
    StaffSpec(
        "jhaddad",
        "Jana",
        "Haddad",
        "Medical Technologist",
        "Laboratory",
        enabled=False,
        demonstrates="disabled in AD, so the managed-login counter shows an inactive one",
    ),
)

STAFF_BY_SAM = {spec.sam: spec for spec in STAFF}

#: The pre-existing local login the seed links to AD. Unlike the accounts above it keeps its
#: password, so it is the one managed login a demo can actually sign in as.
LINKED_LOGIN = "helpdesk"
LINKED_LOGIN_CN = "Casey Nguyen"


# --- Routes -----------------------------------------------------------------------------

# Service applications: a home for the AD groups no vendor application owns. Kept as separate
# rows per owning team rather than one bucket so analyst rights stay scoped per team.
NETWORK_ACCESS = "Network Access"
FILE_SHARES = "File Shares"
PRINTING = "Printing"
PHYSICAL_ACCESS = "Physical Access"


@dataclass(frozen=True)
class RouteSpec:
    pattern: str
    application: str
    priority: int = 100
    notes: str = ""
    is_active: bool = True


ROUTES: tuple[RouteSpec, ...] = (
    RouteSpec("APP_PACS_*", "Sectra PACS", 50, "Imaging owns every APP_PACS_ group."),
    RouteSpec("VPN_*", NETWORK_ACCESS, 100, "Network team convention for remote access."),
    RouteSpec("FS_*", FILE_SHARES, 100, "Storage team convention for file shares."),
    RouteSpec(
        "FS_RADIOLOGY_*",
        "Sectra PACS",
        200,
        "Teaching files belong to Imaging. Lower priority than FS_*, and still wins: an "
        "application always outranks a service.",
    ),
    RouteSpec("PRINT_*", PRINTING, 110, "Print services convention."),
    RouteSpec("BADGE_*", PHYSICAL_ACCESS, 120, "Security office convention for door access."),
    RouteSpec("LIC_*", "Microsoft 365", 150, "Licence groups are managed with the tenant."),
    RouteSpec(
        "TEMP_*",
        FILE_SHARES,
        900,
        "Retired convention from the 2023 migration; kept for reference.",
        is_active=False,
    ),
)

#: The group whose access level is adopted out of its route, and what to call it.
ADOPTED_GROUP = "VPN_VENDOR_SUPPORT"
ADOPTED_LEVEL_NAME = "Vendor support VPN"
ADOPTED_LEVEL_DESCRIPTION = "Supervised remote sessions; requested per engagement."

#: A position default on a route-managed level, so drift can show defaults following a rename
#: and a retired level being deactivated rather than deleted.
ROUTED_DEFAULT_POSITION = "0500-7400"
ROUTED_DEFAULT_GROUP = RENAMED_GROUP
ROUTED_DEFAULT_REASON = "On-call remote access for Information Services"


# --- Drift ------------------------------------------------------------------------------


@dataclass(frozen=True)
class DriftStep:
    """One scripted change to the directory, and its inverse.

    Each step is what a real overnight sync would have *found*, not something anybody did in
    HealthIAM -- which is the point: the catalog has to notice on its own.
    """

    key: str
    headline: str
    detail: str


DRIFT_STEPS: tuple[DriftStep, ...] = (
    DriftStep(
        "rename",
        f"IT renames {ROUTED_DEFAULT_GROUP} to {ROUTED_DEFAULT_GROUP}_V2",
        "The mirror follows the rename by objectGUID and the route-managed level moves with "
        "it, keeping its position default. An access level names its group as free text, so "
        "without the rename being passed through, the level would have been stranded.",
    ),
    DriftStep(
        "deactivate",
        "APP_3M_CDI stops being returned by the group search",
        "3M 360 Encompass - CDI specialist turns 'Not returned by the last sync' and joins "
        "the broken-reference report and the dashboard tile.",
    ),
    DriftStep(
        "add",
        "A new VPN_RESEARCH_REMOTE group appears",
        "The VPN route holds it automatically: Network Access grows an access level nobody "
        "created by hand.",
    ),
    DriftStep(
        "login",
        "dpatel@demo.local leaves IAM-Users",
        "The managed login is deactivated rather than deleted, so its history survives.",
    ),
)

DRIFT_STEPS_BY_KEY = {step.key: step for step in DRIFT_STEPS}

DEACTIVATED_GROUP = "APP_3M_CDI"
DISABLED_LOGIN_SAM = "dpatel"


# --- Fabricated sync runs -----------------------------------------------------------------


@dataclass(frozen=True)
class RunSpec:
    """One `DirectorySyncRun` the seed fabricates, so the history pages have content.

    Identified by `(scope, status, trigger, server)`; all four seeded runs differ on that
    tuple, which is what keeps the seed idempotent without storing a marker anybody can see.
    """

    key: str
    scope: str
    status: str
    trigger: str
    note: str
    offsets: tuple[int, int] = (0, 0)
    fields: dict = field(default_factory=dict)


RUN_FIRST_IMPORT = "first_import"
RUN_NIGHTLY = "nightly"
RUN_FAILED = "failed"
RUN_PREVIEW = "preview"

#: Names in the never-applied preview, chosen so they are *not* in the mirror -- which is why
#: they are not, and what makes the Apply button on that run mean something.
PREVIEW_ONLY_GROUPS = ("FS_CARDIOLOGY_REPORTS", "PRINT_LAB_LABELS")

#: The directory entry the nightly run could not turn into a login.
BAD_MEMBER_DN = f"CN=Scanner Service,{SERVICE_OU}"
