#!/usr/bin/env python3
"""Demo 04 setup: a notebook "session" token minted by token exchange, then downscoped. Idempotent.

  login token (aud=jupyter-session)                                   ← user signs in (hop 0)
    └─ jupyter-session  (SVID .../ns/jupyter/sa/jupyter-session) exchanges it, scope=session:token_exchange
         → session token (aud=jupyter, scp=["session:token_exchange"])  (hop 1)
           └─ jupyter   (SVID .../ns/jupyter/sa/jupyter-user) exchanges it, scope=spark:connect
                → Spark token (aud=spark-connect, scp=["spark:connect"])  (hop 2)
                  └─ spark-connect exchanges it → aud=unity-catalog       (hop 3, as in demo 02)

Audiences chain the hops: each token is only exchangeable by the next client, so the login token
can't skip the session step. Created through the admin API (not realm-zt.json) because a realm
import that lists clientScopes replaces Keycloak's built-in default scopes.

  --bind CLIENTS  RFC 8705 certificate-bound tokens (cnf.x5t#S256 = caller's X.509-SVID, via Istio
                  XFCC → Keycloak "envoy" lookup) for exactly these clients, e.g. "spark-connect",
                  "all" (jupyter-session,jupyter,spark-connect) or "none".
                  Keycloak only lets the client a bound token was issued to exchange it, so binding
                  a token that is handed to the next workload breaks the chain (see demo 04 README).
"""
import argparse, json, pathlib, sys
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from kcadmin import KC

ap = argparse.ArgumentParser()
ap.add_argument("--bind")
args = ap.parse_args()
kc = KC()
TD = "spiffe://zt.lab"

def aud_mapper(target):
    return {"name": f"aud-{target}", "protocol": "openid-connect", "protocolMapper": "oidc-audience-mapper",
            "config": {"included.client.audience": target, "access.token.claim": "true",
                       "id.token.claim": "false", "introspection.token.claim": "true"}}

def scp_mapper(values):
    return {"name": "scp", "protocol": "openid-connect", "protocolMapper": "oidc-hardcoded-claim-mapper",
            "config": {"claim.name": "scp", "claim.value": json.dumps(values), "jsonType.label": "JSON",
                       "access.token.claim": "true", "id.token.claim": "false",
                       "userinfo.token.claim": "false", "introspection.token.claim": "true"}}

GROUPS = {"name": "groups", "protocol": "openid-connect", "protocolMapper": "oidc-group-membership-mapper",
          "config": {"claim.name": "groups", "full.path": "false", "access.token.claim": "true",
                     "id.token.claim": "false", "introspection.token.claim": "true"}}

def ensure_scope(name, desc, mappers):
    rep = {"name": name, "description": desc, "protocol": "openid-connect",
           "attributes": {"include.in.token.scope": "true", "display.on.consent.screen": "false"},
           "protocolMappers": mappers}
    cur = next((s for s in kc.get("/client-scopes") if s["name"] == name), None)
    if cur is None:
        kc.post("/client-scopes", rep); print(f"scope {name}: created")
        cur = next(s for s in kc.get("/client-scopes") if s["name"] == name)
    else:  # replace mappers so edits here take effect
        for m in kc.get(f"/client-scopes/{cur['id']}/protocol-mappers/models"):
            kc.req("DELETE", f"/client-scopes/{cur['id']}/protocol-mappers/models/{m['id']}")
        for m in mappers:
            kc.post(f"/client-scopes/{cur['id']}/protocol-mappers/models", m)
        kc.put(f"/client-scopes/{cur['id']}", {**cur, **{k: rep[k] for k in ("description", "attributes")}})
        print(f"scope {name}: updated")
    return cur["id"]

