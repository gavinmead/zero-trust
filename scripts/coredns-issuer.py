#!/usr/bin/env python3
"""Idempotently add a CoreDNS rewrite so pods resolve keycloak.localhost to the keycloak-issuer Service (10.96.80.80).

Browsers (on the Mac) and the mesh then use the same issuer URL, http://keycloak.localhost/realms/zt.
See platform/keycloak/issuer-in-mesh.yaml for the matching Istio routing.
"""
import json, os, subprocess, sys

CTX = os.environ.get("CTX", "kind-zt")
MARK = "# zt-lab: keycloak issuer v2"
BLOCK = f"""    {MARK}
    rewrite stop {{
        name exact keycloak.localhost. keycloak-issuer.keycloak.svc.cluster.local.
        answer name keycloak-issuer.keycloak.svc.cluster.local. keycloak.localhost.
    }}
"""

def kubectl(*args, data=None):
    return subprocess.run(["kubectl", "--context", CTX, "-n", "kube-system", *args],
                          input=data, capture_output=True, text=True, check=True).stdout

cm = json.loads(kubectl("get", "configmap", "coredns", "-o", "json"))
corefile = cm["data"]["Corefile"]
# drop an older version of the block
import re
corefile = re.sub(r"    # zt-lab: keycloak issuer\n    rewrite stop \{\n.*?\n    \}\n", "", corefile, flags=re.S)
if MARK in corefile:
    print("coredns: keycloak.localhost rewrite already present")
    sys.exit(0)
anchor = "    ready\n"
if anchor not in corefile:
    sys.exit("coredns: unexpected Corefile (no 'ready' line); add the rewrite by hand")
cm["data"]["Corefile"] = corefile.replace(anchor, anchor + BLOCK, 1)
kubectl("apply", "-f", "-", data=json.dumps(cm))
kubectl("rollout", "restart", "deploy/coredns")
kubectl("rollout", "status", "deploy/coredns", "--timeout=120s")
print("coredns: added keycloak.localhost → keycloak-issuer.keycloak.svc.cluster.local (10.96.80.80)")
