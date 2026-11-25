"""Lesson 1 checks: SPIFFE and SPIRE."""
import copy
import json
import re
import time

from common import (ROOT, Report, cert_fields, guard, jwt_parts, kjson, kubectl, openssl_date, run,
                    section, smoke_jwt, spire_server)

TITLE = "Lesson 1: Workload identity with SPIFFE and SPIRE"

TEMPLATE = '''# Lesson 1 answers. Fill in values, then run: check 1

[e1_1]
# Which node runs no SPIRE agent?
node_without_agent = ""

[e1_2]
# How many registration entries exist right now for spiffe://zt.lab/ns/spark/sa/spark-connect?
spark_connect_entries = 0
# The names of the pods those entries belong to (current pod names, with suffixes)
spark_connect_entry_pods = []
# What does an entry's Parent ID pin it to? (a few words)
parent_id_pins_to = ""

[e1_3]
# TTL of an X.509-SVID in hours
x509_ttl_hours = 0
# Serial of spark-connect's current X.509-SVID (hex, as printed by `svid`)
spark_connect_serial = ""
# spark-connect and obo-client have identical SANs. What differs that matters? (a few words)
svid_difference = ""

[e1_4]
# JWT-SVID lifetime (exp - iat) in seconds
jwt_svid_ttl_seconds = 0
# Does fetching twice in a row return the identical token? true/false
jwt_svid_repeat_identical = ""
# Why? (a few words)
jwt_svid_repeat_reason = ""
# The kid of the key that signs JWT-SVIDs right now
jwt_svid_kid = ""
# Oldest SPIFFE client assertion (by iat) Keycloak accepts, in seconds
assertion_max_age_seconds = 0

[e1_5]
# Your prediction, then the observed result: uc-gateway's HTTP status for the rogue pod's request
rogue_gateway_status = 0
# The one ServiceAccount in the lab (namespace/name) that can create pods
pod_creating_sa = ""
# ...and the namespace it can create them in
pod_creating_sa_scope = ""

[e1_6]
# With defaultJwtSvidTTL = 20m: the window, in minutes after a fresh mint, during which
# hop 3 fails, as [start, end]
failure_window_minutes = []
# What you observed when you ran it (minutes after a fresh mint), as [start, end]
observed_failure_minutes = []
'''

SPARK_ID = "spiffe://zt.lab/ns/spark/sa/spark-connect"
GW = "http://uc-gateway.unity-catalog.svc.cluster.local:8080"


def e1_1(r: Report):
    section("1.1 node attestation")

    def probe():
        nodes = {n["metadata"]["name"] for n in kjson("get", "nodes")["items"]}
        agents = {p["spec"].get("nodeName") for p in
                  kjson("-n", "spire-server", "get", "pods", "-l", "app.kubernetes.io/name=agent")["items"]}
        missing = sorted(nodes - agents)
        return missing[0] if len(missing) == 1 else missing

    exp = guard(r, "e1_1.node_without_agent", probe)
    if exp is not None:
        r.live("e1_1.node_without_agent", exp, "match `spire-server agent list` IDs to node UIDs")


def e1_2(r: Report):
    section("1.2 registration entries")

    def probe():
        out = spire_server("entry", "show", "-spiffeID", SPARK_ID)
        uids = re.findall(r"k8s:pod-uid:(\S+)", out)
        pods = {p["metadata"]["uid"]: p["metadata"]["name"] for p in kjson("get", "pods", "-A")["items"]}
        return len(uids), sorted(pods.get(u, f"<gone:{u[:8]}>") for u in uids)

    res = guard(r, "e1_2", probe)
    if res:
        r.live("e1_2.spark_connect_entries", res[0], "spire-server entry show -spiffeID ...")
        r.live("e1_2.spark_connect_entry_pods", res[1], "match each k8s:pod-uid selector to a pod's UID")
    r.keyword("e1_2.parent_id_pins_to", {"8e61b9e6df358d0d", "7fabc51579f5234d"},
              "compare the Parent ID with the IDs in `spire-server agent list`")


def e1_3(r: Report):
    section("1.3 X.509-SVID")
    f = guard(r, "e1_3", lambda: cert_fields("spark", "deploy/spark-connect"))
    if f:
        ttl = round((openssl_date(f["notAfter"]) - openssl_date(f["notBefore"])) / 3600)
        r.live("e1_3.x509_ttl_hours", ttl, "notAfter - notBefore, in hours")
        got = r.answer("e1_3.spark_connect_serial")
        if got is None:
            r.todo("e1_3.spark_connect_serial")
        elif str(got).replace(":", "").lower() == f["serial"].lower():
            r.ok("e1_3.spark_connect_serial", "matches the live SVID")
        else:
            r.fail("e1_3.spark_connect_serial",
                   "doesn't match: the SVID may have rotated since you looked (that's 2.x's question)")
    r.keyword("e1_3.svid_difference",
              {"c553e582f4cd3dc0", "0a79ff181b5f2f87", "0b8a205755d20a53", "07a4869271fb8dec",
               "e1821e4dc30dfe6d", "a3ed60bdfc08a1c1", "0783663700363542", "3ffb7bd46e059fa2"},
              "run `svid` on both and diff everything except the SAN")