def ensure_client(rep, optional_scopes=()):
    found = kc.get(f"/clients?clientId={rep['clientId']}")
    if found:
        cid = found[0]["id"]
        cur = kc.get(f"/clients/{cid}")
        for m in cur.get("protocolMappers", []):
            kc.req("DELETE", f"/clients/{cid}/protocol-mappers/models/{m['id']}")
        cur.update({k: v for k, v in rep.items() if k not in ("attributes", "protocolMappers")})
        cur.pop("protocolMappers", None)
        cur.setdefault("attributes", {}).update(rep.get("attributes", {}))
        kc.put(f"/clients/{cid}", cur)
        for m in rep.get("protocolMappers", []):
            kc.post(f"/clients/{cid}/protocol-mappers/models", m)
        print(f"client {rep['clientId']}: updated")
    else:
        kc.post("/clients", rep); cid = kc.client_uuid(rep["clientId"]); print(f"client {rep['clientId']}: created")
    for sid in optional_scopes:
        kc.put(f"/clients/{cid}/optional-client-scopes/{sid}", None)
    return cid

def workload(client_id, sa, desc, mappers):
    return {"clientId": client_id, "name": client_id, "description": desc, "enabled": True,
            "publicClient": False, "clientAuthenticatorType": "federated-jwt",
            "standardFlowEnabled": False, "directAccessGrantsEnabled": False,
            "serviceAccountsEnabled": False, "consentRequired": False,
            "attributes": {"jwt.credential.issuer": "spiffe", "jwt.credential.sub": f"{TD}/ns/jupyter/sa/{sa}",
                           "standard.token.exchange.enabled": "true"},
            "protocolMappers": mappers}

session = ensure_scope("session:token_exchange",
    "Notebook session token. Only usable as the subject of further (downscoped) exchanges by client jupyter.",
    [aud_mapper("jupyter"), scp_mapper(["session:token_exchange"])])
spark = ensure_scope("spark:connect", "Downscoped token for Spark Connect.",
    [aud_mapper("spark-connect"), scp_mapper(["spark:connect"])])

# Hop 0 stand-in for the JupyterHub login: login token audience is jupyter-session only.
ensure_client({"clientId": "lab-cli-jupyter", "name": "Lab CLI, notebook login (demo only)",
    "description": "LAB ONLY: password grant standing in for the JupyterHub login. Tokens carry aud=jupyter-session only.",
    "enabled": True, "publicClient": True, "standardFlowEnabled": False, "directAccessGrantsEnabled": True,
    "protocolMappers": [aud_mapper("jupyter-session"), GROUPS]})

# The real hop 0: JupyterHub's login client (defined in realm-zt.json). Its access token must name
# only jupyter-session, so the hub's workload client is the only one that can exchange it.
jh = kc.client_uuid("jupyterhub")
for m in kc.get(f"/clients/{jh}/protocol-mappers/models"):
    if m["protocolMapper"] == "oidc-audience-mapper" and m["name"] != "aud-jupyter-session":
        kc.req("DELETE", f"/clients/{jh}/protocol-mappers/models/{m['id']}")
if not any(m["name"] == "aud-jupyter-session" for m in kc.get(f"/clients/{jh}/protocol-mappers/models")):
    kc.post(f"/clients/{jh}/protocol-mappers/models", aud_mapper("jupyter-session"))
print("client jupyterhub: login token aud=jupyter-session")

ensure_client(workload("jupyter-session", "jupyter-session",
    "Hub-side workload. Exchanges a user's login token for a session:token_exchange token (aud=jupyter).",
    [GROUPS]), [session])
ensure_client(workload("jupyter", "jupyter-user",
    "Notebook workload. Exchanges the session token for downscoped tokens (spark:connect).",
    [GROUPS]), [spark])

if args.bind:
    allc = ("jupyter-session", "jupyter", "spark-connect")
    on = set(allc) if args.bind == "all" else set() if args.bind == "none" else set(args.bind.split(","))
    for c in allc:
        cid = kc.client_uuid(c); rep = kc.get(f"/clients/{cid}")
        rep["attributes"]["tls.client.certificate.bound.access.tokens"] = str(c in on).lower()
        kc.put(f"/clients/{cid}", rep)
    print(f"certificate-bound tokens: {', '.join(sorted(on)) or 'none'}")
