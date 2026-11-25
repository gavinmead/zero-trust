#!/usr/bin/env python3
"""Scripted JupyterHub sign-in through Keycloak (auth code + PKCE), then start the user's server.
Lab/demo use: stands in for a person signing in at http://jupyter.localhost.

  HUB_PW=<password> hub_login.py <username>
"""
import http.cookiejar, os, pathlib, sys, urllib.parse, urllib.request
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from browser_login import Forms, LocalhostSecurePolicy

user, pw = sys.argv[1], os.environ["HUB_PW"]
op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(
    http.cookiejar.CookieJar(policy=LocalhostSecurePolicy())))

def get(url, data=None):
    with op.open(url, urllib.parse.urlencode(data).encode() if data else None) as r:
        return r.geturl(), r.read().decode(errors="replace")

url, body = get("http://jupyter.localhost/hub/oauth_login")
p = Forms(); p.feed(body)
f = next(f for f in p.forms if "username" in f["inputs"] or f["id"] == "kc-form-login")
url, _ = get(urllib.parse.urljoin(url, f["action"]), {**f["inputs"], "username": user, "password": pw})
if "jupyter.localhost" not in url:
    sys.exit(f"{user}: sign-in did not return to the hub")
get("http://jupyter.localhost/hub/spawn")
print(f"{user}: signed in, server starting")
