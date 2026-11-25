#!/usr/bin/env bash
# Demo 06: the policy enforcement point in front of Unity Catalog (uc-gateway, agentgateway 1.7).
#
# The gateway admits a request only if the JWT is Keycloak's (aud=unity-catalog), was issued to
# spark-connect, comes from spiffe://zt.lab/ns/spark/sa/spark-connect, AND its cnf.x5t#S256 matches
# the certificate of the mTLS connection presenting it (RFC 8705). It then exchanges it at UC's
# token endpoint (hop 4); UC itself admits only the gateway.
#
# spark/obo-client and spark/spark-connect share the SPIFFE ID spark-connect but hold different
# X.509-SVIDs, which is exactly what tells a bound token's holder from a thief with the same identity.
set -euo pipefail
cd "$(dirname "$0")/../.."
CTX="${CTX:-kind-zt}"
set -a; source .secrets/keycloak.env; set +a
REALM_URL="http://keycloak.localhost/realms/zt"
TOKEN_URL="http://keycloak.keycloak.svc:8080/realms/zt/protocol/openid-connect/token"
GW="http://uc-gateway.unity-catalog.svc.cluster.local:8080/api/2.1/unity-catalog/catalogs"
UC="http://unity-catalog.unity-catalog.svc.cluster.local:8080/api/2.1/unity-catalog/catalogs"
k() { kubectl --context "$CTX" "$@"; }
hr() { printf '\n\033[1m== %s\033[0m\n' "$*"; }
field() { python3 -c 'import json,sys; d=json.load(sys.stdin); print(d.get(sys.argv[1]) or "")' "$1"; }
claim() { python3 -c '
import base64, json, sys
p = sys.stdin.read().strip().split(".")[1]; p += "=" * (-len(p) % 4)
print(json.dumps(json.loads(base64.urlsafe_b64decode(p)).get(sys.argv[1])))' "$1"; }
svid() { k exec -n spark deploy/obo-client -c svid -- /opt/spire/bin/spire-agent api fetch jwt -output json \
  -audience "$REALM_URL" -socketPath /spiffe-workload-api/spire-agent.sock \
  | python3 -c 'import json,sys; print(json.load(sys.stdin)[0]["svids"][0]["svid"])'; }
uc_token() {  # hop 3 from obo-client (SPIFFE ID spark-connect): Spark token → aud=unity-catalog
  k exec -n spark deploy/obo-client -c curl -- curl -s -X POST "$TOKEN_URL" \
    -d grant_type=urn:ietf:params:oauth:grant-type:token-exchange \
    -d client_assertion_type=urn:ietf:params:oauth:client-assertion-type:jwt-spiffe --data-urlencode "client_assertion=$(svid)" \
    --data-urlencode "subject_token=$1" -d subject_token_type=urn:ietf:params:oauth:token-type:access_token \
    -d requested_token_type=urn:ietf:params:oauth:token-type:access_token -d audience=unity-catalog | field access_token
}
call_curl() {  # call_curl <ns> <deploy> <url> <token>  → "<status> <body>"
  k exec -n "$1" "deploy/$2" -c curl -- curl -s -m 10 -w ' %{http_code}' -H "Authorization: Bearer $4" "$3" \
    | python3 -c 'import sys; s=sys.stdin.read(); b, c = s.rsplit(" ", 1); print(c, b[:140])'
}
call_spark_pod() {  # the Spark Connect pod has no curl; use its python
  k exec -n spark deploy/spark-connect -c spark-connect -- python3 -c '
import sys, urllib.request, urllib.error
r = urllib.request.Request(sys.argv[1], headers={"Authorization": "Bearer " + sys.argv[2]})
try:
    with urllib.request.urlopen(r, timeout=10) as resp: print(resp.status, resp.read().decode()[:140])
except urllib.error.HTTPError as e: print(e.code, e.read().decode()[:140])
except Exception as e: print("000", type(e).__name__, str(e)[:120])' "$1" "$2"
}
expect() {  # expect <label> <result> <status> [substring]
  if [[ $2 == "$3"* && ( -z ${4:-} || $2 == *"$4"* ) ]]; then echo "  ✓ $1: $2"
  else echo "  ✗ $1: expected $3 ${4:-}, got: $2"; exit 1; fi
}

hr "0. alice's Spark token (from her notebook; she must be signed in: make lakehouse-demo does that)"
k -n jupyter get pod jupyter-alice >/dev/null
SPARK=$(k -n jupyter exec jupyter-alice -c notebook -- python -c 'import zt_auth; print(zt_auth.spark_token())' 2>/dev/null)
echo "Spark token: aud=$(echo "$SPARK" | claim aud)"

hr "1. hop 3 from obo-client: token bound to obo-client's certificate"
python3 -I scripts/configure-session-exchange.py --bind spark-connect >/dev/null
BOUND=$(uc_token "$SPARK")
echo "aud=$(echo "$BOUND" | claim aud) azp=$(echo "$BOUND" | claim azp) cnf=$(echo "$BOUND" | claim cnf)"
expect "holder presents it (obo-client)" "$(call_curl spark obo-client "$GW" "$BOUND")" 200 lakehouse

hr "2. stolen token replayed by another workload"
expect "same SPIFFE ID, other certificate (spark-connect pod)" "$(call_spark_pod "$GW" "$BOUND")" 403 "authorization failed"
expect "other SPIFFE ID (spark/imposter)" "$(call_curl spark imposter "$GW" "$BOUND")" 403 "RBAC"

hr "3. tokens the gateway must not accept from the right workload"
expect "Spark token (aud=spark-connect)" "$(call_curl spark obo-client "$GW" "$SPARK")" 401 "authentication failed"
python3 -I scripts/configure-session-exchange.py --bind none >/dev/null
UNBOUND=$(uc_token "$SPARK")
python3 -I scripts/configure-session-exchange.py --bind spark-connect >/dev/null
echo "unbound token cnf=$(echo "$UNBOUND" | claim cnf)"
expect "unbound token (no cnf)" "$(call_curl spark obo-client "$GW" "$UNBOUND")" 403 "authorization failed"

hr "4. bypassing the gateway"
expect "spark-connect → Unity Catalog directly" "$(call_spark_pod "$UC" "$BOUND")" 403 "RBAC"

hr "5. Spark never holds a UC-issued token"
# (a script file, not `python -`: pyspark mistakes a __main__ without __file__ for its doctest run)
CLAIMS=$(k -n jupyter exec -i jupyter-alice -c notebook -- sh -c 'cat > /tmp/zt-claims.py && python /tmp/zt-claims.py' <<'PY' 2>/dev/null
import zt_auth; s = zt_auth.spark_session(); s.range(1).collect(); print(s.conf.get("spark.zt.uc.claims"))
PY
)
if [[ $CLAIMS == *'"keycloak"'* && $CLAIMS != *'"uc"'* ]]; then echo "  ✓ session holds only the cert-bound Keycloak token: $CLAIMS" | cut -c1-200
else echo "  ✗ unexpected session claims: $CLAIMS"; exit 1; fi
echo; echo "demo 06 passed"
