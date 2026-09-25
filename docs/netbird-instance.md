# NetBirdInstance contract

`NetBirdInstance` is the root resource for one self-hosted NetBird installation.
It owns the Management and Dashboard singletons, collections of Signal, Relay
and STUN/TURN components, generated Management configuration, migrations,
health and sealed account bootstrap.

PostgreSQL, OIDC, certificates, public DNS and ingress/gateway remain owned by
their existing operators. The instance references them and never copies their
secret values into its spec or status.

The CRD and reconciler implement this contract. Installing the chart only adds
the API and controller; no runtime is created until a `NetBirdInstance` exists.

## Cardinality and naming

One complementary-operator installation accepts exactly one instance in its
own namespace. Its required identity is:

```yaml
apiVersion: netbird.io/v1alpha1
kind: NetBirdInstance
metadata:
  name: default
  namespace: netbird
```

Any other name is rejected, and a second instance is never selected as a
winner. The stable component identities are:

- singleton names `management` and `dashboard`;
- collection key `(kind, name, location)` for Signal, Relay and STUN/TURN;
- generated Kubernetes names `netbird-<kind>` for singletons and
  `netbird-<kind>-<name>` for collection members.

Collection names and locations are immutable DNS labels and must be unique
within their kind. Display labels are not identity.

## Spec

The initial API intentionally exposes only fields required to produce a
working instance. Workload tuning that is not needed by the milestone stays in
the Helm/operator defaults.

```yaml
apiVersion: netbird.io/v1alpha1
kind: NetBirdInstance
metadata:
  name: default
  namespace: netbird
spec:
  management:
    mode: managed
    image:
      repository: ghcr.io/runik-platform/netbird-management
      digest: sha256:0123456789abcdef...
    publicEndpoint: https://vpn.example.com
    service:
      type: ClusterIP

  dashboard:
    mode: managed
    image:
      repository: netbirdio/dashboard
      tag: v2.92.0

  signals:
    - name: primary
      location: example-site
      mode: managed
      image:
        repository: netbirdio/signal
        tag: 0.78.2
      publicEndpoint: grpc://vpn.example.com:33000
    - name: secondary-site
      location: secondary-site
      mode: external
      endpoint: grpcs://signal.secondary-site.example:443

  relays:
    - name: primary
      location: example-site
      mode: managed
      image:
        repository: netbirdio/relay
        tag: 0.78.2
      publicEndpoint: rel://vpn.example.com:33080

  stunTurns:
    - name: coturn
      location: example-site
      mode: external
      endpoints:
        - turns:turn.example:5349
      credentialSecretRef:
        name: netbird-turn
        key: shared-secret

  database:
    secretRef:
      name: netbird-pg-app
      key: uri

  encryptionKeySecretRef:
    name: netbird-management-encryption
    key: key

  oidc:
    issuer: https://sso.example.com/realms/example
    clientID: netbird

  bootstrap:
    accountName: example-organization
    ownerName: operator-owner
    credentialSecretRef:
      name: netbird-owner-api-key
      key: NB_API_KEY
    rotationRevision: "2026-09-17"
    recoveryRevision: "1"

  lifecycle:
    deletionPolicy: Retain
```

Core validation is closed rather than inferred by the reconciler:

| Field | Contract |
|---|---|
| `metadata.name` | Constant `default` |
| `management` | Exactly one, required |
| `bootstrap.accountName` | Non-empty and immutable after bootstrap starts |
| `bootstrap.ownerName` | Defaults to and only accepts `operator-owner` |
| `bootstrap.credentialSecretRef` | Namespaced; immutable after bootstrap starts |
| `bootstrap.adoptionCredentialSecretRef` | Optional current Owner PAT used only to adopt an existing account |
| `bootstrap.rotationRevision` | Opaque trigger; changing it rotates the sealed Owner PAT once |
| `bootstrap.recoveryRevision` | Explicit acknowledgement after restoring a valid Secret from backup |
| `lifecycle.deletionPolicy` | `Retain|DeleteRuntime`, default `Retain` |

Every unknown mode or unsupported field is rejected by the structural CRD
schema. A started bootstrap makes account identity and credential destination
immutable; changing them requires restoring the pre-bootstrap instance rather
than silently creating a second account or root Secret.

The credential reference must match the chart's `netbird.apiKeySecret`
name/key. This makes the sealed Owner PAT immediately become the credential
used by every account-plane reconciler in the same operator process.

### Common component contract

Every component declares `mode: managed|external`.

| Mode | Required | Forbidden | Ownership |
|---|---|---|---|
| `managed` | image | external-mode `endpoint` | Operator owns workload, config and Service |
| `external` | endpoint(s) | image and workload settings | Operator observes/references; never mutates runtime |

