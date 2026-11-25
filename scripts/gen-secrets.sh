#!/usr/bin/env bash
# Generates random lab credentials once into .secrets/ (gitignored) and loads them
# into the cluster as Kubernetes Secrets. Re-running keeps existing values.
set -euo pipefail
cd "$(dirname "$0")/.."
CTX="${CTX:-kind-zt}"
mkdir -p .secrets && chmod 700 .secrets
F=.secrets/keycloak.env
if [[ ! -f $F ]]; then
  r() { openssl rand -base64 24 | tr -d '/+=' | cut -c1-24; }
  cat > "$F" <<EOT
KC_BOOTSTRAP_ADMIN_USERNAME=admin
KC_BOOTSTRAP_ADMIN_PASSWORD=$(r)
POSTGRES_PASSWORD=$(r)
ALICE_PASSWORD=$(r)
BOB_PASSWORD=$(r)
JUPYTERHUB_CLIENT_SECRET=$(r)
EOT
  chmod 600 "$F"
  echo "generated $F"
fi
# JupyterHub auth_state encryption key (CryptKeeper: 32 bytes, hex). Added separately so an
# existing keycloak.env gains it without regenerating the other credentials.
if ! grep -q '^JUPYTERHUB_CRYPT_KEY=' "$F"; then
  echo "JUPYTERHUB_CRYPT_KEY=$(openssl rand -hex 32)" >> "$F"
  echo "added JUPYTERHUB_CRYPT_KEY to $F"
fi
# Phase 7: MinIO root user, and the MinIO user Unity Catalog calls STS AssumeRole with.
add() { grep -q "^$1=" "$F" || { echo "$1=$2" >> "$F"; echo "added $1 to $F"; }; }
add MINIO_ROOT_USER zt-minio-admin
add MINIO_ROOT_PASSWORD "$(openssl rand -base64 30 | tr -d '/+=' | cut -c1-32)"
add MINIO_UC_ACCESS_KEY unity-catalog
add MINIO_UC_SECRET_KEY "$(openssl rand -base64 30 | tr -d '/+=' | cut -c1-32)"
set -a; source "$F"; set +a
kubectl --context "$CTX" create namespace keycloak --dry-run=client -o yaml | kubectl --context "$CTX" apply -f - >/dev/null
kubectl --context "$CTX" label namespace keycloak istio-injection=enabled --overwrite >/dev/null
kubectl --context "$CTX" -n keycloak create secret generic keycloak-secrets \
  --from-literal=KC_BOOTSTRAP_ADMIN_USERNAME="$KC_BOOTSTRAP_ADMIN_USERNAME" \
  --from-literal=KC_BOOTSTRAP_ADMIN_PASSWORD="$KC_BOOTSTRAP_ADMIN_PASSWORD" \
  --from-literal=KC_DB_PASSWORD="$POSTGRES_PASSWORD" \
  --from-literal=POSTGRES_PASSWORD="$POSTGRES_PASSWORD" \
  --from-literal=ALICE_PASSWORD="$ALICE_PASSWORD" \
  --from-literal=BOB_PASSWORD="$BOB_PASSWORD" \
  --from-literal=JUPYTERHUB_CLIENT_SECRET="$JUPYTERHUB_CLIENT_SECRET" \
  --dry-run=client -o yaml | kubectl --context "$CTX" apply -f - >/dev/null
echo "secret keycloak/keycloak-secrets applied"

# JupyterHub's OAuth client secret (same value as the Keycloak client "jupyterhub") and the
# auth_state encryption key
kubectl --context "$CTX" create namespace jupyter --dry-run=client -o yaml | kubectl --context "$CTX" apply -f - >/dev/null
kubectl --context "$CTX" label namespace jupyter istio-injection=enabled --overwrite >/dev/null
kubectl --context "$CTX" -n jupyter create secret generic jupyterhub-oauth \
  --from-literal=OAUTH_CLIENT_SECRET="$JUPYTERHUB_CLIENT_SECRET" \
  --from-literal=JUPYTERHUB_CRYPT_KEY="$JUPYTERHUB_CRYPT_KEY" \
  --dry-run=client -o yaml | kubectl --context "$CTX" apply -f - >/dev/null
echo "secret jupyter/jupyterhub-oauth applied"

# Phase 7: MinIO (namespace storage) and Unity Catalog's MinIO credentials (namespace unity-catalog)
for ns in storage unity-catalog; do
  kubectl --context "$CTX" create namespace $ns --dry-run=client -o yaml | kubectl --context "$CTX" apply -f - >/dev/null
  kubectl --context "$CTX" label namespace $ns istio-injection=enabled --overwrite >/dev/null
done
kubectl --context "$CTX" -n storage create secret generic minio-root \
  --from-literal=MINIO_ROOT_USER="$MINIO_ROOT_USER" --from-literal=MINIO_ROOT_PASSWORD="$MINIO_ROOT_PASSWORD" \
  --from-literal=MINIO_UC_ACCESS_KEY="$MINIO_UC_ACCESS_KEY" --from-literal=MINIO_UC_SECRET_KEY="$MINIO_UC_SECRET_KEY" \
  --dry-run=client -o yaml | kubectl --context "$CTX" apply -f - >/dev/null
echo "secret storage/minio-root applied"
kubectl --context "$CTX" -n unity-catalog create secret generic uc-s3 \
  --from-literal=AWS_ACCESS_KEY_ID="$MINIO_UC_ACCESS_KEY" --from-literal=AWS_SECRET_ACCESS_KEY="$MINIO_UC_SECRET_KEY" \
  --dry-run=client -o yaml | kubectl --context "$CTX" apply -f - >/dev/null
echo "secret unity-catalog/uc-s3 applied"
