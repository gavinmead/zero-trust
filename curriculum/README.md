# Curriculum: Workload Identity and On-Behalf-Of Delegation, Hands-On

A course built on this lab. By the end you should be able to defend every design choice in it in a design review, say which control stops which attack, run the system through failures, and change it safely, including the parts that are still open.

**Audience:** staff engineers. The course assumes Kubernetes, TLS, OAuth 2.0/OIDC basics and general distributed-systems experience. It doesn't explain what a JWT is. It spends its time on *why* (threat models, specs, trade-offs) and *how it breaks*.

## How each lesson works

Every lesson has the same shape:

| Section | What you do |
|---|---|
| **Outcomes** | What you'll be able to explain or do afterwards |
| **Concepts** | Concepts, condensed: the spec, the security property and the trade-off. Links go to primary sources. |
| **Code tour** | The files in this repo that implement it. Read them before the lab. |
| **Lab: observe** | Inspect the running system. Predict the output *before* you run each command. |
| **Lab: break** | Remove or weaken one control and watch what fails, and what doesn't. |
| **Lab: adjust** | Make a real change to the lab. Some are improvements we keep; some are experiments we revert. |
| **Challenge** | Some lessons: build a control, then `check` attacks it |
| **Checkpoint** | Questions to answer in your own words. We discuss them together; there's no answer key in the repo, on purpose. |
| **Design review** | A one-page written prompt, the kind of thing you'd defend in an architecture review. |
| **Reset** | How to get back to a known-good state. |

Workflow with me: start a lesson by saying "lesson N". I'll check the cluster state, then you drive the labs and I review your predictions, answers and changes. Changes we decide to keep go into the lab (code, README, demos) as we go.

## Tools

`curriculum/bin/` has three helpers used throughout:

| Tool | Use |
|---|---|
| `trace [seconds]` | Merges every hop's Istio access log into one timeline, with peer IPs replaced by pod names |
| `svid <ns> <deploy>` | Shows the X.509-SVID a workload's sidecar presents, plus its RFC 8705 `x5t#S256` and the XFCC hex hash |
| `jwt [token]` | Decodes a JWT (stdin or argument) with `exp`/`iat` shown relative to now. It doesn't verify the signature. |

| `check <lesson> [exercise]` | Grades your labs (below) |

Put them on your path for the course: `export PATH="$PWD/curriculum/bin:$PATH"`.

## Checking your work

Every lesson's labs are graded by `check`, against the live cluster wherever possible.

```bash
check 1 --init     # creates curriculum/answers/01.toml: one key per question, with the question as a comment
check 1            # grade the whole lesson
check 1 1.5        # grade one exercise
```

The grader uses three mechanisms:

| Mark | Mechanism | Example |
|---|---|---|
| **live** | Your answer is compared with a value the checker computes from the cluster *at check time*. Nothing is stored, and if the cluster changes, the right answer changes too. | the node without a SPIRE agent; the current SVID serial; a token's `aud` |
| **hashed** | Fixed facts are compared by salted hash, so the repo holds no readable answer key. Free-text answers pass if they name the key concept. | which hop is which; what UC's `POST /` to MinIO is |
| **state** | The checker probes what you built or cleaned up. For challenges, it makes server-side dry-run requests as different identities and checks which are admitted. | the admission policy in 1.7; a reverted Helm value; a deleted rogue pod |

Output: `✓` pass, `✗` fail (with a hint, never the answer), `·` not answered yet, `★`/`☆` stretch goal met or not. Stretch goals are attacks a reasonable first solution misses. The exit status is non-zero only when something fails.

Checkpoint questions and design reviews are deliberately *not* graded by the tool. Those we discuss.

## Syllabus

Total time is roughly 25–30 hours. Each module builds on the one before it, so do them in order. Capstones can be taken in any order after module 10.

### Part I: Identity foundations

| # | Lesson | Core question | Hands-on highlights | Time |
|---|---|---|---|---|
| 0 | [Orientation and threat model](00-orientation.md) | What are we protecting, from whom, and where are the trust boundaries? | Put the lab under git; trace one notebook query across 7 hops; write the threat model you'll refine for the rest of the course | 2 h |
| 1 | [Workload identity: SPIFFE and SPIRE](01-spiffe-spire.md) | How does a pod prove who it is without a shared secret? | Node and workload attestation; read X.509- and JWT-SVIDs; find the real issuance boundary (Kubernetes RBAC on ServiceAccounts); tune JWT-SVID TTL against Keycloak's freshness window; **challenge:** an admission policy so only approved workloads become `spark-connect` | 3 h |
| 2 | Mesh enforcement: Istio on SPIRE | What does the mesh guarantee, and what does it only *look* like it guarantees? | SDS and the SPIRE template; STRICT vs PERMISSIVE; AuthorizationPolicy on principals; forge an XFCC header with `APPEND_FORWARD`, then see `SANITIZE_SET` stop it; why istiod couldn't fetch JWKS | 2.5 h |

### Part II: The authorization server and token exchange

