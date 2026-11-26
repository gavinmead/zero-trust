# Lesson 0: Orientation and threat model

**Time:** about 2 hours. **Prerequisite:** the lab is running (`make status` shows every pod Running).

## Outcomes

- Name the two identities on every request in the lab, and which component checks each one.
- Trace one notebook query across every hop from live logs, and explain each line.
- Keep an inventory of every credential in the chain: who issues it, who holds it, how long it lives, and whether it's bearer or sender-constrained.
- Write a first-pass threat model that the rest of the course refines.

## Concepts

### The problem this lab solves

Alice runs a query in a notebook. The notebook asks a shared Spark server to run it. Spark asks Unity Catalog for table metadata and storage credentials, then reads MinIO. Every hop after the first is a *service* calling another *service*, but the decision that matters at the end ("may this read `trips`?") is about *alice*.

There are two classic wrong answers:

1. **Services act under their own authority.** Spark has a powerful service account and UC trusts Spark. This is the *confused deputy* problem: any caller who can make Spark do something gets Spark's privileges. Authorization collapses to "can you reach Spark?"
2. **Forward the user's token everywhere.** Every service receives alice's login token. Now every hop can replay it against every other hop for its whole lifetime, and a compromise anywhere means alice is compromised everywhere. A token valid at Spark is also valid at UC, because nothing narrows it.

The lab's answer is the **on-behalf-of (OBO) chain**. At each hop the caller exchanges the token it holds for a new one ([RFC 8693](https://www.rfc-editor.org/rfc/rfc8693)) that:
- still says *alice* (`sub`);
- is valid only for the *next* service (`aud`);
- was obtained by an authenticated *workload* (`azp`; with delegation, `act`);
- can never outlive the original login (`exp` is inherited);
- for the last hop, is bound to the presenter's certificate (`cnf`, [RFC 8705](https://www.rfc-editor.org/rfc/rfc8705)).

### Two identities on every request

Every request carries two separate claims of identity, checked by different machinery:

| | "Who is calling?" | "On whose behalf?" |
|---|---|---|
| Carrier | mTLS client certificate (X.509-SVID) | Bearer JWT in `Authorization` |
| Issued by | SPIRE | Keycloak |
| Proves | This pod runs as SA `x` in namespace `y` | User alice authenticated, and this token was minted for audience `z` |
| Checked by | Envoy sidecars (`PeerAuthentication`, `AuthorizationPolicy` on `principals`) | Sidecars (`RequestAuthentication`), the Spark interceptor, uc-gateway, Keycloak, UC |

Most of the lab's interesting controls *join* the two: Keycloak authenticates the workload with its JWT-SVID before exchanging the user's token; Spark's `AuthorizationPolicy` requires peer `jupyter-user` *and* `azp=jupyter`; uc-gateway requires `cnf` to match the TLS peer's certificate.

### Vocabulary (NIST SP 800-207)

- **PDP (policy decision point):** decides. In this lab: Keycloak (may this client exchange this token for this audience?) and Unity Catalog (may alice read this table?).
- **PEP (policy enforcement point):** enforces at the request path: sidecars, the Spark interceptor, uc-gateway, MinIO's STS session policy.
- A recurring theme: **a PEP is only as good as the facts it's handed.** The `cnf` check in lesson 9 is meaningless if the certificate hash it compares against can be forged.

## Code tour

Skim these now; later lessons go deep.

- [README.md](../README.md): Target flow, Phases, and the end-to-end diagram under "End-to-end OBO to Unity Catalog and MinIO".
- [Makefile](../Makefile): every target the rebuild uses, in order.
- [apps/jupyterhub/zt_auth.py](../apps/jupyterhub/zt_auth.py): the notebook's side of the chain (hops 1–2 from the client's view).
- [demos/05-lakehouse/checks.py](../demos/05-lakehouse/checks.py): what a "user query" looks like in the demos.

## Lab

```bash
cd ~/development/zero-trust
export PATH="$PWD/curriculum/bin:$PATH"
k() { kubectl --context kind-zt "$@"; }
check 0 --init   # answers file + threat-model skeleton
```

Record answers in `curriculum/answers/00.toml` as you go, and run the `check` line at the end of each exercise.

### 0.1 Version control

We'll change the lab throughout the course, so every change should be a reviewable diff you can revert. The lab is in git and the repository is **public**, so what gets committed matters.

1. Read [.gitignore](../.gitignore) and run `git status --ignored`. For each ignored path, say what would leak if it were committed. Two are obvious; why is `.claude/settings.local.json` there?
2. Every credential in the manifests is a `${...}` placeholder filled from `.secrets/keycloak.env` by `scripts/gen-secrets.sh`. Find one, for example `JUPYTERHUB_CLIENT_SECRET` in `platform/keycloak/realm-zt.json`, and trace how it reaches the cluster.
3. Your answers in `curriculum/answers/` and `curriculum/notes/` would also be public if committed. Decide whether you want that, and ignore them if not.
4. Work on a branch per lesson (`git switch -c lesson-01`), so every change we keep is a reviewable diff.
5. **Check:** `check 0 0.1`.

### 0.2 Health check

```bash
make status
```

You should see roughly 40 pods, all Running. Note the `READY` counts: `2/2` is app + `istio-proxy`; `3/3` adds a SPIFFE helper or spire-agent container. **Question:** `outside/curl` is `1/1`. What's it for? (Look at its namespace labels.) Which other non-system namespaces run without sidecars, and why is each one acceptable?

**Check:** `check 0 0.2`

### 0.3 Predict, then trace one query