def e1_4(r: Report):
    section("1.4 JWT-SVID")

    def probe():
        t1 = smoke_jwt()
        t2 = smoke_jwt()
        hdr, pl = jwt_parts(t1)
        keys = json.loads(kubectl("-n", "spark", "exec", "deploy/obo-client", "-c", "curl", "--", "curl", "-sk",
                                  "https://spire-spiffe-oidc-discovery-provider.spire-server.svc.cluster.local/keys"
                                  ).stdout)["keys"]
        return pl["exp"] - pl["iat"], t1 == t2, hdr["kid"], {k["kid"] for k in keys}

    res = guard(r, "e1_4", probe)
    if res:
        ttl, same, kid, jwks = res
        r.live("e1_4.jwt_svid_ttl_seconds", ttl, "exp - iat of a freshly fetched JWT-SVID")
        r.live("e1_4.jwt_svid_repeat_identical", same, "fetch twice and compare")
        got = r.answer("e1_4.jwt_svid_kid")
        if got is None:
            r.todo("e1_4.jwt_svid_kid")
        elif got == kid and got in jwks:
            r.ok("e1_4.jwt_svid_kid", "in the JWKS and signing current SVIDs")
        elif got in jwks:
            r.fail("e1_4.jwt_svid_kid", "that kid is published but isn't the one signing SVIDs now")
        else:
            r.fail("e1_4.jwt_svid_kid", "not in the OIDC discovery provider's /keys")
    r.keyword("e1_4.jwt_svid_repeat_reason",
              {"9040e8d79d48dfee", "caef0deb2dab457d", "d4e5616d80720561", "eee8180fef7ea2b2"},
              "platform/spire/values.yaml, the comment above defaultJwtSvidTTL")
    r.hashed("e1_4.assertion_max_age_seconds", "3fa0171a982b775b",
             "README, 'JWT-SVID freshness' (fedClientAssertionMaxExp)")


def e1_5(r: Report):
    section("1.5 the issuance boundary")
    r.hashed("e1_5.rogue_gateway_status", "70c43b4021f12b6f", "run step 3 and look at the last line")

    def probe():
        found = []
        for ns in ("jupyter", "spark", "unity-catalog", "keycloak", "storage", "mesh-test"):
            for sa in kjson("-n", ns, "get", "sa")["items"]:
                name = sa["metadata"]["name"]
                for target in ("jupyter", "spark", "unity-catalog", "keycloak", "storage"):
                    p = kubectl("auth", "can-i", "create", "pods", "-n", target,
                                "--as", f"system:serviceaccount:{ns}:{name}", check=False)
                    if p.stdout.strip() == "yes":
                        found.append((f"{ns}/{name}", target))
        return found

    found = guard(r, "e1_5.pod_creating_sa", probe)
    if found is not None:
        if len(found) == 1:
            r.live("e1_5.pod_creating_sa", found[0][0], "kubectl auth can-i create pods --as=system:serviceaccount:<ns>:<sa>")
            r.live("e1_5.pod_creating_sa_scope", found[0][1], "which namespace?")
        else:
            r.skip("e1_5.pod_creating_sa", f"cluster now has {len(found)} pod-creating SAs: {found}")
    rogue = kubectl("-n", "spark", "get", "pod", "rogue", check=False)
    r.ok("cleanup: rogue pod deleted") if rogue.returncode != 0 else r.fail(
        "cleanup: rogue pod deleted", "kubectl -n spark delete pod rogue")


def e1_6(r: Report):
    section("1.6 JWT-SVID TTL vs freshness")
    r.hashed("e1_6.failure_window_minutes", "2cadcea7762a5b50",
             "oldest presented SVID ≈ age at re-mint (T/2); Keycloak rejects age > 300 s")
    obs = r.answer("e1_6.observed_failure_minutes")
    if obs is None:
        r.todo("e1_6.observed_failure_minutes", "run the experiment and record what you saw")
    elif isinstance(obs, list) and len(obs) == 2 and 3 <= obs[0] <= 7 and 8 <= obs[1] <= 12:
        r.ok("e1_6.observed_failure_minutes", "consistent with the model")
    else:
        r.fail("e1_6.observed_failure_minutes",
               "far from the model; worth discussing (clock skew? which SVID cache did you watch?)")

    def probe():
        vals = json.loads(run(["helm", "--kube-context", "kind-zt", "get", "values", "spire", "-n", "spire-server",
                               "-o", "json"]).stdout)
        return (vals.get("spire-server") or {}).get("defaultJwtSvidTTL"), smoke_jwt()

    res = guard(r, "revert", probe)
    if res:
        ttl, tok = res
        _, pl = jwt_parts(tok)
        if ttl != "5m":
            r.fail("reverted: helm value", f"defaultJwtSvidTTL is {ttl!r}; rerun the revert command")
        elif pl["exp"] - pl["iat"] != 300:
            r.fail("reverted: live SVIDs", "helm value is back but cached 20m SVIDs remain; wait or restart agents")
        else:
            r.ok("reverted", "helm value and live SVIDs both at 5m")