| # | Lesson | Core question | Hands-on highlights | Time |
|---|---|---|---|---|
| 3 | Keycloak as AS: SPIFFE client auth and RFC 8693 | How does a workload authenticate to an AS without a client secret, and what does an exchange actually check? | Do hop 3 by hand with curl; take apart `federated-jwt`; find the 300 s assertion window and the missing `jti`; the V2 audience and scope rules (demo 02) | 2.5 h |
| 4 | Delegation semantics: `act`, `may_act`, standing grants | When must the token say *who is acting*, and how fast does a revoke take effect? | Demo 03 step by step; why `may_act` enters the notebook chain only at hop 2; measure the revocation window for Spark access; what `act` can and can't prove (it names a client, not a pod); the case for nested `act` | 2 h |
| 5 | Token chains, downscoping and sender-constraint | How do you stop a token skipping a hop, and stop a thief replaying it? | Demo 04 by hand; compute `x5t#S256` yourself and match it to `cnf`; hit the holder rule with `--bind all`; reproduce the pooled-connection binding bug on purpose | 3 h |

### Part III: The workloads

| # | Lesson | Core question | Hands-on highlights | Time |
|---|---|---|---|---|
| 6 | JupyterHub: the session broker | How does a user's browser login become something a notebook can use, without the notebook holding the login token? | **First real browser sign-in** (still unverified); `auth_state` and the custom scope; try to read the login token from a notebook; the refresh hook and why every token in the chain inherits `exp` | 2.5 h |
| 7 | Spark Connect: identity at the compute layer | How does a shared multi-tenant server act for many users at once without mixing them up? | Sidecar `RequestAuthentication` + `AuthorizationPolicy`; read the interceptor; spoof `user_id` and get refused; follow a token from interceptor → `ZtTokens` → `ZtUcTokenProvider`; add an audit log line per hop (kept) | 3 h |
| 8 | Unity Catalog and MinIO: authorization and credential vending | Where are data permissions actually decided, and what does the storage layer see? | Hop 4 at UC's token endpoint; grants as the policy decision point; capture a vended STS credential and test its path-scoped policy from outside Spark; why UC asked for `READ_WRITE` on a read | 2.5 h |

### Part IV: Enforcement and operations

| # | Lesson | Core question | Hands-on highlights | Time |
|---|---|---|---|---|
| 9 | The PEP: agentgateway and RFC 8705 enforcement | Where should sender-constraint be enforced, and how do you trust the certificate hash you're comparing against? | Demo 06 by hand; read the hex→base64url CEL; remove `SANITIZE_SET` and defeat the `cnf` check with an injected XFCC; observe hop-4 token caching; add a policy rule (kept) | 2.5 h |
| 10 | Operating it: failure modes and game day | What breaks first, and how do you know? | Game day: stop the SPIRE server, Keycloak, the hub, the gateway one at a time and predict the blast radius and time to impact; rotate the SPIRE CA; rebuild from scratch | 3 h |

### Part V: Capstones (the open items)

Each capstone ends in a change to the lab plus a short design doc.

| # | Capstone | What you build | Key tension |
|---|---|---|---|
| A | Close the JWT-SVID replay | An `AuthorizationPolicy` (or route split) so workload clients can't reach Keycloak's token endpoint through the ingress gateway; a demo proving the replay now fails | Browser users still need the same endpoint |
| B | Executor pods | Spark on Kubernetes with executors; vended storage credentials reach executors and the UC token doesn't | Delta/UC pass credentials through the Hadoop conf |
| C | Variant B review | A written design review: gateway does hops 3 and 4, Spark forwards the user token. Spike whether agentgateway can do SPIFFE client auth. | Fewer moving parts vs a wider-scoped token on the wire |
| D | Production readiness | A design doc: HA Keycloak, an external CA as SPIRE's upstream, federation across clusters, key management, audit | Which of the lab's shortcuts are load-bearing |

## Progress

| Lesson | Status | Changes kept in the lab |
|---|---|---|
| 0 | not started | |
| 1 | not started | |
| 2 | not started | |
| 3 | not started | |
| 4 | not started | |
| 5 | not started | |
| 6 | not started | |
| 7 | not started | |
| 8 | not started | |
| 9 | not started | |
| 10 | not started | |

Lessons 2–10 and the capstones are written as you reach them, so they can take your earlier answers and any changes to the lab into account. Each one ships with its `check` module.

## Reference shelf

- RFC 8693 OAuth 2.0 Token Exchange: https://www.rfc-editor.org/rfc/rfc8693
- RFC 8705 OAuth 2.0 Mutual-TLS Client Authentication and Certificate-Bound Access Tokens: https://www.rfc-editor.org/rfc/rfc8705
- RFC 9068 JWT Profile for OAuth 2.0 Access Tokens: https://www.rfc-editor.org/rfc/rfc9068
- SPIFFE standards (SPIFFE ID, X.509-SVID, JWT-SVID, Workload API): https://github.com/spiffe/spiffe/tree/main/standards
- SPIRE concepts: https://spiffe.io/docs/latest/spire-about/spire-concepts/
- Istio security concepts: https://istio.io/latest/docs/concepts/security/
- Keycloak token exchange: https://www.keycloak.org/securing-apps/token-exchange
- draft-ietf-oauth-identity-chaining (cross-domain chains, relevant to capstone D)
- NIST SP 800-207 Zero Trust Architecture (vocabulary: PDP/PEP)
