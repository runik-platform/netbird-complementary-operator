# Install

## Requirements

- Kubernetes >= 1.26 (CRD features: structural schemas, CEL validations).
- [Metacontroller](https://metacontroller.github.io/metacontroller/) installed
  cluster-wide. Verify with:
  ```bash
  kubectl get compositecontrollers.metacontroller.k8s.io -A >/dev/null \
    && echo "metacontroller OK"
  ```
- A NetBird management endpoint reachable from the cluster (e.g.
  `https://netbird.example.com`).
- For an already-running external Management, a Kubernetes Secret holding a
  valid NetBird PAT. The default is `netbird-mgmt-api-key` / `NB_API_KEY` in
  the chart namespace. When `NetBirdInstance` owns bootstrap, that Secret must
  initially be absent: the reconciler creates it from the one-time sealed
  bootstrap response.

## Option A — Helm (standalone)

```bash
helm install nco chart \
  --namespace netbird --create-namespace
```

The chart installs (in order):

1. CRDs from `chart/crds/`.
2. A `ServiceAccount`, `Role`, `RoleBinding` in the target namespace.
3. A `ConfigMap` with `NB_MGMT_URL`, `LOG_LEVEL`, rate-limit vars.
4. A single-replica `Deployment` running the webhook on port 8080.
5. A `Service` of type `ClusterIP` exposing port 80.
6. 16 `CompositeController` cluster-scoped resources wiring Metacontroller
   to the webhook endpoints.

### Values reference

See [`chart/values.yaml`](../chart/values.yaml) for the full list. The
important ones:

| Key | Default | Purpose |
|---|---|---|
| `image.repository` | `ghcr.io/runik-platform/netbird-complementary-operator` | Container image repository |
| `image.tag` | `""` (falls back to `.Chart.AppVersion`) | Leave empty; chart bumps drive the tag |
| `netbird.managementUrl` | `https://netbird.example.com` | NetBird API base |
| `netbird.apiKeySecret.name` | `netbird-mgmt-api-key` | Secret containing the PAT |
| `netbird.apiKeySecret.key` | `NB_API_KEY` | Key inside the secret |
| `netbird.apiKeySecret.mountAsFile` | `true` | Project the Secret and enable hot reload |
| `netbird.apiKeySecret.mountPath` | `/var/run/secrets/netbird-api-key` | Projected Secret directory |
| `logLevel` | `INFO` | `DEBUG` / `INFO` / `WARNING` / `ERROR` |
| `rateLimit.maxRetries` | `5` | Retries on 429 / 5xx before giving up |
| `rateLimit.baseDelay` | `"0.5"` | Starting backoff seconds |
| `rateLimit.maxDelay` | `"30"` | Cap on backoff seconds |
| `compositeControllers.resyncPeriodSeconds` | `300` | Full resync cadence per parent |
| `roleBinding.resyncPeriodSeconds` | `60` | JWT membership resync cadence |
| `serviceAccount.create` | `true` | Set to false if you manage the SA elsewhere |

### Credential precedence and hot reload

The client selects an explicit constructor token first (embedding/tests), then
`NB_API_KEY_FILE`, then legacy `NB_API_KEY`. The file is re-read on every HTTP
attempt. With chart defaults, kubelet's projected Secret update is therefore
consumed without a Pod restart. Set `mountAsFile: false` only for legacy env
behavior.

The official v0.8 operator injects `NB_API_KEY` as an environment variable.
If it consumes the same rotated Secret, arrange a rollout or a reloader for
that Deployment; this repository cannot hot-reload another operator.

### Other environment variables

Every `rateLimit.*` and `logLevel` value lands in the pod via the `ConfigMap`.
Override per-pod by appending to `extraEnv:` in values. The webhook also
reads these at request time so you can hot-tune by editing the ConfigMap
and restarting the pod (no full reinstall needed).

## Option B — GitOps deployment

The chart falls back to `.Chart.AppVersion` when `image.tag` is empty. To ship
a new operator version:

1. Bump `chart/Chart.yaml` — both `version` and `appVersion`.
2. Merge the version change into Forgejo `upstream`.
3. Publish the matching Forgejo Release. Public automation builds
   `ghcr.io/runik-platform/netbird-complementary-operator:<new>` and packages
   `oci://ghcr.io/runik-platform/charts/netbird-complementary-operator` with
   the same version.

## Verification

After install / rollout:

```bash
kubectl -n netbird get deploy,po \
  -l app.kubernetes.io/name=netbird-complementary-operator

# /readyz returns 200 when NB_MGMT_URL is present; the PAT may be bootstrapped later
kubectl -n netbird run nco-probe --rm -it --restart=Never --quiet \
  --image=curlimages/curl:8.10.1 \
  -- -sfS http://netbird-complementary-operator/readyz

# CompositeControllers should show all 16
kubectl get compositecontrollers.metacontroller.k8s.io | grep netbird-
```

Apply a safe smoke test:

```bash
kubectl apply -f examples/dns-zone-internal.yaml
kubectl -n netbird get netbirddnszones internal -o yaml
kubectl delete -f examples/dns-zone-internal.yaml
```

If `.status.synced: true` appears, you're done.

## Upgrading to 1.0.0

Helm does not upgrade files placed in a chart's `crds/` directory. Apply the
remaining CRD updates before upgrading so Kubernetes knows about
`NetBirdRoleBinding` and PAT renewal fields/status:

```bash
kubectl apply -f crds/
helm upgrade nco chart --namespace netbird
```

Version 1.0.0 removes `NetBirdUser`, `NetBirdNetwork` and
`NetBirdNetworkRouter` without a compatibility API. Argo CD installations with
automated pruning delete their tracked CRDs and CompositeControllers when this
chart revision is synced. Standalone Helm does not delete CRDs that disappeared
from `chart/crds/`; remove any old copies explicitly after confirming no CR
instances remain.

## Upgrading to 1.1.0

Version 1.1.0 adds five CRDs. Helm does not install or upgrade CRDs from the
chart's `crds/` directory during an upgrade, so apply the source schemas first:

```bash
kubectl apply -f crds/
helm upgrade nco chart --namespace netbird
```

The new account-plane `NetBirdNetworkRouter` only attaches existing peers to a
NetBird Network. It is not the official operator's `NetworkRouter` and never
creates routing-peer Pods. Likewise, `NetBirdNetworkResource` manages arbitrary
external addresses and does not reference a Kubernetes Service.

## Uninstall

```bash
# ArgoCD / Helm
helm uninstall nco -n netbird

# CRDs are intentionally NOT removed by Helm — they live under `crds/`.
# Delete them manually if you really want to:
kubectl delete -f crds/
```

Note: deleting CRDs deletes every existing CR, which cascades through
finalize hooks. Make sure the webhook pod is still Running at that moment or
you'll trap CRs behind stuck finalizers (see [debug.md](debug.md)).
