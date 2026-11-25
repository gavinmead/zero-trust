"""Lesson 0 checks: orientation and threat model."""
import re

from common import ROOT, Report, guard, jwt_parts, kjson, nb, run, section

TITLE = "Lesson 0: Orientation and threat model"

TEMPLATE = '''# Lesson 0 answers. Fill in values, then run: check 0
# Leave a value empty ("" or []) until you have it. Names are pod/deployment names
# as `trace` prints them, without the random suffix (e.g. "hub", "jupyter-alice").

[e0_2]
# Which namespaces outside the Kubernetes/Istio system ones run pods WITHOUT sidecar injection?
# (Ignore default, kube-*, local-path-storage, istio-system.)
uninjected_namespaces = []

[e0_3]
# Which pods POST to Keycloak's token endpoint during a fresh notebook query?
keycloak_token_callers = []
# Map each of those callers to its hop number in the README's table, e.g. { hub = 9 }
hop_of = {}
# What is Unity Catalog's "POST /" to MinIO? (a few words)
uc_post_to_minio = ""
# Who asked for operation=READ_WRITE on a read-only count()? (a few words)
read_write_requested_by = ""
# Who decides whether alice actually gets write credentials? (a few words)
write_decided_by = ""

[e0_4]
# Copy these from the decoded tokens in alice's notebook.
session_token_aud = ""
spark_token_aud = ""
spark_token_azp = ""
spark_token_scp = []
uc_token_azp = ""          # the Keycloak token from hop 3 (spark.zt.uc.claims)
# Do the session token and the Spark token expire at the same instant? true/false
same_exp = ""
# Which claim does the end of the chain need that forces "email" scope into the session token?
claim_needed_downstream = ""
'''

THREAT_TEMPLATE = '''# Threat model (lesson 0.5; revisited in lessons 5, 9 and 10)

For each adversary: what they can reach, which credentials they can get, what they can do with
them, **for how long**, and which control limits them. Be specific: name tokens, audiences,
lifetimes and SPIFFE IDs.

## A. Malicious code in alice's notebook

## B. Bob

## C. Compromised Spark Connect server

## D. In-cluster network attacker (no sidecar)

## E. Token leaked to logs

## F. Cluster insider who can create pods in namespace spark
'''

SYSTEM_NS = {"default", "kube-system", "kube-public", "kube-node-lease", "local-path-storage", "istio-system"}


def init_extra():
    notes = ROOT / "curriculum" / "notes"
    notes.mkdir(parents=True, exist_ok=True)
    f = notes / "threat-model.md"
    if not f.exists():
        f.write_text(THREAT_TEMPLATE)
        print(f"created {f.relative_to(ROOT)}")


def e0_1(r: Report):
    section("0.1 version control")
    git = lambda *a: run(["git", "-C", str(ROOT), *a], check=False)
    if git("rev-parse", "--git-dir").returncode != 0:
        return r.todo("git repo", "not a git repository yet (git init)")
    r.ok("git repo")
    if git("rev-parse", "HEAD").returncode != 0:
        r.todo("first commit", "no commits yet")
    else:
        r.ok("first commit", git("log", "-1", "--format=%h %s").stdout.strip()[:60])
    tracked = git("ls-files").stdout.splitlines()
    leaked = [f for f in tracked if f.startswith(".secrets/")]
    r.fail(".secrets not tracked", f"tracked: {leaked[:3]}") if leaked else r.ok(".secrets not tracked")
    ign = git("check-ignore", "-q", ".secrets/keycloak.env").returncode == 0
    r.ok(".secrets ignored") if ign else r.fail(".secrets ignored", ".gitignore must cover .secrets/")
    pyc = [f for f in tracked if "__pycache__" in f]
    ign = git("check-ignore", "-q", "scripts/__pycache__/x.pyc").returncode == 0
    if pyc:
        r.fail("__pycache__ not tracked", f"tracked: {pyc[:2]} (git rm --cached)")
    elif not ign:
        r.fail("__pycache__ ignored", "add it to .gitignore")
    else:
        r.ok("__pycache__ ignored")


