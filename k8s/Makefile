# slice on Kubernetes (kind): see README.md for the full walkthrough.
#
# Usage order for a from-scratch local run:
#   make kind-up        # create the cluster + metrics-server
#   make kind-load      # build the gateway image and load it into the cluster
#   make secrets        # create k8s/secrets.env from the example (edit it after)
#   make deploy         # kustomize apply + wait for rollout
#   make port-forward   # gateway on http://localhost:8080
#   make kind-down      # tear the cluster down
#
# Everything runs relative to this directory (k8s/); the repo root is one up.

CLUSTER      ?= slice
NAMESPACE    ?= slice
IMAGE        ?= slice-gateway:local
REPO_ROOT    := $(abspath ..)
KIND_CONFIG  := kind/kind-config.yaml
METRICS_URL  := https://github.com/kubernetes-sigs/metrics-server/releases/latest/download/components.yaml

.PHONY: help kind-up metrics-server kind-load secrets deploy port-forward status kind-down

help:
	@echo "targets: kind-up  kind-load  secrets  deploy  port-forward  status  kind-down"

## Create the kind cluster (if absent) and install metrics-server for the HPA.
kind-up:
	@command -v kind >/dev/null 2>&1 || { echo "kind not installed: see README.md"; exit 1; }
	@if kind get clusters 2>/dev/null | grep -qx "$(CLUSTER)"; then \
		echo "cluster '$(CLUSTER)' already exists"; \
	else \
		kind create cluster --name "$(CLUSTER)" --config "$(KIND_CONFIG)"; \
	fi
	@$(MAKE) metrics-server

## Install metrics-server and patch it for kind's self-signed kubelet certs.
metrics-server:
	kubectl apply -f "$(METRICS_URL)"
	kubectl -n kube-system patch deployment metrics-server --type=json \
		-p='[{"op":"add","path":"/spec/template/spec/containers/0/args/-","value":"--kubelet-insecure-tls"}]'
	kubectl -n kube-system rollout status deployment/metrics-server --timeout=120s

## Build the gateway image at the repo root and load it into the kind cluster.
kind-load:
	docker build -t "$(IMAGE)" -f "$(REPO_ROOT)/Dockerfile" "$(REPO_ROOT)"
	kind load docker-image "$(IMAGE)" --name "$(CLUSTER)"

## Create k8s/secrets.env from the example if it does not exist yet.
secrets:
	@if [ -f secrets.env ]; then \
		echo "secrets.env already exists, leaving it alone"; \
	else \
		cp secrets.env.example secrets.env; \
		echo "created secrets.env from the example: edit it before deploying"; \
	fi

## Render + apply all manifests, then wait for the gateway rollout.
deploy:
	@if [ ! -f secrets.env ]; then \
		echo "k8s/secrets.env missing: run 'make secrets' and edit it first"; exit 1; \
	fi
	kubectl apply -k .
	kubectl -n "$(NAMESPACE)" rollout status statefulset/postgres --timeout=180s
	kubectl -n "$(NAMESPACE)" rollout status deployment/gateway --timeout=240s

## Forward the gateway Service to localhost:8080.
port-forward:
	kubectl -n "$(NAMESPACE)" port-forward svc/gateway 8080:8080

## Quick look at everything in the namespace (pods, services, HPA).
status:
	kubectl -n "$(NAMESPACE)" get pods,svc,hpa

## Delete the whole kind cluster.
kind-down:
	kind delete cluster --name "$(CLUSTER)"
