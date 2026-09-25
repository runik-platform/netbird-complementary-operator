# Migration and responsibility audit for official operator v0.8.0

This audit compares this repository with the official operator tag
[`v0.8.0` (`41d9956`)](https://github.com/netbirdio/kubernetes-operator/tree/v0.8.0).
That release uses the NetBird v0.72.4 Go client. Role-binding and PAT behavior
in this repository remains validated against the supported NetBird v0.68.3
[OpenAPI](https://github.com/netbirdio/netbird/blob/v0.68.3/shared/management/http/api/openapi.yml):
`User.auto_groups` is the list of group IDs, and `UserRequest` requires
`role`, `auto_groups` and `is_blocked`.

The Policy and Networks resources added in 1.1.0 were separately audited
against NetBird commit [`3f90181`](https://github.com/netbirdio/netbird/tree/3f90181f355f37e86e11de4dfe32f640a4f6aee8),
whose OpenAPI includes policy rules/posture checks and nested Network resources
and routers. Its Network model contains only `name` and `description`, with no
enabled state.

## Decision matrix

| Complementary CRD | NetBird endpoint | Official v0.8 equivalent | Semantic difference | Decision | Migration path |
|---|---|---|---|---|---|
| `NetBirdAccountSettings` | `GET /accounts`, `PUT /accounts/{id}` | None | Account-plane singleton | Keep | None |
| `NetBirdDNSSettings` | `GET/PUT /dns/settings` | None | Global DNS management exclusions | Keep | None |
| `NetBirdNameserverGroup` | CRUD `/dns/nameservers` | None | DNS forwarding configuration | Keep | None |
| `NetBirdRoute` | CRUD `/routes` | None | Legacy route supports CIDR/domains, peer or peer-group routing, metric, masquerade and distribution/access groups. Official `NetworkResource` is not `/routes`. | Keep | None; redesign intentionally before moving to Networks API |
| `NetBirdPostureCheck` | CRUD `/posture-checks` | None | Account-plane posture definitions | Keep | None |
| `NetBirdToken` | CRUD `/users/{userId}/tokens` | None | The official operator consumes a PAT; this resource manages and rotates PATs | Keep | Point consumers at the child Secret; arrange reload/rollout per consumer |
| `NetBirdPeer` | `GET/PUT/DELETE /peers/{id}` | None | Modifies an already registered arbitrary peer. Official routing resources create dedicated workloads. | Keep | None |
| `NetBirdDNSZone` | CRUD `/dns/zones` | None | Authoritative account-plane zone | Keep | None |
| `NetBirdDNSRecord` | CRUD `/dns/zones/{zoneId}/records` | Partial internal behavior of official `NetworkResource` | Complementary record supports explicit A/AAAA/CNAME content. Official creates a specific A record for a referenced ClusterIP Service. | Keep | Use official `NetworkResource` only for that Service-oriented topology |
| `NetBirdPolicy` | CRUD `/policies` | Partial `NBPolicy` compatibility API | Complementary policy supports multiple rules, all/TCP/UDP/ICMP, port ranges and posture checks, plus groups containing Network Resources. | Keep | Do not translate `NBPolicy` blindly; its schema loses rule semantics. |
| `NetBirdNetwork` | CRUD `/networks` | Network implicitly owned by official `NetworkRouter` | Complementary resource manages only the account-plane container and creates no Kubernetes objects. | Keep | Choose based on who owns the routing topology. |
| `NetBirdNetworkResource` | CRUD `/networks/{id}/resources` | Official `NetworkResource` | Complementary resource accepts arbitrary external addresses; official v0.8 derives an address from a referenced Kubernetes Service. | Keep | Kinds differ; migrate only with an explicit address/source-of-truth decision. |
| `NetBirdNetworkRouter` | CRUD `/networks/{id}/routers` | Official `NetworkRouter` | Complementary resource attaches an already-enrolled peer/group; official creates and enrolls routing-peer Pods. | Keep | Never treat the two kinds as equivalent. |
| `NetBirdPeerGroupBinding` | `GET/PUT /groups/{id}` | None | Owns one post-enrollment peer/group edge while preserving all other group members/resources. | Keep | None |
| `NetBirdPeerEnrollment` | `/setup-keys`, `/peers`, `/groups` | Partial `SetupKey` | This is not a reusable SetupKey API: enroll owns a one-off key and temporary Secret through first enrollment; adopt uses no key and resolves an existing peer by ID. | Keep | Continue using official `SetupKey` for reusable operator resources; use this kind only for the bounded lifecycle. |

The official operator also supplies `Group`, `SetupKey`, `NetworkResource`,
`NetworkEgress`, `SidecarProfile` and `ClusterProxy`. The similarly named
complementary kinds above deliberately use a `NetBird` prefix and have narrower,
account-plane semantics.
`NetBirdPeerEnrollment` is the sole setup-key exception and cannot declare a
reusable key: it always creates a one-off key with usage limit one and revokes
it when enrollment completes or its workload/Secret disappears.

## Breaking removal in 1.0.0

`NetBirdUser`, `NetBirdNetwork` and `NetBirdNetworkRouter` were removed from
version 1.0.0 with no compatibility API. Human identities continue to come only
from NetBird JIT/OIDC.

## Networks API reintroduction in 1.1.0

`NetBirdNetwork` and `NetBirdNetworkRouter` return in 1.1.0 with an audited,
account-plane contract. `NetBirdNetworkRouter` does not create workloads and
cannot replace the official `NetworkRouter`. Existing 0.x CRs require explicit
review before re-creation because the new `networkRef` is an object containing
`name`, finalization authority is status-only, and `NetBirdNetwork.enabled=false`
is rejected because the upstream Network model has no disabled state.

## Multiple role bindings

All bindings across namespaces are observed as related resources. For a user
matching more than one binding, the lexically smallest `namespace/name` is the
only binding allowed to promote and record ownership. Others report a conflict.
No binding demotes a user while another active binding still matches. If safe
ownership transfer cannot be proven during deletion, the role is retained and
the conflict is reported rather than risking an incorrect demotion.

Bindings operate on users already returned by Management, including identities
preprovisioned by `NetBirdIdentitySync`; first login is not required. The
`operator-owner` identity never participates, even when
`includeServiceUsers: true`.
