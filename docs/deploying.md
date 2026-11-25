# Deploying to a cluster

Each part of the suite (the three modules and the suite bar) ships as one container image and a
kustomize base in its own `deploy/k8s`. The base says what the part needs to run. What is yours,
which namespace, how it is reached, what it starts out knowing, goes in an overlay you keep
outside this repository.

Nothing about a real environment belongs in the repository: no addresses, host names, accounts or
secrets. The pieces below are how that is kept true.

## What lives where

| What | Where | In the repository |
|---|---|---|
| Base manifests | `<part>/deploy/k8s` | yes |
| Your overlay, one per part | a private folder of your choice | no |
| Which cluster to deploy to | `~/.config/workload-galaxy/deploy.env` on your machine | no |
| Every secret | `~/.config/workload-galaxy/secrets.env` on the cluster ([Secrets](../README.md#secrets)) | no |
| What each module reads (hosts, accounts, groups) | the suite's Settings page, saved by each module | no |

## An overlay

An overlay is a folder with a `kustomization.yaml` that refers to the part's base as `../k8s`.
This one runs the ESXi module in its own namespace and exposes it on a port of the node:

```yaml
# overlays/esxi/kustomization.yaml
apiVersion: kustomize.config.k8s.io/v1beta1
kind: Kustomization
namespace: esxi-galaxy
resources:
  - ../k8s
  - expose.yaml
images:
  - name: esxi-galaxy
    newName: esxi-galaxy
    newTag: latest                 # replaced with the build's tag on every deploy
labels:
  - pairs:
      app.kubernetes.io/version: "0"   # likewise
    includeTemplates: true
commonAnnotations:
  esxi-galaxy/build: "unbuilt"         # likewise
```

```yaml
# overlays/esxi/expose.yaml
apiVersion: v1
kind: Service
metadata:
  name: esxi-galaxy-lan
spec:
  type: LoadBalancer
  selector: {app.kubernetes.io/name: esxi-galaxy}
  ports:
    - {name: http, port: 8092, targetPort: http, protocol: TCP}
```

The namespaces and image names the deploy tool expects:

| Part | Namespace | Image |
|---|---|---|
| `kubernetes` | `workload-galaxy` | `workload-galaxy` |
| `network` | `network-galaxy` | `network-galaxy` |
| `esxi` | `esxi-galaxy` | `esxi-galaxy` |
| `shell` | `workload-galaxy-shell` | `workload-galaxy-shell` |

The suite bar's overlay also has to say where the modules are, as a browser reaches them:

```yaml
configMapGenerator:
  - name: workload-galaxy-shell-config
    literals:
      - GALAXY_MODULES=kubernetes=http://node.example:8090,network=http://node.example:8091,esxi=http://node.example:8092
```

A module needs nothing else to start. Give it what it should read from the suite's Settings page,
which keeps it in the module's own data volume. If you would sooner set something at deploy time,
any setting in a module's README can go in its ConfigMap in the overlay (`esxi-galaxy-config`,
`network-galaxy-config`, `workload-galaxy-config`).

For the Kubernetes module, add a small volume if settings saved from the page should outlive the
pod: a PersistentVolumeClaim, mounted at `/data`, with `GALAXY_DATA_DIR=/data`.

## Deploying to k3s without a registry

[`tools/deploy-k3s.sh`](../tools/deploy-k3s.sh) builds a part, ships the image to the node as a
tarball, imports it, and applies your overlay:

```bash
tools/deploy-k3s.sh shell          # or kubernetes | network | esxi
```

It reads which cluster to use from `~/.config/workload-galaxy/deploy.env`:

```bash
K3S_SSH=my-node                     # ssh name of the k3s node
KUBECONFIG=~/.kube/my-cluster       # how kubectl reaches the cluster from here
OVERLAYS=~/private/galaxy-overlays  # the folder holding overlays/<part>/
URL_SHELL=http://my-node:8089       # optional, per part: printed when the deploy is done
```

Each deploy syncs the cluster's secrets file into Kubernetes Secrets, stamps a copy of your
overlay with the version and a build time, and applies that copy. Your overlay is never changed.
If the cluster runs Rancher, a project is created for each new namespace.

## Deploying anywhere else

Build the image from the repository root and push it to your registry:

```bash
docker build -f modules/esxi/Dockerfile -t registry.example/esxi-galaxy:0.2.0 .
```

Point the overlay's `images:` entry at it and `kubectl apply -k` the overlay next to a copy of
the base. The Kubernetes module also has an OpenShift overlay in `modules/kubernetes/deploy/openshift`.
