#!/usr/bin/env bash
# Demo 05: end-to-end on-behalf-of access to Unity Catalog tables on MinIO, from JupyterLab.
#
#   notebook ──Spark token──▶ Spark Connect ──(interceptor) hop 3 Keycloak, hop 4 UC──▶ UC token (per user)
#   Spark/Delta ──UC token──▶ Unity Catalog (grants) ──STS AssumeRole, path-scoped──▶ MinIO credentials
#
#   1. alice (CREATE TABLE on lakehouse.analytics) creates, writes and reads a managed Delta table
#   2. bob (USE SCHEMA only) can't see, read or create tables          → UC 403
#   3. grant bob SELECT on the table: he reads it, but his insert fails  → MinIO AccessDenied
#      (UC vended him read-only credentials scoped to the table path)
#   4. revoke bob's SELECT again
set -euo pipefail
cd "$(dirname "$0")/../.."
CTX="${CTX:-kind-zt}"
set -a; source .secrets/keycloak.env; set +a
k() { kubectl --context "$CTX" -n jupyter "$@"; }
hr() { printf '\n\033[1m== %s\033[0m\n' "$*"; }
uc_admin() {  # uc_admin <bin/uc args...>   (admin token stays inside the UC pod)
  kubectl --context "$CTX" -n unity-catalog exec deploy/unity-catalog -c unity-catalog -- \
    sh -c 'bin/uc --auth_token "$(cat etc/conf/token.txt)" "$@" >/dev/null 2>&1' uc "$@"
}
run() {  # run <user> <step>  → check lines
  k cp demos/05-lakehouse/checks.py "jupyter-$1:/tmp/zt-checks.py" -c notebook
  k exec "jupyter-$1" -c notebook -- python /tmp/zt-checks.py "$2" 2>/dev/null | grep -E '^[a-z ]+: (OK|DENIED)'
}
expect() {  # expect <output> <label> <OK|DENIED> [substring]
  local line; line=$(echo "$1" | grep -E "^$2: " || true)
  if [[ $line == "$2: $3"* && ( -z ${4:-} || $line == *"$4"* ) ]]; then echo "  ✓ $line"
  else echo "  ✗ expected $2: $3 ${4:-}, got: ${line:-<none>}"; exit 1; fi
}

hr "0. alice and bob sign in to JupyterHub (servers start)"
for u in alice bob; do
  if ! k get pod "jupyter-$u" >/dev/null 2>&1; then
    pw_var="$(echo "$u" | tr a-z A-Z)_PASSWORD"
    HUB_PW="${!pw_var}" python3 -I scripts/hub_login.py "$u"
  fi
done
for u in alice bob; do
  for i in $(seq 1 60); do k get pod "jupyter-$u" >/dev/null 2>&1 && break; sleep 3; done
  k wait --for=condition=Ready "pod/jupyter-$u" --timeout=300s >/dev/null
done
uc_admin permission delete --securable_type table --name lakehouse.analytics.trips --principal bob@zt.lab --privilege SELECT || true

hr "1. alice: create, write and read lakehouse.analytics.trips"
out=$(run alice alice); echo "$out" | grep whoami
expect "$out" "create table" OK
expect "$out" insert OK
expect "$out" read OK "(2, 'Lima', 9.0)"
expect "$out" location OK "s3://lakehouse/managed/"

hr "2. bob, no grant on the table"
out=$(run bob bob); echo "$out" | grep whoami
expect "$out" "show tables" OK "[]"
expect "$out" read DENIED "403"
expect "$out" "create table" DENIED "403"

hr "3. grant bob SELECT on the table"
uc_admin permission create --securable_type table --name lakehouse.analytics.trips --principal bob@zt.lab --privilege SELECT
out=$(run bob bob-select)
expect "$out" "show tables" OK "trips"
expect "$out" read OK "(1, 'Oslo', 21.5)"
expect "$out" insert DENIED
spark_log=$(kubectl --context "$CTX" -n spark logs deploy/spark-connect -c spark-connect --since=3m)
if [[ $spark_log == *"AccessDeniedException: managed/"* ]]; then
  echo "  ✓ the write was refused by MinIO (read-only, path-scoped STS credentials from UC)"
else
  echo "  ✗ no MinIO AccessDenied in the Spark log"; exit 1
fi

hr "4. revoke bob's SELECT"
uc_admin permission delete --securable_type table --name lakehouse.analytics.trips --principal bob@zt.lab --privilege SELECT
out=$(run bob bob)
expect "$out" read DENIED "403"
echo; echo "demo 05 passed"
