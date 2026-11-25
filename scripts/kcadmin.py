"""Tiny Keycloak admin REST helper (stdlib only). Reads admin creds from .secrets/keycloak.env.
Usage as a module:  from kcadmin import KC;  kc = KC();  kc.get("/clients?clientId=x")
"""
import json, os, pathlib, urllib.parse, urllib.request, urllib.error

ROOT = pathlib.Path(__file__).resolve().parent.parent
BASE = os.environ.get("KC_URL", "http://keycloak.localhost")
REALM = os.environ.get("KC_REALM", "zt")

def _env():
    env = {}
    for line in (ROOT / ".secrets/keycloak.env").read_text().splitlines():
        if "=" in line and not line.startswith("#"):
            k, v = line.split("=", 1); env[k] = v
    return env

class KC:
    def __init__(self):
        e = _env()
        data = urllib.parse.urlencode({"grant_type": "password", "client_id": "admin-cli",
            "username": e["KC_BOOTSTRAP_ADMIN_USERNAME"], "password": e["KC_BOOTSTRAP_ADMIN_PASSWORD"]}).encode()
        with urllib.request.urlopen(f"{BASE}/realms/master/protocol/openid-connect/token", data) as r:
            self.token = json.load(r)["access_token"]

    def req(self, method, path, body=None, realm=True):
        url = f"{BASE}/admin/realms/{REALM}{path}" if realm else f"{BASE}/admin/realms{path}"
        data = json.dumps(body).encode() if body is not None else None
        rq = urllib.request.Request(url, data=data, method=method,
            headers={"Authorization": f"Bearer {self.token}", "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(rq) as r:
                raw = r.read()
                return (json.loads(raw) if raw else None), r.headers.get("Location")
        except urllib.error.HTTPError as ex:
            raise RuntimeError(f"{method} {path} -> {ex.code}: {ex.read().decode()[:400]}") from None

    def get(self, p): return self.req("GET", p)[0]
    def post(self, p, b): return self.req("POST", p, b)
    def put(self, p, b): return self.req("PUT", p, b)
    def client_uuid(self, client_id): return self.get(f"/clients?clientId={client_id}")[0]["id"]
