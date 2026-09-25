# External identity lifecycle

`NetBirdIdentitySync` materializes selected Keycloak realm roles and their
enabled users before those users first log in to NetBird. It is the operator
side of the authenticated Runik identity primitive implemented by
`netbird-fork`.

## Authority boundary

One resource owns one identity source. `spec.source` is the stable ownership
key inside the NetBird account and is immutable after creation. Recreating a
Kubernetes object with the same source resumes that ownership; two objects must
never use the same source.

The source owns only:

- users mapped by `source + issuer + subject`;
- groups mapped by `source + external_id`; and
- the memberships it applies between those objects.

It does not own foreign API/JWT groups, foreign memberships, peer membership,
NetBird roles, service users, or another source's objects. The sealed
`operator-owner` remains the only caller authorized to use the Management
primitive. `NetBirdRoleBinding` separately maps a provisioned group to the
internal `admin` role.

Names and email addresses are presentation data. Keycloak user IDs become OIDC
subjects and NetBird user IDs; Keycloak realm-role IDs become group external
IDs. A rename therefore updates the existing object without changing identity.

## Resource contract

```yaml
apiVersion: netbird.io/v1alpha1
kind: NetBirdIdentitySync
metadata:
  name: keycloak-example
  namespace: netbird
spec:
  source: keycloak-example
  instanceId: default
  provider:
    baseUrl: https://sso.example.invalid
    realm: example
    clientId: netbird-identity-sync
    credentialsSecretRef:
      name: netbird-keycloak-client
      key: client-secret
  realmRoles: [example-users, example-operations-staff]
  adoptExistingUsers: false
  adoptExistingJWTGroups: false
  pollIntervalSeconds: 60
  deletionPolicy: Retain
```

The credential Secret must be in the same namespace. The customize hook asks
Metacontroller to observe only that exact Secret, and the credential value is
never copied into status or logs.

The two adoption flags are explicit cutover controls. With them disabled, a
same-ID user or same-name group that is not already owned by the source is a
conflict and the whole snapshot rolls back. Group adoption accepts only one
existing JWT-issued group. User adoption never accepts a service user.

`deletionPolicy: Retain` releases the Kubernetes finalizer without changing
NetBird. `Disable` sends one complete empty snapshot: owned users are blocked
and their owned memberships removed, while owned groups remain. The finalizer
is held and retried if Management does not accept that snapshot.

## Provider snapshot

Each poll uses Keycloak service-account client credentials, then reads the role
representation and every page of
`GET /admin/realms/{realm}/roles/{role}/users` for every configured realm role.
Only enabled users are included. The issuer is
`{baseUrl}/realms/{realm}`.

The operator calls Management only after all roles and all pages have been
fetched and validated. Duplicate role IDs, duplicate names, repeated paginated
users, or inconsistent user data across roles invalidate the observation. A
provider error preserves the last-known-good status and does not make a
Management request. Conversely, a successful response containing no enabled
users is a legitimate complete snapshot and may disable previously managed
users.

## Management request

The operator sends an Owner-PAT-authenticated request to:

```text
POST /api/runik/identity/sync
```

```json
{
  "instance_id": "default",
  "source": "keycloak-example",
  "revision": "16f6...e72a",
  "complete": true,
  "users": [
    {
      "issuer": "https://sso.example.invalid/realms/example",
      "subject": "2d6557b5-6f62-4e76-a7b8-ff6d0b870642",
      "name": "Example User",
      "email": "user@example.invalid",
      "adopt_existing": false
    }
  ],
  "groups": [
    {
      "external_id": "8b9a1038-1fe7-469a-bb58-b86715dfd617",
      "name": "example-users",
      "adopt_existing": false
    }
  ],
  "memberships": [
    {
      "issuer": "https://sso.example.invalid/realms/example",
      "subject": "2d6557b5-6f62-4e76-a7b8-ff6d0b870642",
      "group_external_ids": ["8b9a1038-1fe7-469a-bb58-b86715dfd617"]
    }
  ]
}
```

Only complete authoritative snapshots are accepted. `revision` is the SHA-256
of the canonical users/groups/memberships payload, so ordering differences do
not create a new revision. Reusing a revision with a different payload is
rejected with `412`. Validation, planning, writes, mapping persistence, source
state and network serial changes occur in one database transaction.

The response echoes `source` and `revision` and returns per-object
`external_id`, `internal_id`, and `action` entries for users, groups, and
memberships. The operator validates that envelope before advancing
`lastCompleteSnapshot`.

## Lifecycle rules

| Observation | Result |
|---|---|
| New stable user/group key | Create an integration-owned object |
| Existing owned key with changed profile/name | Update or rename in place |
| Explicit compatible adoption | Record source ownership without changing stable ID |
| New owned membership | Add it while preserving foreign group memberships |
| Membership absent from a complete snapshot | Remove only the source-owned edge |
| User absent from a complete snapshot | Block user and remove owned memberships |
| Group absent from a complete snapshot | Retain group and report `retained` |
| Previously absent user reappears | Re-enable and reconcile the same NetBird ID |
| Ownership/name/service-user collision | Reject atomically; no partial writes |

Status reports observed counts, membership count, the last complete revision,
last successful reconciliation, returned user/group IDs and actions, retained
groups, disabled users, conflicts, and the `Ready`, `ProviderAvailable`,
`SnapshotValid`, and `Reconciled` conditions. It never reports provider
credentials or raw error bodies.

## JWT cutover and rollback

Cutover is an explicit maintenance operation:

1. take and verify a recoverable PostgreSQL backup;
2. enable only the adoption flags required for the first complete snapshot;
3. verify IDs, memberships, conditions and the protected bootstrap Owner;
4. disable the adoption flags;
5. disable JWT group propagation after declarative memberships have converged.

Once cut over, login authenticates a user already materialized with its OIDC
subject as NetBird ID. The fork does not let JWT propagation mutate
integration-owned identities.

Rollback stops `NetBirdIdentitySync` first. Before adoption, the official image
can use the additive database schema. After adoption or disabling users,
restore the verified pre-cutover database backup together with the official
Management image before re-enabling JWT propagation. Do not edit NetBird tables
directly.
