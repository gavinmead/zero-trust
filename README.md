# Zero-Trust OBO Token Exchange Lab

A local lab for **OAuth 2.0 Token Exchange ([RFC 8693](https://www.rfc-editor.org/rfc/rfc8693))** on-behalf-of flows between workloads. Workload identity comes from SPIFFE, the mesh enforces mTLS, and Apache Spark Connect, Unity Catalog OSS, and JupyterLab act as the workloads under test.

A hands-on course that works through the lab layer by layer is in [curriculum/](curriculum/README.md).

## Stack

| Concern | Choice |
|---|---|
| Kubernetes | kind (1 control plane, 2 workers) on Docker Desktop |
| LoadBalancer | cloud-provider-kind (host process) |
| Service mesh | Istio, sidecar mode, STRICT mTLS |
| SPIFFE | SPIRE (server, agent, CSI driver, controller manager); trust domain `zt.lab` |
| IdP / authorization server | Keycloak 26.x: Standard Token Exchange V2, plus federated client auth using SPIFFE JWT-SVIDs |
| Notebooks | JupyterHub (Zero to JupyterHub) with per-user JupyterLab pods; users sign in through Keycloak |
| Compute | Apache Spark 4.x with Spark Connect |
| Catalog | Unity Catalog OSS |
| Object storage | MinIO |
| API gateway / PEP | agentgateway (native RFC 8693 `backendAuth`); fallback is the Istio gateway with ext_authz and OPA |

## Target flow

```
JupyterLab (alice) --session token--> [hub] ... --Spark token, mTLS--> Spark Connect --JWT-SVID client auth--> Keycloak (RFC 8693)
                                                                          |
                     cert-bound token (aud=unity-catalog), mTLS --> agentgateway (PEP: JWT + SPIFFE + cnf) --hop 4--> Unity Catalog --> MinIO
```

As built (phases 5–8): the hub mints the session token, the notebook downscopes it, Spark Connect's interceptor does hop 3, and agentgateway checks the token's certificate binding and does hop 4. Variant B (Spark forwards the user token unchanged and agentgateway does hop 3 too) was not built.

## Phases

1. [x] Cluster: kind and cloud-provider-kind
2. [x] SPIRE: trust domain `zt.lab`, SVIDs for every non-system pod via the CSI driver
3. [x] Istio with SPIRE as CA: sidecar mode (native sidecars), STRICT mTLS, demo 01
4. [x] Keycloak 26.8: realm `zt`, SPIFFE JWT-SVID client auth, RFC 8693 exchange (demo 02), delegation via standing grants with an `act` claim (demo 03), notebook session token with downscoped exchanges and certificate-bound tokens (demo 04)
5. [x] JupyterHub and JupyterLab: Keycloak login, JVM-free notebook image, demo 04's session-token chain wired into the hub and user pods
6. [x] Spark Connect behind Istio: notebook → Spark Connect over mTLS, JWT checked by the sidecar
7. [x] MinIO and Unity Catalog; end-to-end OBO: notebook → Spark Connect (auth interceptor, hops 3 and 4) → Unity Catalog grants → MinIO with UC-vended STS credentials, per user (demo 05)
8. [x] agentgateway in front of Unity Catalog; token exchange at the gateway: PEP enforces JWT, SPIFFE ID and RFC 8705 `cnf` binding, then does hop 4 (demo 06)

## Quick start

```bash
make tools   # kind, istioctl, cloud-provider-kind
make up      # create cluster "zt"
make lb      # LoadBalancer support
make spire   # SPIRE
make spire-smoke
make istio     # Istio 1.32 + STRICT mTLS
make mtls-demo # demo 01
make edge      # http://*.localhost → Istio gateway
curl -H "Host: httpbin.zt.local" http://127.0.0.1/headers
make keycloak  # generates .secrets/keycloak.env, deploys Postgres + Keycloak
make token-exchange-demo   # demo 02
make delegation-demo       # demo 03 (runs 02 first)
make session-exchange-demo # demo 04
make jupyterhub            # http://jupyter.localhost (builds the notebook image first)
make unity-catalog         # MinIO + in-mesh issuer URL + Unity Catalog 0.7.0 + users and grants
make uc-gateway            # agentgateway PEP in front of Unity Catalog
make spark-connect         # Spark Connect 4.2.1 (Delta, UC connector, auth interceptor) in namespace spark
make lakehouse-demo        # demo 05: alice writes a UC table on MinIO, bob is denied, grant/revoke
make uc-pep-demo           # demo 06: replayed, unbound and wrong-audience tokens are rejected at the gateway
make status
```

Lab credentials (Keycloak admin, alice, bob, the JupyterHub client secret, the hub's crypt key, MinIO root and Unity Catalog's MinIO user) are randomly generated once into `.secrets/keycloak.env`, which is gitignored, and loaded into Kubernetes Secrets.

Give Docker Desktop around 40 GB of memory once the full stack is running (Settings → Resources).

Verified on 2026-11-25: `make down`, then the targets above in order (skipping `tools` and the `curl`), rebuilt the whole lab in about 8 minutes with images cached, and demos 01–06 passed. Demos 05 and 06 sign alice and bob in to JupyterHub themselves.

## SPIRE notes

- Chart: `spiffe/spire` 0.31.0 (SPIRE 1.16.0). Every component runs in the `spire-server` namespace.
- Identity: the default `ClusterSPIFFEID` gives every pod outside the system namespaces `spiffe://zt.lab/ns/<ns>/sa/<sa>`, which is the shape Istio requires.
- Workload API: pods mount it with a CSI volume (`driver: csi.spiffe.io`); the socket is `spire-agent.sock`. Istio's SPIRE template mounts the same volume at `/run/secrets/workload-spiffe-uds` (phase 3).
- Trust material for relying parties (Keycloak, phase 4):
  - SPIFFE bundle endpoint (https_spiffe): `https://spire-server.spire-server.svc:8443`
  - OIDC discovery / JWKS: `https://spire-spiffe-oidc-discovery-provider.spire-server.svc.cluster.local/keys`
- JWT-SVIDs carry `iss=https://oidc-discovery.zt.lab`. Keycloak's SPIFFE client auth has had a bug with `iss` (RHBK-3724), so check this in phase 4.

## Istio notes

- Istio 1.32 runs in sidecar mode, configured by `platform/istio/istio-spire.yaml` (an IstioOperator) with `meshConfig.trustDomain: zt.lab`.
- Every injected pod uses the `spire` template (`defaultTemplates: [sidecar, spire]`), which mounts the SPIFFE CSI volume at `/run/secrets/workload-spiffe-uds`. Envoy then gets its SVID from SPIRE over SDS instead of from istiod.
- Kubernetes 1.37 runs `istio-proxy` as a native sidecar, so the template patches `initContainers`.
- The ingress gateway mounts the same volume and waits for the socket before starting. Its identity is `spiffe://zt.lab/ns/istio-system/sa/istio-ingressgateway-service-account`.
- A mesh-wide `PeerAuthentication` in `istio-system` sets mode STRICT.
- To verify, check that `istioctl proxy-config secret -n mesh-test deploy/httpbin` shows subject `O=SPIRE` and SAN `spiffe://zt.lab/ns/mesh-test/sa/httpbin`. The XFCC header httpbin receives carries the caller's SPIFFE ID.
- On macOS, cloud-provider-kind runs in `--enable-lb-port-mapping` mode, because LB IPs aren't routable from the Mac. `make gw-port` prints the localhost port; it changes whenever cloud-provider-kind restarts.
- The SPIRE root is `C=US, O=Zero Trust Lab, CN=zt.lab SPIRE Root CA`, with a 1-year CA TTL (lab choice: it gives Keycloak a static trust anchor).
  - The live cluster was moved off the chart-default CA in place with `spire-server localauthority x509|jwt prepare → activate → taint → revoke`.
  - Every SVID rotated to the new root without restarting any pods.
- `PILOT_JWT_ENABLE_REMOTE_JWKS=envoy` (istiod env): each sidecar fetches a `RequestAuthentication` jwksUri itself, over the mesh. istiod isn't in the mesh, so Keycloak's STRICT mTLS reset its plaintext fetch and every JWT was rejected (`JWKS fetch failed ... JWT requests will be rejected` in istiod's log).
- The ingress gateway has fixed NodePorts (80→30080, 443→30443). `make edge` runs a socat container that publishes `127.0.0.1:80`, so browser URLs are stable: `http://keycloak.localhost`, and later `http://jupyter.localhost`.

## Keycloak notes

- Keycloak 26.8.2 runs in namespace `keycloak` (in the mesh) with Postgres 17.
- The issuer and frontend URL is `http://keycloak.localhost/realms/zt`. The backchannel is dynamic, so in-cluster workloads call `http://keycloak.keycloak.svc:8080` and still receive tokens with that issuer.
- **SPIFFE client auth** (preview feature `spiffe`, plus `client-auth-federated`):
  - Identity provider `spiffe` (`providerId: spiffe`) has `trustDomain: spiffe://zt.lab`. Its `bundleEndpoint` is the SPIRE OIDC discovery provider's `/keys`, over https; Keycloak trusts the SPIRE root through `KC_TRUSTSTORE_PATHS`.
  - Client `spark-connect` uses `clientAuthenticatorType: federated-jwt`, with `jwt.credential.issuer: spiffe` and `jwt.credential.sub: spiffe://zt.lab/ns/spark/sa/spark-connect`.
  - The workload sends its JWT-SVID with `aud=<realm URL>` as `client_assertion`, with `client_assertion_type=urn:ietf:params:oauth:client-assertion-type:jwt-spiffe`.
  - JWT-SVIDs that carry `iss` are accepted in 26.8, so RHBK-3724 doesn't reproduce.
- **Token exchange** (Standard Token Exchange V2, on by default) is enabled per client with `standard.token.exchange.enabled: "true"`.
  - The subject token's `aud` must include the requester (`spark-connect`). User-facing clients add it with an audience mapper.
  - `audience=unity-catalog` must be available to the requester. The `spark-connect` audience mapper provides it; any other audience is rejected.
- **Clients:**
  - `jupyterhub`: confidential, auth code + PKCE, for phase 5. Its access token has `aud=jupyter-session` (set by `configure-session-exchange.py`).
  - `lab-cli`: public password grant, for demo scripts only.
  - `spark-connect`: SPIFFE-authenticated, allowed to exchange.
  - `unity-catalog`: audience only.
- Demo 02 results:
  - The exchanged token keeps alice as `sub` and her `groups`, and gets `aud=unity-catalog` and `azp=spark-connect`.
  - The imposter SVID, an SVID with the wrong `aud`, and a disallowed audience are all rejected.
- Plain V2 exchange (demo 02) produces no `act` claim; the acting workload is visible only as `azp`.
- Realm import uses `IGNORE_EXISTING`. After editing `realm-zt.json`, run `make keycloak-reimport` (deletes the realm, re-imports it, re-applies FGAP).
- Single node: `KC_CACHE=local` and the `Recreate` strategy. Two instances would try to form a JGroups cluster, which the sidecar blocks.
- JWT-SVID freshness: Keycloak rejects SPIFFE client assertions with `iat` older than `fedClientAssertionMaxExp` (300s, set on the `spiffe` identity provider). The SPIRE agent caches JWT-SVIDs until half their TTL, so `defaultJwtSvidTTL` is 5m. With the default 1h TTL, cached SVIDs failed with "Token was issued too far in the past". SPIFFE assertions carry no `jti` and are reusable within that window.

## Delegation with standing grants (`act` claim), demo 03

Uses Keycloak preview features `token-exchange-delegation` and `parameterized-scopes`, plus FGAP v2 (`adminPermissionsEnabled: true`). There is **no interactive consent**. An admin-managed *standing delegation grant* decides who an actor may act for.

| Piece | What it is |
|---|---|
| Standing grant | FGAP v2 permission `standing-delegation/<actor>/users` (scope `delegate`) or `.../groups` (scope `delegate-members`), with policy `actor:<client-id>`. Seeded from `platform/keycloak/delegation-grants.json` (spark-connect → group `data-analysts`). |
| Manage grants | `make grants`, `make grant ACTOR=spark-connect USER_NAME=bob`, `make revoke ACTOR=spark-connect GROUP=data-analysts` (wraps `scripts/delegation_grants.py`) |
| No consent | `scripts/configure-delegation.py` sets `always.display.consent=false` on the built-in `delegation:client` and `delegation:user` scopes; otherwise Keycloak refuses them on non-consent clients. No login client is consent-required. |
| Re-check | Client policy `standing-delegation` (executor `may-act`, `avoid-consent-check: true`, permission check on) re-validates `may_act` on every token response. |

Flow:
1. **Sign-in.** The user's login client asks for scope `delegation:client:spark-connect`. In the demo that's `lab-cli` (password grant); in the browser it's `lab-browser`. JupyterHub doesn't ask for it: its login token goes through demo 04's standard exchange, which refuses tokens carrying `may_act`. If a grant covers the user, the token gets `may_act: {sub: <spark-connect service account>, client_id: spark-connect}`. Otherwise Keycloak silently drops the scope.
2. **Actor token.** `spark-connect` authenticates with its JWT-SVID and gets its own token (`client_credentials`). That token is the `actor_token`.
3. **Delegated exchange.** The exchange takes `subject_token` (user) and `actor_token`, and returns:

   ```json
   { "sub": "<alice>", "aud": "unity-catalog", "azp": "spark-connect",
     "act": { "sub": "<spark-connect service account>", "client_id": "spark-connect" } }
   ```

   It's access-token only (no refresh token, transient session).

Demo 03 checks (`make delegation-demo`):

| Case | Result |
|---|---|
| alice (data-analysts, group grant) | `may_act`, then `act` |
| bob, no grant | no `may_act`; exchange fails: `Invalid may_act claim in the subject_token` |
| `grant` bob, then sign in again | `may_act`, then `act` |
| `revoke` bob, then refresh | refresh succeeds but drops scope and `may_act`; exchange fails |
| bob's access token minted **before** the revoke | **still exchanges** until it expires |
| `may_act` token sent to a plain exchange (no `actor_token`) | `Subject token with a delegation claim is not allowed for standard token exchange` |

**Revocation window.** Keycloak checks the grant when it issues a token carrying `may_act`, at login or refresh, not at exchange time. A revoke therefore takes effect when the user's current access token expires: 10 min here (`accessTokenLifespan: 600`). Shorten the login clients' access-token lifespan to narrow it.

Other notes:
- The actor client (`spark-connect`) must not be consent-required. When it was, the exchange failed with `Missing consents for Token Exchange in client spark-connect`.
- `act.sub` is the Keycloak service-account UUID, not the SPIFFE ID; that's accepted for now. The SPIFFE identity is proven when `spark-connect` authenticates with its JWT-SVID.
- The `actor_token` must be a Keycloak access token; a raw JWT-SVID isn't accepted. Nested `act` for multi-hop chains isn't documented.

### Try it in your own browser

```bash
make browser-delegation-demo
```

1. This opens your browser on the Keycloak login page. Passwords for alice and bob are in `.secrets/keycloak.env`.
2. Sign in as alice. There's no consent screen. The local callback (`127.0.0.1:8765`) shows the token's claims, including `may_act`.
3. The terminal then runs the rest of demo 03 with that token.

Sign in as bob to see "no may_act", or run `make grant ACTOR=spark-connect USER_NAME=bob` first. The helper adds `prompt=login`, so you can switch users.

Tested in a Chromium-based browser. Chrome and Firefox treat `http://*.localhost` as a secure context, so Keycloak's `Secure` cookies work over plain http.

## Session token and downscoped exchanges, demo 04

The notebook never uses the user's ID token. Instead, a SPIFFE-authenticated exchange turns the user's login **access token** into a *session token* (scope `session:token_exchange`), whose only use is as the subject of further, downscoped exchanges. Standard Token Exchange V2 accepts access tokens only as `subject_token`, so ID tokens can't enter the chain anyway.

| Hop | Requester (client ← SPIFFE ID) | Subject token | Result |
|---|---|---|---|
| 0 | user signs in (`lab-cli-jupyter`, stand-in for the JupyterHub login) | | login token, `aud=jupyter-session` |
| 1 | `jupyter-session` ← `spiffe://zt.lab/ns/jupyter/sa/jupyter-session` (hub side) | login token | session token: `aud=jupyter`, `scp=["session:token_exchange"]` |
| 2 | `jupyter` ← `spiffe://zt.lab/ns/jupyter/sa/jupyter-user` (notebook) | session token | Spark token: `aud=spark-connect`, `scp=["spark:connect"]` |
| 3 | `spark-connect` ← `spiffe://zt.lab/ns/spark/sa/spark-connect` | Spark token | UC token: `aud=unity-catalog`, `cnf.x5t#S256` = spark-connect's X.509-SVID |

- **Setup:** `scripts/configure-session-exchange.py` (admin API, idempotent) creates client scopes `session:token_exchange` and `spark:connect`, each with an audience mapper and a hardcoded `scp` claim. It also creates the clients `lab-cli-jupyter`, `jupyter-session` and `jupyter`. It doesn't edit `realm-zt.json`, because a realm import that lists `clientScopes` replaces Keycloak's built-in default scopes. Stand-in workloads are in `demos/04-session-exchange/workloads.yaml` (namespace `jupyter`).
- **`scp`:** Keycloak only emits `scope`, a space-separated string. The hardcoded mapper adds `scp` as a JSON array with one value per scope; that works because each token carries only one of these scopes.
- **What stops the shortcut is the audience chain, not a scope check.** V2 checks a requested scope against the *requester's* optional scopes; it never looks at the subject token's scopes. Each token's `aud` names only the next client, so a token can't skip a hop. All of these are rejected:

| Attempt | Keycloak error |
|---|---|
| notebook exchanges the login token straight to spark-connect | `Client is not within the token audience` |
| spark-connect exchanges the login token, or the session token, straight to unity-catalog | `Client is not within the token audience` |
| session broker requests `spark:connect` | `invalid_scope` |
| notebook re-mints `session:token_exchange` | `invalid_scope` |
| imposter SVID presents the session token | `invalid_client` |

- **Expiry:** every exchanged token keeps the login token's `exp`. Keycloak caps exchanged tokens at the subject token's expiry, so a session can't outlive the login.

### Certificate-bound tokens (RFC 8705)

- **How the certificate reaches Keycloak:** `platform/keycloak/xfcc-envoyfilter.yaml` makes Keycloak's sidecar replace any incoming `X-Forwarded-Client-Cert` (`SANITIZE_SET`) and include the caller's certificate (`Cert`). Keycloak reads it with `KC_SPI_X509CERT_LOOKUP__PROVIDER=envoy`.
- **Turning it on:** clients with `tls.client.certificate.bound.access.tokens` get `cnf.x5t#S256`, the thumbprint of the caller's X.509-SVID as presented by its sidecar. Use `configure-session-exchange.py --bind <clients|all|none>` to choose which.
- **Holder rule.** Standard exchange verifies a bound subject token. If a different client presents it, the error is `Sender-constrained token exchange rejected as the token was not issued for the requesting client`. With `--bind all`, the chain breaks at hop 2, because each intermediate token is handed to the *next* workload.
  - Bind only tokens that go to a resource server and are never exchanged by someone else. Here that's spark-connect's UC token, which is the demo default.
  - Intermediate tokens are already limited to one SPIFFE identity: their `aud` names one client, and that client must authenticate with its SVID.
- **Stolen JWT-SVID replay (5b).** spark-connect's JWT-SVID plus the Spark token, replayed from the Mac through the ingress gateway, **still produce a UC token**. JWT-SVIDs carry no `jti` and are reusable for up to 5 min.
  - The issued token is bound to the *ingress gateway's* certificate, so an in-mesh resource server that compares `cnf` with its mTLS peer rejects it.
  - Spark Connect and Unity Catalog don't check `cnf`, and Istio's `RequestAuthentication` ignores it. Since phase 8, uc-gateway (agentgateway) enforces it in front of Unity Catalog (demo 06).
  - Ways to close the replay itself:
    - Don't expose the token endpoint through the gateway for workload clients, using an Istio `AuthorizationPolicy`.
    - Or authenticate workloads with the mTLS certificate. Keycloak's `client-x509` authenticator can't do that today: it matches Subject DN only, not the SPIFFE URI SAN, and SPIRE SVIDs have no per-workload DN.
- **SVID rotation:** a bound token stops verifying once the holder's X.509-SVID rotates. Keep bound tokens short-lived.
- **Pooled connections bind to old certificates.** Keycloak binds to the client certificate of the mTLS connection that carried the token request. Sidecars reuse upstream connections, so after a rotation, a long-lived workload's requests could still ride a connection from its previous SVID, and Keycloak bound tokens to a certificate the workload no longer presented (demo 04 failed with `cnf mismatch` after ~2.5 h). `platform/keycloak/token-endpoint-dr.yaml` caps connection lifetime to Keycloak at 60s, so a token is bound to a certificate at most a minute old.

## JupyterHub notes

- Zero to JupyterHub 4.4.5 (JupyterHub 5.5.5, OAuthenticator 17.4) in namespace `jupyter`, in the mesh. Config: `apps/jupyterhub/values.yaml`; SAs and Istio route: `apps/jupyterhub/extras.yaml`. `make jupyterhub` builds both images, applies the Keycloak client changes, and runs the Helm upgrade.
- Users sign in through the Keycloak client `jupyterhub` (auth code + PKCE). The hub keeps their login tokens in encrypted `auth_state`. `scripts/gen-secrets.sh` generates the key (`JUPYTERHUB_CRYPT_KEY`) into `.secrets/keycloak.env` and the `jupyter/jupyterhub-oauth` Secret.

### Demo 04 inside JupyterHub

| Hop | Who | SPIFFE ID / Keycloak client | Result |
|---|---|---|---|
| 0 | user signs in at `http://jupyter.localhost` | client `jupyterhub` | login token, `aud=jupyter-session`, kept in `auth_state` on the hub |
| 1 | hub, `GET /hub/api/zt/session-token` | `.../sa/jupyter-session` → `jupyter-session` | session token: `aud=jupyter`, `scp=["session:token_exchange"]` |
| 2 | notebook, `zt_auth.spark_token()` | `.../sa/jupyter-user` → `jupyter` | Spark token: `aud=spark-connect`, `scp=["spark:connect"]` |
| 3 | Spark Connect's interceptor (phase 7) | `.../ns/spark/sa/spark-connect` | Keycloak UC token: `aud=unity-catalog`, `cnf.x5t#S256` bound |
| 4 | Spark Connect's interceptor → Unity Catalog's token endpoint | (UC checks issuer + audience) | UC-issued token, `sub=<email>` |

In a notebook:

```python
import zt_auth
zt_auth.show()                   # claims of the session token and a fresh Spark token
spark = zt_auth.spark_session()  # Spark Connect; hops 3 and 4 happen server-side
```

- **Hub side.** The hub runs as SA `jupyter-session` (the same identity as demo 04's stand-in `session-broker`) with the SPIFFE CSI volume. Its image is the chart's hub image plus `py-spiffe` (`apps/jupyterhub/hub-image`). The endpoint lives in `hub.extraConfig.zt-session-token`. It refreshes the login token when it's older than `auth_refresh_age` (300s), exchanges it with the hub's JWT-SVID, and caches the session token per login token.
- **Who may call it.** Only a user's own token with the custom scope `custom:zt:session-token`, which the `user` role and the server token carry. It returns the caller's own session token only.
- **No `auth_state` in notebooks.** The server token no longer has `admin:auth_state!user`, so a notebook can't read the login, ID or refresh token.
- **Notebook side.** User pods run as SA `jupyter-user` with the SPIFFE CSI volume at `/spiffe-workload-api`; the image adds `py-spiffe`. `zt_auth.py` is mounted at `/opt/zt`.
- Verified for alice and bob:
  - the chain above, including hop 3 from spark-connect;
  - `/hub/api/users/alice` has no `auth_state`, and `/users/bob` is 404 from alice's server;
  - the notebook re-minting `session:token_exchange` fails with `invalid_scope`;
  - the notebook asking for `unity-catalog` directly fails with `Requested audience not available`;
  - the endpoint without a token returns 403.
- **Every user pod shares one SPIFFE ID** (`jupyter-user`). Keycloak can't tell alice's notebook from bob's; what keeps them apart is that each notebook only gets its own user's session token. A notebook that obtained someone else's session token (a bearer token) could exchange it.
- JupyterHub logs that `extra_handlers` is deprecated since 3.1 in favour of services. It works in 5.5; a hub-managed service would be the long-term home for the endpoint.
- After `make jupyterhub` restarts the hub, the gateway can return 503 for a few seconds until the proxy routes are back. If a user's pod was deleted, wait for the hub to notice ("server stopped") before signing in again.

## Spark Connect notes (phase 6)

- Apache Spark **4.2.1** (`apache/spark:4.2.1`, Java 21) runs `start-connect-server.sh` in namespace `spark` as SA `spark-connect`, the same SPIFFE ID and Keycloak client as demo 02. Manifests: `apps/spark/spark-connect.yaml`; deploy with `make spark-connect`.
  - Phase 6 started on 4.3.0. Phase 7 moved server and `pyspark-client` back to 4.2.1, because Delta (`delta-spark_4.2_2.13` 4.4.1) and the Unity Catalog connector (`unitycatalog-spark_4.2_2.13` 0.7.0) publish no Spark 4.3 builds yet.
- The driver runs in local mode (`local[2]`) inside the pod; there are no executor pods yet.
- The image (`apps/spark/image`, `make spark-image`) adds Delta, the UC connector and `hadoop-aws` 3.5.0. They're resolved at build time with Spark's own `--packages` resolver, keeping only jars Spark doesn't already ship. The image also adds the auth interceptor.
- **Spark Connect OSS doesn't authenticate callers, so the sidecar does:**
  - `RequestAuthentication`: Keycloak realm `zt` issuer, `aud=spark-connect`, `forwardOriginalToken: true` (the header stays for phase 7).
  - `AuthorizationPolicy`: ALLOW only when the mTLS peer is `zt.lab/ns/jupyter/sa/jupyter-user` and the JWT has `scp` containing `spark:connect` and `azp=jupyter`, i.e. the downscoped token from demo 04's hop 2, presented by the notebook workload.
- **From a notebook:**

  ```python
  import zt_auth
  spark = zt_auth.spark_session()
  spark.range(1, 1001).selectExpr("sum(id)").collect()
  ```

  - `spark_session()` uses a plain gRPC channel (the sidecars provide mTLS) and sends `authorization: Bearer <Spark token>` on every RPC. It re-mints the token when it has under a minute left.
  - pyspark's `token=` URL parameter can't be used: it forces TLS to the server for any host but `localhost`.
- **Checks (`grpc` calls to `SparkConnectService/Config`):**

| Caller | Token | Result |
|---|---|---|
| alice's notebook | Spark token | reaches Spark |
| alice's notebook | none | `PERMISSION_DENIED`, RBAC: access denied |
| alice's notebook | session token (`aud=jupyter`) | `PERMISSION_DENIED`, Audiences in Jwt are not allowed |
| alice's notebook | `lab-cli` login token (`aud=spark-connect`, no `scp`) | `PERMISSION_DENIED`, RBAC: access denied |
| `jupyter/imposter` (other SPIFFE ID) | alice's valid Spark token | `grpc-status 7`, RBAC: access denied |
| namespace `outside` (no sidecar) | alice's valid Spark token | connection reset (STRICT mTLS) |

- Verified as alice and bob from a real Jupyter kernel (`jupyter nbconvert --execute`).
- `SELECT current_user()` still returns `spark`, the server process; the interceptor (below) binds the Connect session to the token's user instead.
- Running pyspark-client from `python -c` exits with "Could not find valid SPARK_HOME". pyspark mistakes a `__main__` without `__file__` for its own doctest run. Notebooks and script files are unaffected.

## End-to-end OBO to Unity Catalog and MinIO (phases 7–8, demos 05–06)

```
notebook ──Spark token (scp spark:connect)──▶ Spark Connect
   interceptor: hop 3 Keycloak (JWT-SVID) → token aud=unity-catalog, cnf = spark-connect's X.509-SVID
Spark/Delta ──cert-bound token (ZtUcTokenProvider), mTLS──▶ uc-gateway (agentgateway PEP)
   checks JWT, caller SPIFFE ID, cnf == this connection's certificate → hop 4 at UC's token endpoint
uc-gateway ──UC-issued token──▶ Unity Catalog: grants, then STS AssumeRole (path-scoped) ──▶ MinIO creds
Spark/Delta ──temporary, table-scoped credentials──▶ MinIO s3://lakehouse/managed/...
```

Phase 7 first ran hop 4 inside the interceptor too, with Spark calling UC directly. Phase 8 moved hop 4 to the gateway, so the UC-issued bearer token never reaches Spark.

### Spark Connect: the auth interceptor and token provider

`lab.zt.spark.ZtAuthInterceptor` (`apps/spark/image/interceptor`) is a gRPC server interceptor. It's compiled against the image's own jars (Spark shades gRPC as `org.sparkproject.connect.grpc`) and enabled with `spark.connect.grpc.interceptor.classes`. For every Connect request it:

1. Reads the `authorization` header, which the sidecar forwards (`forwardOriginalToken: true`). It decodes the JWT without re-verifying it: Istio already checked signature, issuer, audience and the mTLS peer, and port 15002 is reachable only through the sidecar. It still fails closed if `scp` lacks `spark:connect`.
2. **Binds the session to the token.** The request's `user_context.user_id` must equal the token's `preferred_username`; otherwise `PERMISSION_DENIED: zt: user_id 'bob' does not match the token's user 'alice'`. Spark Connect keys sessions by (user_id, session_id), so a notebook can't reach another user's session by claiming their name.
3. **Hop 3, Keycloak.** Exchanges the Spark token for `audience=unity-catalog` as client `spark-connect`. It authenticates with the pod's JWT-SVID, which a `spiffe-helper` 0.11.0 native sidecar keeps fresh in `/run/zt/jwt_svid.token`. Keycloak binds the result to spark-connect's X.509-SVID.
4. **Records the token** for (user, Connect session) in `ZtTokens` and points the session's catalog at it (`spark.sql.catalog.lakehouse.auth.zt.session`). A minted token is reused for at most 60s (and never within 60s of expiry). After an SVID rotation, a token bound to the previous certificate fails the gateway's `cnf` check, so the cache has to stay short.

`lab.zt.spark.ZtUcTokenProvider` is the UC connector's `auth.type` (the connector accepts a fully qualified `TokenProvider` class):
- It returns the session's *current* token on every call, so rotation works without re-initialising the catalog.
- Its `configs()` hand the same settings to the connector's Hadoop credential provider, so S3 credential renewals use the same user.
- No token is put in Spark conf. `spark.zt.user` and `spark.zt.uc.claims` (the token's non-secret claims) are readable for inspection.
- The catalog URI is `http://uc-gateway.unity-catalog.svc.cluster.local:8080`.

### The PEP: uc-gateway (`apps/uc-gateway`, `make uc-gateway`, phase 8)

agentgateway 1.7.0, pinned by digest, runs in namespace `unity-catalog` as SA `uc-gateway`, in the mesh. Per request:

1. **`jwtAuth` (strict).** Issuer `http://keycloak.localhost/realms/zt`, `aud=unity-catalog`, JWKS from Keycloak's in-cluster URL. `preserveToken: true` keeps the token for hop 4.
2. **Authorization (CEL), caller.** `jwt.azp == "spark-connect"`, and the caller's SPIFFE ID (from X-Forwarded-Client-Cert `URI=`) is `spiffe://zt.lab/ns/spark/sa/spark-connect`.
3. **Authorization (CEL), RFC 8705.** `jwt.cnf["x5t#S256"]` must equal base64url(SHA-256) of the certificate on this mTLS connection.
   - The gateway's sidecar terminates mTLS. A `SANITIZE_SET` EnvoyFilter makes it pass exactly one XFCC element built from the verified peer, whose `Hash=` is the hex SHA-256 of the client certificate.
   - agentgateway's CEL has `sha256.encode` (hex) but no hex decoder. The policy converts hex to base64url with a lookup map and arithmetic: 21 groups of 3 hex digits make 2 base64 characters each, plus one final character.
   - agentgateway expands `$1` in its config as an environment variable, so the policy uses `split`/`substring`, not regex capture groups.
4. **Hop 4.** `backendAuth.oauthTokenExchange` posts the verified token to UC's `/api/1.0/unity-control/auth/tokens` and sends the UC-issued token upstream instead.

UC 0.7 accepts external tokens only at that endpoint (RFC 8693, no actor tokens):
- it checks `iss` against `server.allowed-issuers`, `aud` against `server.audiences`, and the signature via the issuer's discovery/JWKS;
- it maps the token to an enabled UC user by the `email` claim;
- it returns its own token (`iss=internal`, `sub=alice@zt.lab`, 10 min). UC's REST API and grants only accept UC tokens.

Mesh layer:
- the gateway's `AuthorizationPolicy` admits only spark-connect;
- UC's `AuthorizationPolicy` admits only `uc-gateway`;
- a `DestinationRule` caps connections to the gateway at 60s, so the certificate it sees is never much older than the one the token was bound to.

agentgateway's admin, metrics and readiness ports moved to 19000/19020/19021, because the defaults are the Istio sidecar's.

### Demo 06 (`make uc-pep-demo`)

`spark/obo-client` and the Spark Connect pod share the SPIFFE ID `spark-connect` but hold different X.509-SVIDs. That's exactly what separates a bound token's holder from a thief with the same identity.

| Request to uc-gateway | Result |
|---|---|
| token bound to obo-client's cert, sent by obo-client | 200, alice's catalogs |
| same token, sent from the Spark Connect pod (same SPIFFE ID, other cert) | 403 `authorization failed` (cnf) |
| same token, sent from `spark/imposter` | 403 `RBAC: access denied` (mesh) |
| alice's Spark token (`aud=spark-connect`) | 401 `authentication failed` |
| unbound token (`aud=unity-catalog`, no `cnf`; minted with `--bind none`) | 403 `authorization failed` |
| Spark Connect → Unity Catalog directly | 403 `RBAC: access denied` |
| `spark.zt.uc.claims` in alice's session | only the cert-bound Keycloak token; no UC-issued token |

Demo 05 passes through the gateway unchanged, and demo 04 still passes.

### Unity Catalog (`apps/unity-catalog`, `make unity-catalog`)

- UC OSS 0.7.0 in namespace `unity-catalog`, in the mesh. Its `AuthorizationPolicy` admits only `uc-gateway` (phase 8).
- `server.authorization=enable`, `server.allowed-issuers=http://keycloak.localhost/realms/zt`, `server.audiences=unity-catalog`, `server.access-token-timeout=PT10M`.
- Managed Delta tables go to `storage-root.tables=s3://lakehouse/managed`. A catalog-level `storage_root` would need UC external locations and storage credentials (creating one without them is a 403).
- H2 database, signing keys and the admin token live under `etc/` on a PVC, seeded from the image on first start.
- `scripts/uc-setup.sh` (idempotent; runs the UC CLI inside the pod with the admin token) creates:
  - users `alice@zt.lab` and `bob@zt.lab`;
  - catalog `lakehouse` and schema `lakehouse.analytics`;
  - grants: both users get `USE CATALOG` and `USE SCHEMA`; alice also gets `CREATE TABLE`.
- **Credential vending to MinIO.** For each table access, UC calls STS `AssumeRole` with a session policy scoped to the table path and to read or read/write.
  - UC's policy includes an AWS KMS statement with a `kms:ViaService` condition, which MinIO rejects (`invalid condition key`).
  - `lab.zt.uc.MinioStsCredentialGenerator` (`apps/unity-catalog/image`, set as `s3.credentialGenerator.0`) reuses UC's own policy generator and drops only that statement.
  - The STS endpoint is `AWS_ENDPOINT_URL_STS`. UC calls it as MinIO user `unity-catalog`, whose keys come from Secret `uc-s3` and whose policy is limited to bucket `lakehouse`.

### MinIO (`platform/storage/minio.yaml`, `make minio`)

- Upstream MinIO no longer publishes container images. The lab uses Chainguard's from-source build of the current release (RELEASE.2026-11-23), pinned by digest, plus their `mc` image.
- A setup Job (one `mc` command per init container, since the image has no shell) creates bucket `lakehouse`, policy `uc-lakehouse` and user `unity-catalog`.
- `AuthorizationPolicy`: only Unity Catalog (STS), Spark Connect (data) and the setup job reach `:9000`.

### The issuer URL inside the mesh (`platform/keycloak/issuer-in-mesh.yaml`, `make keycloak-issuer`)

UC fetches `http://keycloak.localhost/realms/zt/.well-known/openid-configuration` and its `jwks_uri`. Inside the cluster:
- **Address:** Service `keycloak-issuer` has a fixed ClusterIP, `10.96.80.80` (port 80 → Keycloak 8080).
- **DNS:** a CoreDNS rewrite (`scripts/coredns-issuer.py`) resolves `keycloak.localhost` to it.
- **Routing:** a `ServiceEntry` makes sidecars route Host `keycloak.localhost`; without it the request falls through to plaintext passthrough and Keycloak's STRICT mTLS rejects it. A `DestinationRule` forces Istio mTLS and pins Keycloak's SPIFFE ID.
- **Netty ignores DNS for `*.localhost`.** UC's HTTP client (Armeria) resolved it to 127.0.0.1 (RFC 6761 behaviour) even though the OS and JDK resolvers returned the Service IP. It reads `/etc/hosts` first, so the UC pod has `hostAliases: keycloak.localhost → 10.96.80.80`.

### Demo 05 (`make lakehouse-demo`)

Signs alice and bob in to JupyterHub (`scripts/hub_login.py`), then runs `demos/05-lakehouse/checks.py` in their JupyterLab pods:

| Step | Result |
|---|---|
| alice creates, writes and reads managed Delta table `lakehouse.analytics.trips` | works; data at `s3://lakehouse/managed/__unitystorage/tables/<id>` |
| bob lists `lakehouse.analytics` | `[]`: UC hides tables he has no privilege on |
| bob reads alice's table / creates his own | UC `403 Access denied` |
| grant bob `SELECT` on the table; bob reads | works |
| bob inserts | fails at MinIO, `AccessDeniedException` on the object write: UC vended read-only credentials scoped to the table path |
| revoke bob's `SELECT`; bob reads | UC 403 again |

Both sessions' tokens show `UC sub=<email>` and a Keycloak hop with `aud=unity-catalog azp=spark-connect` that is cert-bound. Demo 04 still passes.

### Caveats

- **The last hop is bearer.** The UC-issued token isn't sender-constrained. Since phase 8 it exists only between uc-gateway and UC, and UC admits only the gateway.
- **Rotation windows.** `cnf` is checked against the certificate of the connection carrying the request. Connections to Keycloak and to the gateway are capped at 60s and minted tokens are reused for at most 60s, but a request can still land in the brief window right after a rotation. It would be refused; a retry with a fresh token succeeds.
- **Pre-computed hashes in XFCC.** The `cnf` check trusts the gateway's own sidecar to report the peer certificate hash. That holds because of the `SANITIZE_SET` EnvoyFilter; without it Istio's default `APPEND_FORWARD` would let a caller inject an XFCC element.
- **Driver-only design.** `ZtTokens` lives in the driver JVM. With executor pods, executors would need the vended storage credentials (Delta/UC normally pass them in the Hadoop conf) but must never see the UC token.
- **Spawn timeout.** The hub's spawn timeout is raised to 120s (`Spawner.http_timeout`). At the default 30s, a busy cluster occasionally got a new user pod deleted just before it came up.

### Login token refresh (fix in the hub)

Every token in the chain inherits the login token's `exp`. OAuthenticator only uses the refresh token after the access token is *rejected*, and only re-checks every `auth_refresh_age` (300s). So the hub served nearly expired login tokens: UC tokens arrived with about 25s left, and after expiry exchanges would fail for up to 5 minutes.

`hub.extraConfig.zt-session-token` now sets a `refresh_user_hook` that refreshes when less than 240s is left, and the session-token endpoint forces that refresh when needed. Session tokens now come back with at least 4 minutes left.

## Open questions to spike

- ~~Does Keycloak emit `act`?~~ Yes, with the `token-exchange-delegation` preview and standing grants (demo 03). Open: multi-hop chains (Spark → gateway → UC), and whether to shorten the revocation window.
- ~~Can Unity Catalog OSS accept tokens from an external OIDC issuer (Keycloak)?~~ Yes, at its own token endpoint (hop 4), not on the API directly (phase 7).
- ~~Does the UC connector cache the token at catalog initialisation?~~ With `auth.type` = a custom `TokenProvider`, it asks for the token on every call (`ZtUcTokenProvider`).
- Executors: with real executor pods, data access happens off the driver; UC-vended storage credentials have to reach them, the UC token must not.
- ~~Should UC tokens be bound too?~~ Instead, the PEP requires the cert-bound Keycloak token and does hop 4 itself, so the UC bearer token stays between gateway and UC (phase 8).
- ~~Enforce `cnf` at a PEP in front of Unity Catalog.~~ Done (demo 06).
- Close the JWT-SVID replay through the ingress gateway (`AuthorizationPolicy` on Keycloak's token endpoint). Today a replayed JWT-SVID still gets a token, though one bound to the ingress gateway's certificate, which uc-gateway rejects.
- Can agentgateway authenticate to Keycloak with a SPIFFE JWT-SVID client assertion? Variant B (the gateway also doing hop 3) would need it; `oauthTokenExchange.clientAuth` shows client secrets and certificate-based JWS assertions, not SPIFFE.
