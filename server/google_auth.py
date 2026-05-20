"""Google OAuth 2.0 flow + credential helpers."""
import os
import json
from datetime import datetime, timedelta

# Google often returns scopes in a different order or with implicit additions
# (e.g. adding 'openid'). Relax oauthlib's strict scope validation so this
# doesn't raise on the callback.
os.environ.setdefault("OAUTHLIB_RELAX_TOKEN_SCOPE", "1")

from google_auth_oauthlib.flow import Flow
from google.oauth2.credentials import Credentials as GoogleCreds
from google.auth.transport.requests import Request
from googleapiclient.discovery import build

from db import session, Credentials

SCOPES = [
    "openid",
    "https://www.googleapis.com/auth/userinfo.email",
    "https://www.googleapis.com/auth/userinfo.profile",
    "https://www.googleapis.com/auth/tasks",
    "https://www.googleapis.com/auth/calendar",
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.labels",
]


def _client_config():
    """Build the OAuth client config from env vars (avoids committing client_secret.json)."""
    return {
        "web": {
            "client_id": os.environ["GOOGLE_CLIENT_ID"],
            "client_secret": os.environ["GOOGLE_CLIENT_SECRET"],
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
            "auth_provider_x509_cert_url": "https://www.googleapis.com/oauth2/v1/certs",
            "redirect_uris": [os.environ["OAUTH_REDIRECT_URI"]],
        }
    }


def make_flow(state=None):
    flow = Flow.from_client_config(
        _client_config(),
        scopes=SCOPES,
        state=state,
    )
    flow.redirect_uri = os.environ["OAUTH_REDIRECT_URI"]
    return flow


def authorization_url():
    flow = make_flow()
    url, state = flow.authorization_url(
        access_type="offline",
        include_granted_scopes="true",
        prompt="consent",
    )
    # PKCE: the library generated a one-time code_verifier when building the
    # URL. Google will require us to echo it back during the token exchange,
    # so we hand it to the caller to stash in the session alongside state.
    return url, state, getattr(flow, "code_verifier", None)


def exchange_code(code, state, code_verifier=None):
    flow = make_flow(state=state)
    if code_verifier:
        flow.code_verifier = code_verifier
    flow.fetch_token(code=code)
    creds = flow.credentials
    userinfo = build("oauth2", "v2", credentials=creds).userinfo().get().execute()
    user = _ensure_user_and_household(userinfo)
    save_credentials(creds, user)
    return user


def _ensure_user_and_household(userinfo):
    """Find or create a User from the Google userinfo response, and make
    sure they have a household (joining 'My Tasks' if one already exists
    and isn't theirs, otherwise creating one)."""
    from db import session, User, Household, Membership
    from sqlalchemy import text
    s = session()
    try:
        gid = userinfo.get("id", "")
        if not gid:
            raise RuntimeError("Google userinfo missing id")
        user = s.query(User).filter_by(google_id=gid).first()
        is_new_user = user is None
        if is_new_user:
            user = User(
                google_id=gid,
                email=userinfo.get("email", ""),
                name=userinfo.get("name") or userinfo.get("email", ""),
                picture_url=userinfo.get("picture"),
            )
            s.add(user); s.flush()
        else:
            # Keep email / name / picture in sync with Google
            user.email = userinfo.get("email", user.email)
            user.name = userinfo.get("name") or user.name
            user.picture_url = userinfo.get("picture") or user.picture_url

        # Make sure the user has at least one household membership.
        has_membership = s.query(Membership).filter_by(user_id=user.id).first()
        if not has_membership:
            # Adopt the legacy "My Tasks" household if it has no owner yet.
            orphan = s.query(Household).filter_by(created_by_user_id=None).first()
            if orphan:
                orphan.created_by_user_id = user.id
                s.add(Membership(household_id=orphan.id, user_id=user.id, role="owner"))
            else:
                h = Household(name="My Tasks", created_by_user_id=user.id)
                s.add(h); s.flush()
                s.add(Membership(household_id=h.id, user_id=user.id, role="owner"))

        # Claim orphan email_suggestions if this is the only user — they
        # belong to whoever was using the single-user database before the
        # multi-user migration. Without this, the scanner would try to
        # re-INSERT the same email_ids and trip (formerly) UNIQUE.
        if is_new_user and s.query(User).count() == 1:
            try:
                s.execute(text(
                    "UPDATE email_suggestions SET user_id = :uid "
                    "WHERE user_id IS NULL"
                ), {"uid": user.id})
            except Exception as e:
                print(f"[auth] claiming orphan suggestions failed: {e}")
        s.commit()
        return user.to_dict() | {"id": user.id}
    finally:
        s.close()


def save_credentials(creds, user):
    """Persist refreshed OAuth credentials for a specific user."""
    from db import session, Credentials
    s = session()
    try:
        row = s.query(Credentials).filter_by(user_id=user["id"]).first()
        if not row:
            row = Credentials(user_id=user["id"])
            s.add(row)
        row.google_id = user.get("googleId") or user.get("google_id") or ""
        row.email = user.get("email", "")
        if creds.refresh_token:
            row.refresh_token = creds.refresh_token
        row.access_token = creds.token
        row.token_expiry = creds.expiry
        row.scopes = " ".join(creds.scopes or [])
        s.commit()
    finally:
        s.close()


def load_credentials(user_id):
    """Return a refreshed google.oauth2.credentials.Credentials for the
    given user, or None if they have no stored token."""
    from db import session, Credentials
    if user_id is None:
        return None
    s = session()
    try:
        row = s.query(Credentials).filter_by(user_id=user_id).first()
        if not row:
            return None
        creds = GoogleCreds(
            token=row.access_token,
            refresh_token=row.refresh_token,
            token_uri="https://oauth2.googleapis.com/token",
            client_id=os.environ["GOOGLE_CLIENT_ID"],
            client_secret=os.environ["GOOGLE_CLIENT_SECRET"],
            scopes=(row.scopes or "").split(),
            expiry=row.token_expiry,
        )
        if not creds.valid:
            try:
                creds.refresh(Request())
                row.access_token = creds.token
                row.token_expiry = creds.expiry
                s.commit()
            except Exception as e:
                print(f"[auth] refresh failed for user_id={user_id}: {e}")
                return None
        return creds
    finally:
        s.close()


def current_user(user_id):
    from db import session, User
    if user_id is None:
        return None
    s = session()
    try:
        u = s.get(User, user_id)
        return u.to_dict() if u else None
    finally:
        s.close()


def disconnect(user_id):
    from db import session, Credentials
    if user_id is None:
        return
    s = session()
    try:
        row = s.query(Credentials).filter_by(user_id=user_id).first()
        if row:
            s.delete(row)
            s.commit()
    finally:
        s.close()


def service(name, version, user_id):
    creds = load_credentials(user_id)
    if not creds:
        return None
    return build(name, version, credentials=creds, cache_discovery=False)
