# metrics-server on kind

The gateway HPA scales on CPU, which means the cluster needs **metrics-server**.
kind's kubelet serves metrics over a self-signed certificate that metrics-server
won't trust by default, so it needs the `--kubelet-insecure-tls` flag or it stays
`Unavailable` and the HPA shows `<unknown>` targets forever.

`make kind-up` does this for you:

1. Applies the upstream manifest:

   ```bash
   kubectl apply -f https://github.com/kubernetes-sigs/metrics-server/releases/latest/download/components.yaml
   ```

2. Appends the insecure-TLS flag to its Deployment:

   ```bash
   kubectl -n kube-system patch deployment metrics-server --type=json \
     -p='[{"op":"add","path":"/spec/template/spec/containers/0/args/-","value":"--kubelet-insecure-tls"}]'
   ```

3. Waits for it to become available:

   ```bash
   kubectl -n kube-system rollout status deployment/metrics-server --timeout=120s
   ```

Verify it works:

```bash
kubectl top nodes
kubectl top pods -n slice
```

If `kubectl top` returns numbers, the HPA can read CPU. Give it ~30–60s after the
rollout before metrics populate.
