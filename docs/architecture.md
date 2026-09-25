# Architecture

How the pieces fit together end to end, from a `kubectl apply` of a Custom
Resource to a real NetBird API call.

## The big picture

```
 +----------------+      +------------------+      +------------------------+
 | NetBirdRoute   |      | CompositeCtrl    |      | netbird-complementary- |
 | (Custom        | <--  |  netbird-route   |  --> | operator webhook Pod   |
 |  Resource in   |      |  (Metacontroller |      |                        |
 |  the cluster)  |      |   manages it)    |      |  POST /sync/route      |
 +----------------+      +------------------+      |  POST /finalize/route  |
                                                   |                        |
                                                   |  Python FastAPI        |
                                                   |     |                  |
                                                   |     v                  |
                                                   |  NetBirdClient +       |
                                                   |  per-request Resolver  |
                                                   +-----------+------------+
                                                               |
                                                               v
                                                   https://netbird.example.com
                                                        /api/routes/*
```

Metacontroller is **not** a library — it is a separate controller-manager
deployment already present in the cluster (`metacontroller` namespace). Our
image is dumb: it is just the HTTP webhook that serves two routes per CRD.

## The CompositeController contract

For each of the 16 CRDs, a `CompositeController` resource registers:

- A **parent**: `apiVersion: netbird.io/v1alpha1`, `resource: netbirdroutes`, etc.
- Two **hooks** pointing at this webhook:
  - `sync`: called whenever the parent changes or on every `resyncPeriodSeconds`
    (default 300s) tick. Must return the desired `status` and any `children`.
  - `finalize`: called when the parent is being deleted. Must return
    `finalized: true` for Metacontroller to release the finalizer.

Metacontroller automatically adds a finalizer to every parent so that a
Delete does not complete until our `finalize` hook returns success. Cleanup is
handler-specific: ordinary owned resources are deleted, singleton settings are
released, and role-binding restoration is conservative/best-effort.

Relevant code: [`webhook/main.py`](../webhook/main.py).

## The sync path in one picture

```
POST /sync/route
  |
  +-- parse the envelope: {parent, children, finalizing}
  |
  +-- enforce singleton (if kind in SINGLETON_KINDS)
  |
  +-- new NetBirdClient (reads NB_MGMT_URL and resolves the PAT using explicit
  |   argument > NB_API_KEY_FILE > NB_API_KEY; files are re-read per attempt)
  +-- new Resolver(client)
  |
  +-- call handlers.<kind>.sync(parent, resolver)
  |      |
  |      +-- build desired body from parent.spec
  |      |   (resolving any *Ref field to a NetBird ID via the Resolver)
  |      |
  |      +-- recover current state:
  |      |     - GET by status.id if set
  |      |     - fall back to "find by name/identity" (handler-specific)
  |      |
  |      +-- create if missing (POST)
  |      |   or diff + PUT if drift detected
  |
  +-- wrap status: synced, observedGeneration, lastSyncTime, conditions
  |
  +-- return { status, children: [...] }
```

The `children` list is populated for `NetBirdToken` (its PAT `Secret`) and
`NetBirdInstance` (its managed runtime). See
[webhook/handlers/token.py](../webhook/handlers/token.py),
[webhook/handlers/instance.py](../webhook/handlers/instance.py) and the
special-cased `CHILDREN_KINDS` branch in `main.py`.

`NetBirdRoleBinding` additionally uses a Metacontroller `customize` hook. The
hook v2 selects every binding across namespaces as read-only related data.
This lets each stateless webhook invocation select a deterministic lexical
winner for overlapping memberships and prevents an uninformed demotion.
The response also asks for a per-object resync (default 60 seconds) because
JWT membership changes occur outside Kubernetes.

## Finalize path

On parent deletion Metacontroller calls:

```
POST /finalize/route
  |
  +-- handler.<kind>.finalize(parent, resolver)
  |      |
  |      +-- nb_id = status.id
  |      +-- for status-only ownership handlers, release immediately when
  |      |   the recorded upstream ID is empty
  |      +-- legacy handlers may try best-effort recovery by name/identity
  |      |
  |      +-- DELETE /api/<endpoint>/<nb_id> where that handler owns deletion
  |          (tolerates 404 — already gone)
  |
  +-- return { status: {}, children: [], finalized: true }
```

Finalize handlers are deliberately forgiving when no owned ID has been
persisted. If we required perfect upstream availability to release a finalizer,
a 5-minute NetBird outage would trap every pending delete until somebody
manually patched the finalizer off. The cost of this forgiveness is that a
successful finalize during a real outage may orphan a single upstream
resource — an acceptable trade compared to a queue of stuck CRs.

`NetBirdRoleBinding` is even more conservative: it restores only status-owned
roles that still equal the applied role, skips users claimed by another active
binding, and releases its finalizer on API failure to avoid permanent
termination. An upstream admin assignment may therefore remain for manual
review; it will never risk an uninformed demotion.