# ---------- 1.7 challenge: admission control for the spark-connect identity ----------

def _pod_from(src, name=None):
    """A creatable copy of a live pod (keeps ownerReferences, so it looks controller-made)."""
    p = copy.deepcopy(src)
    md = p["metadata"]
    for k in ("uid", "resourceVersion", "creationTimestamp", "managedFields", "name"):
        md.pop(k, None)
    md["generateName"] = name or (src["metadata"]["name"].rsplit("-", 1)[0] + "-")
    p.pop("status", None)
    p["spec"].pop("nodeName", None)
    return p


def _dry_create(obj, as_user=None):
    p = kubectl("create", "--dry-run=server", "-o", "name", "-f", "-", input=json.dumps(obj),
                check=False, as_user=as_user)
    return p.returncode == 0, (p.stderr or p.stdout).strip().splitlines()[-1:] or [""]


def e1_7(r: Report):
    section("1.7 challenge: only approved workloads may become spark-connect")
    f = ROOT / "platform" / "admission" / "spark-identity.yaml"
    r.ok("policy file", str(f.relative_to(ROOT))) if f.exists() else r.todo(
        "policy file", "write platform/admission/spark-identity.yaml")
    vap = guard(r, "policy installed", lambda: kjson("get", "validatingadmissionpolicybindings")["items"])
    if vap is None:
        return
    if not vap:
        return r.todo("policy installed", "no ValidatingAdmissionPolicyBinding in the cluster yet")
    r.ok("policy installed", ", ".join(b["metadata"]["name"] for b in vap))

    RS_CTRL = "system:serviceaccount:kube-system:replicaset-controller"
    pods = {p["metadata"]["labels"].get("app"): p for p in kjson("-n", "spark", "get", "pods")["items"]
            if p["status"].get("phase") == "Running"}
    rogue_pod = json.loads(kubectl("create", "--dry-run=client", "-o", "json",
                                   "-f", str(ROOT / "curriculum" / "labs" / "01-rogue.yaml")).stdout)

    def must(key, allowed, obj, as_user=None, stretch=False):
        ok, msg = _dry_create(obj, as_user)
        good = ok == allowed
        detail = ("allowed" if ok else "denied") + ("" if good or ok else f": {msg[0][:70]}")
        if stretch:
            return r.stretch(key, good, detail)
        r.ok(key, detail) if good else r.fail(key, ("should be allowed, got " if allowed else
                                                    "should be denied, got ") + detail)

    # 1. what the platform does must keep working
    if "spark-connect" in pods:
        must("allow: spark-connect pod by its RS", True, _pod_from(pods["spark-connect"]), RS_CTRL)
    if "obo-client" in pods:
        must("allow: obo-client pod by its RS", True, _pod_from(pods["obo-client"]), RS_CTRL)
    p = kubectl("apply", "--dry-run=server", "-f", str(ROOT / "apps" / "spark" / "spark-connect.yaml"), check=False)
    r.ok("allow: re-apply spark-connect.yaml") if p.returncode == 0 else r.fail(
        "allow: re-apply spark-connect.yaml", p.stderr.strip().splitlines()[-1][:90])
    if "imposter" in pods:
        must("allow: other SAs unaffected", True, _pod_from(pods["imposter"]), RS_CTRL)

    # 2. the attack from 1.5 must fail
    must("deny: bare rogue pod (you)", False, rogue_pod)
    dep = {"apiVersion": "apps/v1", "kind": "Deployment",
           "metadata": {"name": "rogue", "namespace": "spark"},
           "spec": {"selector": {"matchLabels": {"app": "rogue"}},
                    "template": {"metadata": {"labels": {"app": "rogue"}}, "spec": rogue_pod["spec"]}}}
    must("deny: rogue Deployment (you)", False, dep)

    # 3. stretch: the bypasses a name/ownerRef-only policy misses
    if "spark-connect" in pods:
        must("deny: forged ownerRef pod (you)", False, _pod_from(pods["spark-connect"]), stretch=True)
        rs_name = pods["spark-connect"]["metadata"]["ownerReferences"][0]["name"]
        rs = kjson("-n", "spark", "get", "rs", rs_name)
        evil = _pod_from(rs, "spark-connect-evil-")
        evil["metadata"].pop("ownerReferences", None)
        evil["metadata"].pop("annotations", None)
        must("deny: RS 'spark-connect-evil' (you)", False, evil, stretch=True)


EXERCISES = {"1.1": e1_1, "1.2": e1_2, "1.3": e1_3, "1.4": e1_4, "1.5": e1_5, "1.6": e1_6, "1.7": e1_7}
