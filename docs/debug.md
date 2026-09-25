# Debug

Failure modes, the symptom each produces, and the fix.

## First three commands, every time

```bash
# 1. Is the pod Running and healthy?
kubectl -n netbird get deploy,po \
  -l app.kubernetes.io/name=netbird-complementary-operator

# 2. What does the last sync report on the CR?
kubectl -n netbird get netbirdroute cop-smoke -o jsonpath='{.status}' | python3 -m json.tool

# 3. Pod logs, filtered to the kind you care about
kubectl -n netbird logs deploy/netbird-complementary-operator \
  | grep -v "healthz\|readyz" | tail -50
```

The `status.conditions` on every CR tells you what happened on the last
reconcile. Read `Synced`, `Ready`, `Degraded`, `Deprecated` and `Expiring` as
applicable before anything else.

## Reading `status.conditions`

Simple resources expose `Synced`; role bindings and tokens add readiness,
degradation, deprecation or expiry signals where applicable.

| `reason` | Meaning | Where to look |
|---|---|---|
| `Reconciled` | Last sync succeeded | Nothing to do. `synced: true`, `id:` populated |
| `NetBirdAPIError` | The NetBird API returned a non-OK status | `message` contains method + path + first 500 bytes of the body |
| `RefNotFound` | A `*Ref` field points at a NetBird name that doesn't exist | Check spelling of `groupRefs`, `peerRef`, `networkRef`, `zoneRef`, etc. |
| `InternalError` | Unhandled exception in the handler | Traceback is in pod logs |
| `InvalidPAT` | NetBird returned 401 | Check expiry and management backend auth |
| `RateLimited` | Retries exhausted after 429 | Reduce churn or tune retry/poll settings |
| `NetBirdUnavailable` | Retries exhausted after transport/5xx | Restore management availability; no role demotion occurs |
| `GroupNotFound` | RoleBinding group is absent | Verify the exact group name and the last complete identity snapshot |
| `IncompleteResponse` | Users response lacked required OpenAPI fields | No promotion/demotion is attempted |
| `BootstrapRequired` | Managed PAT is already expired | Perform break-glass bootstrap below |
| `WorkloadNotFound` | An enroll-mode workload is not observable in the CR namespace | Check `workloadRef` and create the workload before enrollment |
| `SecretNotConsumed` | The workload does not reference the declared temporary Secret | Add an optional Secret reference and persistent NetBird state |
| `DuplicateHostname` | More than one peer matches the enrollment identity assertion | Stop enrollment and choose a unique hostname/serial assertion; use stable ID for adoption |
| `PeerMissing` | A previously enrolled peer ID no longer exists | Diagnose the workload/profile first; increment `replacementRevision` only for an intentional replacement |

## Common failure modes

### `NetBirdAPIError` 401 "token invalid"

Does NOT necessarily mean the PAT is wrong. NetBird management returns a
generic 401 also when its own Postgres backend has DB auth problems (the
auth middleware tries to look up the token and any DB failure bubbles up
as "token invalid").

Before rotating the PAT:

```bash
# Direct curl — if THIS works but the operator doesn't, the issue is ours
PAT=$(kubectl -n netbird get secret netbird-mgmt-api-key \
  -o jsonpath='{.data.NB_API_KEY}' | base64 -d)
curl -si -H "Authorization: Token $PAT" https://netbird.example.com/api/peers | head -5

# Management Pod logs often reveal the real cause (DB auth, etc.)
kubectl -n netbird logs -l app.kubernetes.io/name=netbird --tail=50 \
  | grep -iE "token|auth|sql"
```

If the direct curl also returns 401, investigate NetBird (PAT expiry, DB
credentials, or rotate the PAT via the UI).

### PAT expired: bootstrap / break-glass

`NetBirdToken` rotates only while its active PAT is still valid. If status
shows `TokenExpired`/`BootstrapRequired`, do not edit the NetBird database.

1. Sign in as the break-glass human owner.
2. Create or recover an administrative service user explicitly.
3. Create a new PAT for that service user in NetBird.
4. Replace the bootstrap Secret key (`netbird-mgmt-api-key/NB_API_KEY`).
5. Wait for the projected file to update and verify `/readyz` plus CR status.
6. Reconcile/roll out other consumers such as the official operator if they
   read the Secret through environment variables.

Keep owner credentials outside automation and test this procedure before an
incident.

### `RefNotFound` on a group / peer / network / zone

Case-sensitive. The Resolver uses `?name=X` on the groups endpoint (exact
match) and indexes the list responses for the others. Verify upstream:

```bash
PAT=$(kubectl -n netbird get secret netbird-mgmt-api-key \
  -o jsonpath='{.data.NB_API_KEY}' | base64 -d)
curl -s -H "Authorization: Token $PAT" https://netbird.example.com/api/groups | jq '.[].name'
curl -s -H "Authorization: Token $PAT" https://netbird.example.com/api/peers | jq '.[].name'
```

### CR stuck in `Terminating`

