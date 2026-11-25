#!/usr/bin/env python3
"""Standing delegation grants in Keycloak (realm zt).

A standing grant means: actor client X may act on behalf of user U (or every member of group G),
without asking U for interactive consent. It is stored as a Keycloak FGAP v2 permission:

  Users  resource, scope `delegate`         → per-user grants
  Groups resource, scope `delegate-members` → per-group grants
  policy: "actor:<client-id>" (client policy matching the actor's service account)

Permissions are named  standing-delegation/<actor>/users  and  standing-delegation/<actor>/groups,
and their resource lists hold the granted user or group IDs. An FGAP permission with an empty
resource list means "all", so a permission is deleted when its last grant is revoked.

Effect: when U signs in and asks for scope delegation:client:<actor>, Keycloak adds may_act only if a
grant covers U (checked when the scope is resolved). The realm client policy `standing-delegation`
re-checks it on every token response, refreshes included, so revoking takes effect at the next refresh.

CLI:
  delegation_grants.py list
  delegation_grants.py grant  --actor spark-connect (--user bob | --group data-analysts)
  delegation_grants.py revoke --actor spark-connect (--user bob | --group data-analysts)
  delegation_grants.py apply  platform/keycloak/delegation-grants.json   # idempotent seed
"""
import argparse, json, pathlib, sys
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from kcadmin import KC

KINDS = {"users": ("Users", "delegate"), "groups": ("Groups", "delegate-members")}


class Grants:
    def __init__(self, kc=None):
        self.kc = kc or KC()
        self.authz = f"/clients/{self.kc.client_uuid('admin-permissions')}/authz/resource-server"

    # --- lookups -------------------------------------------------------------------------
    def _find(self, kind, name):
        items = self.kc.get(f"{self.authz}/{kind}?name={name}&max=100") or []
        return next((i for i in items if i["name"] == name), None)

    def _user_id(self, username):
        u = self.kc.get(f"/users?username={username}&exact=true")
        if not u: sys.exit(f"no such user: {username}")
        return u[0]["id"]

    def _group_id(self, name):
        g = [g for g in self.kc.get(f"/groups?search={name}&exact=true") if g["name"] == name]
        if not g: sys.exit(f"no such group: {name}")
        return g[0]["id"]

    def _actor_policy(self, actor):
        name = f"actor:{actor}"
        p = self._find("policy", name)
        if p: return p["id"]
        self.kc.post(f"{self.authz}/policy/client", {"name": name, "logic": "POSITIVE",
            "description": f"Actor client {actor} (its service account)", "clients": [self.kc.client_uuid(actor)]})
        return self._find("policy", name)["id"]

    def _perm_name(self, actor, kind): return f"standing-delegation/{actor}/{kind}"

    def _perm(self, actor, kind):
        p = self._find("permission", self._perm_name(actor, kind))
        if not p: return None, []
        res = self.kc.get(f"{self.authz}/policy/{p['id']}/resources") or []
        return p, [r["name"] for r in res]   # FGAP v2 resource names are the user/group IDs

    def _save(self, actor, kind, ids, existing):
        rtype, scope = KINDS[kind]
        if not ids:
            if existing: self.kc.req("DELETE", f"{self.authz}/permission/{existing['id']}")
            return
        body = {"name": self._perm_name(actor, kind), "resourceType": rtype, "resources": sorted(ids),
                "scopes": [scope], "policies": [self._actor_policy(actor)], "decisionStrategy": "UNANIMOUS",
                "description": f"Standing delegation: {actor} may act on behalf of these {kind}"}
        if existing:
            self.kc.put(f"{self.authz}/permission/scope/{existing['id']}", {**body, "id": existing["id"]})
        else:
            self.kc.post(f"{self.authz}/permission/scope", body)

    # --- operations ----------------------------------------------------------------------
    def change(self, actor, kind, name, add):
        target = self._user_id(name) if kind == "users" else self._group_id(name)
        perm, ids = self._perm(actor, kind)
        ids = set(ids) | {target} if add else set(ids) - {target}
        self._save(actor, kind, ids, perm)

    def list(self):
        out = []
        perms = self.kc.get(f"{self.authz}/permission?name=standing-delegation/&max=100") or []
        for p in perms:
            if not p["name"].startswith("standing-delegation/"): continue
            _, actor, kind = p["name"].split("/")
            for rid in [r["name"] for r in self.kc.get(f"{self.authz}/policy/{p['id']}/resources") or []]:
                if kind == "users":
                    label = self.kc.get(f"/users/{rid}")["username"]
                else:
                    label = self.kc.get(f"/groups/{rid}")["name"]
                out.append({"actor": actor, kind[:-1]: label})
        return out


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list")
    for c in ("grant", "revoke"):
        s = sub.add_parser(c); s.add_argument("--actor", required=True)
        g = s.add_mutually_exclusive_group(required=True); g.add_argument("--user"); g.add_argument("--group")
    s = sub.add_parser("apply"); s.add_argument("file")
    a = ap.parse_args()
    gr = Grants()
    if a.cmd in ("grant", "revoke"):
        kind, name = ("users", a.user) if a.user else ("groups", a.group)
        gr.change(a.actor, kind, name, add=(a.cmd == "grant"))
        print(f"{a.cmd}: {a.actor} → {kind[:-1]} {name}")
    elif a.cmd == "apply":
        for g in json.loads(pathlib.Path(a.file).read_text()):
            kind, name = ("users", g["user"]) if "user" in g else ("groups", g["group"])
            gr.change(g["actor"], kind, name, add=True)
    for g in gr.list():
        print("  " + json.dumps(g))

if __name__ == "__main__":
    main()