Management and Dashboard are single objects rather than lists. Signals, relays
and STUN/TURN are lists because one Management may advertise several
locations. Dashboard may be omitted or external; Management is required and
may be managed or external. An external Management that is not already sealed
must reference a bootstrap Secret that was configured independently on that
runtime; the operator never transports it to another cluster.

Image `repository` is required in managed mode. The Runik Management image is
pinned by digest. Other images may initially use a version tag, while status
always records the resolved image ID. An image value, mode, component name or
location never contains credentials.

Managed Signal and Relay components may set `publicEndpoint`. Management
advertises that endpoint to clients while the generated Service remains the
runtime target inside Kubernetes. Managed STUN/TURN may likewise declare its
public `endpoints`; if omitted, only its cluster-local Service endpoint is
advertised. `oidc.clientSecretRef` is optional for public PKCE clients and is
required only when the configured provider client is confidential.

### External references

An external component exposes only endpoints and optional Secret references.
The reconciler validates reachability and renders the endpoint into Management
configuration, but it creates no Deployment, Service or configuration for that
runtime. Removing an external entry only removes the reference.

Database, encryption and OIDC fields contain Kubernetes Secret names and keys,
not values. References are restricted to the instance namespace. The operator
reads values only when rendering managed configuration and never copies them
to status, annotations, events or logs.

For managed Management, the operator creates the temporary bootstrap Secret
described below. For external Management, `management.bootstrapSecretRef` is a
reference to a pre-existing local Secret whose value is already configured on
the external server. The operator neither creates that Secret nor modifies the
remote runtime.

### Derived Management configuration

The reconciler is the sole writer of the managed Management configuration. It
derives advertised Signal, Relay and STUN/TURN endpoints from the component
lists after validation. There is no second free-form endpoint list in spec.
Duplicate effective endpoints, duplicate stable component keys or an endpoint
that disagrees with a managed Service produce `InvalidSpec` and no rollout.

## Status

Status is observational and contains no credentials:

```yaml
status:
  observedGeneration: 7
  phase: Ready
  accountID: 8e4f...
  ownerUserID: 11d2...
  credentialSecretRef:
    name: netbird-owner-api-key
    key: NB_API_KEY
  bootstrap:
    phase: BootstrapSealed
    sealedAt: "2026-09-15T14:00:00Z"
  migrations:
    phase: Succeeded
    observedVersion: 0.78.2-runik.0
  components:
    - kind: Management
      name: management
      location: example-site
      mode: managed
      ready: true
      version: 0.78.2-runik.0
      imageID: ghcr.io/runik-platform/netbird-management@sha256:...
      endpoint: https://vpn.example
  effectiveEndpoints:
    signals: [grpcs://signal.example:443]
    relays: [rels://relay.example:443]
    stunTurns: [turns:turn.example:5349]
  conditions:
    - type: Ready
      status: "True"
      reason: Reconciled
      lastTransitionTime: "2026-09-15T14:01:00Z"
```

Top-level phases are `Pending`, `Progressing`, `Ready`, `Degraded` and
`Deleting`. Conditions include `SpecValid`, `DependenciesReady`,
`RuntimeReady`, `MigrationsReady`, `BootstrapReady` and `Ready`. Every
condition records `observedGeneration`; secrets and provider response bodies
are redacted before errors reach status or logs.

## Reconciliation order

The instance reconciler runs before account-plane reconcilers and follows one
ordered state machine:

1. validate singleton cardinality, immutable identities and mode fields;
2. resolve referenced Secrets and external endpoints;
3. render configuration from declared components;
4. reconcile managed Services and workloads;
5. wait for Management health and migrations;
6. complete or verify sealed bootstrap;
7. publish `Ready=True` so PAT-dependent controllers may reconcile.

Account-plane controllers treat a missing credential or non-sealed bootstrap
as `DependencyNotReady`. They do not retry destructive API actions with empty
credentials.

## Sealed bootstrap

The bootstrap service identity has the fixed name `operator-owner`, is marked
non-deletable, is not an OIDC identity and is the only Owner. No human user is
promoted during bootstrap.

The state is persisted by Management, not inferred only from Kubernetes:

| Phase | Meaning | Allowed transition |
|---|---|---|
| `BootstrapPending` | No account bootstrap has started | `Bootstrapping` |
| `Bootstrapping` | Account/Owner exist and one unacknowledged PAT may exist | retry or `BootstrapSealed` |
| `BootstrapSealed` | Published PAT was verified; bootstrap auth is disabled | remain sealed |
| `RecoveryRequired` | Secret/PAT/database state disagree | explicit restore procedure only |

The operator creates a random, short-lived bootstrap Secret as a managed child
and mounts it into Management. It never places that value in the CR. The
one-shot fork endpoint uses it to atomically:

