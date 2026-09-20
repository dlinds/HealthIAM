"""Populate a development database with realistic sample data. Idempotent.

The Active Directory half is a fiction written straight into the mirror: there is no fake
LDAP server, so Test connection and Sync now still fail honestly. `apps.core.demo.data`
describes the synthetic domain and `apps.core.demo.mirror` writes it; `manage.py demo_ad`
drifts it afterwards. See `docs/ad-setup.md` section 12.
"""

import datetime as dt
import logging

from django.conf import settings
from django.contrib.auth.models import Group
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from apps.access import services
from apps.access.models import PositionDefault
from apps.accounts import roles
from apps.accounts.models import User
from apps.catalog import services as catalog_services
from apps.catalog.models import (
    AccessLevel,
    Application,
    ApplicationAlias,
    ApplicationAnalyst,
    ApplicationContact,
    Contact,
    SupportTier,
    Vendor,
)
from apps.core.demo import data as demo
from apps.core.demo import mirror
from apps.directory import reconcile
from apps.directory.models import ADGroup, ADGroupRoute, DirectorySyncRun
from apps.directory.sync import MISSING_GROUP_MESSAGE, SyncResult
from apps.orgs.models import Department, JobCode, Position, Source

PASSWORD = "healthiam"

DEPARTMENTS = [
    ("0100", "Nursing"),
    ("0200", "Pharmacy"),
    ("0300", "Radiology"),
    ("0400", "Health Information Management"),
    ("0500", "Information Services"),
    ("0600", "Patient Access"),
    ("0700", "Laboratory"),
    ("0800", "Finance"),
]

JOB_CODES = [
    ("7000", "Registered Nurse"),
    ("7001", "Licensed Practical Nurse"),
    ("7002", "Nurse Manager"),
    ("7100", "Pharmacist"),
    ("7101", "Pharmacy Technician"),
    ("7200", "Radiologic Technologist"),
    ("7300", "Coding Specialist"),
    ("7400", "Systems Analyst"),
    ("7500", "Patient Access Representative"),
    ("7600", "Medical Technologist"),
    ("7700", "Financial Analyst"),
    ("9000", "Department Director"),
]

POSITIONS = [
    ("0100", "7000"),
    ("0100", "7001"),
    ("0100", "7002"),
    ("0100", "9000"),
    ("0200", "7100"),
    ("0200", "7101"),
    ("0200", "9000"),
    ("0300", "7200"),
    ("0300", "7000"),
    ("0300", "9000"),
    ("0400", "7300"),
    ("0400", "9000"),
    ("0500", "7400"),
    ("0500", "9000"),
    ("0600", "7500"),
    ("0700", "7600"),
    ("0800", "7700"),
    ("0800", "9000"),
]

VENDORS = {
    "Epic Systems": ("https://www.epic.com", "608-271-9000", "support@epic.example"),
    "Sectra": ("https://sectra.com", "", "support@sectra.example"),
    "Omnicell": ("https://www.omnicell.com", "800-910-2220", ""),
    "3M Health Information Systems": ("https://www.3m.com", "", "his-support@3m.example"),
    "Microsoft": ("https://www.microsoft.com", "", ""),
    "ServiceNow": ("https://www.servicenow.com", "", ""),
    "UKG": ("https://www.ukg.com", "", "support@ukg.example"),
    "Oracle Health": ("https://www.oracle.com/health", "", ""),
}