## The Resolver

Users do not know NetBird internal IDs. Every CRD that references other
NetBird entities does so by human-readable name:

- `groupRefs`, `peerGroupRefs`, `distributionGroupRefs`, `autoGroupRefs`
- `peerRef`, `userRef`, `networkRef.name`, `zoneRef`
- `sourcePostureCheckRefs`

Peer enrollment deliberately does not use the peer name resolver. `adopt`
addresses a peer by its stable NetBird ID. `enroll` captures a baseline,
exposes a one-off setup key through a Metacontroller-owned Secret child, and
accepts exactly one new peer satisfying the declared identity assertions. The
Secret is absent from desired children as soon as the key is revoked.

The `Resolver` class (in `webhook/netbird_client.py`) translates these to IDs
per sync request. It caches results for the duration of a single webhook
call only — stateless across requests. Key characteristics:

- Groups use the server-side `?name=X` filter for exact-match. Cached per
  name so repeated refs in the same sync reuse the hit.
- Peers / networks / posture checks / users / DNS zones have no filter param; the resolver
  lists once on first demand and indexes the response.
- A resolver miss raises `KeyError(f"group not found: 'devs'")`. The webhook
  converts this into a `RefNotFound` condition on the CR status (not an
  `InternalError`) so the user can distinguish a configuration mistake from
  a real bug.

## The NetBird HTTP client

[`webhook/netbird_client.py`](../webhook/netbird_client.py) is a thin wrapper
around `requests.Session`. It handles:

- Auth: `Authorization: Token <PAT>` header.
- Base URL: `NB_MGMT_URL` env, with automatic `https://` prefix and `/api`
  path normalization.
- Retries: exponential backoff with full jitter on 429 / 5xx / transport
  errors. Parameters come from env vars — see
  [install.md#environment-variables](install.md#environment-variables).
- `Retry-After` header is respected if the server sends one.
- `404` is modeled as a distinct `NotFound` exception so callers can do
  "create if missing" flows naturally.

## Name → ID lifecycle

- **First sync**: no `.status.id` yet. The handler does a list-and-match-by-name
  to detect a pre-existing resource (recover from a "created upstream,
  crashed before writing status" scenario). If none is found, it POSTs.
- **Subsequent syncs**: `.status.id` drives the flow. A GET by ID confirms
  the resource still exists; a 404 falls back to the name recovery and
  ultimately re-creates.
- **Finalize**: newer destructive handlers use only IDs already persisted in
  status. `NetBirdPolicy.state: absent` uses a two-phase adoption so a
  name-discovered object is never deleted before its ID is recorded.

## Edge ownership

`NetBirdPeerGroupBinding` owns one relationship rather than an upstream object.
It resolves the peer and group, reads the full group, and PUTs the group back
with only the target peer added or removed. Existing peers and resource entries
are preserved. The peer/group IDs in status are the sole finalization authority,
so a missing peer merely produces `RefNotFound` until enrollment completes.

## Singleton CRDs

`NetBirdAccountSettings` and `NetBirdDNSSettings` wrap endpoints that operate
on a single global resource (the account / the DNS settings object). Multiple
instances would cause reconcile wars. The webhook enforces
`metadata.name == "default"`; anything else is rejected with a 400 so Metacontroller
does not even retry. See [`webhook/main.py`](../webhook/main.py) — `_check_singleton`.

## Token child Secret

NetBird returns the plaintext `PersonalAccessToken` only once, at creation.
The webhook therefore:

1. Inspects the `children` field of the incoming Metacontroller payload to
   see whether the target `Secret` already exists and already has the key
   set. If it does, and upstream still has the token, no-op.
2. If absent or inside `renewBeforeDays`, POSTs a new token, base64-encodes the returned `plain_token`,
   and returns a `v1.Secret` in the `children` array. Metacontroller reconciles
   it with the `InPlace` update strategy declared in the
   `CompositeController.childResources`.
3. Records the former token ID and a hash of the pending Secret value. A later
   sync observes that hash, then releases the former PAT to expire naturally.
4. On delete, Metacontroller garbage-collects the Secret and the handler
   deletes both active and still-pending-old token IDs upstream.

## Why Metacontroller and not controller-runtime / kopf

- **Low code surface.** No informers, no event handlers, no leader election
  boilerplate. Just two HTTP endpoints.
- **Language agnostic.** If we ever want to rewrite in Go, the only thing
  that changes is the webhook server — the YAML stays.
- **Declarative child GC.** Parent deletion cascades through Metacontroller's
  controllerRef tracking; we don't re-implement owner reference logic.

Trade-off: no custom subresources, no webhook conversion. For `v1alpha1` we
don't need either. A future `v1` graduation will likely move to
`controller-runtime` if we need conversion webhooks.