1. create or adopt exactly one account;
2. create or adopt `operator-owner` by its persisted bootstrap identity;
3. ensure every other user is non-Owner;
4. create exactly one Owner PAT and return its plaintext once.

For a database that already contains an account,
`bootstrap.adoptionCredentialSecretRef` names the current Owner PAT. Adoption
requires that PAT and the temporary bootstrap secret together, preserves the
account and its data, demotes every previous Owner to Admin, and installs the
deterministic service user as the sole Owner. Fresh databases omit the field
and use the empty-database bootstrap path. The adoption and output references
must name different Secrets so the old human PAT cannot be mistaken for the
new service PAT during an interrupted reconciliation.

The plaintext is transient in the authenticated response but is persisted only
in the declared Kubernetes Secret. The operator writes that response directly,
reads the Secret back, and authenticates an acknowledgement with the new PAT.
Management then records the sealed state and disables bootstrap authentication.
The operator removes the temporary bootstrap Secret only after observing the
seal.

The Owner credential Secret is intentionally not a Metacontroller child and
has no owner reference to `NetBirdInstance`; the reconciler creates or adopts
only the exact `credentialSecretRef`. This is required for `Retain` deletion
semantics. The temporary bootstrap Secret remains an ordinary managed child.

Retries are safe. Before the credential Secret exists, retrying an
unacknowledged bootstrap revokes the former unclaimed PAT before returning one
replacement; there is never more than one active Owner PAT. If the Secret
exists but acknowledgement was interrupted, the operator retries the
acknowledgement with that Secret instead of requesting another PAT. Restarting
either process resumes from persisted Management state and the observed child
Secret.

Deleting or corrupting the Owner Secret never reopens bootstrap. The instance
reports `RecoveryRequired` and stops account-plane reconciliation.

## Rotation, backup and recovery

Changing `bootstrap.rotationRevision` starts a two-phase rotation. Management
creates a non-expiring replacement while the current PAT remains valid. The
operator publishes and reads back the Secret, confirms with the replacement,
and only then revokes the former token. A lost prepare response replaces only
the unacknowledged PAT for that revision; a lost acknowledgement is retried
with the projected replacement.

Normal PAT rotation authenticates with the current Owner PAT. It creates one
replacement, publishes and reads back the Secret, verifies the replacement,
then revokes the former token. Exactly one PAT exists at steady state; the
bounded two-phase handoff may temporarily contain the old and new PAT so a
failed Secret write cannot destroy access. Rotation never uses bootstrap
credentials.

The credential Secret and PostgreSQL backup form one recovery set and must be
backed up out of band. Recovery is one of:

- restore the missing Secret while its PAT remains valid;
- restore a matching database-and-Secret recovery set; or
- rotate normally while the current Secret still authenticates.

There is no automatically generated break-glass credential and no spec field
that reopens the initial channel. A future destructive recovery mechanism, if
needed, requires a separate reviewed API version rather than weakening
`v1alpha1`.

If the retained Secret is lost or restored with an invalid PAT, the instance
reports `RecoveryRequired`. Restore the matching Secret or database-and-Secret
backup, then change `bootstrap.recoveryRevision`. The reconciler verifies the
active PAT before recording that revision and returning to Ready, leaving the
declarative change as the recovery audit trail.

## Upgrade and rollback

An image change creates a new rendered revision. The operator keeps the old
workload serving until dependencies resolve and the candidate starts. It waits
for migrations and Management health before advancing observed generation.
Signal, Relay and Dashboard roll only after Management is ready when their
configuration changes.

Before a Management upgrade, take a recoverable PostgreSQL backup and record
the current credential Secret version and image digest. Image-only rollback is
allowed only when migrations are backward compatible. Otherwise rollback
restores the database, Owner Secret and prior image as one set. Encryption keys
are never regenerated during upgrade or rollback.

Changing a component between managed and external mode is an explicit
handoff: the new endpoint must be healthy before the old managed runtime is
removed. Stable names and locations cannot change in place; replacement uses a
new list entry and a later removal.

## Deletion and data protection

`spec.lifecycle.deletionPolicy` is `Retain|DeleteRuntime` and defaults to
`Retain`:

- Account, PostgreSQL, encryption Secret and Owner credential Secret are
  always retained.
- External runtimes are never changed or deleted.
- With `Retain`, managed workloads, Services and generated configuration are
  orphaned deliberately after removing controller ownership. If deletion
  starts before bootstrap is sealed, the reconciler first removes the temporary
  Secret and rolls Management without its bootstrap mount.
- Explicit `DeleteRuntime` may delete only managed runtime
  objects and waits until Metacontroller observes no children before releasing
  the parent finalizer; it still cannot delete account data or credentials.

Finalization tolerates an unavailable Management API because deleting a
Kubernetes object must not require deleting NetBird data. Any retained runtime
is reported in the final event without secret values.
