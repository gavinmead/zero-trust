# Lesson 1: Workload identity with SPIFFE and SPIRE

**Time:** about 2.5 hours. **Prerequisite:** lesson 0.

## Outcomes

- Explain how a pod gets a cryptographic identity with no secret provisioned into it: node attestation, workload attestation, and SVID issuance.
- Read X.509- and JWT-SVIDs and say what each is for and how each can be misused.
- Find the **real issuance boundary** for any SPIFFE ID in this lab, and show it's Kubernetes RBAC, not SPIRE.
- Explain the coupling between JWT-SVID TTL, agent caching and Keycloak's assertion freshness check, and predict what breaks when it's wrong.

## Concepts

### SPIFFE vs SPIRE

- **SPIFFE** is the set of specs: the ID format (`spiffe://<trust-domain>/<path>`), two document types (X.509-SVID and JWT-SVID), the Workload API (a local, unauthenticated gRPC endpoint), and trust bundles/federation.
- **SPIRE** is one implementation: a server (the CA and registration store) and an agent per node (attests workloads and serves the Workload API).

### Attestation: the two questions

1. **Node attestation: is this agent running on a node we trust?** Here: `k8s_psat`. The agent presents a *projected ServiceAccount token* (audience `spire-server`). The server validates it with the Kubernetes TokenReview API and checks the pod and node it belongs to. The agent gets its own SVID: `spiffe://zt.lab/spire/agent/k8s_psat/zt/<node-uid>`.
2. **Workload attestation: which workload is calling the Workload API?** The agent sees the caller's PID through the Unix socket (`SO_PEERCRED`), maps it to a container and pod via the kubelet, and collects *selectors* (`k8s:ns`, `k8s:sa`, `k8s:pod-uid`, labels, image...). If a registration entry's selectors all match, the agent hands over that entry's SVID.

No secret is ever delivered into the pod. Identity comes from facts the platform asserts about the caller.

### Who writes the registration entries?

The **SPIRE Controller Manager** watches pods and writes one entry per pod from a `ClusterSPIFFEID` template. This lab uses one template, `spiffe://zt.lab/ns/{{namespace}}/sa/{{serviceAccountName}}`, for every non-system namespace. That's the shape Istio expects for its principals (`<td>/ns/<ns>/sa/<sa>`).

Consequence: **the selector that matters is `pod-uid`, and the SPIFFE ID is a pure function of (namespace, ServiceAccount).** Whoever can create a pod with a given ServiceAccount in a given namespace can obtain that identity. You'll prove this in 1.5.

### The CSI driver and the socket

Pods mount the Workload API with a CSI volume (`csi.spiffe.io`), which bind-mounts the node agent's socket into the pod. Anything in the pod that can open that socket can ask for SVIDs. **Socket access *is* identity.** There's no per-process authentication inside a pod.

### X.509-SVID vs JWT-SVID

| | X.509-SVID | JWT-SVID |
|---|---|---|
| Use | mTLS: proves possession of a private key on every connection | Bearer assertion, e.g. an OAuth `client_assertion` to Keycloak |
| Replayable if stolen? | Only with the private key | **Yes**, until `exp`, to any relying party that accepts its `aud` |
| Here | Envoy gets it over SDS from SPIRE; issued by `zt.lab SPIRE Root CA`; 4 h TTL, rotated at about half-life | `iss=https://oidc-discovery.zt.lab`, verified against SPIRE's OIDC discovery `/keys`; 5 min TTL |
| Audience | n/a (the peer validates the SAN) | `aud` chosen by the requester; here Keycloak's realm URL |

The lab needs JWT-SVIDs only because Keycloak's client authentication can't use the SPIFFE URI SAN of an mTLS certificate (README, "Certificate-bound tokens"). Lesson 3 and capstone A come back to that cost.

## Code tour

- [platform/spire/values.yaml](../platform/spire/values.yaml): trust domain, CA subject and TTL, `defaultJwtSvidTTL: 5m` (read the comment), the default `ClusterSPIFFEID`.
- [platform/spire/test-workload.yaml](../platform/spire/test-workload.yaml): the smallest possible SPIFFE workload.
- [apps/spark/spark-connect.yaml](../apps/spark/spark-connect.yaml): the `spiffe-helper` sidecar writes a fresh JWT-SVID to `/run/zt/jwt_svid.token` for the interceptor.
- [apps/jupyterhub/zt_auth.py](../apps/jupyterhub/zt_auth.py) `jwt_svid()`: the same thing via py-spiffe.
- README, sections "SPIRE notes" and the JWT-SVID freshness bullet under "Keycloak notes".

## Lab

