"""The synthetic Entra ID tenant a demo install pretends to have synced from.

The cloud half of the demo organization: the people and groups of `data.py`'s demo.local, as
a hybrid tenant sees them once Entra Connect has synchronized them, plus what lives only in the
cloud -- cloud groups, cloud-only accounts, guests and external members.

As with the directory, there is no fake Graph. The dev settings point `ENTRA_AUTHORITY_HOST`
and `ENTRA_GRAPH_ENDPOINT` at `.invalid` hosts, which never resolve, and switch authority
validation off so MSAL goes straight to them instead of asking login.microsoftonline.com about
them first: **Test connection, Sync now and `manage.py sync_entra` fail** without reaching
Microsoft or sending a credential. What `seed_demo` writes is the *mirror* a sync would have
left behind -- `EntraGroup`, `EntraAccount` and `EntraSyncRun` rows -- through the sync's own
value helpers, so the source of every group and account is worked out exactly as a sync would,
and every page downstream runs its real code.

Data only, like `data.py`, and for the same reason: `config.settings.dev` imports it while the
settings are still being read. The writers are in `entra_mirror.py`.

Like the directory, the inventory reaches every status a sync can find a level in: one that is
fine, one whose group was deleted, one whose group became dynamic after the level was made and
one naming a group the tenant never returned; an AD group whose source of authority moved to
the cloud; the AD copy group writeback made of a cloud group; and an account in every worklist.
"""

from __future__ import annotations

import base64
import uuid
from dataclasses import dataclass

from . import data

# --- The tenant -------------------------------------------------------------------------

TENANT_NAME = "Demo Health"
INITIAL_DOMAIN = "demohealth.onmicrosoft.com"
#: The organization's mail domain, verified in the tenant: every seeded login and person already
#: uses it. demo.local cannot be verified, so Entra Connect gives synchronized users a UPN in the
#: initial domain instead -- the usual state of a tenant whose Active Directory was named .local.
MAIL_DOMAIN = "example.org"
DOMAINS = (INITIAL_DOMAIN, MAIL_DOMAIN)

# uuid5 over this namespace, as `data.py` does for GUIDs: every seed run produces the same
# object IDs, so a re-seed finds the rows it wrote and changes nothing.
NAMESPACE = uuid.uuid5(uuid.NAMESPACE_DNS, INITIAL_DOMAIN)
TENANT_ID = uuid.uuid5(NAMESPACE, "tenant")
CLIENT_ID = uuid.uuid5(NAMESPACE, "application:healthiam-sync")
#: A placeholder, like `data.BIND_PASSWORD`: without one `entra.W001` complains about the missing
#: credential on every `manage.py` command. Nothing ever receives it -- there is no host.
CLIENT_SECRET = "demo-only-not-a-real-secret"

AUTHORITY_HOST = "https://login.demohealth.invalid"
GRAPH_ENDPOINT = "https://graph.demohealth.invalid"
GRAPH_HOST = GRAPH_ENDPOINT.split("://", 1)[1]

#: The group excludes the dev settings use, and the ones `config/settings/test.py` uses, so the
#: reference statuses come out the same under both.
GROUP_EXCLUDE_PATTERNS = ["IAM-*"]


def group_id(name: str) -> uuid.UUID:
    return uuid.uuid5(NAMESPACE, f"group:{name}")


def user_id(key: str) -> uuid.UUID:
    return uuid.uuid5(NAMESPACE, f"user:{key}")


def immutable_id(sam: str) -> str:
    """The source anchor Entra Connect stamps on a synchronized account: its objectGUID, as the
    sixteen little-endian bytes Active Directory stores, in base64."""
    return base64.b64encode(data.user_guid(sam).bytes_le).decode()


# --- Groups -----------------------------------------------------------------------------


class Kind:
    """What `mailEnabled`, `securityEnabled` and `groupTypes` add up to."""

    SECURITY = "security"
    MAIL_SECURITY = "mail_security"
    M365 = "m365"
    DISTRIBUTION = "distribution"