def e0_2(r: Report):
    section("0.2 health check")

    def probe():
        out = set()
        for ns in kjson("get", "ns")["items"]:
            name = ns["metadata"]["name"]
            if name in SYSTEM_NS:
                continue
            if ns["metadata"].get("labels", {}).get("istio-injection") != "enabled":
                out.add(name)
        return sorted(out)

    exp = guard(r, "e0_2.uninjected_namespaces", probe)
    if exp is not None:
        r.live("e0_2.uninjected_namespaces", exp,
               "compare with `kubectl get ns -L istio-injection`")


def e0_3(r: Report):
    section("0.3 trace one query")
    r.hashed("e0_3.keycloak_token_callers", {"e59f63de184d5919", "e7f00e3fafa6eb64"},
             "run the query, then `trace 30 | grep openid-connect/token`; there are three distinct callers")
    r.hashed("e0_3.hop_of", {"cc5855f04604f68e", "de90a21920ce9997"},
             "README, 'Demo 04 inside JupyterHub': hops 1-3 and who performs each")
    r.keyword("e0_3.uc_post_to_minio", {"98f3deaf99c44799", "7c10f911888b2f89"},
              "README, 'Credential vending to MinIO': what UC calls at MinIO for each table access")
    r.keyword("e0_3.read_write_requested_by", {"0b2383c081cb1787", "3d9675cdbba16226", "e7eeadf6ca00f432"},
              "look at the User-Agent on the credentials request in the raw uc-gateway access log")
    r.keyword("e0_3.write_decided_by", {"13e4da4a9dd5656b", "585aca9c1ed89745"},
              "which component holds the grants? (demo 05: bob gets read-only credentials)")


def e0_4(r: Report):
    section("0.4 credential inventory")
    code = ('import json, zt_auth\n'
            'print(zt_auth.session_token()); print(zt_auth.spark_token())\n'
            's = zt_auth.spark_session(); s.range(1).collect(); print(s.conf.get("spark.zt.uc.claims"))\n')
    out = guard(r, "e0_4 (tokens)", lambda: nb(code).splitlines())
    if not out:
        return r.skip("e0_4", "needs alice signed in to JupyterHub (make lakehouse-demo signs her in)")
    import json
    _, sess = jwt_parts(out[0])
    _, spark = jwt_parts(out[1])
    uc = json.loads(out[2])["keycloak"]
    r.live("e0_4.session_token_aud", sess["aud"], "decode zt_auth.session_token() with `jwt`")
    r.live("e0_4.spark_token_aud", spark["aud"], "decode zt_auth.spark_token() with `jwt`")
    r.live("e0_4.spark_token_azp", spark["azp"], "azp is the client that obtained the token")
    r.live("e0_4.spark_token_scp", spark.get("scp", []), "the scp claim, as a list")
    r.live("e0_4.uc_token_azp", uc["azp"], "spark.zt.uc.claims → keycloak.azp")
    r.live("e0_4.same_exp", sess["exp"] == spark["exp"], "compare the exp claims (not iat)")
    r.keyword("e0_4.claim_needed_downstream", {"4271069da813522f"},
              "README, Unity Catalog: how does UC map an external token to a UC user?")


def e0_5(r: Report):
    section("0.5 threat model")
    f = ROOT / "curriculum" / "notes" / "threat-model.md"
    if not f.exists():
        return r.todo("threat-model.md", "run `check 0 --init` to create the skeleton")
    parts = re.split(r"^## ", f.read_text(), flags=re.M)[1:]
    by_letter = {p[0]: p for p in parts if p[:2] in {"A.", "B.", "C.", "D.", "E.", "F."}}
    for letter in "ABCDEF":
        body = by_letter.get(letter, "")
        text = body.split("\n", 1)[1] if "\n" in body else ""
        n = len(re.findall(r"\w+", text))
        timed = re.search(r"\b\d+\s*(s|sec|seconds?|m|mins?|minutes?|h|hours?)\b", text)
        key = f"adversary {letter}"
        if n == 0:
            r.todo(key, "empty")
        elif n < 40:
            r.fail(key, f"{n} words; be specific (≥40)")
        elif not timed:
            r.fail(key, "say for how long the attacker keeps access (e.g. '≤10 min')")
        else:
            r.ok(key, f"{n} words, time-bounded")


EXERCISES = {"0.1": e0_1, "0.2": e0_2, "0.3": e0_3, "0.4": e0_4, "0.5": e0_5}
