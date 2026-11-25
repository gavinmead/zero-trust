#!/usr/bin/env bash
# Unity Catalog bootstrap (idempotent): users, the lakehouse catalog, and lab grants.
# Runs the UC CLI inside the UC pod with the admin token UC generated (etc/conf/token.txt), over
# loopback, so no admin credential leaves the pod.
#
#   users    alice@zt.lab, bob@zt.lab   (UC maps Keycloak tokens to users by the email claim)
#   catalog  lakehouse (managed tables go to storage-root.tables = s3://lakehouse/managed), schema lakehouse.analytics
#   grants   alice: USE CATALOG, USE SCHEMA, CREATE TABLE  → creates and owns her tables
#            bob:   USE CATALOG, USE SCHEMA                → can see the schema, can't read alice's tables
set -euo pipefail
CTX="${CTX:-kind-zt}"
kubectl --context "$CTX" -n unity-catalog exec -i deploy/unity-catalog -c unity-catalog -- sh -s <<'EOF'
T=$(cat etc/conf/token.txt)
uc() { out=$(bin/uc --auth_token "$T" "$@" 2>&1) && echo "ok: $*" || {
  echo "$out" | grep -qiE 'already exists|ALREADY_EXISTS' && echo "exists: $*" || { echo "FAILED: $* -> $out"; exit 1; }; }; }
uc user create --name alice --email alice@zt.lab
uc user create --name bob --email bob@zt.lab
uc catalog create --name lakehouse --comment "Zero Trust Lab managed tables (server storage-root.tables)"
uc schema create --catalog lakehouse --name analytics
for p in alice@zt.lab bob@zt.lab; do
  uc permission create --securable_type catalog --name lakehouse --principal "$p" --privilege "USE CATALOG"
  uc permission create --securable_type schema --name lakehouse.analytics --principal "$p" --privilege "USE SCHEMA"
done
uc permission create --securable_type schema --name lakehouse.analytics --principal alice@zt.lab --privilege "CREATE TABLE"
EOF