@dataclass(frozen=True)
class CloudGroupSpec:
    name: str
    description: str = ""
    kind: str = Kind.SECURITY
    #: A membership rule makes the group dynamic.
    membership_rule: str = ""
    role_assignable: bool = False
    #: The AD group Entra Connect synchronizes this one from, by sAMAccountName.
    synced_from: str = ""
    #: Synchronized once, mastered in the cloud since: Graph reports `onPremisesSyncEnabled`
    #: null, as for a group born there, but keeps the on-premises name and SID.
    converted: bool = False
    #: ACTIVE, INACTIVE (mirrored, then no longer returned) or ABSENT (never returned).
    state: str = data.State.ACTIVE
    demonstrates: str = ""

    @property
    def object_id(self) -> uuid.UUID:
        return group_id(self.name)

    @property
    def mail_nickname(self) -> str:
        return self.name

    @property
    def mail(self) -> str:
        if self.kind == Kind.SECURITY:
            return ""
        return f"{self.name.lower()}@{MAIL_DOMAIN}"


#: The AD group whose source of authority moved to the cloud: licence groups are the first ones
#: Microsoft suggests moving. Its access level is the one Admin > Entra ID offers to convert.
CONVERTED_GROUP = "LIC_M365_E3"

# Entra Connect synchronizes OU=Groups but neither OU below it: Infrastructure is scoped out, as
# resource groups often are, and Cloud Groups holds what writeback wrote -- Connect never sends
# those back up. So not every AD group has a copy, which is what `ad_not_synced` is for.
SYNCED_GROUPS: tuple[CloudGroupSpec, ...] = tuple(
    CloudGroupSpec(
        spec.name,
        spec.description,
        kind=Kind.SECURITY if spec.group_type < 0 else Kind.DISTRIBUTION,
        synced_from=spec.name,
        converted=spec.name == CONVERTED_GROUP,
        demonstrates=(
            "its source of authority moved to the cloud: the conversion worklist"
            if spec.name == CONVERTED_GROUP
            else ""
        ),
    )
    for spec in data.GROUPS
    if spec.ou == data.GROUPS_OU and spec.state == data.State.ACTIVE
)

CLOUD_GROUPS: tuple[CloudGroupSpec, ...] = (
    CloudGroupSpec(
        "LIC_M365_COPILOT",
        "Microsoft 365 Copilot add-on licences (group-based licensing)",
        demonstrates="an Entra-group level with a position default: the healthy case",
    ),
    CloudGroupSpec(
        data.WRITTEN_BACK_FROM,
        "File share: nursing education materials. Written back to AD for the file server.",
        demonstrates="written back to AD; the catalog references the cloud group, not the copy",
    ),
    CloudGroupSpec(
        "Teams-Pharmacy-Informatics",
        "Pharmacy informatics team: Teams, SharePoint and Planner",
        kind=Kind.M365,
        demonstrates="unreferenced: the Entra adoption worklist",
    ),
    CloudGroupSpec(
        "MESG-Pharmacy-Alerts",
        "Mail-enabled security group: pharmacy on-call alerts and their share",
        kind=Kind.MAIL_SECURITY,
    ),
    CloudGroupSpec(
        "DYN-All-Nursing-Staff",
        "Everyone in Nursing, kept by a membership rule",
        membership_rule='(user.department -eq "Nursing") -and (user.accountEnabled -eq true)',
        demonstrates="dynamic, so never a level; the one level made before the rule is broken",
    ),
    CloudGroupSpec(
        "PIM-Helpdesk-Administrators",
        "Eligible for the Helpdesk Administrator role through PIM",
        role_assignable=True,
        demonstrates="role-assignable: refused as an access level",
    ),
    CloudGroupSpec(
        "DL-Medical-Staff-Announcements",
        "Announcements to the medical staff",
        kind=Kind.DISTRIBUTION,
        demonstrates="a cloud distribution list: grants nothing",
    ),
    CloudGroupSpec(
        "LIC_TEAMS_PHONE_PILOT",
        "Teams Phone pilot licences",
        state=data.State.INACTIVE,
        demonstrates="deleted after the pilot: its level keeps a position default",
    ),
    CloudGroupSpec(
        "LIC_POWERBI_PRO",
        "Power BI Pro licences",
        state=data.State.ABSENT,
        demonstrates="Not found in Entra ID: the level holds an object ID nobody can find",
    ),
)