**Before running anything**, write down the sequence of network calls you expect when alice runs `spark.table("lakehouse.analytics.trips").count()` from a fresh session: caller → callee, and what's exchanged. Use the README diagram, but commit to a prediction.

Now run it:

```bash
k -n jupyter exec -i jupyter-alice -c notebook -- sh -c 'cat > /tmp/q.py && python /tmp/q.py' <<'PY'
import zt_auth
spark = zt_auth.spark_session()
print(spark.table("lakehouse.analytics.trips").count())
PY
trace 30 | grep -v 'ReleaseExecute\|/certs\|well-known'
```

(Why a file and not `python -c`? pyspark mistakes a `__main__` without `__file__` for its own doctest run and exits. It's in the README; you'll hit it again.)

Answer from the trace:

1. How many `POST .../token` calls reach Keycloak, and from which pods? Map each one to a hop number in the README's table.
2. The hub makes a `GET .../userinfo` call. Why would a session-token endpoint need it? (Read `zt-session-token` in [values.yaml](../apps/jupyterhub/values.yaml).)
3. uc-gateway calls `POST /api/1.0/unity-control/auth/tokens` once, but forwards several catalog requests. What does that tell you about the gateway, and what's the security consequence?
4. Unity Catalog makes a `POST /` to MinIO. What is it?
5. The credentials request says `operation=READ_WRITE` for a read-only `count()`. Who asked for write, and who decides whether alice gets it?
6. Request IDs (`x-request-id`, the UUID in each raw log line) differ at every hop. What would you need to correlate one user action across the chain in production?

Compare with your prediction. Anything you missed is a topic for a later lesson. Note it.

**Check:** `check 0 0.3` (questions 1, 3, 4 and 5 are graded; 2 and 6 we discuss)

### 0.4 Credential inventory

Pull the tokens alice's notebook can see and decode them:

```bash
nb() { k -n jupyter exec -i jupyter-alice -c notebook -- sh -c 'cat > /tmp/nb.py && python /tmp/nb.py'; }
echo 'import zt_auth; print(zt_auth.session_token())' | nb | jwt
echo 'import zt_auth; print(zt_auth.spark_token())'   | nb | jwt
echo 'import zt_auth; s=zt_auth.spark_session(); s.range(1).collect(); print(s.conf.get("spark.zt.uc.claims"))' | nb
```

(These print live bearer tokens into your terminal. That's fine in a lab; in production it's an incident.)

Fill in this table in your notes. Some cells need later lessons; mark them `?` and come back.

| Credential | Issuer | Holder(s) | Audience | Lifetime | Bearer or bound? | Where it's checked |
|---|---|---|---|---|---|---|
| Alice's login access token | | | | | | |
| Refresh token | | | | | | |
| Session token | | | | | | |
| Spark token | | | | | | |
| Keycloak UC token (hop 3) | | | | | | |
| Spark Connect's actor token (hop 3) | | | | | | |
| UC-issued token (hop 4) | | | | | | |
| MinIO STS credentials | | | | | | |
| X.509-SVID (any pod) | | | | | | |
| JWT-SVID (any pod) | | | | | | |
| UC admin token | | | | | | |
| MinIO root credentials | | | | | | |

Two things to notice:
- The session token's `scope` includes `profile email`, not just `session:token_exchange`. Is that needed? (Who at the end of the chain needs `email`?)
- Compare `exp` across the session token and the Spark token.

**Check:** `check 0 0.4` compares your values with tokens it mints from alice's notebook at check time.

### 0.5 First-pass threat model

Create `curriculum/notes/threat-model.md` (your file; we'll revisit it in lessons 5, 9 and 10). For each adversary below, write: what they can reach, what credentials they can get, what they can do with them, for how long, and which control limits them.

| Adversary | Starting position |
|---|---|
| A. Malicious code in alice's notebook | Code execution in `jupyter-alice`. **Notebooks run arbitrary user code by design**, so assume this is normal, not exotic. |
| B. Bob | A legitimate user who wants alice's data |
| C. Compromised Spark Connect server | Code execution in `spark-connect` |
| D. In-cluster network attacker | A pod with no sidecar (like `outside/curl`) |
| E. Token leaked to logs | One token from your inventory, chosen by the attacker |
| F. Cluster insider | Can create pods in namespace `spark` (but isn't cluster-admin) |

Don't aim for completeness. Aim to be specific: "the attacker gets the Spark token, valid ≤10 min, usable only from a pod running as `jupyter-user`, because ...".

**Check:** `check 0 0.5` only checks the shape: every adversary covered, specific enough, and time-bounded. The substance we review together.

## Checkpoint

1. For the Spark → uc-gateway request, name both identities, and every check applied to each before UC sees the request.
2. Adversary C (compromised Spark) is the worst case in the lab. Why? What bounds the damage, in time and in scope?
3. List the PDPs and PEPs in the lab. Which PEP has the least context to make its decision?
4. What's the single most valuable key in the system? (There are a few defensible answers; argue for one.)
5. Every token in the chain inherits the login token's `exp`. Name one security benefit and one operational cost.

## Design review

A colleague proposes simplifying the design: "drop hops 1 and 2. The notebook sends alice's login token straight to Spark, and Spark exchanges it for UC. It's one fewer Keycloak round trip and no hub endpoint."

Write a one-page response. Cover what's lost (be concrete about which adversary in 0.5 gains what), what's gained, and under what conditions you'd accept the simplification.

## Reset

Nothing in this lesson changes the cluster. If you committed in 0.1, `git status` should be clean apart from your notes.
