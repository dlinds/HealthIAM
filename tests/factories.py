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