GROUPS: tuple[CloudGroupSpec, ...] = SYNCED_GROUPS + CLOUD_GROUPS
GROUPS_BY_NAME = {spec.name: spec for spec in GROUPS}
#: Display names a seeded `entra_group` access level may reference.
KNOWN_GROUP_NAMES = frozenset(GROUPS_BY_NAME)
MIRRORED_GROUPS = tuple(spec for spec in GROUPS if spec.state != data.State.ABSENT)


# --- Accounts ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AccountSpec:
    """One user as Graph would list it."""

    key: str
    upn: str
    first_name: str
    last_name: str
    mail: str = ""
    display_name: str = ""
    job_title: str = ""
    department: str = ""
    company: str = ""
    employee_id: str = ""
    #: `userType`: Member or Guest.
    user_type: str = "Member"
    #: `creationType`: Invitation for anyone invited through B2B.
    creation_type: str = ""
    #: `externalUserState`: Accepted, PendingAcceptance, or "" for the organization's own.
    invitation: str = ""
    #: The identity provider an external account signs in with, as Graph names its issuer;
    #: "" until the invitation is redeemed.
    issuer: str = ""
    enabled: bool = True
    #: The sAMAccountName in demo.local while Entra Connect synchronizes the account.
    synced_from: str = ""
    converted: bool = False
    #: Set by hand in HealthIAM; the sync never changes it.
    kind: str = "user"
    #: Relative to the seed, so the worklists that count days hold whenever it runs; None means
    #: "fixed": `data.ACCOUNT_CREATED` for the created date, "never" for the sign-in.
    created_days_ago: int | None = None
    last_sign_in_days_ago: int | None = None
    #: Linked by hand to this person (first, last): nothing the sync compares would find them.
    linked_by_hand_to: tuple[str, str] | None = None
    #: The person the sync is expected to link it to by e-mail, for the tests; never stored.
    person: str = ""
    demonstrates: str = ""

    @property
    def object_id(self) -> uuid.UUID:
        return user_id(self.key)

    @property
    def name(self) -> str:
        return self.display_name or f"{self.first_name} {self.last_name}".strip()

    @property
    def is_external(self) -> bool:
        return self.user_type == "Guest" or "#EXT#" in self.upn


def external_upn(mail: str) -> str:
    """The UPN B2B gives an invited user: the address with @ as _, then #EXT#."""
    return f"{mail.replace('@', '_')}#EXT#@{INITIAL_DOMAIN}"


def _mail_of(first: str, last: str) -> str:
    """The address `seed_demo` gives every person of the organization."""
    return f"{first}.{last}@{MAIL_DOMAIN}".lower().replace(" ", "")


#: How long ago the synchronized members last signed in, where not yesterday.
MEMBER_SIGN_IN_DAYS_AGO = {"jhaddad": 200, "pgrant": 32, "nvale": 12}


def _synced(spec) -> AccountSpec:
    is_staff = isinstance(spec, data.StaffSpec)
    return AccountSpec(
        spec.sam,
        f"{spec.sam}@{INITIAL_DOMAIN}",
        spec.first_name,
        spec.last_name,
        mail=_mail_of(spec.first_name, spec.last_name),
        job_title=spec.job_title if is_staff else spec.title,
        department=spec.department_name if is_staff else spec.department,
        employee_id=spec.employee_id,
        enabled=spec.enabled,
        synced_from=spec.sam,
        last_sign_in_days_ago=MEMBER_SIGN_IN_DAYS_AGO.get(spec.sam, 1),
    )


