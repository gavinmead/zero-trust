#!/usr/bin/env bash
# Demo 02: SPIFFE workload authentication + RFC 8693 on-behalf-of token exchange with Keycloak.
#
#   1. alice gets a user token (lab-cli password grant, via the edge at http://keycloak.localhost)
#      → aud includes spark-connect, the workload that will act for her
#   2. the spark-connect workload fetches a JWT-SVID from SPIRE (aud = the Keycloak realm URL)
#   3. spark-connect exchanges alice's token for a Unity Catalog token, authenticating with
#      the JWT-SVID as its client assertion (client_assertion_type=...:jwt-spiffe) — no secret
#   4. negative tests: imposter workload, wrong SVID audience, audience not allowed
set -euo pipefail
cd "$(dirname "$0")/../.."
CTX="${CTX:-kind-zt}"
REALM_URL="http://keycloak.localhost/realms/zt"            # issuer (frontend URL)
TOKEN_URL_EDGE="$REALM_URL/protocol/openid-connect/token"   # from the Mac
TOKEN_URL_INT="http://keycloak.keycloak.svc:8080/realms/zt/protocol/openid-connect/token"  # in-cluster
SPIFFE_ASSERTION="urn:ietf:params:oauth:client-assertion-type:jwt-spiffe"
set -a; source .secrets/keycloak.env; set +a

k() { kubectl --context "$CTX" "$@"; }
hr() { printf '\n\033[1m== %s\033[0m\n' "$*"; }
claims() {  # print selected JWT claims
  python3 -c '
import base64, json, sys
tok = sys.stdin.read().strip()
p = tok.split(".")[1]; p += "=" * (-len(p) % 4)
c = json.loads(base64.urlsafe_b64decode(p))
keys = sys.argv[1].split(",")
print(json.dumps({k: c[k] for k in keys if k in c}, indent=2))' "$1"
}
field() { python3 -c 'import json,sys; d=json.load(sys.stdin); print(d.get(sys.argv[1]) or "")' "$1"; }
svid() {  # svid <deployment> <audience>
  k exec -n spark "deploy/$1" -c svid -- /opt/spire/bin/spire-agent api fetch jwt -output json \
    -audience "$2" -socketPath /spiffe-workload-api/spire-agent.sock \
  | python3 -c 'import json,sys; print(json.load(sys.stdin)[0]["svids"][0]["svid"])'
}
incluster_post() {  # incluster_post <deployment> <curl -d args...>
  local d=$1; shift
  k exec -n spark "deploy/$d" -c curl -- curl -s -X POST "$TOKEN_URL_INT" "$@"
}

hr "1. alice signs in (lab-cli, password grant)"
USER_TOKEN=$(curl -s -X POST "$TOKEN_URL_EDGE" -d grant_type=password -d client_id=lab-cli \
  -d username=alice --data-urlencode "password=$ALICE_PASSWORD" -d scope=openid | field access_token)
echo "$USER_TOKEN" | claims iss,sub,preferred_username,aud,azp,groups

hr "2. spark-connect workload fetches a JWT-SVID from SPIRE"
SVID=$(svid obo-client "$REALM_URL")
echo "$SVID" | claims iss,sub,aud,exp

hr "3. RFC 8693 exchange: spark-connect (JWT-SVID auth) acting for alice → audience unity-catalog"
RESP=$(incluster_post obo-client \
  -d grant_type=urn:ietf:params:oauth:grant-type:token-exchange \
  -d client_assertion_type="$SPIFFE_ASSERTION" --data-urlencode "client_assertion=$SVID" \
  --data-urlencode "subject_token=$USER_TOKEN" \
  -d subject_token_type=urn:ietf:params:oauth:token-type:access_token \
  -d requested_token_type=urn:ietf:params:oauth:token-type:access_token \
  -d audience=unity-catalog)
UC_TOKEN=$(echo "$RESP" | field access_token)
[[ -n $UC_TOKEN ]] || { echo "exchange failed: $RESP"; exit 1; }
echo "issued_token_type: $(echo "$RESP" | field issued_token_type)"
echo "$UC_TOKEN" | claims iss,sub,preferred_username,aud,azp,groups,act,scope

hr "4a. imposter workload (spiffe://zt.lab/ns/spark/sa/imposter) tries the same exchange"
incluster_post imposter \
  -d grant_type=urn:ietf:params:oauth:grant-type:token-exchange \
  -d client_assertion_type="$SPIFFE_ASSERTION" \
  --data-urlencode "client_assertion=$(svid imposter "$REALM_URL")" \
  --data-urlencode "subject_token=$USER_TOKEN" \
  -d subject_token_type=urn:ietf:params:oauth:token-type:access_token -d audience=unity-catalog; echo

hr "4b. spark-connect presents a JWT-SVID minted for a different audience"
incluster_post obo-client \
  -d grant_type=urn:ietf:params:oauth:grant-type:token-exchange \
  -d client_assertion_type="$SPIFFE_ASSERTION" \
  --data-urlencode "client_assertion=$(svid obo-client https://some-other-service)" \
  --data-urlencode "subject_token=$USER_TOKEN" \
  -d subject_token_type=urn:ietf:params:oauth:token-type:access_token -d audience=unity-catalog; echo

hr "4c. spark-connect asks for an audience it is not allowed to target (jupyterhub)"
incluster_post obo-client \
  -d grant_type=urn:ietf:params:oauth:grant-type:token-exchange \
  -d client_assertion_type="$SPIFFE_ASSERTION" --data-urlencode "client_assertion=$(svid obo-client "$REALM_URL")" \
  --data-urlencode "subject_token=$USER_TOKEN" \
  -d subject_token_type=urn:ietf:params:oauth:token-type:access_token -d audience=jupyterhub; echo
