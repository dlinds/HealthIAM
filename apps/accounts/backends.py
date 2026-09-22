"""Entra ID (Azure AD) OpenID Connect backend.

Matches users by Entra object ID first, then username (the ``preferred_username``
claim, which is the UPN the directory syncs store as the login name), then
email, where the last two never match a login already bound to a different Entra
object ID; creates users on first login with a lowercased username; keeps names in sync;
and applies the ENTRA_GROUP_ROLE_MAP so that membership in a mapped Entra group is
the source of truth for that app role. The baseline role a directory sync guarantees
(``AD_BASELINE_ROLE`` for ``ad_managed`` logins, ``ENTRA_BASELINE_ROLE`` for
``entra_managed`` ones) is owned by that sync and is never revoked here.
"""

from __future__ import annotations

import logging

from django.conf import settings
from django.contrib.auth.models import Group
from django.db.models import Q
from mozilla_django_oidc.auth import OIDCAuthenticationBackend

from . import roles

logger = logging.getLogger(__name__)

ID_TOKEN_CLAIMS = ("oid", "groups", "preferred_username", "name", "email")


class EntraOIDCBackend(OIDCAuthenticationBackend):
    def get_userinfo(self, access_token, id_token, payload):
        """Merge ID-token claims (oid, groups) into the userinfo response, since Entra
        only puts the groups claim in the ID token."""
        claims = super().get_userinfo(access_token, id_token, payload)
        for key in ID_TOKEN_CLAIMS:
            if key in payload and key not in claims:
                claims[key] = payload[key]
        return claims

    def verify_claims(self, claims):
        return bool(claims.get("oid") or claims.get("email") or claims.get("preferred_username"))

    def filter_users_by_claims(self, claims):
        oid = claims.get("oid")
        candidates = self.UserModel.objects.all()
        if oid:
            by_oid = candidates.filter(entra_object_id=oid)
            if by_oid.exists():
                return by_oid
            # No login carries this identity, so the username and email fallbacks may only
            # link a login that is not yet bound to another Entra identity; otherwise a
            # reassigned UPN or mailbox would sign in as the previous holder's login.
            candidates = candidates.filter(entra_object_id__isnull=True)
        preferred_username = claims.get("preferred_username")
        if preferred_username:
            # Only logins a directory sync created or linked carry a UPN as their username; a
            # local login that merely shares the spelling must not be claimable through this
            # claim. (The Entra sync records the object ID as well, so its logins normally match
            # above; this covers one whose ID was cleared to re-link a re-created account.)
            by_username = candidates.filter(username__iexact=preferred_username).filter(
                Q(ad_managed=True) | Q(entra_managed=True)
            )
            if by_username.exists():
                return by_username
            self._warn_linked_elsewhere(
                oid, "preferred_username", username__iexact=preferred_username
            )
        email = _email_from(claims)
        if email:
            by_email = candidates.filter(email__iexact=email)
            if not by_email.exists():
                self._warn_linked_elsewhere(oid, "email", email__iexact=email)
            return by_email
        return self.UserModel.objects.none()

    def _warn_linked_elsewhere(self, oid, claim, **lookup):
        if not oid:
            return
        for user in self.UserModel.objects.filter(entra_object_id__isnull=False, **lookup):
            logger.warning(
                "Entra login %s matches login %r by %s, but that login is linked to another "
                "Entra identity (%s); not matched",
                oid,
                user.username,
                claim,
                user.entra_object_id,
            )

    def create_user(self, claims):
        email = _email_from(claims)
        username = (claims.get("preferred_username") or email or claims.get("oid")).lower()
        user = self.UserModel.objects.create_user(username=username, email=email)
        self._sync_profile(user, claims)
        user.save()
        apply_group_roles(user, claims.get("groups") or [])
        logger.info("Created user %s from Entra login", user.username)
        return user

    def update_user(self, user, claims):
        self._sync_profile(user, claims)
        user.save()
        apply_group_roles(user, claims.get("groups") or [])
        return user

    @staticmethod
    def _sync_profile(user, claims):
        if claims.get("oid") and not user.entra_object_id:
            user.entra_object_id = claims["oid"]
        email = _email_from(claims)
        if email:
            user.email = email
        if claims.get("given_name"):
            user.first_name = claims["given_name"]
        if claims.get("family_name"):
            user.last_name = claims["family_name"]
        if not (user.first_name or user.last_name) and claims.get("name"):
            parts = claims["name"].split(" ", 1)
            user.first_name = parts[0]
            user.last_name = parts[1] if len(parts) > 1 else ""


def _email_from(claims) -> str:
    return (
        claims.get("email") or claims.get("upn") or claims.get("preferred_username") or ""
    ).lower()


def apply_group_roles(user, entra_group_ids, mapping: dict | None = None) -> None:
    """Grant/revoke mapped roles based on Entra group membership.

    Only roles that appear in the mapping are touched; roles granted in-app that are
    not mapped to an Entra group are left alone. For a login managed by a directory
    sync the baseline role that sync guarantees is never revoked here, even when it is
    mapped: ``AD_BASELINE_ROLE`` for ``ad_managed`` logins (every IAM-Users member),
    ``ENTRA_BASELINE_ROLE`` for ``entra_managed`` ones (every member of ENTRA_USER_GROUP)."""
    mapping = settings.ENTRA_GROUP_ROLE_MAP if mapping is None else mapping
    if not mapping:
        return
    member_of = {str(g).lower() for g in entra_group_ids}
    managed_roles = {role for role in mapping.values() if role in roles.GROUP_ROLES}
    should_have = {
        role
        for gid, role in mapping.items()
        if role in roles.GROUP_ROLES and str(gid).lower() in member_of
    }
    protected = set()
    if getattr(user, "ad_managed", False):
        protected.add(settings.AD_BASELINE_ROLE)
    if getattr(user, "entra_managed", False):
        protected.add(getattr(settings, "ENTRA_BASELINE_ROLE", "Help Desk"))
    for role in managed_roles:
        group, _ = Group.objects.get_or_create(name=role)
        if role in should_have:
            user.groups.add(group)
        elif role not in protected:
            user.groups.remove(group)