# Every person's account in demo.local; the service account is kept out of synchronization.
# The sync links them by employee ID, like the AD account mirror.
SYNCED_ACCOUNTS: tuple[AccountSpec, ...] = tuple(
    _synced(spec)
    for spec in (*data.STAFF, *data.ACCOUNTS)
    if getattr(spec, "kind", "user") == "user"
)

MEMBERS: tuple[AccountSpec, ...] = (
    AccountSpec(
        "gito",
        f"grace.ito@{MAIL_DOMAIN}",
        "Grace",
        "Ito",
        mail=f"grace.ito@{MAIL_DOMAIN}",
        job_title="Director, Information Services",
        department="Information Services",
        employee_id="E1010",
        synced_from="gito",
        converted=True,
        last_sign_in_days_ago=1,
        person="Grace Ito",
        demonstrates="synchronized once, mastered in the cloud since: Cloud member (was synced)",
    ),
    AccountSpec(
        "ravi.menon",
        f"ravi.menon@{MAIL_DOMAIN}",
        "Ravi",
        "Menon",
        mail=f"ravi.menon@{MAIL_DOMAIN}",
        job_title="Epic analyst (contractor)",
        department="Information Services",
        last_sign_in_days_ago=1,
        linked_by_hand_to=("Ravi", "Menon"),
        person="Ravi Menon",
        demonstrates="a cloud-only contractor with no employee ID: linked by hand",
    ),
    AccountSpec(
        "breakglass01",
        f"breakglass01@{INITIAL_DOMAIN}",
        "Emergency",
        "Access 01",
        display_name="Emergency Access 01",
        kind="admin",
        demonstrates="an emergency-access account marked Admin, so no worklist asks whose it is",
    ),
    AccountSpec(
        "pharmacy.alerts",
        f"pharmacy.alerts@{MAIL_DOMAIN}",
        "",
        "",
        display_name="Pharmacy Alerts",
        mail=f"pharmacy.alerts@{MAIL_DOMAIN}",
        enabled=False,
        kind="shared",
        demonstrates="a shared mailbox: Exchange blocks its sign-in",
    ),
)


def _guest(key, mail, first, last, issuer, **fields) -> AccountSpec:
    fields.setdefault("invitation", "Accepted")
    return AccountSpec(
        key,
        external_upn(mail),
        first,
        last,
        mail=mail,
        user_type="Guest",
        creation_type="Invitation",
        issuer=issuer if fields["invitation"] == "Accepted" else "",
        **fields,
    )