When a CR is deleting, Metacontroller runs our `finalize` hook and waits for
`finalized: true`. The finalize handlers are designed to tolerate API errors
when `.status.id` is empty — but they DO fail-loud if the ID is set and the
DELETE itself times out or hits something other than 404.

Inspect:

```bash
kubectl -n netbird get netbirdroute cop-smoke -o yaml | yq '.metadata.finalizers, .status'
kubectl -n netbird logs deploy/netbird-complementary-operator \
  | grep finalize | tail -20
```

Options to unstick:

1. **Preferred**: fix the underlying issue (NetBird reachable? credentials
   OK?) and wait for the next finalize retry.
2. **Last resort** (only when the upstream resource is confirmed gone and
   you accept orphaning the finalizer): remove the finalizer manually.
   ```bash
   kubectl -n netbird patch netbirdroute cop-smoke \
     --type=json -p='[{"op":"remove","path":"/metadata/finalizers"}]'
   ```

### CEL rejection at apply time

If `kubectl apply` says:

```
spec: Invalid value: "object": peerRef and peerGroupRefs are mutually exclusive
```

this is good — the admission plugin caught a bad spec before the operator
even saw it. Fix the spec. Do not work around CEL rules by editing the CRD.

The current CEL-enforced mutexes:

- `NetBirdRoute`: `peerRef` XOR `peerGroupRefs`; `network` XOR `domains`.

### "unknown kind" from the webhook

```
HTTP 404: unknown kind 'dns-zone'
```

Symptom of a version mismatch: the chart shipped new `CompositeController`
resources that reference `/sync/<newkind>` but the Deployment is still
running an older image that has no handler registered for that kind.

Diagnose:

```bash
kubectl -n netbird get deploy netbird-complementary-operator \
  -o jsonpath='{.spec.template.spec.containers[0].image}'
kubectl get compositecontrollers.metacontroller.k8s.io -l \
  app.kubernetes.io/instance=<release> -o name
```

Fix: bump the chart `appVersion` / `version` together (the spell does not
override `image.tag`, so the chart version drives the image tag). If they
already match, re-sync the Argo Application or `helm upgrade`.

### Silence: hook never fires

The CR is created but no `.status` ever appears. Usually Metacontroller is
unhappy, not us.

```bash
# Metacontroller itself
kubectl -n metacontroller logs statefulset/metacontroller --tail=50

# Does the CompositeController exist and is it healthy?
kubectl get compositecontroller netbird-dns-zone -o yaml

# Is the webhook Service reachable from the metacontroller namespace?
kubectl -n metacontroller run probe --rm -it --restart=Never --quiet \
  --image=curlimages/curl:8.10.1 \
  -- -sfS \
  http://netbird-complementary-operator.netbird.svc.cluster.local/readyz
```

## Turning on DEBUG logging live

```bash
kubectl -n netbird set env deploy/netbird-complementary-operator LOG_LEVEL=DEBUG
kubectl -n netbird rollout status deploy/netbird-complementary-operator
kubectl -n netbird logs deploy/netbird-complementary-operator -f
```

Revert:

```bash
kubectl -n netbird set env deploy/netbird-complementary-operator LOG_LEVEL=INFO
```

Note that this overwrites the value from the ConfigMap for this pod's
lifetime — the next Helm / Argo sync will reset it.

## Tuning rate-limit retries

Under heavy churn or a flaky network, the defaults (5 retries, 0.5 → 30s
full-jitter backoff) may be too aggressive or too timid. Tune via env:

```bash
kubectl -n netbird set env deploy/netbird-complementary-operator \
  NB_RATE_LIMIT_MAX_RETRIES=10 \
  NB_RATE_LIMIT_BASE_DELAY=1.0 \
  NB_RATE_LIMIT_MAX_DELAY=60
```

For a permanent change, set `rateLimit.*` in Helm values.

## Cross-checking what the operator owns upstream

```bash
PAT=$(kubectl -n netbird get secret netbird-mgmt-api-key \
  -o jsonpath='{.data.NB_API_KEY}' | base64 -d)

# Every NetBird resource with its id and name
for path in dns/nameservers routes networks dns/zones users \
            posture-checks peers; do
  echo "=== $path ==="
  curl -s -H "Authorization: Token $PAT" \
    "https://netbird.example.com/api/$path" \
    | jq -r '.[] | "\(.id) \(.name // .description)"'
done
```

The CR `.status.id` for each managed object should match one line here. A
row with no matching CR is an orphan (user-created in UI, or a botched
delete).

## When all else fails

1. Read [architecture.md](architecture.md) to confirm the mental model.
2. Run the unit tests locally — if those break, the bug is in our code:
   `.venv/bin/python -m pytest tests/unit -v`.
3. Apply the corresponding file in `examples/` on a clean CR name and watch
   the webhook logs. If the example doesn't work against your NetBird, the
   issue is almost certainly configuration (PAT scope, missing group
   "All", etc.), not the operator.
