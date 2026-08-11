"""
Microsoft Graph authentication (app-only / client-credentials flow).

This app talks to Microsoft Graph as itself, with no signed-in user, to
watch loads@monstertrucking.com's inbox for new mail. That requires an app
registration in Microsoft Entra with Mail.Read application permission,
admin-consented for the monstertrucking.com tenant.

Configuration (Render > your service > Environment):
    AZURE_TENANT_ID     - the Entra tenant ID
    AZURE_CLIENT_ID     - the app registration's client ID
    AZURE_CLIENT_SECRET - the app registration's client secret
"""

import os
import time

import requests

AZURE_TENANT_ID = os.environ.get("AZURE_TENANT_ID", "")
AZURE_CLIENT_ID = os.environ.get("AZURE_CLIENT_ID", "")
AZURE_CLIENT_SECRET = os.environ.get("AZURE_CLIENT_SECRET", "")

GRAPH_SCOPE = "https://graph.microsoft.com/.default"

# Access tokens are valid ~1 hour. Cache the current one in memory so we're
# not requesting a fresh token on every single Graph call.
_token_cache = {"access_token": None, "expires_at": 0}


def get_graph_token():
    """
    Returns a valid Graph access token, reusing the cached one until it's
    close to expiring.

    Raises RuntimeError if the Azure env vars aren't set, or
    requests.HTTPError if Microsoft rejects the request (e.g. wrong client
    secret, or Mail.Read was never admin-consented).
    """
    now = time.time()
    if _token_cache["access_token"] and now < _token_cache["expires_at"] - 60:
        return _token_cache["access_token"]

    if not (AZURE_TENANT_ID and AZURE_CLIENT_ID and AZURE_CLIENT_SECRET):
        raise RuntimeError(
            "AZURE_TENANT_ID, AZURE_CLIENT_ID, and AZURE_CLIENT_SECRET must "
            "all be set (Render > Environment) to talk to Microsoft Graph."
        )

    resp = requests.post(
        f"https://login.microsoftonline.com/{AZURE_TENANT_ID}/oauth2/v2.0/token",
        data={
            "grant_type": "client_credentials",
            "client_id": AZURE_CLIENT_ID,
            "client_secret": AZURE_CLIENT_SECRET,
            "scope": GRAPH_SCOPE,
        },
        timeout=15,
    )
    resp.raise_for_status()
    payload = resp.json()

    _token_cache["access_token"] = payload["access_token"]
    _token_cache["expires_at"] = now + payload.get("expires_in", 3600)
    return _token_cache["access_token"]
