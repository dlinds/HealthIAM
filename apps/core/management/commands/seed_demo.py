"""Populate a development database with realistic sample data. Idempotent."""

import datetime as dt
import uuid

from django.conf import settings
from django.contrib.auth.models import Group
from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone

from apps.access import services
from apps.access.models import PositionDefault
from apps.accounts import roles
from apps.accounts.models import User
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
from apps.directory.matching import decode_group_type
from apps.directory.models import ADGroup, DirectorySyncRun
from apps.orgs.models import Department, JobCode, Position, Source

PASSWORD = "healthiam"

# Synthetic on-prem AD. GUIDs are uuid5 of the object's name in this namespace, so every
# seed run produces the same rows and a real sync (which keys on objectGUID) simply
# deactivates them.
DEMO_AD_NAMESPACE = uuid.uuid5(uuid.NAMESPACE_DNS, "demo.local")
DEMO_AD_GROUPS_OU = "OU=Groups,DC=demo,DC=local"
DEMO_AD_STAFF_OU = "OU=Staff,DC=demo,DC=local"
DEMO_AD_SERVER = "dc1.demo.local"
# Left out of the seeded directory on purpose: the UKG "Employee" level then shows the
# "Not found in AD" badge, the dashboard entry and the broken-reference report.
DEMO_AD_MISSING_GROUP = "APP_UKG_EMPLOYEE"
DEMO_AD_UNUSED_GROUP = "APP_DEMO_UNUSED"
GLOBAL_SECURITY_GROUP = -2147483646  # groupType: GLOBAL | SECURITY as AD stores it (signed)

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
        groups = {name: Group.objects.get_or_create(name=name)[0] for name in roles.GROUP_ROLES}
        users = self._users(groups)
        depts, jobs, positions = self._orgs()
        vendors = {name: self._vendor(name, *vals) for name, vals in VENDORS.items()}
        contacts = self._contacts(vendors, users)
        apps = self._applications(vendors, contacts, users)
        self._defaults(apps, positions, users["admin"])
        self._directory(users)
        self.stdout.write(self.style.SUCCESS("Demo data loaded."))
        self.stdout.write(
            "Sign in with one of: admin / analyst.epic / analyst.imaging / owner.epic / "
            f"helpdesk / auditor  (password: {PASSWORD})"
        )
        if not settings.AD_ENABLED:
            self.stdout.write(
                self.style.WARNING(
                    "Active Directory is disabled, so the seeded AD groups, badges and the "
                    "Admin > Active Directory page stay hidden. To browse them without a domain "
                    "controller, set the commented local-demo pair from .env.example in .env "
                    "(AD_SERVER_URIS=ldaps://dc.test.invalid and AD_BASE_DN=DC=test,DC=invalid); "
                    "a real sync against that host fails harmlessly."
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

    def _directory(self, users):
        """A synthetic AD mirror so the AD pages have something to show without a domain
        controller: one `ADGroup` per group name the seeded access levels reference (minus
        `APP_UKG_EMPLOYEE`, which demonstrates a broken reference), one group nothing
        references, one completed groups-only sync run, and `helpdesk` marked as a login
        the sync manages. Keyed on deterministic GUIDs, so re-running changes nothing."""
        now = timezone.now()
        referenced = (
            AccessLevel.objects.filter(access_model=AccessLevel.AccessModel.AD_GROUP)
            .exclude(ad_group_name=DEMO_AD_MISSING_GROUP)
            .select_related("application")
            .order_by("application__name", "sort_order")
        )
        descriptions = {}
        for level in referenced:
            descriptions.setdefault(
                level.ad_group_name, f"{level.application.name} – {level.name}: {level.description}"
            )
        descriptions[DEMO_AD_UNUSED_GROUP] = "Pilot group from 2023; no access level references it."

        log = []
        created = 0
        for row, (name, description) in enumerate(descriptions.items(), start=1):
            group, was_created = self._directory_group(name, description, now)
            created += was_created
            log.append(
                {
                    "kind": "groups",
                    "row": row,
                    "code": group.name,
                    "action": "created",
                    "message": f"{group.scope} {group.category} group",
                    "dn": group.distinguished_name,
                }
            )
        counts = {
            "created": len(log),
            "updated": 0,
            "reactivated": 0,
            "deactivated": 0,
            "unchanged": 0,
            "errors": 0,
            "rows": len(log),
            "skipped": 0,
            "read": len(log),
        }
        DirectorySyncRun.objects.get_or_create(
            scope=DirectorySyncRun.Scope.GROUPS,
            status=DirectorySyncRun.Status.COMPLETED,
            server=DEMO_AD_SERVER,
            defaults={
                "trigger": DirectorySyncRun.Trigger.MANUAL,
                "created_by": users["admin"],
                "started_at": now - dt.timedelta(seconds=4),
                "finished_at": now,
                "summary": {"users": None, "groups": counts},
                "log": log,
            },
        )

        helpdesk = users["helpdesk"]
        wanted = {
            "ad_object_guid": uuid.uuid5(DEMO_AD_NAMESPACE, "user:helpdesk"),
            "ad_sam_account_name": "helpdesk",
            "ad_distinguished_name": f"CN=Casey Nguyen,{DEMO_AD_STAFF_OU}",
            "ad_managed": True,
        }
        changed = [field for field, value in wanted.items() if getattr(helpdesk, field) != value]
        if changed or helpdesk.ad_synced_at is None:
            for field in changed:
                setattr(helpdesk, field, wanted[field])
            helpdesk.ad_synced_at = helpdesk.ad_synced_at or now
            helpdesk.save(update_fields=[*changed, "ad_synced_at"])
        return created

    def _directory_group(self, name, description, now):
        scope, category = decode_group_type(GLOBAL_SECURITY_GROUP)
        values = {
            "name": name,
            "cn": name,
            "description": description,
            "distinguished_name": f"CN={name},{DEMO_AD_GROUPS_OU}",
            "group_type": GLOBAL_SECURITY_GROUP,
            "scope": scope,
            "category": category,
            "managed_by_dn": f"CN=IAM Team,{DEMO_AD_STAFF_OU}",
            "when_changed": dt.datetime(2025, 9, 1, 8, 0, tzinfo=dt.UTC),
        }
        group, created = ADGroup.objects.get_or_create(
            object_guid=uuid.uuid5(DEMO_AD_NAMESPACE, f"group:{name}"),
            defaults={**values, "first_seen_at": now, "last_seen_at": now},
        )
        if not created:
            # Save only on a real difference so re-seeding leaves timestamps and history alone.
            changed = [field for field, value in values.items() if getattr(group, field) != value]
            if changed:
                for field in changed:
                    setattr(group, field, values[field])
                group.save(update_fields=[*changed, "updated_at"])
        return group, created
