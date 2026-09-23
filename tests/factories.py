import uuid

import factory
from django.contrib.auth.models import Group
from django.utils import timezone

from apps.accounts import roles
from apps.accounts.models import User


class UserFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = User
        django_get_or_create = ("username",)
        skip_postgeneration_save = True

    username = factory.Sequence(lambda n: f"user{n}")
    email = factory.LazyAttribute(lambda o: f"{o.username}@example.org")
    first_name = factory.Faker("first_name")
    last_name = factory.Faker("last_name")
    password = factory.django.Password("pass1234")

    @factory.post_generation
    def groups(self, create, extracted, **kwargs):
        if not create or not extracted:
            return
        for name in extracted:
            group, _ = Group.objects.get_or_create(name=name)
            self.groups.add(group)


def make_admin(**kwargs):
    return UserFactory(groups=[roles.ADMIN], **kwargs)


def make_help_desk(**kwargs):
    return UserFactory(groups=[roles.HELP_DESK], **kwargs)


def make_auditor(**kwargs):
    return UserFactory(groups=[roles.AUDITOR], **kwargs)


class DepartmentFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = "orgs.Department"
        django_get_or_create = ("code",)

    code = factory.Sequence(lambda n: f"{1000 + n:04d}")
    name = factory.Sequence(lambda n: f"Department {n}")


class JobCodeFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = "orgs.JobCode"
        django_get_or_create = ("code",)

    code = factory.Sequence(lambda n: f"{5000 + n:04d}")
    title = factory.Sequence(lambda n: f"Job Title {n}")


class PositionFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = "orgs.Position"

    department = factory.SubFactory(DepartmentFactory)
    job_code = factory.SubFactory(JobCodeFactory)


class VendorFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = "catalog.Vendor"
        django_get_or_create = ("name",)

    name = factory.Sequence(lambda n: f"Vendor {n}")


class ContactFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = "catalog.Contact"

    name = factory.Sequence(lambda n: f"Contact {n}")
    email = factory.LazyAttribute(lambda o: f"{o.name.lower().replace(' ', '.')}@example.org")


class ApplicationFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = "catalog.Application"
        django_get_or_create = ("name",)

    name = factory.Sequence(lambda n: f"Application {n}")
    tier = 3


class ServiceFactory(ApplicationFactory):
    """An Application of kind `service`: the home for AD groups no application owns."""

    name = factory.Sequence(lambda n: f"Service {n}")
    kind = "service"


class DynamicServiceFactory(ServiceFactory):
    """A service that holds its routed AD groups automatically."""

    name = factory.Sequence(lambda n: f"Dynamic Service {n}")
    dynamic_ad_groups = True


class DynamicEntraServiceFactory(ServiceFactory):
    """A service that holds its routed cloud groups automatically."""

    name = factory.Sequence(lambda n: f"Dynamic Entra Service {n}")
    dynamic_entra_groups = True


class AccessLevelFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = "catalog.AccessLevel"

    application = factory.SubFactory(ApplicationFactory)
    name = factory.Sequence(lambda n: f"Level {n}")
    access_model = "ad_group"
    ad_group_name = factory.LazyAttribute(lambda o: f"APP_{o.name.upper().replace(' ', '_')}")


class ADGroupRouteFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = "directory.ADGroupRoute"

    pattern = factory.Sequence(lambda n: f"ROUTE_{n}_*")
    application = factory.SubFactory(ServiceFactory)


class EntraGroupRouteFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = "entra.EntraGroupRoute"

    pattern = factory.Sequence(lambda n: f"ROUTE-{n}-*")
    application = factory.SubFactory(ServiceFactory)


def make_analyst(application, user, is_primary=False):
    from apps.catalog.models import ApplicationAnalyst

    return ApplicationAnalyst.objects.create(
        application=application, user=user, is_primary=is_primary
    )


class ADGroupFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = "directory.ADGroup"

    object_guid = factory.LazyFunction(uuid.uuid4)
    name = factory.Sequence(lambda n: f"APP_GROUP_{n}")
    cn = factory.LazyAttribute(lambda o: o.name)
    description = ""
    distinguished_name = factory.LazyAttribute(lambda o: f"CN={o.cn},OU=Groups,DC=test,DC=invalid")
    group_type = -2147483646  # global security group
    scope = "global"
    category = "security"
    first_seen_at = factory.LazyFunction(timezone.now)
    last_seen_at = factory.LazyAttribute(lambda o: o.first_seen_at)


class DirectoryAccountFactory(factory.django.DjangoModelFactory):
    """An enabled, unlinked user account as the sync would mirror it."""

    class Meta:
        model = "directory.DirectoryAccount"

    object_guid = factory.LazyFunction(uuid.uuid4)
    sam_account_name = factory.Sequence(lambda n: f"account{n}")
    upn = factory.LazyAttribute(lambda o: f"{o.sam_account_name}@test.invalid")
    distinguished_name = factory.LazyAttribute(
        lambda o: f"CN={o.sam_account_name},OU=People,DC=test,DC=invalid"
    )
    given_name = factory.Faker("first_name")
    surname = factory.Faker("last_name")
    display_name = factory.LazyAttribute(lambda o: f"{o.given_name} {o.surname}")
    mail = factory.LazyAttribute(lambda o: o.upn)
    enabled = True
    first_seen_at = factory.LazyFunction(timezone.now)
    last_seen_at = factory.LazyAttribute(lambda o: o.first_seen_at)


# --- People ---------------------------------------------------------------------------


class PersonTypeFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = "people.PersonType"
        django_get_or_create = ("code",)

    code = factory.Sequence(lambda n: f"type{n}")
    name = factory.LazyAttribute(lambda o: o.code.title())
    is_external = True


class ExternalOrganizationFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = "people.ExternalOrganization"
        django_get_or_create = ("name",)

    name = factory.Sequence(lambda n: f"Agency {n}")
    kind = "agency"


class PersonFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = "people.Person"

    first_name = factory.Faker("first_name")
    last_name = factory.Faker("last_name")
    employee_id = factory.Sequence(lambda n: f"E{10000 + n}")
    email = factory.LazyAttribute(
        lambda o: f"{o.first_name}.{o.last_name}{o.employee_id}@example.org".lower()
    )


class PositionAssignmentFactory(factory.django.DjangoModelFactory):
    """A current, open-ended primary assignment unless told otherwise."""

    class Meta:
        model = "people.PositionAssignment"

    person = factory.SubFactory(PersonFactory)
    position = factory.SubFactory(PositionFactory)
    person_type = factory.SubFactory(PersonTypeFactory, code="employee", is_external=False)
    kind = "primary"
    start_date = factory.LazyFunction(lambda: timezone.localdate() - timezone.timedelta(days=30))


def make_coordinator(person_type, user):
    from apps.people.models import PersonTypeCoordinator

    return PersonTypeCoordinator.objects.create(person_type=person_type, user=user)


class EntraGroupFactory(factory.django.DjangoModelFactory):
    """A cloud group as the Entra sync mirrors it: by default one that can back a level."""

    class Meta:
        model = "entra.EntraGroup"

    object_id = factory.LazyFunction(uuid.uuid4)
    display_name = factory.Sequence(lambda n: f"SG-Group-{n}")
    description = ""
    kind = "security"
    membership = "assigned"
    source = "cloud"
    first_seen_at = factory.LazyFunction(timezone.now)
    last_seen_at = factory.LazyAttribute(lambda o: o.first_seen_at)
