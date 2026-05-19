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
    save_credentials(creds, userinfo)
    return userinfo


def save_credentials(creds, userinfo):
    s = session()
    try:
        row = s.get(Credentials, 1)
        if not row:
            row = Credentials(id=1)
            s.add(row)
        row.google_id = userinfo.get("id", "")
        row.email = userinfo.get("email", "")
        if creds.refresh_token:
            row.refresh_token = creds.refresh_token
        row.access_token = creds.token
        row.token_expiry = creds.expiry
        row.scopes = " ".join(creds.scopes or [])
        s.commit()
    finally:
        s.close()


def load_credentials():
    """Return a refreshed google.oauth2.credentials.Credentials, or None."""
    s = session()
    try:
        row = s.get(Credentials, 1)
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
                print(f"[auth] refresh failed: {e}")
                return None
        return creds
    finally:
        s.close()


def current_user():
    s = session()
    try:
        row = s.get(Credentials, 1)
        if not row:
            return None
        return {"email": row.email, "googleId": row.google_id}
    finally:
        s.close()


def disconnect():
    s = session()
    try:
        row = s.get(Credentials, 1)
        if row:
            s.delete(row)
            s.commit()
    finally:
        s.close()


def service(name, version):
    creds = load_credentials()
    if not creds:
        return None
    return build(name, version, credentials=creds, cache_discovery=False)
