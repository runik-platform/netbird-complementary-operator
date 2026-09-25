# API reference

All complementary resources are namespaced and use `netbird.io/v1alpha1`.
Every status contains `synced`, `observedGeneration`, `lastSyncTime` and
conditions unless noted. The CRD YAML under `crds/` is the schema source of
truth.

## NetBirdIdentitySync

Builds complete identity snapshots from selected Keycloak realm roles and
submits them to the Runik endpoint in `netbird-fork`. Stable `source` ownership,
provider configuration, a same-namespace credential Secret reference, and at
least one `realmRoles` entry are required. `pollIntervalSeconds` defaults to 60
and ranges from 15 to 3600 seconds.

`adoptExistingUsers` and `adoptExistingJWTGroups` default to `false` and should
only be enabled during an explicit cutover. `deletionPolicy` defaults to
`Retain`; `Disable` blocks absent owned users with a final complete empty
snapshot. Provider failures preserve the last-known-good snapshot and never
submit an inferred empty result. See [identity-lifecycle.md](identity-lifecycle.md)
for the request, ownership, status, and rollback contracts.

## NetBirdRoleBinding

Maps exact membership of an existing NetBird group, including one populated by
`NetBirdIdentitySync`, to the internal `admin` role. It never creates groups or
users. The sealed `operator-owner` identity and every `owner` are always
protected; other service users are excluded by default.

| Spec field | Type/default | Meaning |
|---|---|---|
| `groupRef.name` | string, required | Exact existing NetBird group name |
| `role` | enum `admin`, required | Only supported target role |
| `revokeOnExit` | boolean, `true` | Restore owned assignments after group exit |
| `restoreOnDelete` | boolean, `true` | Best-effort restore on CR deletion |
| `includeServiceUsers` | boolean, `false` | Include service users explicitly |
| `pollIntervalSeconds` | integer, `60` | Per-object poll, range 15–3600 seconds |

Status adds `matchedUsers`, per-cycle `promotedUsers`, `restoredUsers` and
`skippedUsers`, a `conflicts` list, and `managedUsers`. Each managed entry
contains `userId`, `previousRole` and `appliedRole`. An already-admin user is
matched but never added to `managedUsers` unless ownership is being
deterministically transferred from another binding. Drift on an owned member
is repaired on the next cycle. Conditions include `Ready`, `Synced` and
`Degraded`, with reasons such as `GroupNotFound`, `InvalidPAT`, `Conflict`,
`IncompleteResponse` and `RestoreSkipped`.

The role binding changes account administration only. It does not add the user
or any peer to a NetBird access group and therefore grants no VPN access by
itself.

## NetBirdToken

Manages a PAT and a child Secret. `expiresInDays` defaults to 90;
`renewBeforeDays` normally defaults in the controller to 7 (or
`expiresInDays - 1` for a legacy shorter lifetime) and, when specified, must
be smaller than `expiresInDays`.
`secretRef` selects the child Secret name/key. Status exposes compatibility
`id`, `activeTokenId`, `previousTokenId` while publication is pending,
`expiresAt`, `lastRotationTime`, and expiry/degraded conditions.

## NetBirdPolicy

Manages full access policies through `/policies`. `sourceGroupRefs` and
`destinationGroupRefs` are exact NetBird group names and are copied into every
rule after ID resolution. Groups may be API-, integration- or JWT-issued; a
destination group may contain ordinary peers, Network Resources, or both.

Rules support `all`, `tcp`, `udp` and `icmp`, individual integer `ports`, and
inclusive `portRanges`. Ports and ranges are mutually exclusive within a rule,
and `all`/`icmp` do not accept either. `sourcePostureCheckRefs` resolves exact
posture-check names. Existing policies are adopted by exact name, including
`Default`.

`enabled: false` disables a present policy. `state: absent` removes it. Removal
is deliberately two-phase when adopting: the first sync persists the adopted
ID in `status.id`; a later sync deletes exactly that ID. Finalization never
rediscovers a policy by name.

## Networks API resources

These kinds manage NetBird's account-plane Networks API. They do not create
Pods, Services, Setup Keys, or any other Kubernetes workload.

| Kind | Principal spec fields | Endpoint |
|---|---|---|
| `NetBirdNetwork` | `name`, `description`, `enabled` | `/networks` |
| `NetBirdNetworkResource` | `networkRef.name`, arbitrary `address`, resource groups | `/networks/{id}/resources` |
| `NetBirdNetworkRouter` | `networkRef.name`, peer XOR peer groups, metric and masquerade | `/networks/{id}/routers` |
| `NetBirdPeerEnrollment` | `mode: enroll\|adopt`, stable peer identity, optional settings and groups | `/setup-keys`, `/peers`, `/groups` |

The upstream Network object currently has no enabled/disabled property.
Therefore `NetBirdNetwork.enabled` must be `true`; `false` produces an
`InvalidSpec` condition and never falls back to deleting the Network. Resource
and router `enabled` fields are supported by their upstream endpoints.

`NetBirdNetworkResource.address` accepts an IP, `/32`, CIDR, domain, or wildcard
domain. `NetBirdNetworkRouter` refers only to already-enrolled peers or existing
groups and is intentionally distinct from the official operator's
`NetworkRouter`, which creates routing-peer workloads in Kubernetes.

### NetBirdPeerEnrollment

`enroll` is restricted to a workload in the CR namespace. The workload must
reference `enrollment.secretRef`, and that Secret reference must be optional so
the workload can restart after the one-off key is removed. The reconciler
captures the pre-enrollment peer IDs, accepts exactly one new peer matching the
declared hostname/assertions, verifies its returned ID/IP/DNS profile, persists
the peer ID in status, revokes the setup key, and stops returning the Secret.

`adopt` requires `peerIdentity.id`; no hostname-only lookup and no Secret are
allowed. `groupRefs` owns only memberships that the resource actually added.
The default `deletionPolicy: Retain` restores adopted settings and removes
owned membership edges. Deleting an adopted peer requires both
`ownership.takeOwnership: true` and `deletionPolicy: Delete`.

## NetBirdPeerGroupBinding

Owns one peer-to-group membership. Both refs are exact existing names. If the
peer has not enrolled yet, status reports `RefNotFound` and periodic
reconciliation waits without changing the group. Adding or removing the edge
uses a read-modify-write request that preserves every other peer and Network
Resource currently in the group. Status records `peerId`, `groupId` and
`managed`; finalization acts only on those recorded IDs.

## Account-plane resources

| Kind | Principal spec fields | Endpoint |
|---|---|---|
| `NetBirdAccountSettings` | account expiration, JWT groups, propagation, network and extra settings | `/accounts` |
| `NetBirdDNSSettings` | `disabledManagementGroupRefs` | `/dns/settings` |
| `NetBirdNameserverGroup` | nameservers, distribution groups, domains, primary/search flags | `/dns/nameservers` |
| `NetBirdRoute` | description/network ID, CIDR or domains, peer or peer groups, distribution/access groups | `/routes` |
| `NetBirdPostureCheck` | client/OS versions, geo, network-range and process checks | `/posture-checks` |
| `NetBirdPeer` | peer ref and mutable SSH/expiration/approval settings; delete unregisters it | `/peers` |
| `NetBirdDNSZone` | name, domain, enabled/search flags, distribution groups | `/dns/zones` |
| `NetBirdDNSRecord` | zone ref, name, A/AAAA/CNAME content, TTL | `/dns/zones/{id}/records` |

See [migration-v0.8.md](migration-v0.8.md) for semantic boundaries with the
official operator and [examples](../examples/) for manifests.
