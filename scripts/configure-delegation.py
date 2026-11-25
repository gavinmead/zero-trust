#!/usr/bin/env python3
"""Realm-level setup for *standing* delegation grants (no interactive consent). Idempotent.

1. Built-in scope `delegation:client` (created by the token-exchange-delegation feature) is
   always-consent by default, which Keycloak refuses on non-consent clients. Turn that off.
2. Client policy `standing-delegation` (executor `may-act`, avoid-consent-check=true) re-validates
   every may_act in a token response against the FGAP `delegate` permission. Consent is not required.
3. Seed grants from platform/keycloak/delegation-grants.json (see scripts/delegation_grants.py).
4. Remove the consent-era permission from the earlier setup, if present.
"""
import json, pathlib, sys
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from kcadmin import KC
from delegation_grants import Grants

ROOT = pathlib.Path(__file__).resolve().parent.parent
kc = KC()

# 1. delegation scopes: no consent screen
for s in kc.get("/client-scopes"):
    if s["name"] in ("delegation:client", "delegation:user"):
        s["attributes"].update({"always.display.consent": "false", "display.on.consent.screen": "false"})
        kc.put(f"/client-scopes/{s['id']}", s)
        print(f"scope {s['name']}: consent off")

# 2. client policy: re-check the delegate permission, skip the consent check
kc.put("/client-policies/profiles", {"profiles": [{
    "name": "standing-delegation",
    "description": "may_act is valid only while a standing delegation grant (FGAP delegate permission) exists",
    "executors": [{"executor": "may-act", "configuration": {
        "reject-any": False, "avoid-permission-check": False, "avoid-consent-check": True}}]}]})
kc.put("/client-policies/policies", {"policies": [{
    "name": "standing-delegation", "enabled": True,
    "description": "Applies the standing-delegation profile to every client",
    "conditions": [{"condition": "any-client", "configuration": {}}],
    "profiles": ["standing-delegation"]}]})
print("client policy standing-delegation: may-act (permission check on, consent check off)")

# 3. seed grants
g = Grants(kc)
for grant in json.loads((ROOT / "platform/keycloak/delegation-grants.json").read_text()):
    kind, name = ("users", grant["user"]) if "user" in grant else ("groups", grant["group"])
    g.change(grant["actor"], kind, name, add=True)

# 4. clean up the consent-era permission/policy names
authz = g.authz
for kind, name in (("permission", "spark-connect-may-act-for-data-analysts"), ("policy", "actor-is-spark-connect")):
    for p in kc.get(f"{authz}/{kind}?name={name}") or []:
        if p["name"] == name:
            kc.req("DELETE", f"{authz}/policy/{p['id']}"); print(f"removed old {kind} {name}")

print("standing grants:")
for x in g.list():
    print("  " + json.dumps(x))
