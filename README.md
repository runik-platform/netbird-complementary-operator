# netbird-complementary-operator

A namespaced, Metacontroller-based operator for NetBird account-plane APIs
that are not covered by the official Kubernetes operator. It is intended to
run alongside [`netbirdio/kubernetes-operator` v0.8+](https://github.com/netbirdio/kubernetes-operator/releases/tag/v0.8.0),
not to replace it.

## Responsibility split

Use the official operator for `Group`, `SetupKey`, `NetworkRouter`,
`NetworkResource`, `NetworkEgress`, `SidecarProfile` and `ClusterProxy`.
This operator retains account settings, DNS configuration, nameserver groups,
legacy routes, peer settings, posture checks, DNS zones/records, PAT lifecycle,
JWT-group role bindings, full access policies, account-plane Networks, arbitrary
external Network Resources, metadata-only Network routers, and post-enrollment
peer/group bindings.

`NetBirdPeerEnrollment` is the narrow lifecycle exception: it creates a
one-off setup key only long enough to enroll a declared local workload, then
revokes it and removes its same-namespace Secret. It does not replace the
official operator's reusable `SetupKey` API.

`NetBirdIdentitySync` adds a declarative Keycloak identity path for the Runik
NetBird fork: selected realm roles become stable NetBird groups, and enabled
members are materialized before first login. It uses the fork's authenticated,
transactional complete-snapshot API and preserves the last-known-good state on
provider failures.

`NetBirdInstance` additionally reconciles the self-hosted Management,
Dashboard, Signal, Relay and STUN/TURN runtime. It does not install another
operator and remains the single root object for that installation.

The `NetBirdNetworkResource` and `NetBirdNetworkRouter` names are intentional.
They do not compete with the official `NetworkResource` and `NetworkRouter`:
the complementary kinds never derive addresses from Services or create routing
Pods. They only reconcile existing NetBird account-plane objects and peers.

The complete endpoint-by-endpoint audit and migration decisions are in
[the v0.8 migration guide](docs/migration-v0.8.md).

## Recommended human access design

The declarative path is:

```text
Covenant/Keycloak realm roles
  -> NetBirdIdentitySync complete snapshot
  -> NetBird users/groups exist before first login
  -> OIDC login authenticates the existing subject
  -> NetBirdRoleBinding optionally promotes a provisioned group to admin
```

The legacy JWT/JIT path below remains useful during migration to the fork:

```text
Covenant/Keycloak
  -> claim groups: ["vpn-admins"]
  -> NetBird JWT group sync
  -> NetBird creates the human user through JIT/OIDC
  -> NetBirdRoleBinding promotes that existing user to admin
```

```yaml
apiVersion: netbird.io/v1alpha1
kind: NetBirdRoleBinding
metadata:
  name: vpn-admins
  namespace: netbird
spec:
  groupRef:
    name: vpn-admins
  role: admin
  revokeOnExit: true
  restoreOnDelete: true
  includeServiceUsers: false
```

`NetBirdRoleBinding` never creates a group or user and never calls
`POST /users`. Membership is read from the `auto_groups` IDs returned by the
NetBird users API, as defined by the supported OpenAPI. Users that have not
logged in yet are simply picked up by a later poll.

Important identity rules:

- A JWT group named `admin` does not grant the internal NetBird `admin` role.
- Group names are exact and case-sensitive.
- A Keycloak groups mapper should emit flat paths (`full.path=false`) when the
  expected claim value is `vpn-admins`.
- `owner` is never modified. The current deployment retains its human
  break-glass Owner until the sealed bootstrap migration; the target contract
  uses `operator-owner` as the only Owner.
- Service users are excluded by default.

## PAT lifecycle and hot reload

`NetBirdToken` defaults to a 90-day lifetime and rotates seven days before
expiry. Rotation is two phase: it creates the new PAT, publishes its plaintext
to the child Secret, then lets the previous PAT expire naturally after that
Secret value is observed. An already expired PAT is not repaired through the database; the
CR reports `BootstrapRequired`.

The API client credential precedence is:

1. explicit constructor argument (tests/embedding);
2. `NB_API_KEY_FILE`, re-read on every HTTP attempt;
3. legacy `NB_API_KEY`.

The chart mounts `netbird.apiKeySecret` as a projected file by default, so a
rotated Secret is consumed without restarting this operator. The official
operator v0.8 reads the same Secret into an environment variable and may still
need a rollout or reloader; that process is outside this repository.

## Peer enrollment and adoption

`NetBirdPeerEnrollment` has two explicit modes:

- `enroll` observes one same-namespace Deployment, StatefulSet, or DaemonSet,
  issues a one-off setup key, and publishes it only to the declared temporary
  Secret. The workload must use an optional Secret reference and persist its
  `/var/lib/netbird` profile. Once a unique matching peer has an ID, overlay IP,
  and DNS label, the key is revoked and the Secret is garbage-collected.
- `adopt` resolves an existing peer exclusively by immutable NetBird peer ID.
  Hostname, DNS label, and serial number are assertions, never selectors. It
  emits no Secret and does not delete the peer unless both takeover and
  `deletionPolicy: Delete` are explicit.

See `examples/peer-enrollment-local.yaml` and
`examples/peer-enrollment-adopt.yaml`.

## Development and installation

- [Installation guide](docs/install.md)
- [API reference](docs/api-reference.md)
- [External identity lifecycle contract](docs/identity-lifecycle.md)
- [NetBirdInstance contract](docs/netbird-instance.md)
- [Migration guide](docs/migration-v0.8.md)
- [Architecture](docs/architecture.md)
- [Troubleshooting](docs/debug.md)
- [Examples](examples/)

Local verification does not require or contact a Kubernetes cluster:

```bash
python3 -m venv .venv
.venv/bin/pip install -r tests/requirements.txt
.venv/bin/python -m pytest -q
helm template t chart --namespace netbird
helm lint chart
```

## License

This project is licensed under the GNU Affero General Public License v3.0 only
(`AGPL-3.0-only`). See [LICENSE](LICENSE).
