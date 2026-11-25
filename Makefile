CLUSTER := zt
KIND_CONFIG := cluster/kind-config.yaml
CTX := kind-$(CLUSTER)

.PHONY: help up down status lb lb-stop tools spire spire-smoke istio mtls-demo gw-port \
        edge edge-stop secrets keycloak-truststore keycloak keycloak-reimport token-exchange-demo delegation-demo session-exchange-demo browser-delegation-demo grants grant revoke jupyterhub jupyter-image hub-image spark-connect spark-image \
        minio keycloak-issuer uc-image unity-catalog lakehouse-demo uc-gateway uc-pep-demo

help:
	@echo "make tools    - install kind, istioctl, cloud-provider-kind (Homebrew)"
	@echo "make up       - create the kind cluster"
	@echo "make lb       - start cloud-provider-kind (LoadBalancer IPs) in the background"
	@echo "make lb-stop  - stop cloud-provider-kind"
	@echo "make spire    - install SPIRE (server, agents, CSI driver, OIDC discovery)"
	@echo "make spire-smoke - fetch X.509 + JWT SVIDs from a test pod"
	@echo "make istio    - install Istio (sidecar mode, SPIRE as CA, STRICT mTLS)"
	@echo "make mtls-demo - deploy demo 01 and test mTLS enforcement"
	@echo "make gw-port  - print the localhost port for the Istio ingress gateway"
	@echo "make edge     - expose the Istio gateway on http://*.localhost (127.0.0.1:80)"
	@echo "make keycloak - generate lab secrets, deploy Postgres + Keycloak, import realm zt"
	@echo "make token-exchange-demo - demo 02: JWT-SVID client auth + RFC 8693 exchange"
	@echo "make delegation-demo - demo 03: standing delegation grants, token carries act claim"
	@echo "make session-exchange-demo - demo 04: session:token_exchange token, downscoped exchanges, cert-bound tokens"
	@echo "make browser-delegation-demo - demo 03 with alice signing in through your own browser"
	@echo "make grants | make grant ACTOR=spark-connect USER_NAME=bob | make revoke ACTOR=... GROUP=..."
	@echo "make keycloak-reimport - delete + re-import realm zt from realm-zt.json"
	@echo "make jupyterhub - deploy JupyterHub (Keycloak login) at http://jupyter.localhost"
	@echo "make hub-image - build the hub image (chart hub + py-spiffe) and load it into kind"
	@echo "make jupyter-image - build the JVM-free notebook image (pyspark-client) and load it into kind"
	@echo "make spark-image - build Spark 4.2.1 + Delta + UC connector + ZtAuthInterceptor and load it into kind"
	@echo "make spark-connect - deploy Spark Connect 4.2.1 (spark ns, auth interceptor) behind Istio JWT + mTLS policies"
	@echo "make unity-catalog - MinIO + in-mesh issuer URL + Unity Catalog 0.7.0 (Keycloak auth, MinIO STS) + users/grants"
	@echo "make uc-gateway - agentgateway PEP in front of Unity Catalog (cnf check + hop 4 exchange)"
	@echo "make lakehouse-demo - demo 05: alice writes a UC table on MinIO, bob is denied, grant/revoke SELECT"
	@echo "make uc-pep-demo - demo 06: the UC gateway rejects replayed, unbound and wrong-audience tokens"
	@echo "make status   - show nodes and pods"
	@echo "make down     - delete the cluster"

tools:
	brew install kind istioctl cloud-provider-kind

up:
	@kind get clusters | grep -qx $(CLUSTER) || kind create cluster --config $(KIND_CONFIG) --wait 120s
	kubectl --context $(CTX) get nodes -o wide

# cloud-provider-kind runs on the host. On macOS (Docker Desktop) LB IPs (172.18.x) are not
# routable from the Mac, so use port-mapping mode (no sudo): each LoadBalancer gets an Envoy
# container whose ports are published on localhost (see `make gw-port`).
# Its built-in Gateway API/Ingress controller is disabled so it doesn't compete with Istio/agentgateway.
lb:
	@pgrep -x cloud-provider-kind >/dev/null && echo "cloud-provider-kind already running" || \
	  (nohup cloud-provider-kind --enable-lb-port-mapping --enable-default-ingress=false \
	    > .cloud-provider-kind.log 2>&1 & echo "started; log: .cloud-provider-kind.log")

lb-stop:
	-pkill -x cloud-provider-kind

