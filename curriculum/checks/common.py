"""Shared helpers for the lesson checkers (curriculum/bin/check).

Three kinds of check:
  live    the expected value is computed from the cluster at check time (nothing stored)
  hashed  the expected value is fixed; only a salted SHA-256 is stored, so the repo
          holds no readable answer key
  state   the cluster (or repo) must be in a given state, probed directly
"""
import base64
import hashlib
import json
import os
import re
import subprocess
import time
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
ANSWERS = ROOT / "curriculum" / "answers"
CTX = os.environ.get("CTX", "kind-zt")
SALT = "zt-lab-curriculum:"
REALM_URL = "http://keycloak.localhost/realms/zt"


# ---------- shell / kubectl ----------
class CmdError(RuntimeError):
    pass


def run(cmd, input=None, check=True):
    p = subprocess.run(cmd, input=input, capture_output=True, text=True)
    if check and p.returncode != 0:
        raise CmdError((p.stderr or p.stdout).strip()[:400])
    return p


def kubectl(*args, input=None, check=True, as_user=None):
    cmd = ["kubectl", "--context", CTX]
    if as_user:
        cmd += ["--as", as_user]
    return run(cmd + list(args), input=input, check=check)


def kjson(*args):
    return json.loads(kubectl(*args, "-o", "json").stdout)


def nb(code, user="alice"):
    """Run python in a user's JupyterLab pod (as a file: pyspark breaks under python -c)."""
    p = kubectl("-n", "jupyter", "exec", "-i", f"jupyter-{user}", "-c", "notebook", "--",
                "sh", "-c", "cat > /tmp/zt-check.py && python /tmp/zt-check.py", input=code)
    return p.stdout.strip()


def spire_server(*args):
    return kubectl("-n", "spire-server", "exec", "spire-server-0", "-c", "spire-server", "--",
                   "/opt/spire/bin/spire-server", *args).stdout


def smoke_jwt(audience=REALM_URL):
    out = kubectl("-n", "spire-smoke", "exec", "smoke", "--", "/opt/spire/bin/spire-agent", "api",
                  "fetch", "jwt", "-audience", audience, "-output", "json",
                  "-socketPath", "/spiffe-workload-api/spire-agent.sock").stdout
    return json.loads(out)[0]["svids"][0]["svid"]



def cert_fields(ns, workload):
    d = json.loads(run(["istioctl", "--context", CTX, "proxy-config", "secret", "-n", ns, workload,
                        "-o", "json"]).stdout)
    for s in d["dynamicActiveSecrets"]:
        c = s["secret"].get("tlsCertificate")
        if c:
            pem = base64.b64decode(c["certificateChain"]["inlineBytes"]).decode()
            first = pem.split("-----END CERTIFICATE-----")[0] + "-----END CERTIFICATE-----\n"
            out = run(["openssl", "x509", "-noout", "-serial", "-startdate", "-enddate"], input=first).stdout
            f = dict(line.split("=", 1) for line in out.strip().splitlines())
            der = subprocess.run(["openssl", "x509", "-outform", "DER"], input=first.encode(),
                                 capture_output=True, check=True).stdout
            f["x5t#S256"] = base64.urlsafe_b64encode(hashlib.sha256(der).digest()).rstrip(b"=").decode()
            return f
    raise CmdError(f"no certificate in {ns}/{workload}")


def openssl_date(s):
    return time.mktime(time.strptime(s.replace("  ", " "), "%b %d %H:%M:%S %Y GMT"))


# ---------- tokens ----------
def jwt_parts(tok):
    def dec(p):
        p += "=" * (-len(p) % 4)
        return json.loads(base64.urlsafe_b64decode(p))
    h, p = tok.strip().split(".")[:2]
    return dec(h), dec(p)


