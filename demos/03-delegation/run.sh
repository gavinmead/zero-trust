#!/usr/bin/env bash
# Demo 03: RFC 8693 delegation with STANDING delegation grants (no interactive consent).
#
# A standing grant ("spark-connect may act on behalf of group data-analysts / user bob") is an FGAP v2
# `delegate` permission managed by scripts/delegation_grants.py. When a covered user signs in with
# scope delegation:client:spark-connect, the token gets may_act; the delegated exchange then produces
# a token with sub=<user> and act={sub,client_id=spark-connect}. The realm client policy
# `standing-delegation` re-checks the grant on every token response, refreshes included.
#
#   1. list standing grants (seed: spark-connect → group data-analysts)
#   2. alice (data-analysts) signs in. No consent screen; may_act present.
#   3. spark-connect: JWT-SVID → own token (actor_token) → delegated exchange → act claim
#   4. bob (contractors) has no grant: no may_act, so the exchange is refused
#   5. grant bob individually: may_act, and the exchange succeeds
#   6. revoke bob: his next refresh still works, but the token loses may_act, so the exchange is refused.
#      His already-issued access token still exchanges until it expires (Keycloak does not re-check the
#      grant at exchange time). The revocation window is the subject token lifespan.
#   7. a may_act token sent to a plain (non-delegation) exchange is refused
#
# Bob's seeded grant (needed for Spark, demo 05) is removed at the start and restored on exit.
#
# ALICE_TOKEN=<token> skips the scripted login for alice (e.g. from scripts/browser_consent.py).
set -euo pipefail
cd "$(dirname "$0")/../.."
CTX="${CTX:-kind-zt}"
REALM_URL="http://keycloak.localhost/realms/zt"
TOKEN_URL_EDGE="$REALM_URL/protocol/openid-connect/token"
TOKEN_URL_INT="http://keycloak.keycloak.svc:8080/realms/zt/protocol/openid-connect/token"
SPIFFE_ASSERTION="urn:ietf:params:oauth:client-assertion-type:jwt-spiffe"
AT="urn:ietf:params:oauth:token-type:access_token"
TE="urn:ietf:params:oauth:grant-type:token-exchange"
SCOPE="openid delegation:client:spark-connect"
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
grants() { python3 -I scripts/delegation_grants.py "$@"; }
login() {  # login <user> <password> → full token response JSON (lab-cli password grant, no consent)
  curl -s -X POST "$TOKEN_URL_EDGE" -d grant_type=password -d client_id=lab-cli \
    -d username="$1" --data-urlencode "password=$2" -d "scope=$SCOPE"
}
svid() {
  k exec -n spark deploy/obo-client -c svid -- /opt/spire/bin/spire-agent api fetch jwt -output json \
    -audience "$REALM_URL" -socketPath /spiffe-workload-api/spire-agent.sock \
  | python3 -c 'import json,sys; print(json.load(sys.stdin)[0]["svids"][0]["svid"])'
}
token_post() { k exec -n spark deploy/obo-client -c curl -- curl -s -X POST "$TOKEN_URL_INT" \
  -d client_assertion_type="$SPIFFE_ASSERTION" --data-urlencode "client_assertion=$(svid)" "$@"; }
delegate() {  # delegate <subject_token> <actor_token> → token response JSON
  token_post -d grant_type=$TE --data-urlencode "subject_token=$1" -d subject_token_type=$AT \
    --data-urlencode "actor_token=$2" -d actor_token_type=$AT -d requested_token_type=$AT -d audience=unity-catalog
}

# The lab seeds a grant for bob too (he needs one to use Spark: demo 05). This demo's story starts
# with bob ungranted, so it removes that grant first and restores the seed on exit.
trap 'grants grant --actor spark-connect --user bob >/dev/null' EXIT
grants revoke --actor spark-connect --user bob >/dev/null

hr "1. standing delegation grants (bob's seeded grant is set aside for this demo)"
grants list

hr "2. alice signs in (no consent screen) asking for scope delegation:client:spark-connect"
if [[ -n ${ALICE_TOKEN:-} ]]; then echo "(using ALICE_TOKEN)"; ALICE=$ALICE_TOKEN
else ALICE=$(login alice "$ALICE_PASSWORD" | field access_token); fi
echo "$ALICE" | claims sub,preferred_username,aud,azp,groups,scope,may_act

hr "3. spark-connect: JWT-SVID → own access token (actor_token) → delegated exchange"
ACTOR=$(token_post -d grant_type=client_credentials | field access_token)
echo "actor_token:"; echo "$ACTOR" | claims sub,azp,preferred_username
RESP=$(delegate "$ALICE" "$ACTOR")
DELEGATED=$(echo "$RESP" | field access_token)
[[ -n $DELEGATED ]] || { echo "delegated exchange failed: $RESP"; exit 1; }
RT=$(echo "$RESP" | field refresh_token)
echo "issued_token_type: $(echo "$RESP" | field issued_token_type)   refresh_token: ${RT:+present}${RT:-none (transient delegated session)}"
echo "$DELEGATED" | claims iss,sub,preferred_username,aud,azp,groups,act,scope
python3 - "$ALICE" "$DELEGATED" <<'PY'
import base64, json, sys
def c(t):
    p = t.split(".")[1]; p += "=" * (-len(p) % 4); return json.loads(base64.urlsafe_b64decode(p))
s, d = c(sys.argv[1]), c(sys.argv[2])
ok = lambda b: "✅" if b else "❌"
print(f"{ok(d['sub'] == s['sub'])} sub preserved ({d['sub']} = {d.get('preferred_username')})")
print(f"{ok(d.get('act', {}).get('sub') == s.get('may_act', {}).get('sub'))} act.sub == may_act.sub (spark-connect's service account)")
PY

hr "4. bob (contractors) has no standing grant"
BOB=$(login bob "$BOB_PASSWORD" | field access_token)
echo "$BOB" | claims preferred_username,scope,may_act
echo "exchange: $(delegate "$BOB" "$ACTOR")"

hr "5. grant spark-connect → user bob, then bob signs in again"
grants grant --actor spark-connect --user bob
BOB_RESP=$(login bob "$BOB_PASSWORD")
BOB=$(echo "$BOB_RESP" | field access_token); BOB_RT=$(echo "$BOB_RESP" | field refresh_token)
echo "$BOB" | claims preferred_username,scope,may_act
echo "exchange → $(delegate "$BOB" "$ACTOR" | field access_token | claims preferred_username,aud,act)"

hr "6. revoke bob's grant, then bob's client refreshes his token"
grants revoke --actor spark-connect --user bob
BOB2=$(curl -s -X POST "$TOKEN_URL_EDGE" -d grant_type=refresh_token -d client_id=lab-cli \
  --data-urlencode "refresh_token=$BOB_RT" | field access_token)
echo "refreshed token (the session lives on, but the delegation scope and may_act are dropped):"
echo "$BOB2" | claims preferred_username,scope,may_act
echo "exchange with refreshed token: $(delegate "$BOB2" "$ACTOR")"
echo "exchange with bob's PRE-revoke access token (the exchange does not re-check the grant):"
echo "  → $(delegate "$BOB" "$ACTOR" | python3 -c 'import json,sys; d=json.load(sys.stdin); print("still issued (revocation window = subject token lifespan)" if "access_token" in d else d)')"

hr "7. alice's may_act token sent to a plain (non-delegation) exchange"
token_post -d grant_type=$TE --data-urlencode "subject_token=$ALICE" -d subject_token_type=$AT -d audience=unity-catalog; echo
