# slice on Kubernetes

Plain manifests + kustomize to run the whole slice stack on a local
[kind](https://kind.sigs.k8s.io/) cluster. This is a **parallel deploy target**,
not a migration — the live EC2 box keeps running docker compose. Everything here
is self-contained under `k8s/`.

## What's in the box

| Piece | Kind | Notes |
|-------|------|-------|
| `namespace.yaml` | Namespace | Everything lives in `slice`; delete it to wipe the stack. |
| `postgres.yaml` | StatefulSet + headless Service | `postgres:16`, 1 replica, 1Gi PVC from the default storageclass. Password from the Secret. The gateway self-applies migrations on startup, so there is no migration Job. |
| `redis.yaml` | Deployment + Service | `redis:7`, no volume — the cache/counters are disposable. |
| `gateway-config.yaml` | ConfigMap | Non-secret env: `REDIS_URL`, `AWS_REGION`, `ALERT_FROM`, `SLICE_BASE_URL`. |
| `gateway.yaml` | Deployment + Service | The FastAPI app, 2 replicas, ClusterIP Service on 8080. Secrets injected key-by-key; probes hit `/docs`; requests 750Mi/250m, limits 1.5Gi/1000m. An initContainer waits for Postgres. |
| `gateway-hpa.yaml` | HorizontalPodAutoscaler | 2→4 replicas at 70% CPU. **Needs metrics-server.** |
| Secret (generated) | Secret | Built by kustomize's `secretGenerator` from `secrets.env` (gitignored). Holds `POSTGRES_PASSWORD`, `DATABASE_URL`, `JWT_SECRET`, `ANTHROPIC_API_KEY`, `RESEND_API_KEY`. |

The health probes use `/docs`: the app has no dedicated `/health` route, and
FastAPI's `/docs` always returns 200 once the app is serving — the same endpoint
the production ALB health check uses.

## Prerequisites

- **Docker Desktop** (running).
- **kubectl** — `brew install kubectl`
- **kind** — `brew install kind` (or `go install sigs.k8s.io/kind@latest`)

Check:

```bash
docker info >/dev/null && kubectl version --client && kind version
```

## Secrets — nothing real in git

`k8s/secrets.env` is gitignored. Only `k8s/secrets.env.example` (placeholders) is
committed. kustomize's `secretGenerator` reads `secrets.env` at deploy time and
builds the `slice-secrets` Secret with a content-hash suffix, so changing a secret
rolls the gateway pods automatically.

```bash
cp k8s/secrets.env.example k8s/secrets.env   # or: make -C k8s secrets
# then edit k8s/secrets.env — keep POSTGRES_PASSWORD and the password inside
# DATABASE_URL identical.
```

## Command order (from the repo root)

```bash
make -C k8s kind-up        # create the kind cluster + metrics-server
make -C k8s kind-load      # build the gateway image and load it into the cluster
make -C k8s secrets        # create k8s/secrets.env (edit it before deploying)
make -C k8s deploy         # kustomize apply + wait for rollouts
make -C k8s port-forward   # gateway on http://localhost:8080  (leave running)
```

Tear down when done:

```bash
make -C k8s kind-down
```

> `make -C k8s <target>` runs the target from inside `k8s/`. You can also `cd k8s`
> and run `make <target>` directly.

## How to verify

With `make -C k8s port-forward` running in one terminal, in another:

```bash
# 1. Everything is up
kubectl -n slice get pods,svc,hpa
#    -> postgres-0, redis-*, and 2x gateway-* all Running/Ready

# 2. The app answers (200)
curl -s -o /dev/null -w '%{http_code}\n' http://localhost:8080/docs   # -> 200

# 3. metrics-server is feeding the HPA (no <unknown> in the TARGETS column)
kubectl top pods -n slice
kubectl -n slice get hpa gateway

# 4. Migrations ran (tables exist)
kubectl -n slice exec statefulset/postgres -- \
  psql -U slice -d slice -c '\dt' | head
```

To watch the HPA scale, drive CPU load through the gateway and `watch kubectl -n
slice get hpa gateway` — replicas climb toward 4 as average CPU passes 70% of the
250m request.

## Deploying the ECR image instead of the local build

The base pins `slice-gateway:local` with `imagePullPolicy: IfNotPresent`. To use
the real ECR image, override it in an overlay (or with `kustomize edit set image`):

```yaml
images:
  - name: slice-gateway
    newName: <acct>.dkr.ecr.us-east-1.amazonaws.com/slice-gateway
    newTag: latest
```

## Rendering without applying

```bash
kubectl kustomize k8s        # prints the full rendered manifest set
```

`kubectl kustomize k8s` needs `k8s/secrets.env` to exist (the secretGenerator
reads it). A client-side `kubectl apply -k k8s --dry-run=client` additionally
needs a reachable cluster context for schema resolution, so once the kind cluster
is up you can dry-run against it.