SPIRE_CHART_VERSION := 0.31.0
SPIRE_CRDS_VERSION  := 0.6.1

spire:
	helm repo add spiffe https://spiffe.github.io/helm-charts-hardened/ >/dev/null 2>&1 || true
	helm repo update spiffe >/dev/null
	helm --kube-context $(CTX) upgrade --install spire-crds spiffe/spire-crds --version $(SPIRE_CRDS_VERSION) \
	  -n spire-server --create-namespace --wait
	helm --kube-context $(CTX) upgrade --install spire spiffe/spire --version $(SPIRE_CHART_VERSION) \
	  -n spire-server -f platform/spire/values.yaml --wait --timeout 6m
	kubectl --context $(CTX) get pods -n spire-server

spire-smoke:
	kubectl --context $(CTX) apply -f platform/spire/test-workload.yaml
	kubectl --context $(CTX) wait -n spire-smoke --for=condition=Ready pod/smoke --timeout=90s
	@for i in 1 2 3 4 5 6 7 8 9 10; do \
	  kubectl --context $(CTX) exec -n spire-smoke smoke -- /opt/spire/bin/spire-agent api fetch x509 \
	    -socketPath /spiffe-workload-api/spire-agent.sock && break; sleep 3; done
	kubectl --context $(CTX) exec -n spire-smoke smoke -- /opt/spire/bin/spire-agent api fetch jwt \
	  -audience keycloak -socketPath /spiffe-workload-api/spire-agent.sock

istio:
	istioctl install --context $(CTX) -y -f platform/istio/istio-spire.yaml
	kubectl --context $(CTX) apply -f platform/istio/peer-authentication.yaml
	kubectl --context $(CTX) get pods -n istio-system

mtls-demo:
	kubectl --context $(CTX) apply -f demos/01-mtls/apps.yaml
	kubectl --context $(CTX) rollout status -n mesh-test deploy/httpbin deploy/curl --timeout=180s
	kubectl --context $(CTX) rollout status -n outside deploy/curl --timeout=120s
	@echo "--- in-mesh call (expect XFCC with URI=spiffe://zt.lab/ns/mesh-test/sa/curl)"
	@kubectl --context $(CTX) exec -n mesh-test deploy/curl -c curl -- \
	  curl -s http://httpbin.mesh-test:8000/headers | grep -A1 Forwarded-Client-Cert
	@echo "--- plaintext call from outside the mesh (expect failure)"
	@kubectl --context $(CTX) exec -n outside deploy/curl -- curl -s -m 5 http://httpbin.mesh-test:8000/get \
	  && echo "UNEXPECTED: plaintext accepted" || echo "rejected as expected"

# Stable browser entry point: 127.0.0.1:80 → Istio ingress gateway NodePort 30080.
# macOS resolves *.localhost to 127.0.0.1, so http://keycloak.localhost, http://jupyter.localhost, ...
# all reach the gateway and are routed by Host header.
edge:
	@docker inspect zt-edge >/dev/null 2>&1 && docker start zt-edge >/dev/null || \
	  docker run -d --name zt-edge --restart unless-stopped --network kind -p 127.0.0.1:80:80 \
	    alpine/socat:1.8.0.1 TCP-LISTEN:80,fork,reuseaddr TCP:$(CLUSTER)-worker:30080 >/dev/null
	@echo "edge: http://*.localhost → istio-ingressgateway:30080"

edge-stop:
	-docker rm -f zt-edge >/dev/null 2>&1

secrets:
	CTX=$(CTX) ./scripts/gen-secrets.sh

# Keycloak trusts the SPIRE root (for https JWKS fetches from the SPIRE OIDC discovery provider).
keycloak-truststore:
	kubectl --context $(CTX) exec -n spire-server spire-server-0 -c spire-server -- \
	  /opt/spire/bin/spire-server bundle show > .secrets/spire-bundle.pem
	kubectl --context $(CTX) -n keycloak create configmap spire-bundle \
	  --from-file=spire-bundle.pem=.secrets/spire-bundle.pem --dry-run=client -o yaml | kubectl --context $(CTX) apply -f -

