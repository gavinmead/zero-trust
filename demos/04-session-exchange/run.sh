#!/usr/bin/env bash
# Demo 04: notebook session token (scope session:token_exchange) via SPIFFE-authenticated token
# exchange, then downscoped exchanges for Spark Connect and Unity Catalog. Optionally with
# RFC 8705 certificate-bound tokens (cnf.x5t#S256 = the caller's X.509-SVID).
#
#   hop 0  alice signs in (lab-cli-jupyter, stand-in for JupyterHub)  → login token, aud=jupyter-session
#   hop 1  session-broker (client jupyter-session, JWT-SVID)          → session token, aud=jupyter
#   hop 2  notebook       (client jupyter, JWT-SVID)                  → Spark token, aud=spark-connect
#   hop 3  spark-connect  (demo 02 workload, JWT-SVID)                → UC token, aud=unity-catalog
#   then negative tests (bypass, re-minting, imposter) and, with BIND=1, cnf checks.
#
# The user's ID token is never requested or sent; only access tokens are exchanged.
set -euo pipefail
cd "$(dirname "$0")/../.."
CTX="${CTX:-kind-zt}"
BIND="${BIND:-0}"   # 1 = run section 5 (needs: configure-session-exchange.py --bind spark-connect)
REALM_URL="http://keycloak.localhost/realms/zt"
TOKEN_URL_EDGE="$REALM_URL/protocol/openid-connect/token"
TOKEN_URL_INT="http://keycloak.keycloak.svc:8080/realms/zt/protocol/openid-connect/token"
SPIFFE_ASSERTION="urn:ietf:params:oauth:client-assertion-type:jwt-spiffe"
AT=urn:ietf:params:oauth:token-type:access_token
set -a; source .secrets/keycloak.env; set +a