```bash
cd ~/development/zero-trust
export PATH="$PWD/curriculum/bin:$PATH"
k() { kubectl --context kind-zt "$@"; }
ss() { k -n spire-server exec spire-server-0 -c spire-server -- /opt/spire/bin/spire-server "$@"; }
wl() { k -n spire-smoke exec smoke -- /opt/spire/bin/spire-agent api "$@" -socketPath /spiffe-workload-api/spire-agent.sock; }
check 1 --init
```

Record answers in `curriculum/answers/01.toml` as you go, and run the `check` line at the end of each exercise. Most answers are compared with values the checker reads from the cluster at check time, so copy what you observe, not what you expect.

### 1.1 Node attestation

```bash
ss agent list
k get nodes -o 'custom-columns=NAME:.metadata.name,UID:.metadata.uid,ROLE:.metadata.labels.zt\.lab/role,TAINT:.spec.taints[*].key'
```

- Match each agent's SPIFFE ID to a node. What's the last path segment?
- There are 3 nodes but 2 agents. Which node has no agent, and why does that matter for where workloads can run? (Compare the agent DaemonSet's tolerations with the node taints.) The SPIRE server is pinned to the `platform` node by `nodeSelector`. What runs on `workloads`?
- The agents' own SVIDs expire too ("Expiration time"). What does "Can re-attest: true" mean for a node that's rebuilt?

**Check:** `check 1 1.1`

### 1.2 Registration entries

```bash
ss entry show | grep -c 'Entry ID'
ss entry show -spiffeID spiffe://zt.lab/ns/spark/sa/spark-connect
k get pods -A -o custom-columns=NS:.metadata.namespace,NAME:.metadata.name,UID:.metadata.uid | grep spark
k get clusterspiffeid default -o yaml | sed -n '/^spec:/,/^status:/p'
```

- Why are there **two** entries for `spark-connect`? Match each `k8s:pod-uid` selector to a pod.
- **Predict**, then check: you `kubectl -n spark rollout restart deploy/spark-connect`. What happens to the entries over the next 30 s? (Run it and watch `ss entry show -spiffeID ...` a few times. Spark Connect takes about 40 s to become ready again, and alice's open sessions are dropped.)
- Each entry has a `Parent ID`. What does that pin the entry to, and what does that stop?

**Check:** `check 1 1.2` (after the restart settles, the pod names have changed; the checker knows)

### 1.3 Read an X.509-SVID

```bash
svid spark deploy/spark-connect
svid spark deploy/obo-client
```

- The SANs are identical. What differs? Which of those differences does uc-gateway rely on in lesson 9?
- Subject is `C=US, O=SPIRE` with no CN. Why can't Keycloak's `client-x509` authenticator (which matches Subject DN) tell these workloads apart? Note this for lesson 3.
- From `notBefore` and `notAfter`, compute the TTL. When will the spark-connect SVID rotate? Write the current serial down. You'll check it again in lesson 2.

Now fetch the full Workload API response in the smoke pod:

```bash
wl fetch x509 -output json | python3 -c 'import json,sys; s=json.load(sys.stdin)["svids"][0]; print(s["spiffe_id"], sorted(s))'
```

- Look at the keys. What does the Workload API hand to *any* process in the pod that can open the socket? What does that imply for a pod that runs untrusted code next to a trusted one? Think about JupyterLab: alice's notebook kernel runs in the same pod as the server process, and the pod mounts the socket at `/spiffe-workload-api`.

**Check:** `check 1 1.3`

### 1.4 Read a JWT-SVID

```bash
wl fetch jwt -audience http://keycloak.localhost/realms/zt -output json \
  | python3 -c 'import json,sys; print(json.load(sys.stdin)[0]["svids"][0]["svid"])' | jwt
```

Run it twice, a few seconds apart.

- Is the second token the same as the first? Why? (Values.yaml comment.) What does that mean for a relying party trying to detect replay?
- There's no `jti`. RFC 7523 §3 says an AS *may* use `jti` to prevent assertion replay. What does Keycloak do instead? (README, "JWT-SVID freshness".)
- Fetch one with `-audience https://evil.example`. SPIRE issues it happily. Why is that fine, and who must reject it?
- How does Keycloak verify the signature? Find the JWKS URL in the README ("SPIRE notes"). It's served over https by a certificate issued by the SPIRE root. Fetch it from inside the cluster (`k -n spark exec deploy/obo-client -c curl -- curl -sk <url>`) and match a `kid` to the JWT-SVID header. Then find how Keycloak trusts that https endpoint without `-k` (`KC_TRUSTSTORE_PATHS` in [keycloak.yaml](../platform/keycloak/keycloak.yaml)). What happens to that trust when the SPIRE root rotates?

**Check:** `check 1 1.4`

### 1.5 Break: find the real issuance boundary

Hypothesis: "only the Spark Connect server can act as `spiffe://zt.lab/ns/spark/sa/spark-connect`." Test it.

1. Read [labs/01-rogue.yaml](labs/01-rogue.yaml). It's unprivileged: a ServiceAccount name and the CSI volume, nothing else.
2. Apply it and fetch a JWT-SVID from it:

   ```bash
   k apply -f curriculum/labs/01-rogue.yaml
   k -n spark wait --for=condition=Ready pod/rogue
   SVID=$(k -n spark exec rogue -c svid -- /opt/spire/bin/spire-agent api fetch jwt \
     -audience http://keycloak.localhost/realms/zt -output json -socketPath /spiffe-workload-api/spire-agent.sock \
     | python3 -c 'import json,sys; print(json.load(sys.stdin)[0]["svids"][0]["svid"])')
   echo "$SVID" | jwt | grep sub
   ```

3. Take a Spark token from alice's notebook (as in lesson 0.4) and do hop 3 *from the rogue pod*, exactly as Spark Connect's interceptor does it. First get spark-connect's own token (the *actor token*), then do a delegated exchange of alice's Spark token, which carries `may_act` naming spark-connect:

   ```bash
   SPARK=$(echo 'import zt_auth; print(zt_auth.spark_token())' | k -n jupyter exec -i jupyter-alice -c notebook -- sh -c 'cat > /tmp/nb.py && python /tmp/nb.py')
   echo "$SPARK" | jwt | grep -A2 may_act
   kc() { k -n spark exec rogue -c curl -- curl -s http://keycloak.keycloak.svc:8080/realms/zt/protocol/openid-connect/token \
     -d client_assertion_type=urn:ietf:params:oauth:client-assertion-type:jwt-spiffe --data-urlencode "client_assertion=$SVID" "$@"; }
   AT=urn:ietf:params:oauth:token-type:access_token
   ACTOR=$(kc -d grant_type=client_credentials | python3 -c 'import json,sys; print(json.load(sys.stdin)["access_token"])')
   UC=$(kc -d grant_type=urn:ietf:params:oauth:grant-type:token-exchange \
     --data-urlencode "subject_token=$SPARK" -d subject_token_type=$AT \
     --data-urlencode "actor_token=$ACTOR" -d actor_token_type=$AT -d audience=unity-catalog \
     | python3 -c 'import json,sys; print(json.load(sys.stdin)["access_token"])')
   echo "$UC" | jwt | grep -E 'azp|preferred_username|client_id|x5t'
   svid spark pod/rogue | grep x5t
   k -n spark exec rogue -c curl -- curl -s -w '\n%{http_code}\n' -H "Authorization: Bearer $UC" \
     http://uc-gateway.unity-catalog.svc.cluster.local:8080/api/2.1/unity-catalog/catalogs
   ```

   **Predict before the last command:** what status does uc-gateway return? Every downstream check (Keycloak's client auth, the standing delegation grant behind `may_act`/`act`, the `cnf` binding, the gateway's SPIFFE ID and `act` rules, UC's mesh policy) was designed in phases 4–8. Which of them can tell `rogue` from Spark Connect?

4. Now find who can do this:

   ```bash
   for sa in jupyter:jupyter-session jupyter:jupyter-user spark:spark-connect; do
     for ns in jupyter spark; do
       echo "$sa can create pods in $ns: $(k auth can-i create pods -n $ns --as=system:serviceaccount:$sa)"
     done
   done
   ```

   One workload in the lab can create pods. Which one, in which namespace, and why does it need to? Which SPIFFE IDs could an attacker who compromised it obtain? Add this to your threat model as a new adversary.

5. Clean up: `k -n spark delete pod rogue`.

**Check:** `check 1 1.5` (it also checks the cleanup)

The conclusion to write down in your own words: **SPIRE faithfully attests what Kubernetes tells it. The trust root for a SPIFFE ID in this lab is whoever can create pods with that (namespace, ServiceAccount).** Hardening that is an admission-control problem (who may use which ServiceAccount, signed images, `k8s:pod-image` selectors), not a SPIRE problem. That's a design review question below.

### 1.6 Adjust: JWT-SVID TTL vs Keycloak's freshness window

Keycloak rejects SPIFFE client assertions whose `iat` is more than 300 s old (`fedClientAssertionMaxExp`). The agent caches each JWT-SVID until half its TTL. Those two numbers together decide whether hop 3 works.

1. **On paper first.** For a TTL of `T`, the oldest assertion a workload may present is about `T/2`. For which values of `T` does hop 3 fail some of the time? For `T = 20m`, sketch a timeline: in which minutes after a fresh mint do exchanges fail?
2. **Run it** (this changes the live cluster; budget 20 minutes):

   ```bash
   helm --kube-context kind-zt upgrade spire spiffe/spire --version 0.31.0 -n spire-server \
     -f platform/spire/values.yaml --set spire-server.defaultJwtSvidTTL=20m --wait
   ```

   Then, every 60 s, fetch a JWT-SVID in the smoke pod and print its age (`now - iat`). In parallel, run `make session-exchange-demo` or a lesson 0 query. Record when the first `Token was issued too far in the past` appears, in Spark Connect's log or the demo output, and when it stops.

   The `spiffe-helper` in the Spark pod and py-spiffe in notebooks fetch independently. Does each one see the same cached SVID? (Compare the `jti`... there is none. Compare `iat`.)
3. **Revert:**

   ```bash
   helm --kube-context kind-zt upgrade spire spiffe/spire --version 0.31.0 -n spire-server -f platform/spire/values.yaml --wait
   ```

   Already-cached 20-minute SVIDs live on until their half-life. How long until the system is healthy again without a restart? How would you speed it up?
4. **Decide:** is 5 min the right TTL? What would go wrong at 1 min? (Think about SPIRE server load, agent outages, and clock skew.) Write the one-line rule you'd put in a runbook.

We haven't run step 2 before: you're the first. If your timeline from step 1 doesn't match what you observe, that's the interesting part.

**Check:** `check 1 1.6` grades your prediction, checks that your observation fits the model, and confirms the revert actually took effect (Helm value *and* live SVIDs).

### 1.7 Challenge: only approved workloads may become spark-connect

1.5 showed that anyone who can create a pod as `spark/spark-connect` gets the identity. Close that with a Kubernetes **ValidatingAdmissionPolicy** (CEL, built in; no webhook to run).

**Requirements.** In namespace `spark`, a workload may run as ServiceAccount `spark-connect` only if it belongs to an approved Deployment: `spark-connect` or `obo-client` (demo 06 needs it).

- Everything the platform does must keep working: controller-made pods for both Deployments, re-applying [spark-connect.yaml](../apps/spark/spark-connect.yaml), and workloads using other ServiceAccounts.
- The 1.5 attack must fail: a bare pod, and a new Deployment, using that ServiceAccount.

Put the policy and its binding in `platform/admission/spark-identity.yaml`, apply it, and run `check 1 1.7`. The checker sends server-side dry-run requests, some as you and some impersonating the Kubernetes controllers, and reports which ones were admitted. Nothing is actually created.

**Stretch (★).** Two bypasses beat a policy that only looks at names and `ownerReferences`. Both are fields any pod creator controls. The checker tries both. Find out what they are from the output, then close them. You'll need to think about *who* is making the request, not just *what* it contains.

**Gotchas worth thinking about before you write CEL:**
- Which operations should the policy match? Who else updates pods and ReplicaSets after they're created (kubelet, garbage collector, HPA)? A policy that denies their updates can wedge deletions or rollouts.
- What happens to the running Spark Connect pod when you install the policy? What happens when you `rollout restart` it? Try it.
- `failurePolicy`: what should happen if the policy itself has an error?

When it passes, run `make uc-pep-demo` to make sure demo 06 still works. This is a change we **keep**: add a `make admission` target, and put it in the rebuild order before `spark-connect`.

## Checkpoint

1. Walk through, step by step, how `spark-connect`'s Envoy obtains its certificate, from the agent's node attestation to the SDS push. Where is the trust anchor at each step?
2. Why does the lab use a `pod-uid` selector per pod, rather than a static entry with `k8s:ns:spark` + `k8s:sa:spark-connect`? What would change about the issuance boundary if it used the static entry?
3. JupyterLab pods run alice's arbitrary code and mount the Workload API. What can her code obtain? Which later control stops alice's notebook from acting as bob, given that every user pod shares one SPIFFE ID?
4. A JWT-SVID is a 5-minute bearer credential with no `jti`. List every relying party in the lab that accepts one, and what an attacker holding one could do at each.
5. What are the operational consequences of the lab's 1-year self-signed root (`caTTL: 8760h`)? What would you use in production, and what in the lab depends on the root being static?

## Design review

You've built one hardening option in 1.7. Write a one-page proposal comparing it with at least two others:

- Kubernetes admission control (what you built in 1.7). What does it still trust? Who can edit an approved Deployment's pod template, and who can edit the policy itself?
- richer SPIRE selectors (`k8s:pod-image` digest, `k8s:container-name`, signature verification via the sigstore plugin);
- moving the high-value identity to its own namespace, or its own node pool, with a dedicated agent.

For each, say what attacker it stops, what it costs in deploy friction, and how it interacts with demo 06's `obo-client`, which *deliberately* shares spark-connect's identity. Recommend one.

## Reset

- 1.5: `k -n spark delete pod rogue` (harmless if left, but it's an extra holder of the spark-connect identity).
- 1.6: the revert command above; confirm with `ss entry show` that nothing else changed, and run `make session-exchange-demo` to check hop 3 works again.
- 1.7: keep the policy. To remove it for an experiment: `k delete -f platform/admission/spark-identity.yaml`.