keycloak: secrets keycloak-truststore
	kubectl --context $(CTX) -n keycloak create configmap keycloak-realm \
	  --from-file=realm-zt.json=platform/keycloak/realm-zt.json --dry-run=client -o yaml | kubectl --context $(CTX) apply -f -
	kubectl --context $(CTX) apply -f platform/keycloak/postgres.yaml
	kubectl --context $(CTX) rollout status -n keycloak sts/postgres --timeout=180s
	kubectl --context $(CTX) apply -f platform/keycloak/xfcc-envoyfilter.yaml -f platform/keycloak/token-endpoint-dr.yaml
	kubectl --context $(CTX) apply -f platform/keycloak/keycloak.yaml
	kubectl --context $(CTX) rollout status -n keycloak deploy/keycloak --timeout=400s
	@for i in $$(seq 1 30); do curl -sf -o /dev/null http://keycloak.localhost/realms/zt && break; sleep 2; done
	python3 -I scripts/configure-delegation.py
	@echo "Keycloak: http://keycloak.localhost/admin  (admin creds in .secrets/keycloak.env)"

# Delete and re-import realm zt from platform/keycloak/realm-zt.json (import is IGNORE_EXISTING).
keycloak-reimport:
	python3 -I -c 'import sys; sys.path.insert(0,"scripts"); from kcadmin import KC; KC().req("DELETE","")'
	kubectl --context $(CTX) -n keycloak create configmap keycloak-realm \
	  --from-file=realm-zt.json=platform/keycloak/realm-zt.json --dry-run=client -o yaml | kubectl --context $(CTX) apply -f -
	kubectl --context $(CTX) rollout restart -n keycloak deploy/keycloak
	kubectl --context $(CTX) rollout status -n keycloak deploy/keycloak --timeout=400s
	@for i in $$(seq 1 30); do curl -sf -o /dev/null http://keycloak.localhost/realms/zt && break; sleep 2; done
	python3 -I scripts/configure-delegation.py
	python3 -I scripts/configure-session-exchange.py --bind spark-connect

delegation-demo: token-exchange-demo
	CTX=$(CTX) ./demos/03-delegation/run.sh

# Standing delegation grants:  make grants  |  make grant ACTOR=spark-connect USER=bob  |  make revoke ACTOR=spark-connect GROUP=data-analysts
grants:
	@python3 -I scripts/delegation_grants.py list
grant revoke:
	@python3 -I scripts/delegation_grants.py $@ --actor $(ACTOR) $(if $(USER_NAME),--user $(USER_NAME))$(if $(GROUP),--group $(GROUP))

# Same as demo 03, but alice signs in through your own browser (no consent screen with standing grants).
browser-delegation-demo:
	@T=$$(python3 -I scripts/browser_consent.py) && ALICE_TOKEN=$$T CTX=$(CTX) ./demos/03-delegation/run.sh

token-exchange-demo:
	kubectl --context $(CTX) apply -f demos/02-token-exchange/workloads.yaml
	kubectl --context $(CTX) rollout status -n spark deploy/obo-client deploy/imposter --timeout=180s
	CTX=$(CTX) ./demos/02-token-exchange/run.sh

# Demo 04: session token (scope session:token_exchange) → downscoped exchanges; spark-connect's tokens cert-bound.
session-exchange-demo:
	kubectl --context $(CTX) apply -f demos/02-token-exchange/workloads.yaml -f demos/04-session-exchange/workloads.yaml
	kubectl --context $(CTX) rollout status -n spark deploy/obo-client --timeout=180s
	kubectl --context $(CTX) rollout status -n jupyter deploy/session-broker deploy/notebook deploy/imposter --timeout=180s
	python3 -I scripts/configure-session-exchange.py --bind spark-connect
	BIND=1 CTX=$(CTX) ./demos/04-session-exchange/run.sh

JUPYTERHUB_CHART_VERSION := 4.4.5

jupyterhub: secrets hub-image jupyter-image
	helm repo add jupyterhub https://hub.jupyter.org/helm-chart/ >/dev/null 2>&1 || true
	helm repo update jupyterhub >/dev/null
	kubectl --context $(CTX) apply -f apps/jupyterhub/extras.yaml
	python3 -I scripts/configure-session-exchange.py --bind spark-connect
	helm --kube-context $(CTX) upgrade --install jupyterhub jupyterhub/jupyterhub --version $(JUPYTERHUB_CHART_VERSION) \
	  -n jupyter -f apps/jupyterhub/values.yaml \
	  --set-file singleuser.extraFiles.zt-auth.stringData=apps/jupyterhub/zt_auth.py --wait --timeout 10m
	kubectl --context $(CTX) get pods -n jupyter
	@echo "JupyterHub: http://jupyter.localhost  (sign in as alice/bob; passwords in .secrets/keycloak.env)"

