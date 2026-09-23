"""Populate a development database with realistic sample data. Idempotent.

The Active Directory half is a fiction written straight into the mirror: there is no fake
LDAP server, so Test connection and Sync now still fail honestly. `apps.core.demo.data`
describes the synthetic domain and `apps.core.demo.mirror` writes it; `manage.py demo_ad`
drifts it afterwards. See `docs/ad-setup.md` section 12.

The Entra ID half is the same fiction for the hybrid tenant that domain synchronizes to:
`apps.core.demo.entra_data` describes it and `apps.core.demo.entra_mirror` writes it. See
`docs/entra-setup.md`, "The demo tenant".
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
from apps.core.demo import entra_data as entra_demo
from apps.core.demo import entra_mirror, mirror
from apps.directory import reconcile
from apps.directory.models import ADGroup, ADGroupRoute, DirectoryAccount, DirectorySyncRun
from apps.directory.sync import (
    MISSING_GROUP_MESSAGE,
    AccountSyncResult,
    SyncResult,
    link_accounts,
)
from apps.entra import sync as entra_sync
from apps.entra import worklists as entra_worklists
from apps.entra.models import EntraAccount, EntraGroup, EntraGroupRoute, EntraSyncRun
from apps.orgs.models import Department, JobCode, Position, Source
from apps.people import services as people_services
from apps.people.bootstrap import ensure_person_types
from apps.people.models import ExternalOrganization, Person

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
    ("0900", "Medical Staff"),
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
    ("7003", "Student Nurse"),
    ("8000", "Physician"),
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
    ("0100", "7003"),
    ("0900", "8000"),
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
            people = self._people(users, positions, vendors, apps)
            counts = self._directory(users, positions)
            entra_counts = self._entra(users)
            # The AD link pass ran before the demo tenant existed. Once more, as the next sync
            # would: an AD account follows a hand link on its Entra ID copy, and a second seed
            # finds nothing left to change.
            link_accounts()
        self.stdout.write(f"People: {people} on record.")
        self.stdout.write(self.style.SUCCESS("Demo data loaded."))
        self.stdout.write(
            "Sign in with one of: admin / analyst.epic / analyst.imaging / owner.epic / "
            f"helpdesk / auditor  (password: {PASSWORD})"
        )
        self._report_directory(counts)
        self._report_entra(entra_counts)

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
            "{accounts} mirrored account(s), {runs} sync run(s).".format(**counts)
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
            elif model == AccessLevel.AccessModel.ENTRA_GROUP:
                defaults["entra_group_id"] = entra_demo.group_id(target)
                defaults["entra_group_name"] = target
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
        AD, TICKET, IN_APP, ENTRA = (
            AccessLevel.AccessModel.AD_GROUP,
            AccessLevel.AccessModel.TICKET,
            AccessLevel.AccessModel.IN_APP,
            AccessLevel.AccessModel.ENTRA_GROUP,
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
                # Cloud groups of the demo tenant (apps/core/demo/entra_data.py), one per badge:
                # in Entra ID, deleted after its pilot, never returned, and made dynamic after
                # the level was created.
                (
                    "Copilot (add-on)",
                    ENTRA,
                    "LIC_M365_COPILOT",
                    "Microsoft 365 Copilot in Word, Outlook and Teams",
                ),
                ("Teams Phone (pilot)", ENTRA, "LIC_TEAMS_PHONE_PILOT", "Calling from Teams"),
                ("Power BI Pro", ENTRA, "LIC_POWERBI_PRO", "Publish and share reports"),
                (
                    "All nursing staff",
                    ENTRA,
                    "DYN-All-Nursing-Staff",
                    "Nursing news and the shared nursing calendar",
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
        # owning team rather than one bucket, so analyst rights stay scoped per team. Their AD
        # groups are not declared here -- `Network Access` gets them from its route, and the
        # rest are the worklist that "Add to catalog" adopts from. File Shares holds one
        # cloud group: a share governed in Entra ID and written back to AD for the file server.
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
            levels=[
                (
                    "Nursing education share",
                    ENTRA,
                    demo.WRITTEN_BACK_FROM,
                    "Course materials; the file server sees the group's AD copy",
                ),
            ],
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
            # Cloud groups: position defaults work the same whichever directory holds them.
            ("0100-7000", demo.FILE_SHARES, "Nursing education share", "Course materials"),
            ("0500-7400", "M365", "Copilot (add-on)", "Pilot for Information Services analysts"),
            ("0500-9000", "M365", "Teams Phone (pilot)", "Pilot for Information Services leads"),
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

    # --- People ---------------------------------------------------------------------

    def _people(self, users, positions, vendors, apps):
        """The workforce the people database exists for: employees on the seeded positions
        (one with an alternate position, one on leave, one who left last month, one who
        changed her name), an employed and an affiliated provider, a traveler about to
        expire and one open-ended, a student starting next week, an internal contractor
        with no end date, a vendor representative and a volunteer.

        Every row goes through `apps.people.services`, so it carries a reason like a real
        one would, and every step is keyed so a re-seed changes nothing.
        """
        types = {t.code: t for t, _ in ensure_person_types()}
        actor = users["admin"]
        today = timezone.localdate()
        reason = "Demo seed"

        def day(offset):
            return today + dt.timedelta(days=offset)

        def org(name, kind, vendor=None):
            return ExternalOrganization.objects.get_or_create(
                name=name, defaults={"kind": kind, "vendor": vendor}
            )[0]

        aya = org("Aya Healthcare", ExternalOrganization.Kind.AGENCY)
        college = org("State University College of Nursing", ExternalOrganization.Kind.SCHOOL)
        epic = org("Epic Systems", ExternalOrganization.Kind.VENDOR, vendors["Epic Systems"])

        def person(first, last, employee_id="", email=None, **fields):
            lookup = (
                {"employee_id": employee_id}
                if employee_id
                else {
                    "first_name": first,
                    "last_name": last,
                }
            )
            existing = Person.objects.filter(**lookup).first()
            if existing is not None:
                # A database seeded before a demo person had an address of their own: the
                # guest accounts link by it.
                if email and existing.email != email:
                    people_services.update_person(
                        existing, actor=actor, reason=reason, system=True, email=email
                    )
                return existing
            return people_services.create_person(
                actor=actor,
                reason=reason,
                source=Source.HR if employee_id else Source.MANUAL,
                first_name=first,
                last_name=last,
                employee_id=employee_id,
                email=email or f"{first}.{last}@example.org".lower().replace(" ", ""),
                **fields,
            )

        def assign(who, code, type_code, start, end=None, **fields):
            pos = positions[code]
            if who.assignments.filter(position=pos, start_date=day(start)).exists():
                return
            people_services.add_assignment(
                who,
                pos,
                types[type_code],
                start_date=day(start),
                end_date=day(end) if end is not None else None,
                source=Source.HR if who.employee_id else Source.MANUAL,
                actor=actor,
                reason=reason,
                **fields,
            )

        # Managers first, so the reports can point at them.
        maria = person("Maria", "Alvarez", "E1001", hire_date=day(-3000))
        assign(maria, "0100-7002", "employee", -3000)
        grace = person("Grace", "Ito", "E1010", hire_date=day(-2500))
        assign(grace, "0500-9000", "employee", -2500)
        victor = person("Victor", "Salas", "E1015", hire_date=day(-1800))
        assign(victor, "0800-9000", "employee", -1800)
        nora = person("Nora", "Feld", "E1012", hire_date=day(-900))
        assign(nora, "0600-7500", "employee", -900)

        # Employees. Daniel also covers radiology: the alternate-position demo.
        daniel = person("Daniel", "Okoro", "E1002", hire_date=day(-1200), manager=maria)
        assign(daniel, "0100-7000", "employee", -1200)
        assign(daniel, "0300-7000", "employee", -100, kind="alternate")
        hannah = person("Hannah", "Weiss", "E1003", hire_date=day(-700), manager=maria)
        assign(hannah, "0100-7000", "employee", -700)
        if not hannah.on_leave:
            people_services.update_person(
                hannah, actor=actor, reason="Leave of absence per HR", on_leave=True
            )
        # Her AD account was made without the employee ID; the username the HR feed carries
        # links it (and its Entra ID copy) all the same.
        if hannah.network_username != demo.USERNAME_ONLY_ACCOUNT:
            people_services.update_person(
                hannah,
                actor=actor,
                reason="Network username per HR feed",
                network_username=demo.USERNAME_ONLY_ACCOUNT,
            )
        emily = person("Emily", "Brooks", "E1004", hire_date=day(-400), manager=maria)
        assign(emily, "0100-7000", "employee", -400)
        if not emily.former_names.exists():
            people_services.change_name(
                emily,
                first_name="Emily",
                last_name="Carter",
                effective_on=day(-14),
                actor=actor,
                reason="Marriage; HR record updated",
                source=Source.HR,
            )
        for first, last, eid, code, mgr, hired in (
            ("Robert", "Chen", "E1005", "0200-7100", None, -1500),
            ("Aisha", "Karim", "E1006", "0200-7101", None, -300),
            ("Tom", "Becker", "E1007", "0300-7200", None, -2000),
            ("Linda", "Park", "E1008", "0400-7300", None, -1100),
            ("Jamal", "Wright", "E1013", "0700-7600", None, -600),
            ("Helen", "Voss", "E1014", "0800-7700", victor, -800),
        ):
            who = person(first, last, eid, hire_date=day(hired), manager=mgr)
            assign(who, code, "employee", hired)

        # The logins that exist as people too, so a person page can show its HealthIAM login.
        for first, last, eid, code, login in (
            ("Sam", "Rivera", "E1011", "0500-7400", "admin"),
            ("Jordan", "Lee", "E1018", "0500-7400", "iam"),
            ("Priya", "Natarajan", "E1019", "0500-7400", "analyst_epic"),
            ("Marcus", "Okafor", "E1020", "0500-7400", "analyst_imaging"),
            ("Dana", "Whitfield", "E1021", "0100-9000", "owner_epic"),
            ("Casey", "Nguyen", "E1022", "0500-7400", "helpdesk"),
            ("Robin", "Alvarez", "E1023", "0800-7700", "auditor"),
        ):
            who = person(
                first,
                last,
                eid,
                hire_date=day(-1000),
                manager=grace if code.startswith("0500") else None,
                user=users[login],
            )
            assign(who, code, "employee", -1000)

        # The seven AD-managed logins of the demo directory are people too: their accounts
        # link to these records by employee ID.
        for spec in demo.STAFF:
            who = person(spec.first_name, spec.last_name, spec.employee_id, hire_date=day(-1000))
            assign(who, spec.position_code, "employee", -1000)

        # Left last month. The demo directory keeps his account enabled: the orphan worklist.
        paul = person("Paul", "Grant", "E1016", hire_date=day(-1300), manager=grace)
        assign(paul, "0400-7300", "employee", -1300)
        if paul.is_active:
            people_services.deactivate_person(
                paul, actor=actor, reason="Terminated per HR feed", separation_date=day(-30)
            )

        # Providers: one employed, one affiliated.
        anita = person("Anita", "Rao", "E1017", hire_date=day(-2200), suffix="MD")
        assign(anita, "0900-8000", "provider", -2200)
        if not anita.identifiers.filter(kind="npi").exists():
            people_services.add_identifier(
                anita, kind="npi", value="1234567893", actor=actor, reason="Credentialing file"
            )
        # Externals carry the address they were invited to the tenant with, which is what
        # links their Entra ID accounts to them (apps/core/demo/entra_data.py).
        marcus = person(
            "Marcus", "Bell", suffix="MD", email="marcus.bell@lakesidephysicians.example"
        )
        assign(marcus, "0900-8000", "provider", -900, title="Affiliated physician")
        if not marcus.identifiers.filter(kind="npi").exists():
            people_services.add_identifier(
                marcus, kind="npi", value="1987654321", actor=actor, reason="Credentialing file"
            )

        # Externals.
        chloe = person("Chloe", "Martin", email="chloe.martin@gmail.example")
        assign(chloe, "0100-7000", "traveler", -78, 12, organization=aya, sponsor=maria)
        ben = person("Ben", "Osei")
        assign(ben, "0300-7000", "traveler", -40, organization=aya, sponsor=maria)
        lily = person("Lily", "Zhang", email="lily.zhang@stateu.example")
        assign(lily, "0100-7003", "student", 7, 90, organization=college, sponsor=maria)
        ravi = person("Ravi", "Menon")
        assign(ravi, "0500-7400", "contractor", -200, sponsor=grace, title="Epic analyst")
        dana = person("Dana", "Fox", email="dana.fox@epic.example")
        assign(dana, "0500-7400", "vendor", -20, 60, organization=epic, sponsor=grace)
        ruth = person("Ruth", "Adler", email="ruth.adler@mailbox.example")
        assign(ruth, "0600-7500", "volunteer", -365, sponsor=nora)
        # Her contract ended three weeks ago. The demo tenant keeps her guest account enabled:
        # the orphaned-guest worklist.
        ines = person("Ines", "Duarte", email="ines.duarte@ayahealthcare.example")
        assign(ines, "0100-7000", "traveler", -112, -21, organization=aya, sponsor=maria)

        # Beyond the positions: a grant with its ticket, and an exclusion.
        def access(who, app_key, level_name, kind, **fields):
            level = AccessLevel.objects.get(application=apps[app_key], name=level_name)
            if who.access_grants.filter(access_level=level, kind=kind).exists():
                return
            people_services.add_person_access(
                who, level, kind=kind, actor=actor, reason=reason, system=True, **fields
            )

        access(
            ravi,
            "Epic",
            "Read-only chart review",
            "grant",
            approved_by=grace,
            ticket_ref="REQ0012345",
            justification="Validates Epic build against live charts during the upgrade.",
            end_date=day(120),
        )
        access(
            daniel,
            "PACS",
            "Clinical viewer",
            "exclusion",
            approved_by=maria,
            justification="Restricted at the manager's request pending a review.",
        )
        return Person.objects.count()

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
        self._directory_accounts(now)
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
                source=AccessLevel.Source.ROUTE,
                access_model=AccessLevel.AccessModel.AD_GROUP,
                is_active=True,
            ).count(),
            "logins": User.objects.filter(ad_managed=True).count(),
            "accounts": DirectoryAccount.objects.count(),
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

    def _directory_accounts(self, now):
        """The account mirror: one account per managed login, plus the ones that have no
        login, then the same employee-ID link pass a sync runs."""
        for spec in (*demo.STAFF, *demo.ACCOUNTS):
            mirror.upsert_directory_account(spec, now=now)
        link_accounts(now=now)

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

    # --- Entra ID -------------------------------------------------------------------

    def _entra(self, users):
        """Write the synthetic tenant's mirror: groups, accounts, their links, run history.

        The same idea as `_directory`: no fake Graph, only what a sync would have left behind,
        worked out by the sync's own value helpers (`apps.core.demo.entra_mirror`). The Entra
        group levels and their position defaults were seeded with the rest of the catalog;
        this is what gives their badges something to say.

        Written whether or not Entra ID is enabled, as the directory is -- except over a
        mirror that already holds a real tenant: the sync refuses to mix two tenants, and so
        does the seed. (Only the mirror is spared: the demo catalog is written regardless, as
        the AD half's is. Do not seed a real instance.)
        """
        foreign = entra_mirror.foreign_tenants()
        if foreign:
            return {"skipped": sorted(str(tenant) for tenant in foreign)}
        self._check_referenced_entra_groups()
        now = timezone.now()
        for spec in entra_demo.GROUPS:
            entra_mirror.upsert_group(spec, now=now)
        for spec in entra_demo.ROUTES:
            mirror.upsert_route(spec, actor=users["admin"], model=EntraGroupRoute)
        for spec in entra_demo.ACCOUNTS:
            entra_mirror.upsert_account(spec, now=now)
        for spec in entra_demo.ACCOUNTS:
            entra_mirror.link_by_hand(spec, now=now)
        entra_sync.link_accounts(now=now)
        self._entra_runs(users, now)
        accounts = EntraAccount.objects.all()
        return {
            "groups": EntraGroup.objects.count(),
            "synced": EntraGroup.objects.filter(source=EntraGroup.Source.SYNCED).count(),
            "inactive": EntraGroup.objects.filter(is_active=False).count(),
            "accounts": accounts.count(),
            "external": accounts.filter(source__in=EntraAccount.EXTERNAL_SOURCES).count(),
            "linked": accounts.filter(person__isnull=False).count(),
            "routes": EntraGroupRoute.objects.count(),
            "runs": EntraSyncRun.objects.count(),
        }

    def _check_referenced_entra_groups(self):
        """Refuse to seed an Entra group level naming a group the demo tenant does not have,
        for the reason `_check_referenced_groups` gives."""
        known = {spec.object_id for spec in entra_demo.GROUPS}
        unknown = sorted(
            AccessLevel.objects.filter(access_model=AccessLevel.AccessModel.ENTRA_GROUP)
            .exclude(entra_group_id__in=known)
            .values_list("entra_group_name", flat=True)
        )
        if unknown:
            raise CommandError(
                "Seeded access levels reference Entra groups that "
                f"apps/core/demo/entra_data.py does not describe: {', '.join(unknown)}. Add a "
                "CloudGroupSpec for each (state=ABSENT if it is meant to be a broken reference)."
            )

    def _entra_runs(self, users, now):
        """Three runs, oldest first, each identified like the directory's: the first import, a
        scheduled run that failed on an expired client secret, and last night's, which found
        the E3 licence group's source of authority moved to the cloud, the Teams Phone pilot
        group gone, and a guest blocked. `SyncResult` builds their summaries and logs."""
        Status, Trigger = EntraSyncRun.Status, EntraSyncRun.Trigger
        groups = sorted(entra_demo.MIRRORED_GROUPS, key=lambda spec: spec.name.lower())
        accounts = sorted(entra_demo.ACCOUNTS, key=lambda spec: spec.upn.lower())
        unmatched = entra_worklists.unmatched(EntraAccount.objects.all()).count()

        # 1. The first import: every group and account became a row, and the link pass linked
        #    whoever it could.
        first_groups = SyncResult(kind="groups", dry_run=False)
        for row, spec in enumerate(groups, start=1):
            note = entra_mirror.describe_group(spec, before_conversion=True)
            first_groups.record(row, spec.name, "created", note, dn=str(spec.object_id))
        first_accounts = AccountSyncResult(kind="accounts", dry_run=False)
        for row, spec in enumerate(accounts, start=1):
            enabled = True if spec.key == entra_demo.DISABLED_LAST_NIGHT else None
            note = entra_mirror.describe_account(spec, now=now, enabled=enabled)
            first_accounts.record(row, spec.upn, "created", note, dn=str(spec.object_id))
        linked = (
            EntraAccount.objects.filter(person__isnull=False)
            .exclude(link_method=EntraAccount.LinkMethod.MANUAL)
            .select_related("person")
            .order_by("upn")
        )
        for account in linked:
            first_accounts.record(
                0, account.upn, "linked", entra_mirror.link_note(account), dn=str(account.object_id)
            )
        first_accounts.unmatched = unmatched
        entra_mirror.record_run(
            status=Status.COMPLETED,
            trigger=Trigger.MANUAL,
            created_by=users["admin"],
            started_at=now - dt.timedelta(days=21),
            seconds=41,
            groups=first_groups,
            accounts=first_accounts,
        )

        # 2. The secret expired: the token request failed before anything was read.
        entra_mirror.record_run(
            status=Status.FAILED,
            trigger=Trigger.SCHEDULED,
            started_at=now - dt.timedelta(days=2),
            seconds=2,
            error=entra_demo.EXPIRED_SECRET_ERROR,
        )

        # 3. Last night's, after the secret was renewed: the one the status card reports.
        nightly_groups = SyncResult(kind="groups", dry_run=False)
        active = [spec for spec in groups if spec.state == demo.State.ACTIVE]
        for row, spec in enumerate(active, start=1):
            if spec.converted:
                nightly_groups.record(
                    row,
                    spec.name,
                    "updated",
                    "source of authority moved to the cloud",
                    dn=str(spec.object_id),
                )
            else:
                nightly_groups.record(row, spec.name, "unchanged", dn=str(spec.object_id))
        for spec in groups:
            if spec.state == demo.State.INACTIVE:
                nightly_groups.record(
                    0,
                    spec.name,
                    "deactivated",
                    entra_sync.MISSING_GROUP_MESSAGE,
                    dn=str(spec.object_id),
                )
        nightly_accounts = AccountSyncResult(kind="accounts", dry_run=False)
        for row, spec in enumerate(accounts, start=1):
            if spec.key == entra_demo.DISABLED_LAST_NIGHT:
                nightly_accounts.record(
                    row, spec.upn, "updated", "disabled in Entra ID", dn=str(spec.object_id)
                )
            else:
                nightly_accounts.record(row, spec.upn, "unchanged", dn=str(spec.object_id))
        nightly_accounts.unmatched = unmatched
        entra_mirror.record_run(
            status=Status.COMPLETED,
            trigger=Trigger.SCHEDULED,
            started_at=now - dt.timedelta(days=1),
            seconds=38,
            groups=nightly_groups,
            accounts=nightly_accounts,
        )

    def _report_entra(self, counts):
        """Say what the Entra ID half of the seed did, and what it cannot do."""
        if counts.get("skipped"):
            self.stdout.write(
                self.style.WARNING(
                    f"The Entra ID mirror holds tenant {', '.join(counts['skipped'])}: a real "
                    "tenant has been synchronized here, so the demo tenant was not written. "
                    "The sync refuses to mix two tenants, and so does the seed."
                )
            )
            return
        if not settings.ENTRA_ENABLED:
            self.stdout.write(
                self.style.WARNING(
                    "Entra ID is disabled, so the seeded tenant, the Entra group badges and the "
                    "Admin > Entra ID page stay hidden. Development settings turn the demo tenant "
                    "on by themselves while ENTRA_TENANT_ID and ENTRA_SYNC_CLIENT_ID are both "
                    "unset; under production settings, copy the demo block at the end of the "
                    "Entra ID section of .env.example into .env."
                )
            )
            return
        self.stdout.write(
            "Demo tenant: {groups} group(s) ({synced} synced from AD, {inactive} inactive), "
            "{accounts} account(s) ({external} guests and external members, {linked} linked "
            "to people), {routes} group route(s), {runs} sync run(s).".format(**counts)
        )
        if str(settings.ENTRA_TENANT_ID) == str(entra_demo.TENANT_ID):
            self.stdout.write(
                f"Entra ID is on with the synthetic demo tenant ({entra_demo.TENANT_NAME}). "
                "Test connection, Sync now and `manage.py sync_entra` fail: there is no tenant."
            )
        else:
            self.stdout.write(
                self.style.WARNING(
                    f"ENTRA_TENANT_ID is {settings.ENTRA_TENANT_ID}, not the demo tenant. The "
                    "next Entra ID sync will refuse to run until the demo rows are deleted "
                    "(Django admin, Entra groups and Entra accounts, as a superuser). Do not "
                    "seed demo data on a real instance."
                )
            )