#: The people these accounts belong to carry the same address in `seed_demo`, which is what
#: links a guest: they rarely have an employee ID of ours.
GUESTS: tuple[AccountSpec, ...] = (
    _guest(
        "dana.fox",
        "dana.fox@epic.example",
        "Dana",
        "Fox",
        "ExternalAzureAD",
        company="Epic Systems",
        job_title="Implementation consultant",
        created_days_ago=25,
        last_sign_in_days_ago=2,
        person="Dana Fox",
        demonstrates="a guest from another Entra tenant, linked by e-mail: the healthy case",
    ),
    _guest(
        "chloe.martin",
        "chloe.martin@gmail.example",
        "Chloe",
        "Martin",
        "google.com",
        company="Aya Healthcare",
        job_title="Travel RN",
        created_days_ago=80,
        last_sign_in_days_ago=1,
        person="Chloe Martin",
        demonstrates="a traveler invited at the personal address the agency gave: Google",
    ),
    _guest(
        "lily.zhang",
        "lily.zhang@stateu.example",
        "Lily",
        "Zhang",
        "mail",
        invitation="PendingAcceptance",
        company="State University College of Nursing",
        job_title="Student nurse",
        created_days_ago=45,
        person="Lily Zhang",
        demonstrates="invited 45 days ago for a start next week: pending too long, not orphaned",
    ),
    _guest(
        "ruth.adler",
        "ruth.adler@mailbox.example",
        "Ruth",
        "Adler",
        "mail",
        job_title="Volunteer",
        created_days_ago=400,
        last_sign_in_days_ago=140,
        person="Ruth Adler",
        demonstrates="signs in with a one-time passcode, last 140 days ago: a stale guest",
    ),
    _guest(
        "ines.duarte",
        "ines.duarte@ayahealthcare.example",
        "Ines",
        "Duarte",
        "ExternalAzureAD",
        company="Aya Healthcare",
        job_title="Travel RN",
        created_days_ago=115,
        last_sign_in_days_ago=22,
        person="Ines Duarte",
        demonstrates="her contract ended three weeks ago, the account did not: orphaned",
    ),
    _guest(
        "kofi.mensah",
        "kofi.mensah@stlukes.example",
        "Kofi",
        "Mensah",
        # A SAML/WS-Fed partner: the issuer is the partner's own domain.
        "stlukes.example",
        company="St. Luke's Radiology Partners",
        job_title="Radiologist (teleradiology)",
        created_days_ago=60,
        last_sign_in_days_ago=3,
        demonstrates="a federated partner nobody has a person record for: Create person",
    ),
    _guest(
        "erik.lund",
        "erik.lund@outlook.example",
        "Erik",
        "Lund",
        "MicrosoftAccount",
        company="Sectra",
        job_title="Field engineer",
        enabled=False,
        created_days_ago=300,
        last_sign_in_days_ago=250,
        demonstrates="sign-in blocked after the PACS upgrade, never deleted",
    ),
    AccountSpec(
        "marcus.bell",
        external_upn("marcus.bell@lakesidephysicians.example"),
        "Marcus",
        "Bell",
        display_name="Marcus Bell, MD",
        mail="marcus.bell@lakesidephysicians.example",
        user_type="Member",
        creation_type="Invitation",
        invitation="Accepted",
        issuer="ExternalAzureAD",
        company="Lakeside Physicians",
        job_title="Affiliated physician",
        created_days_ago=400,
        last_sign_in_days_ago=5,
        person="Marcus Bell",
        demonstrates="an external member: a B2B user made a member, still signing in at home",
    ),
)

ACCOUNTS: tuple[AccountSpec, ...] = SYNCED_ACCOUNTS + MEMBERS + GUESTS

# --- Routes -----------------------------------------------------------------------------

#: Entra group routes, matched against display names. Advisory only -- Microsoft 365 does not
#: hold cloud groups automatically -- so the seed creates no level from it: it pre-selects the
#: target for Teams-Pharmacy-Informatics on Entra groups > Add to catalog, and leaves
#: MESG-Pharmacy-Alerts as the group no route matches.
ROUTES: tuple[data.RouteSpec, ...] = (
    data.RouteSpec(
        "Teams-*", "Microsoft 365", 100, "Teams belong with the tenant they are created in."
    ),
)
ACCOUNTS_BY_KEY = {spec.key: spec for spec in ACCOUNTS}

#: Blocked in Entra ID since last night's run, which is what that run recorded about it.
DISABLED_LAST_NIGHT = "erik.lund"


# --- Fabricated sync runs -----------------------------------------------------------------

#: What a secret that expired looks like on a run: the scheduled sync the night before last. The
#: secret was renewed the next morning, so last night's run went through.
EXPIRED_SECRET_ERROR = (
    "GraphAuthError: invalid_client: AADSTS7000222: The provided client secret keys for app "
    f"'{CLIENT_ID}' are expired. Visit the Azure portal to create new keys for your app: "
    "https://aka.ms/NewClientSecret, or consider using certificate credentials for added "
    "security: https://aka.ms/certCreds."
)