# Hub image: the chart's hub image plus py-spiffe (the hub fetches its own JWT-SVID for hop 1).
HUB_IMAGE := zt-lab/k8s-hub-spiffe:4.4.5
hub-image:
	docker build -t $(HUB_IMAGE) apps/jupyterhub/hub-image
	kind load docker-image $(HUB_IMAGE) --name $(CLUSTER) --nodes $(CLUSTER)-worker2

# Build the JVM-free notebook image (minimal-notebook + pyspark-client) and load it into kind.
NOTEBOOK_IMAGE := zt-lab/notebook-spark-client:4.2.1
jupyter-image:
	docker build -t $(NOTEBOOK_IMAGE) apps/jupyterhub/image
	kind load docker-image $(NOTEBOOK_IMAGE) --name $(CLUSTER) --nodes $(CLUSTER)-worker2

# Phase 6: Spark Connect server (local-mode driver) in namespace spark, guarded by Istio
# RequestAuthentication + AuthorizationPolicy. Needs demo 02's namespace and SA (spark/spark-connect).
SPARK_IMAGE := zt-lab/spark-connect:4.2.1
spark-image:
	docker build -t $(SPARK_IMAGE) apps/spark/image
	kind load docker-image $(SPARK_IMAGE) --name $(CLUSTER) --nodes $(CLUSTER)-worker2

spark-connect: spark-image
	kubectl --context $(CTX) apply -f demos/02-token-exchange/workloads.yaml -f apps/spark/spark-connect.yaml
	kubectl --context $(CTX) rollout restart -n spark deploy/spark-connect
	kubectl --context $(CTX) rollout status -n spark deploy/spark-connect --timeout=300s

# Phase 7: MinIO (S3 + STS), the issuer URL inside the mesh, Unity Catalog (+ users and grants).
minio: secrets
	kubectl --context $(CTX) apply -f platform/storage/minio.yaml
	kubectl --context $(CTX) -n storage rollout status sts/minio --timeout=180s
	kubectl --context $(CTX) -n storage wait --for=condition=complete job/minio-setup --timeout=180s

keycloak-issuer:
	kubectl --context $(CTX) apply -f platform/keycloak/issuer-in-mesh.yaml
	CTX=$(CTX) python3 -I scripts/coredns-issuer.py

UC_IMAGE := zt-lab/unity-catalog:0.7.0
uc-image:
	docker build -t $(UC_IMAGE) apps/unity-catalog/image
	kind load docker-image $(UC_IMAGE) --name $(CLUSTER) --nodes $(CLUSTER)-worker2

unity-catalog: minio keycloak-issuer uc-image
	kubectl --context $(CTX) apply -f apps/unity-catalog/unity-catalog.yaml
	kubectl --context $(CTX) rollout restart -n unity-catalog deploy/unity-catalog
	kubectl --context $(CTX) rollout status -n unity-catalog deploy/unity-catalog --timeout=300s
	CTX=$(CTX) ./scripts/uc-setup.sh

# Phase 8: agentgateway in front of Unity Catalog (JWT + SPIFFE + RFC 8705 cnf check, hop 4 exchange).
uc-gateway:
	kubectl --context $(CTX) apply -f apps/uc-gateway/uc-gateway.yaml
	kubectl --context $(CTX) rollout restart -n unity-catalog deploy/uc-gateway
	kubectl --context $(CTX) rollout status -n unity-catalog deploy/uc-gateway --timeout=180s

# Demo 05: notebook → Spark Connect → Unity Catalog → MinIO, per user (alice writes, bob denied, grant/revoke).
lakehouse-demo:
	CTX=$(CTX) ./demos/05-lakehouse/run.sh

# Demo 06: the UC gateway's checks (holder vs replay, unbound/wrong-audience tokens, bypass).
uc-pep-demo:
	CTX=$(CTX) ./demos/06-uc-pep/run.sh

# localhost port mapped to the Istio ingress gateway's :80 (changes when cloud-provider-kind restarts)
gw-port:
	@for c in $$(docker ps -q --filter name=kindccm); do docker port $$c 80/tcp | head -1; done

status:
	kubectl --context $(CTX) get nodes -o wide
	kubectl --context $(CTX) get pods -A

down: lb-stop edge-stop
	kind delete cluster --name $(CLUSTER)