# ---------- answers ----------
def norm(v):
    """Canonical string for comparison/hashing: lists sorted, case and whitespace folded."""
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return str(int(v)) if float(v).is_integer() else str(v)
    if isinstance(v, list):
        return json.dumps(sorted(norm(x) for x in v))
    if isinstance(v, dict):
        return json.dumps({norm(k): norm(x) for k, x in sorted(v.items())}, sort_keys=True)
    return re.sub(r"\s+", " ", str(v)).strip().lower()


def h(v):
    return hashlib.sha256((SALT + norm(v)).encode()).hexdigest()[:16]


def words(v):
    return set(re.findall(r"[a-z0-9]+", str(v).lower()))


def load_answers(lesson):
    f = ANSWERS / f"{lesson:02d}.toml"
    if not f.exists():
        return None, f
    with open(f, "rb") as fh:
        return tomllib.load(fh), f


# ---------- reporting ----------
GREEN, RED, YELLOW, DIM, BOLD, OFF = "\033[32m", "\033[31m", "\033[33m", "\033[2m", "\033[1m", "\033[0m"


class Report:
    def __init__(self, answers):
        self.a = answers or {}
        self.counts = {"pass": 0, "fail": 0, "todo": 0, "skip": 0, "stretch": 0}

    def _p(self, mark, color, key, msg):
        print(f"  {color}{mark}{OFF} {key:<34} {DIM}{msg}{OFF}")

    def ok(self, key, msg=""):
        self.counts["pass"] += 1
        self._p("✓", GREEN, key, msg)

    def fail(self, key, hint):
        self.counts["fail"] += 1
        self._p("✗", RED, key, hint)

    def todo(self, key, msg="not answered yet"):
        self.counts["todo"] += 1
        self._p("·", YELLOW, key, msg)

    def skip(self, key, why):
        self.counts["skip"] += 1
        self._p("-", DIM, key, why)

    def stretch(self, key, passed, msg):
        if passed:
            self.counts["stretch"] += 1
            self._p("★", GREEN, key, msg)
        else:
            self._p("☆", DIM, key, msg)

    # --- answer comparisons ---
    def answer(self, key):
        v = self.a
        for part in key.split("."):
            if not isinstance(v, dict) or part not in v:
                return None
            v = v[part]
        if v in ("", [], {}):
            return None
        return v

    def live(self, key, expected, hint):
        """Compare the learner's answer with a value computed from the cluster now."""
        got = self.answer(key)
        if got is None:
            return self.todo(key)
        if norm(got) == norm(expected):
            return self.ok(key, "matches the cluster")
        self.fail(key, hint)

    def hashed(self, key, digests, hint):
        """Fixed answer, compared by salted hash. digests: one hash or a set of accepted hashes."""
        got = self.answer(key)
        if got is None:
            return self.todo(key)
        accepted = {digests} if isinstance(digests, str) else set(digests)
        if h(got) in accepted:
            return self.ok(key, "correct")
        self.fail(key, hint)

    def keyword(self, key, digests, hint):
        """Free text: passes if any word in the answer hashes to an accepted keyword."""
        got = self.answer(key)
        if got is None:
            return self.todo(key)
        if {h(w) for w in words(got)} & set(digests):
            return self.ok(key, "names the right thing")
        self.fail(key, hint)

    def summary(self):
        c = self.counts
        line = f"{c['pass']} passed, {c['fail']} failed, {c['todo']} unanswered"
        if c["skip"]:
            line += f", {c['skip']} skipped"
        if c["stretch"]:
            line += f", {c['stretch']} stretch"
        print(f"\n{BOLD}{line}{OFF}")
        return 0 if c["fail"] == 0 else 1


def section(title):
    print(f"\n{BOLD}{title}{OFF}")


def guard(r, key, fn):
    """Run a live probe; report a skip instead of crashing if the cluster can't answer."""
    try:
        return fn()
    except (CmdError, subprocess.CalledProcessError, KeyError, IndexError, ValueError) as e:
        r.skip(key, f"couldn't probe the cluster: {str(e).splitlines()[0][:110]}")
        return None
