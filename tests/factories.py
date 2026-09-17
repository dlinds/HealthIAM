import factory
from django.contrib.auth.models import Group

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


class AccessLevelFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = "catalog.AccessLevel"

    application = factory.SubFactory(ApplicationFactory)
    name = factory.Sequence(lambda n: f"Level {n}")
    access_model = "ad_group"
    ad_group_name = factory.LazyAttribute(lambda o: f"APP_{o.name.upper().replace(' ', '_')}")


def make_analyst(application, user, is_primary=False):
    from apps.catalog.models import ApplicationAnalyst

    return ApplicationAnalyst.objects.create(
        application=application, user=user, is_primary=is_primary
    )