k() { kubectl --context "$CTX" "$@"; }
hr() { printf '\n\033[1m== %s\033[0m\n' "$*"; }
claims() {
  python3 -c '
import base64, json, sys
tok = sys.stdin.read().strip()
p = tok.split(".")[1]; p += "=" * (-len(p) % 4)
c = json.loads(base64.urlsafe_b64decode(p))
print(json.dumps({k: c[k] for k in sys.argv[1].split(",") if k in c}, indent=2))' "$1"
}
field() { python3 -c 'import json,sys; d=json.load(sys.stdin); print(d.get(sys.argv[1]) or "")' "$1"; }
svid() {  # svid <ns> <deployment>   → JWT-SVID with aud = realm URL
  k exec -n "$1" "deploy/$2" -c svid -- /opt/spire/bin/spire-agent api fetch jwt -output json \
    -audience "$REALM_URL" -socketPath /spiffe-workload-api/spire-agent.sock \
  | python3 -c 'import json,sys; print(json.load(sys.stdin)[0]["svids"][0]["svid"])'
}
x5t() {  # x5t <ns> <deployment>  → base64url(SHA-256(DER)) of the cert the pod's sidecar presents (its X.509-SVID)
  istioctl --context "$CTX" proxy-config secret -n "$1" "deploy/$2" -o json | python3 -c '
import base64, hashlib, json, re, sys
for s in json.load(sys.stdin)["dynamicActiveSecrets"]:
    if s["name"] == "default":
        pem = base64.b64decode(s["secret"]["tlsCertificate"]["certificateChain"]["inlineBytes"]).decode()
        der = base64.b64decode(re.search(r"-----BEGIN CERTIFICATE-----(.*?)-----END", pem, re.S).group(1))
        print(base64.urlsafe_b64encode(hashlib.sha256(der).digest()).rstrip(b"=").decode())'
}
cnf() { echo "$1" | python3 -c '
import base64, json, sys
p = sys.stdin.read().strip().split(".")[1]; p += "=" * (-len(p) % 4)
print(json.loads(base64.urlsafe_b64decode(p)).get("cnf", {}).get("x5t#S256", ""))'; }
exchange() {  # exchange <ns> <deployment> <subject_token> [curl -d args...]   → raw JSON response
  local ns=$1 d=$2 subj=$3; shift 3
  k exec -n "$ns" "deploy/$d" -c curl -- curl -s -X POST "$TOKEN_URL_INT" \
    -d grant_type=urn:ietf:params:oauth:grant-type:token-exchange \
    -d client_assertion_type="$SPIFFE_ASSERTION" --data-urlencode "client_assertion=$(svid "$ns" "$d")" \
    --data-urlencode "subject_token=$subj" -d subject_token_type=$AT -d requested_token_type=$AT "$@"
}
must_token() { local t; t=$(echo "$1" | field access_token); [[ -n $t ]] || { echo "FAILED: $1" >&2; exit 1; }; echo "$t"; }
expect_fail() {  # expect_fail <label> <response>
  if [[ -n $(echo "$2" | field access_token) ]]; then echo "UNEXPECTED: $1 succeeded"; exit 1; fi
  echo "rejected as expected: $(echo "$2" | python3 -c 'import json,sys; d=json.load(sys.stdin); print(d.get("error"), "-", d.get("error_description"))')"
}
SHOW=iss,sub,preferred_username,aud,azp,scope,scp,groups,cnf,exp

hr "0. alice signs in (lab-cli-jupyter, stand-in for the JupyterHub login; access token only)"
LOGIN=$(curl -s -X POST "$TOKEN_URL_EDGE" -d grant_type=password -d client_id=lab-cli-jupyter \
  -d username=alice --data-urlencode "password=$ALICE_PASSWORD" | field access_token)
echo "$LOGIN" | claims $SHOW

hr "1. session-broker (spiffe://zt.lab/ns/jupyter/sa/jupyter-session) → session token, scope session:token_exchange"
SESSION=$(must_token "$(exchange jupyter session-broker "$LOGIN" -d scope=session:token_exchange -d audience=jupyter)")
echo "$SESSION" | claims $SHOW

hr "2. notebook (spiffe://zt.lab/ns/jupyter/sa/jupyter-user) → downscoped Spark token, scope spark:connect"
SPARK=$(must_token "$(exchange jupyter notebook "$SESSION" -d scope=spark:connect -d audience=spark-connect)")
echo "$SPARK" | claims $SHOW
if echo "$SPARK" | claims scope,scp | grep -q session:token_exchange; then echo "UNEXPECTED: Spark token still carries session:token_exchange"; exit 1; fi
echo "ok: no session:token_exchange in the Spark token"

hr "3. spark-connect (spiffe://zt.lab/ns/spark/sa/spark-connect) → Unity Catalog token"
UC=$(must_token "$(exchange spark obo-client "$SPARK" -d audience=unity-catalog)")
echo "$UC" | claims $SHOW

hr "4. negative tests"
echo "4a. notebook exchanges alice's LOGIN token straight to spark-connect (skipping the session step)"
expect_fail "bypass via login token" "$(exchange jupyter notebook "$LOGIN" -d scope=spark:connect -d audience=spark-connect)"
echo "4b. spark-connect exchanges alice's LOGIN token straight to unity-catalog"
expect_fail "bypass via login token" "$(exchange spark obo-client "$LOGIN" -d audience=unity-catalog)"
echo "4c. spark-connect exchanges the SESSION token straight to unity-catalog"
expect_fail "session token at spark-connect" "$(exchange spark obo-client "$SESSION" -d audience=unity-catalog)"
echo "4d. session-broker asks for audience spark-connect directly"
expect_fail "broker to spark-connect" "$(exchange jupyter session-broker "$LOGIN" -d scope=spark:connect -d audience=spark-connect)"
echo "4e. notebook tries to re-mint a session token from the session token"
expect_fail "re-mint session" "$(exchange jupyter notebook "$SESSION" -d scope=session:token_exchange -d audience=jupyter)"
echo "4f. imposter (spiffe://zt.lab/ns/jupyter/sa/imposter) presents the session token"
expect_fail "imposter" "$(exchange jupyter imposter "$SESSION" -d scope=spark:connect -d audience=spark-connect)"

[[ $BIND == 1 ]] || exit 0

hr "5. certificate-bound tokens (RFC 8705): only spark-connect's tokens are bound"
echo "Intermediate tokens are handed to the next workload, and Keycloak only lets the client a bound"
echo "token was issued to exchange it, so only the last hop (UC token → resource server) is bound."
SVID_X5T=$(x5t spark obo-client)
printf '%-28s cnf=%s\n' "session token" "$(cnf "$SESSION")" "spark token" "$(cnf "$SPARK")" "uc token" "$(cnf "$UC")"
echo "spark-connect X.509-SVID    x5t=$SVID_X5T"
[[ $(cnf "$UC") == "$SVID_X5T" ]] && echo "ok: UC token is bound to spark-connect's SVID" || { echo "UNEXPECTED: cnf mismatch"; exit 1; }

echo
echo "5b. attacker replays spark-connect's stolen JWT-SVID + the Spark token from OUTSIDE the mesh (Mac → gateway)"
R=$(curl -s -X POST "$TOKEN_URL_EDGE" -d grant_type=urn:ietf:params:oauth:grant-type:token-exchange \
  -d client_assertion_type="$SPIFFE_ASSERTION" --data-urlencode "client_assertion=$(svid spark obo-client)" \
  --data-urlencode "subject_token=$SPARK" -d subject_token_type=$AT -d audience=unity-catalog)
STOLEN=$(echo "$R" | field access_token)
if [[ -z $STOLEN ]]; then echo "rejected: $R"; else
  GW_X5T=$(x5t istio-system istio-ingressgateway)
  echo "issued (JWT-SVIDs carry no jti and are reusable for up to 5 min), but:"
  echo "  cnf=$(cnf "$STOLEN")"
  echo "  ingress gateway x5t=$GW_X5T  → $([[ $(cnf "$STOLEN") == "$GW_X5T" ]] && echo bound to the gateway, not spark-connect)"
  echo "  an in-mesh resource server that checks cnf against its mTLS peer rejects it"
fi