class Command(BaseCommand):
    help = "Load demo departments, job codes, positions, applications, and defaults."

    @transaction.atomic
    def handle(self, *args, **options):
        # Saving routes and access levels queues a deferred full reconcile on
        # `transaction.on_commit`, which would run after this command has already printed its
        # summary, swallow its own errors, and never run at all under a test that rolls its
        # transaction back. Suppress the signals and call the reconciler directly instead, so
        # the seeded world is the same whoever is looking at it.
        with reconcile.suppressed():
            groups = {name: Group.objects.get_or_create(name=name)[0] for name in roles.GROUP_ROLES}
            users = self._users(groups)
            depts, jobs, positions = self._orgs()
            vendors = {name: self._vendor(name, *vals) for name, vals in VENDORS.items()}
            contacts = self._contacts(vendors, users)
            apps = self._applications(vendors, contacts, users)
            self._defaults(apps, positions, users["admin"])
            counts = self._directory(users, positions)
        self.stdout.write(self.style.SUCCESS("Demo data loaded."))
        self.stdout.write(
            "Sign in with one of: admin / analyst.epic / analyst.imaging / owner.epic / "
            f"helpdesk / auditor  (password: {PASSWORD})"
        )
        self._report_directory(counts)

    def _report_directory(self, counts):
        """Say what the directory half of the seed did, and what it cannot do."""
        if not settings.AD_ENABLED:
            self.stdout.write(
                self.style.WARNING(
                    "Active Directory is disabled, so the seeded AD groups, routes, badges and "
                    "the Admin > Active Directory page stay hidden. Development turns the demo "
                    "directory on by itself; under production settings, copy the demo block at "
                    "the end of the Active Directory section of .env.example into .env."
                )
            )
            return
        self.stdout.write(
            "Demo directory: {groups} group(s) ({inactive} inactive), {routes} route(s), "
            "{route_levels} route-managed level(s), {logins} AD-managed login(s), "
            "{runs} sync run(s).".format(**counts)
        )
        if settings.AD_SERVER_URIS == [demo.SERVER_URI]:
            self.stdout.write(
                f"Active Directory is on with the synthetic demo directory ({demo.SERVER}). "
                "Test connection, Sync now and `manage.py sync_ad` fail: there is no domain "
                "controller. Simulate an overnight sync with `manage.py demo_ad drift`."
            )
        else:
            self.stdout.write(
                self.style.WARNING(
                    f"AD_SERVER_URIS points at {', '.join(settings.AD_SERVER_URIS)}, not the "
                    "demo directory. The synthetic groups and logins this seed just wrote are "
                    "not in that directory, so the next real sync will deactivate them. Do not "
                    "seed demo data on a real instance."
                )
            )

    # --- helpers ------------------------------------------------------------------

    def _user(self, username, first, last, group=None, **extra):
        user, created = User.objects.get_or_create(
            username=username,
            defaults={
                "first_name": first,
                "last_name": last,
                "email": f"{username}@example.org",
                **extra,
            },
        )
        if created:
            user.set_password(PASSWORD)
            user.save()
        if group:
            user.groups.add(group)
        return user

    def _users(self, groups):
        return {
            "admin": self._user(
                "admin", "Sam", "Rivera", groups[roles.ADMIN], is_staff=True, is_superuser=True
            ),
            "iam": self._user("iam.lee", "Jordan", "Lee", groups[roles.ADMIN]),
            "analyst_epic": self._user("analyst.epic", "Priya", "Natarajan"),
            "analyst_imaging": self._user("analyst.imaging", "Marcus", "Okafor"),
            "owner_epic": self._user("owner.epic", "Dana", "Whitfield"),
            "helpdesk": self._user("helpdesk", "Casey", "Nguyen", groups[roles.HELP_DESK]),
            "auditor": self._user("auditor", "Robin", "Alvarez", groups[roles.AUDITOR]),
        }

    def _orgs(self):
        depts = {
            code: Department.objects.get_or_create(
                code=code, defaults={"name": name, "source": Source.HR}
            )[0]
            for code, name in DEPARTMENTS
        }
        jobs = {
            code: JobCode.objects.get_or_create(
                code=code, defaults={"title": title, "source": Source.HR}
            )[0]
            for code, title in JOB_CODES
        }
        positions = {}
        for d, j in POSITIONS:
            pos, _ = Position.objects.get_or_create(
                department=depts[d], job_code=jobs[j], defaults={"source": Source.HR}
            )
            positions[pos.code] = pos
        return depts, jobs, positions

    def _vendor(self, name, website, phone, email):
        vendor, _ = Vendor.objects.get_or_create(
            name=name,
            defaults={"website": website, "support_phone": phone, "support_email": email},
        )
        return vendor

    def _contact(self, name, **fields):
        contact, _ = Contact.objects.get_or_create(name=name, defaults=fields)
        return contact

    def _contacts(self, vendors, users):
        return {
            "cno": self._contact(
                "Dana Whitfield",
                title="Chief Nursing Officer",
                team="Nursing Administration",
                email="owner.epic@example.org",
                user=users["owner_epic"],
            ),
            "cio": self._contact(
                "Alex Morgan", title="CIO", team="Information Services", email="amorgan@example.org"
            ),
            "clin_apps": self._contact(
                "Clinical Applications Team",
                team="Information Services",
                email="clinapps@example.org",
                phone="x4400",
            ),
            "service_desk": self._contact(
                "IS Service Desk",
                team="Information Services",
                email="help@example.org",
                phone="x4357",
            ),
            "imaging_mgr": self._contact(
                "Lena Fischer",
                title="Imaging Informatics Manager",
                team="Radiology",
                email="lfischer@example.org",
            ),
            "pharm_dir": self._contact(
                "Omar Haddad",
                title="Director of Pharmacy",
                team="Pharmacy",
                email="ohaddad@example.org",
            ),
            "epic_tam": self._contact(
                "Epic Technical Services",
                vendor=vendors["Epic Systems"],
                email="ts@epic.example",
                phone="608-271-9000",
            ),
            "sectra_support": self._contact(
                "Sectra Support Desk", vendor=vendors["Sectra"], email="support@sectra.example"
            ),
        }

    def _app(self, name, **fields):
        aliases = fields.pop("aliases", [])
        levels = fields.pop("levels", [])
        analysts = fields.pop("analysts", [])
        tiers = fields.pop("tiers", [])
        contacts = fields.pop("contacts", [])
        app, _ = Application.objects.get_or_create(name=name, defaults=fields)
        for alias in aliases:
            ApplicationAlias.objects.get_or_create(application=app, alias=alias)
        for i, (lname, model, target, desc) in enumerate(levels, start=1):
            defaults = {"access_model": model, "description": desc, "sort_order": i * 10}
            if model == AccessLevel.AccessModel.AD_GROUP:
                defaults["ad_group_name"] = target
            elif model == AccessLevel.AccessModel.TICKET:
                defaults["ticket_assignment_team"] = target
            elif model == AccessLevel.AccessModel.IN_APP:
                defaults["in_app_instructions"] = target
            AccessLevel.objects.get_or_create(application=app, name=lname, defaults=defaults)
        for i, user in enumerate(analysts):
            ApplicationAnalyst.objects.get_or_create(
                application=app, user=user, defaults={"is_primary": i == 0}
            )
        for level, tname, contact, hours in tiers:
            SupportTier.objects.get_or_create(
                application=app,
                level=level,
                defaults={"name": tname, "contact": contact, "hours": hours},
            )
        for contact, role in contacts:
            ApplicationContact.objects.get_or_create(application=app, contact=contact, role=role)
        return app

    def _applications(self, vendors, contacts, users):
        AD, TICKET, IN_APP = (
            AccessLevel.AccessModel.AD_GROUP,
            AccessLevel.AccessModel.TICKET,
            AccessLevel.AccessModel.IN_APP,
        )
        apps = {}
        apps["Epic"] = self._app(
            "Epic",
            description=(
                "Electronic health record: clinical documentation, orders, registration, "
                "and billing."
            ),
            vendor=vendors["Epic Systems"],
            website="https://epic.example.org",
            tier=1,
            go_live_date=dt.date(2018, 3, 1),
            holds_phi=True,
            holds_pii=True,
            holds_clinical_records=True,
            holds_pci=True,
            data_description="Full patient record, demographics, insurance and payment card data.",
            host_location=Application.HostLocation.VENDOR,
            host_details="Epic Hosting, Verona WI",
            auth_method=Application.AuthMethod.SSO_SAML,
            mfa_enforced=True,
            rto_hours=4,
            maintenance_window="Sun 02:00–06:00",
            dr_status=Application.DRStatus.TESTED,
            contract_renewal_date=dt.date(2027, 6, 30),
            cost_center="IS-CLIN",
            business_owner=contacts["cno"],
            technical_owner=contacts["clin_apps"],
            aliases=["EHR", "Hyperspace", "MyChart"],
            levels=[
                (
                    "Clinical – Nurse",
                    IN_APP,
                    "Assign the RN template in Epic user admin; requires unit security class.",
                    "Inpatient nursing workflows",
                ),
                (
                    "Clinical – Provider",
                    IN_APP,
                    "Assign provider template; NPI required.",
                    "Ordering and documentation",
                ),
                (
                    "Registration",
                    IN_APP,
                    "Assign Prelude/Cadence template.",
                    "Scheduling and registration",
                ),
                ("Coding", IN_APP, "Assign HIM coding template.", "Chart coding and abstracting"),
                (
                    "Read-only chart review",
                    IN_APP,
                    "Assign chart review template.",
                    "View-only access",
                ),
                # Its AD group is mirrored but deactivated, so this level carries the
                # "Not returned by the last sync" badge straight out of the seed.
                (
                    "Research chart review",
                    AD,
                    "APP_EPIC_RESEARCH",
                    "Chart access for IRB-approved studies",
                ),
            ],
            analysts=[users["analyst_epic"]],
            tiers=[
                (1, "IS Service Desk", contacts["service_desk"], "24x7"),
                (
                    2,
                    "Clinical Applications",
                    contacts["clin_apps"],
                    "M–F 7a–6p, on-call after hours",
                ),
                (3, "Epic Technical Services", contacts["epic_tam"], "24x7"),
            ],
            contacts=[(contacts["epic_tam"], ApplicationContact.Role.VENDOR_SUPPORT)],
        )
        apps["PACS"] = self._app(
            "Sectra PACS",
            description="Picture archiving and communication system for diagnostic imaging.",
            vendor=vendors["Sectra"],
            tier=1,
            holds_phi=True,
            holds_clinical_records=True,
            host_location=Application.HostLocation.ONSITE,
            host_details="Main data center, VMware cluster B",
            auth_method=Application.AuthMethod.AD_LDAP,
            mfa_enforced=False,
            rto_hours=2,
            dr_status=Application.DRStatus.PLANNED,
            business_owner=contacts["imaging_mgr"],
            technical_owner=contacts["clin_apps"],
            aliases=["PACS", "IDS7"],
            levels=[
                ("Radiologist", AD, "APP_PACS_RADIOLOGIST", "Full diagnostic read"),
                ("Technologist", AD, "APP_PACS_TECH", "Acquire and QA images"),
                ("Clinical viewer", AD, "APP_PACS_VIEW", "View images and reports"),
            ],
            analysts=[users["analyst_imaging"]],
            tiers=[
                (1, "IS Service Desk", contacts["service_desk"], "24x7"),
                (2, "Imaging Informatics", contacts["imaging_mgr"], "M–F 7a–5p"),
                (3, "Sectra Support", contacts["sectra_support"], "24x7"),
            ],
            contacts=[(contacts["sectra_support"], ApplicationContact.Role.VENDOR_SUPPORT)],
        )
        apps["Omnicell"] = self._app(
            "Omnicell",
            description="Automated dispensing cabinets and pharmacy inventory.",
            vendor=vendors["Omnicell"],
            tier=2,
            holds_phi=True,
            host_location=Application.HostLocation.ONSITE,
            auth_method=Application.AuthMethod.LOCAL,
            mfa_enforced=False,
            rto_hours=8,
            business_owner=contacts["pharm_dir"],
            technical_owner=contacts["clin_apps"],
            levels=[
                (
                    "Nurse – cabinet access",
                    IN_APP,
                    "Create user in OmniCenter and assign to unit cabinets.",
                    "Remove medications at unit cabinets",
                ),
                (
                    "Pharmacist",
                    IN_APP,
                    "Assign pharmacist role in OmniCenter.",
                    "Fill, override review",
                ),
                (
                    "Pharmacy technician",
                    IN_APP,
                    "Assign tech role in OmniCenter.",
                    "Restock cabinets",
                ),
            ],
            analysts=[users["analyst_epic"]],
            tiers=[
                (1, "IS Service Desk", contacts["service_desk"], "24x7"),
                (2, "Pharmacy Informatics", contacts["pharm_dir"], "M–F"),
            ],
        )
        apps["3M"] = self._app(
            "3M 360 Encompass",
            description="Computer-assisted coding and clinical documentation integrity.",
            vendor=vendors["3M Health Information Systems"],
            tier=2,
            holds_phi=True,
            host_location=Application.HostLocation.VENDOR,
            auth_method=Application.AuthMethod.SSO_SAML,
            mfa_enforced=True,
            business_owner=contacts["cio"],
            aliases=["360", "CAC"],
            levels=[
                ("Coder", AD, "APP_3M_CODER", "Code encounters"),
                ("CDI specialist", AD, "APP_3M_CDI", "Documentation queries"),
            ],
            analysts=[users["analyst_epic"]],
            tiers=[(1, "IS Service Desk", contacts["service_desk"], "24x7")],
        )
        apps["M365"] = self._app(
            "Microsoft 365",
            description="Email, Teams, OneDrive, and Office.",
            vendor=vendors["Microsoft"],
            tier=1,
            holds_pii=True,
            holds_employee_data=True,
            host_location=Application.HostLocation.VENDOR,
            auth_method=Application.AuthMethod.SSO_OIDC,
            mfa_enforced=True,
            technical_owner=contacts["cio"],
            aliases=["O365", "Outlook", "Teams"],
            levels=[
                ("Standard user (E3)", AD, "LIC_M365_E3", "Mailbox, Teams, OneDrive"),
                ("Frontline (F3)", AD, "LIC_M365_F3", "Web/mobile Office, 2 GB mailbox"),
                # Its group name matches an exclude pattern, so the sync never imports it and
                # the level reads "Outside sync filter" rather than claiming the group is
                # gone -- the warning .env.example gives about narrow filters, made visible.
                (
                    "Visio 2013 (retired licence)",
                    AD,
                    "LIC_RETIRED_VISIO_2013",
                    "Legacy licence group kept out of the sync filter",
                ),
            ],
            analysts=[users["iam"]],
            tiers=[(1, "IS Service Desk", contacts["service_desk"], "24x7")],
        )
        apps["ServiceNow"] = self._app(
            "ServiceNow",
            description="IT service management: incidents, requests, and change.",
            vendor=vendors["ServiceNow"],
            tier=2,
            holds_employee_data=True,
            host_location=Application.HostLocation.VENDOR,
            auth_method=Application.AuthMethod.SSO_SAML,
            mfa_enforced=True,
            technical_owner=contacts["cio"],
            aliases=["SNOW"],
            levels=[
                ("Requester", AD, "APP_SNOW_USER", "Submit and track tickets"),
                ("ITIL fulfiller", TICKET, "IS Service Management", "Work assigned tickets"),
            ],
            analysts=[users["iam"]],
            tiers=[(1, "IS Service Desk", contacts["service_desk"], "24x7")],
        )
        apps["UKG"] = self._app(
            "UKG Dimensions",
            description="Timekeeping and scheduling.",
            vendor=vendors["UKG"],
            tier=2,
            holds_pii=True,
            holds_employee_data=True,
            host_location=Application.HostLocation.VENDOR,
            auth_method=Application.AuthMethod.SSO_SAML,
            mfa_enforced=True,
            aliases=["Kronos", "Timekeeping"],
            levels=[
                ("Employee", AD, "APP_UKG_EMPLOYEE", "Clock in/out, view schedule"),
                ("Manager", TICKET, "HRIS Team", "Approve timecards and schedules"),
            ],
            analysts=[users["iam"]],
            tiers=[
                (1, "IS Service Desk", contacts["service_desk"], "24x7"),
                (2, "HRIS", contacts["cio"], "M–F"),
            ],
        )
        apps["Meditech"] = self._app(
            "Meditech Magic",
            description="Legacy EHR retained for record retrieval only.",
            vendor=vendors["Oracle Health"],
            tier=4,
            lifecycle_status=Application.Lifecycle.RETIRED,
            sunset_date=dt.date(2019, 1, 31),
            holds_phi=True,
            holds_clinical_records=True,
            host_location=Application.HostLocation.ONSITE,
            auth_method=Application.AuthMethod.LOCAL,
            levels=[("Read-only", IN_APP, "Contact HIM.", "Historical lookups")],
        )
        # Services: a home for the AD groups no vendor application owns. Separate rows per
        # owning team rather than one bucket, so analyst rights stay scoped per team. None of
        # them declares its access levels here -- `Network Access` gets them from its route,
        # and the rest are the worklist that "Add to catalog" adopts from.
        apps[demo.NETWORK_ACCESS] = self._app(
            demo.NETWORK_ACCESS,
            kind=Application.Kind.SERVICE,
            dynamic_ad_groups=True,
            description=(
                "Remote access: VPN profiles managed by the network team. Holds every "
                "VPN_ group automatically."
            ),
            tier=2,
            host_location=Application.HostLocation.ONSITE,
            auth_method=Application.AuthMethod.AD_LDAP,
            mfa_enforced=True,
            technical_owner=contacts["cio"],
            analysts=[users["iam"]],
            tiers=[(1, "IS Service Desk", contacts["service_desk"], "24x7")],
        )
        apps[demo.FILE_SHARES] = self._app(
            demo.FILE_SHARES,
            kind=Application.Kind.SERVICE,
            description="Departmental file shares on the main file cluster.",
            tier=3,
            host_location=Application.HostLocation.ONSITE,
            auth_method=Application.AuthMethod.AD_LDAP,
            technical_owner=contacts["cio"],
            analysts=[users["iam"]],
        )
        apps[demo.PRINTING] = self._app(
            demo.PRINTING,
            kind=Application.Kind.SERVICE,
            description="Print queues published by the print servers.",
            tier=4,
            host_location=Application.HostLocation.ONSITE,
            auth_method=Application.AuthMethod.AD_LDAP,
        )
        apps[demo.PHYSICAL_ACCESS] = self._app(
            demo.PHYSICAL_ACCESS,
            kind=Application.Kind.SERVICE,
            description="Badge access to controlled doors, managed by the security office.",
            tier=3,
            host_location=Application.HostLocation.ONSITE,
            auth_method=Application.AuthMethod.LOCAL,
        )
        return apps

    def _defaults(self, apps, positions, actor):
        def level(app_key, name):
            return AccessLevel.objects.get(application=apps[app_key], name=name)

        plan = [
            ("0100-7000", "Epic", "Clinical – Nurse", "Standard for all inpatient RNs"),
            ("0100-7000", "Omnicell", "Nurse – cabinet access", "Medication administration"),
            ("0100-7000", "PACS", "Clinical viewer", "View imaging at bedside"),
            (
                "0100-7001",
                "Epic",
                "Clinical – Nurse",
                "LPN scope; same template with restricted orders",
            ),
            ("0100-7001", "Omnicell", "Nurse – cabinet access", "Medication administration"),
            ("0100-7002", "Epic", "Clinical – Nurse", "Managers keep clinical access"),
            ("0100-7002", "UKG", "Manager", "Approves unit timecards"),
            ("0100-9000", "Epic", "Read-only chart review", "Oversight only"),
            ("0100-9000", "UKG", "Manager", "Department leadership"),
            ("0200-7100", "Epic", "Clinical – Provider", "Pharmacist verification workflows"),
            ("0200-7100", "Omnicell", "Pharmacist", "Cabinet management"),
            ("0200-7101", "Omnicell", "Pharmacy technician", "Restocking"),
            ("0300-7200", "PACS", "Technologist", "Image acquisition"),
            ("0300-7200", "Epic", "Read-only chart review", "Verify orders"),
            ("0300-7000", "PACS", "Clinical viewer", "Radiology nursing"),
            ("0300-7000", "Epic", "Clinical – Nurse", "Radiology nursing"),
            ("0400-7300", "Epic", "Coding", "Coding and abstracting"),
            ("0400-7300", "3M", "Coder", "Computer-assisted coding"),
            ("0500-7400", "ServiceNow", "ITIL fulfiller", "Works IS tickets"),
            ("0600-7500", "Epic", "Registration", "Front-desk registration"),
            ("0800-7700", "UKG", "Employee", "Timekeeping"),
        ]
        for code, app_key, level_name, reason in plan:
            pos = positions[code]
            lvl = level(app_key, level_name)
            if PositionDefault.objects.filter(position=pos, access_level=lvl).exists():
                continue
            services.add_default(pos, lvl, actor=actor, reason=reason)
        # Everyone gets M365 and ServiceNow requester.
        for pos in positions.values():
            for app_key, level_name in (
                ("M365", "Standard user (E3)"),
                ("ServiceNow", "Requester"),
            ):
                lvl = level(app_key, level_name)
                if not PositionDefault.objects.filter(position=pos, access_level=lvl).exists():
                    services.add_default(pos, lvl, actor=actor, reason="Baseline for all staff")

    # --- Active Directory -----------------------------------------------------------

    def _directory(self, users, positions):
        """Write the synthetic AD mirror and everything the catalog builds on top of it.

        There is no fake LDAP server: this writes what a sync would have *left behind*, so
        every page downstream of the mirror -- reference badges, the group list, routes,
        route-managed levels, adoption, the broken-reference report -- runs its real code.
        `apps.core.demo.data` holds the inventory and says what each entry demonstrates.

        The order matters. Groups and routes have to exist before the reconciler can give the
        dynamic service its levels; those levels have to exist before one of them can be
        adopted or carry a position default.
        """
        now = timezone.now()
        self._check_referenced_groups()
        for spec in demo.GROUPS:
            mirror.upsert_group(spec, now=now)
        self._directory_logins(users, now)
        for spec in demo.ROUTES:
            mirror.upsert_route(spec, actor=users["admin"])
        mirror.reconcile_and_attach(None, demo.MIRRORED_NAMES, actor=users["admin"])
        self._adopt_from_route(users["admin"])
        self._routed_default(positions, users["admin"])
        self._directory_runs(users, now)
        return {
            "groups": ADGroup.objects.count(),
            "inactive": ADGroup.objects.filter(is_active=False).count(),
            "routes": ADGroupRoute.objects.count(),
            "route_levels": AccessLevel.objects.filter(
                source=AccessLevel.Source.ROUTE, is_active=True
            ).count(),
            "logins": User.objects.filter(ad_managed=True).count(),
            "runs": DirectorySyncRun.objects.count(),
        }

    def _check_referenced_groups(self):
        """Refuse to seed a level naming a group the demo inventory does not describe.

        Without this, adding an `ad_group` level to this command and forgetting to add the
        group to `demo.GROUPS` produces a silent broken reference that looks exactly like the
        two deliberate ones.
        """
        referenced = set(
            AccessLevel.objects.filter(access_model=AccessLevel.AccessModel.AD_GROUP).values_list(
                "ad_group_name", flat=True
            )
        )
        unknown = sorted(referenced - demo.KNOWN_GROUP_NAMES)
        if unknown:
            raise CommandError(
                "Seeded access levels reference AD groups that apps/core/demo/data.py does "
                f"not describe: {', '.join(unknown)}. Add a GroupSpec for each (state=ABSENT "
                "if it is meant to be a broken reference)."
            )

    def _directory_logins(self, users, now):
        for spec in demo.STAFF:
            mirror.upsert_staff_login(spec, baseline_role=settings.AD_BASELINE_ROLE, now=now)
        mirror.link_existing_login(demo.LINKED_LOGIN, demo.LINKED_LOGIN_CN, now=now)

    def _adopt_from_route(self, actor):
        """Take one route-managed level over by hand, so the "Taken over" badge has a row.

        Adopting a group into the very application that already holds it by route rewrites
        that row rather than adding a second one -- which is the only path that produces
        `source=adopted`, the one source the reconciler will not re-capture.
        """
        claimed = AccessLevel.objects.filter(
            ad_group_name__iexact=demo.ADOPTED_GROUP, source=AccessLevel.Source.ADOPTED
        ).exists()
        if claimed:
            return
        service = Application.objects.filter(name=demo.NETWORK_ACCESS).first()
        held = AccessLevel.objects.filter(
            application=service, ad_group_name__iexact=demo.ADOPTED_GROUP
        ).exists()
        if service is None or not held:
            return
        catalog_services.adopt_group(
            demo.ADOPTED_GROUP,
            service,
            actor=actor,
            level_name=demo.ADOPTED_LEVEL_NAME,
            description=demo.ADOPTED_LEVEL_DESCRIPTION,
        )

    def _routed_default(self, positions, actor):
        """Put one position default on a route-managed level.

        It is what makes drift show the subtlest rule in the feature: defaults follow their
        group through a rename, and a level that still carries one is deactivated rather than
        deleted when its group goes away.
        """
        level = AccessLevel.objects.filter(
            ad_group_name__iexact=demo.ROUTED_DEFAULT_GROUP, is_active=True
        ).first()
        position = positions.get(demo.ROUTED_DEFAULT_POSITION)
        if level is None or position is None:
            return
        if PositionDefault.objects.filter(position=position, access_level=level).exists():
            return
        services.add_default(position, level, actor=actor, reason=demo.ROUTED_DEFAULT_REASON)

    # -- fabricated run history ------------------------------------------------------

    def _directory_runs(self, users, now):
        """Four sync runs so the history pages, the status card and run detail have content.

        Each is identified by `(scope, status, trigger, server)` and all four differ on that
        tuple, which is what keeps re-seeding idempotent without storing a marker anybody can
        see. They are written oldest first, because the run list orders by creation.

        `SyncResult` builds the summaries and logs rather than a dict literal here, so a
        fabricated run carries exactly the counts and row shapes `run_detail`, `run_apply` and
        `sync_ad` read, and stays right if that shape changes. The one cost is that recording
        the deliberate error row below also logs it, which during `make seed` reads like
        something went wrong -- so that logger is quietened for the length of this method.
        """
        sync_log = logging.getLogger("apps.directory.sync")
        previous_level = sync_log.level
        sync_log.setLevel(logging.ERROR)
        try:
            self._write_runs(users, now)
        finally:
            sync_log.setLevel(previous_level)

    def _write_runs(self, users, now):
        Scope, Status, Trigger = (
            DirectorySyncRun.Scope,
            DirectorySyncRun.Status,
            DirectorySyncRun.Trigger,
        )
        admin = users["admin"]
        mirrored = sorted(demo.MIRRORED_NAMES)
        active = sorted(demo.SEEDED_ACTIVE_NAMES)

        # 1. The first import: every group the search returned became a row.
        first = SyncResult(kind="groups", dry_run=False)
        for row, name in enumerate(mirrored, start=1):
            first.record(row, name, "created", "global security group", dn=self._dn_of(name))
        mirror.record_run(
            scope=Scope.GROUPS,
            status=Status.COMPLETED,
            trigger=Trigger.MANUAL,
            server=demo.SERVER,
            created_by=admin,
            groups=first,
            started_at=now - dt.timedelta(days=21),
            finished_at=now - dt.timedelta(days=21) + dt.timedelta(seconds=6),
        )

        # 2. A dry run nobody applied: its two groups are absent from the mirror, which is
        #    what the Apply button on that run would put right.
        preview = SyncResult(kind="groups", dry_run=True)
        for row, name in enumerate(demo.PREVIEW_ONLY_GROUPS, start=1):
            preview.record(
                row, name, "created", "would be imported", dn=f"CN={name},{demo.INFRA_OU}"
            )
        mirror.record_run(
            scope=Scope.GROUPS,
            status=Status.PREVIEWED,
            trigger=Trigger.MANUAL,
            server=demo.SERVER,
            created_by=admin,
            groups=preview,
            started_at=now - dt.timedelta(days=5),
            finished_at=now - dt.timedelta(days=5) + dt.timedelta(seconds=3),
        )

        # 3. A scheduled run that could not reach a domain controller, recorded exactly as
        #    `run_sync` records one: no counts, no rows, just the error.
        mirror.record_run(
            scope=Scope.ALL,
            status=Status.FAILED,
            trigger=Trigger.SCHEDULED,
            server=demo.SERVER,
            error=(
                f"DirectoryUnavailable: {demo.SERVER}: socket connection error while opening: "
                "[Errno -2] Name or service not known"
            ),
            started_at=now - dt.timedelta(days=2),
            finished_at=now - dt.timedelta(days=2) + dt.timedelta(seconds=30),
        )

        # 4. Last night's run, and the one the status card reports: the seven members of
        #    IAM-Users became logins, one directory entry could not, and the group search
        #    stopped returning APP_EPIC_RESEARCH.
        nightly_users = SyncResult(kind="users", dry_run=False)
        for row, spec in enumerate(demo.STAFF, start=1):
            note = f"+{settings.AD_BASELINE_ROLE}"
            if not spec.enabled:
                note += ", inactive: disabled in AD"
            nightly_users.record(row, spec.upn, "created", note, dn=spec.dn)
        nightly_users.record(
            len(demo.STAFF) + 1,
            demo.BAD_MEMBER_DN,
            "error",
            "No userPrincipalName on the directory entry.",
            dn=demo.BAD_MEMBER_DN,
        )
        nightly_groups = SyncResult(kind="groups", dry_run=False)
        for row, name in enumerate(active, start=1):
            nightly_groups.record(row, name, "unchanged", "", dn=self._dn_of(name))
        for name in sorted(demo.SEEDED_INACTIVE_NAMES):
            # Row 0: the pass that deactivates what the listing stopped returning has no
            # directory row to point at, which is how the real sync records it too.
            nightly_groups.record(0, name, "deactivated", MISSING_GROUP_MESSAGE)
        mirror.record_run(
            scope=Scope.ALL,
            status=Status.COMPLETED,
            trigger=Trigger.SCHEDULED,
            server=demo.SERVER,
            users=nightly_users,
            groups=nightly_groups,
            group_dn=demo.USER_GROUP_DN,
            started_at=now - dt.timedelta(days=1),
            finished_at=now - dt.timedelta(days=1) + dt.timedelta(seconds=94),
        )

    @staticmethod
    def _dn_of(name):
        spec = demo.GROUPS_BY_NAME.get(name)
        return spec.dn if spec else ""
