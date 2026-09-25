# CLAUDE.md

Meta-context for future Claude Code sessions working on this repository.

## What this is

A Kubernetes operator, built on top of [Metacontroller](https://metacontroller.github.io/metacontroller/),
that manages NetBird resources the [official NetBird Kubernetes operator](https://github.com/netbirdio/kubernetes-operator)
does not cover. The responsibility split was audited against official operator
v0.8.0 (commit `41d9956`, NetBird client v0.72.4). Legacy client payloads were
audited against NetBird OpenAPI v0.68.3; Policy and Networks API payloads were
audited against NetBird commit `3f90181` on 2026-08-24.

The official operator v0.8 owns reusable `Group`, `SetupKey`, `NetworkRouter`,
`NetworkResource`, `NetworkEgress`, `SidecarProfile` and `ClusterProxy`
(`netbird.io/v1alpha1`), plus compatibility `NB*` APIs. This operator owns 18
complementary CRDs. See `docs/migration-v0.8.md`; never infer equivalence from
a similar Kind name.

`NetBirdPeerEnrollment` is a narrow lifecycle primitive, not a second generic
SetupKey API. Its enroll mode owns one temporary one-off key and Secret; its
adopt mode never transports credentials.

## Supporting docs (read as needed)

- [docs/architecture.md](docs/architecture.md) — how the webhook, Metacontroller
  CompositeControllers, the NetBird client and the Resolver fit together.
- [docs/install.md](docs/install.md) — Helm values reference, deployment via
  the Runik spell, and the versioning contract.
- [docs/debug.md](docs/debug.md) — status conditions, log levels, common
  failure modes, and how to test the NetBird API directly.
- [docs/api-reference.md](docs/api-reference.md) — complementary API fields.
- [docs/migration-v0.8.md](docs/migration-v0.8.md) — audited ownership matrix
  and safe migration paths.
- [docs/netbird-declarative-limitations.md](docs/netbird-declarative-limitations.md)
  — native NetBird lifecycle and API limitations that currently prevent a
  deterministic declaration of JWT-backed access groups.
- [examples/](examples/) — one working CR per CRD. These mirror what is tested
  in `tests/e2e/`.
- [tests/e2e/](tests/e2e/) — integration-ready manifests; never apply them
  from an automated/local verification run or without explicit approval.

## Tech stack

- **Python 3.12**, FastAPI, `requests`. No async HTTP client on purpose — the
  operator is IO-light and the serialized path keeps retries simple.
- **Metacontroller** runs the control loop out of process; this repo is just
  the sync / finalize webhook handler.
- **Helm** ships the chart (`chart/`) for use by Runik and other consumers.
- **pytest + responses** for unit and webhook-envelope tests. There is no
  automated real-instance integration harness; `tests/e2e/*.yaml` are manual
  manifests and are not part of local verification.

## Repo layout

```
netbird-complementary-operator/
  CLAUDE.md                    <- this file
  Containerfile               <- sole image build definition
  docs/
    architecture.md
    install.md
    debug.md
  crds/                        <- source of truth for every CRD
  controllers/                 <- standalone CompositeController manifests
                                  (duplicated into the Helm chart templates —
                                  kept here for direct kubectl apply testing)
  webhook/
    main.py                    <- FastAPI app, /sync and /finalize routing
    netbird_client.py          <- HTTP client + per-request name->ID Resolver
    handlers/                  <- one module per CRD kind
    requirements.txt
  chart/                       <- Helm chart shipped to users
    Chart.yaml                 <- single source of truth for version (see below)
    values.yaml
    values.schema.json
    crds/                      <- installed once per release by Helm (not
                                  upgraded; keep them compatible across
                                  minor versions)
    templates/
  examples/                    <- ready-to-apply CRs (kubectl apply -f)
  tests/
    unit/                      <- mocked webhook/client tests (`responses`)
    e2e/                       <- smoke manifests used during hands-on runs
```

## Versioning rule (important)

**Chart `appVersion` is the single source of truth for the deployed image tag.**
The chart template falls back to `.Chart.AppVersion` when `values.image.tag`
is empty.

Release flow:
1. Bump `chart/Chart.yaml` `version` + `appVersion` together (always in sync).
2. Merge the version change into Forgejo `upstream`.
3. Publish the matching Forgejo Release. Public automation builds
   `ghcr.io/runik-platform/netbird-complementary-operator:<version>`.

If you hardcode a different `image.tag` in the spell you break that contract
and future upgrades require a spell edit. Don't.

## Running locally

```bash
# unit tests
python3 -m venv .venv && .venv/bin/pip install -q -r tests/requirements.txt
.venv/bin/python -m pytest tests/unit -q

# render chart without installing
helm template t chart --namespace netbird

# build image from the repository root
podman build -f Containerfile -t netbird-complementary-operator:local .

# run image locally against a real NetBird (no K8s)
podman run --rm -p 8080:8080 \
  -e NB_MGMT_URL=https://netbird.example.com \
  -e NB_API_KEY=<PAT> \
  netbird-complementary-operator:local
```

## CRDs at a glance

| Kind (Namespaced) | Endpoint | Notes |
|---|---|---|
| `NetBirdAccountSettings` | `PUT /accounts/{id}` | Singleton (`metadata.name: default`) |
| `NetBirdDNSSettings` | `PUT /dns/settings` | Singleton; diff-skip |
| `NetBirdNameserverGroup` | CRUD `/dns/nameservers` | Forwarder config |
| `NetBirdRoute` | CRUD `/routes` | `peerRef` XOR `peerGroupRefs`, `network` XOR `domains` (CEL-enforced) |
| `NetBirdRoleBinding` | `GET /groups`, `GET/PUT /users` | Existing external-group membership → admin; never POSTs users/groups |
| `NetBirdPostureCheck` | CRUD `/posture-checks` | Diff-normalized to ignore null / {} / [] |
| `NetBirdToken` | `/users/{uid}/tokens` | Emits a Secret; two-phase rotation before expiry |
| `NetBirdPeer` | `PUT/DELETE /peers/{id}` | Cannot create — peers self-register |
| `NetBirdDNSZone` | CRUD `/dns/zones` | Authoritative zone, distinct from NameserverGroup |
| `NetBirdDNSRecord` | CRUD `/dns/zones/{zid}/records` | Matched by (zone, name, type) for idempotency |
| `NetBirdPolicy` | CRUD `/policies` | Full rules, ICMP, port ranges, posture checks; safe two-phase `state: absent` |
| `NetBirdNetwork` | CRUD `/networks` | Account-plane container only; upstream has no disabled state |
| `NetBirdNetworkResource` | CRUD `/networks/{nid}/resources` | Arbitrary external IP/CIDR/domain/wildcard, not a Service |
| `NetBirdNetworkRouter` | CRUD `/networks/{nid}/routers` | Existing peer/group only; never creates workloads |
| `NetBirdPeerGroupBinding` | `GET/PUT /groups/{gid}` | Owns one membership edge and preserves all others |
| `NetBirdPeerEnrollment` | `/setup-keys`, `/peers`, `/groups` | Temporary local enrollment or stable-ID remote adoption |

## Things that bit us, documented so they don't bite again

- `RouteRequest` in the upstream OpenAPI has **no `name` field** — the CRD
  identifies a route by `description + networkId`.
- `AccountExtraSettings` requires **all five fields** (peer_approval_enabled,
  user_approval_required, network_traffic_logs_enabled,
  network_traffic_logs_groups, network_traffic_packet_counter_enabled). The
  handler fills defaults for omitted ones.
- A `PersonalAccessToken` plaintext value is **only returned once at create**.
  `NetBirdToken` publishes it to a Secret, observes the update, then lets the
  previous token expire naturally. Never delete the old PAT during rotation.
- API-token precedence is explicit constructor token > `NB_API_KEY_FILE`
  (re-read on every HTTP attempt) > legacy `NB_API_KEY`.
- `NetBirdRoleBinding` ownership lives only in `.status.managedUsers`. Never
  claim a pre-existing admin, modify owner or `operator-owner`, create a
  user/group, or demote after a partial API response. Owned role drift is
  repaired. Related bindings arrive through a customize v2 hook and the
  cluster-wide lexical `namespace/name` winner applies.
- `finalize` handlers tolerate NetBird API errors when `.status.id` is empty.
  Otherwise a transient 401 or network outage would trap every pending
  deletion behind a stuck finalizer.
- CEL `x-kubernetes-validations` on `NetBirdRoute` and `NetBirdNetworkRouter` enforce mutex
  at admission time. Don't remove the Python-side checks in the handler —
  they're a second line of defense for server-synced resources.
- `NetBirdPolicy.state: absent` first persists an adopted ID, then deletes on a
  later sync. Its finalize path and the new Networks handlers never rediscover
  by name; destructive authority comes only from status.
- `/api/networks` has no `enabled` field. `NetBirdNetwork.enabled: false`
  produces `InvalidSpec`; never reinterpret it as deletion.
- NetBird returns "token invalid" for both PAT validation failures AND
  backend DB auth failures. A 401 does not necessarily mean the PAT is wrong.
  See `docs/debug.md`.

## What is intentionally NOT a CRD

After auditing the OpenAPI, these are the endpoints deliberately left out:

- `GET /events` — read-only audit log; doesn't fit a declarative resource.
- `GET /geo/*` — lookup-only, no state to manage.
- `POST /peers/{id}/approve` — action, not state. Cloud-only flow anyway.
- `/users/invites/*` — interactive token flow; better served by a Job.
- `/integrations/*`, `/idp/users/*` — NetBird Cloud features, not self-hosted.

Don't add CRDs for these unless the semantics change.

## When to bump chart major / minor / patch

- **Major (0.x → 1.x)**: once we graduate `v1alpha1` to `v1`. Requires a
  conversion webhook.
- **Minor (0.2.0 → 0.3.0)**: new CRDs, new fields, behavior changes.
- **Patch (0.3.0 → 0.3.1)**: bug fixes, dependency bumps, no CRD changes.

CRDs in `chart/crds/` are installed only on first release by Helm — changes to
them are NOT applied by `helm upgrade`. If you need a CRD change that is not
backwards-compatible, ship a `kubectl apply -f crds/` note in the PR and the
rollout docs.
