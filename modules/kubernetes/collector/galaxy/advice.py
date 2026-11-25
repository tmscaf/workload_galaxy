"""What a signal means and what to do about it.

Pure text tables and message classifiers used by diagnose.py. Kept apart so the
wording can be reviewed (and extended) without touching detection logic.
Commands use {cli} (oc / kubectl), {ns}, {name}, {ref} (deploy/x, statefulset/x ...), {pod}.
"""
from __future__ import annotations

import re

# Triage categories, in the order a troubleshooter usually wants them.
CATEGORIES = [
    ("rollout", "Rollout"), ("crash", "Crashing"), ("image", "Image"), ("config", "Config"),
    ("admission", "Admission"), ("quota", "Quota"), ("scheduling", "Scheduling"), ("storage", "Storage"),
    ("probes", "Probes"), ("availability", "Availability"), ("network", "Network"), ("latency", "Latency & errors"),
    ("saturation", "Saturation"), ("node", "Nodes"), ("platform", "Platform"), ("batch", "Jobs"),
    ("sizing", "Right-sizing"), ("hygiene", "Hygiene"),
]

REF = {"Deployment": "deploy", "StatefulSet": "statefulset", "DaemonSet": "daemonset", "DeploymentConfig": "dc",
       "CronJob": "cronjob", "Job": "job", "Pod": "pod", "ReplicaSet": "rs", "ReplicationController": "rc"}

EXIT_CODES = {
    0: ("exited normally (code 0)",
        "The main process finished without an error, so Kubernetes restarts it. The container has nothing "
        "long-running to do: wrong command/args, or a one-shot task deployed as a Deployment.",
        "Check the container's command and args. If it is meant to run once, make it a Job."),
    1: ("application error (exit 1)", "The application started and then exited with an error.",
        "Read the last log lines of the previous container: the reason is almost always printed there."),
    2: ("application error (exit 2)", "The application exited with a usage/config error (often a bad flag or "
        "missing argument).", "Read the previous container's log and check the command, args and env."),
    126: ("command not executable (exit 126)",
          "The entrypoint exists but can't be executed: missing execute permission, or the container runs as a "
          "user that may not run it (on OpenShift, a random non-root UID).",
          "Fix file permissions in the image (group 0, g=u), or run with a service account allowed the needed SCC."),
    127: ("command not found (exit 127)", "The command or entrypoint does not exist in the image.",
          "Check command/args against the image contents (typo, wrong image, missing shell in a distroless image)."),
    134: ("aborted (SIGABRT, exit 134)", "The process aborted itself (assertion failure, runtime panic, JVM abort).",
          "Read the previous container's log for the abort message / core dump note."),
    137: ("killed (SIGKILL, exit 137)",
          "The container was killed from outside: out of memory, or the kubelet killed it after a failed liveness "
          "probe or at the end of its termination grace period.",
          "Check whether the last state says OOMKilled and whether liveness-probe events precede the restarts."),
    139: ("segmentation fault (exit 139)", "The process crashed with a memory access error (native code bug, "
          "incompatible library, wrong CPU architecture).", "Check the image architecture and native dependencies."),
    143: ("terminated (SIGTERM, exit 143)",
          "The container was asked to stop: a failed liveness probe, a rollout, an eviction or a node drain.",
          "Look for liveness-probe or eviction events just before the restarts."),
}


def exit_advice(code, reason: str) -> tuple[str, str, str]:
    if reason == "OOMKilled":
        return ("out of memory (OOMKilled)",
                "The container used more memory than its limit and the kernel killed it.",
                "Raise the memory limit, or find what grows: heap sized above the limit (-Xmx, NODE_OPTIONS), a "
                "leak, or a burst at startup.")
    if code in EXIT_CODES:
        return EXIT_CODES[code]
    if isinstance(code, int) and code > 128:
        return (f"killed by signal {code - 128} (exit {code})", "The process was terminated by a signal.",
                "Read the previous container's log and the pod's events.")
    return (f"exit code {code}" if code is not None else "keeps exiting",
            "The container keeps exiting shortly after it starts.",
            "Read the last log lines of the previous container.")


_IMAGE = [
    (r"manifest unknown|not found|does not exist|no such image|NotFound|name unknown",
     "the image or tag does not exist",
     "The registry has no image with this name and tag (typo, tag never pushed, or the build failed).",
     "Check the exact image:tag against the registry. If a build should have produced it, look at the build."),
    (r"unauthorized|authentication required|denied|forbidden|no basic auth|pull access",
     "the registry refused access",
     "The node may not pull this image: the repository is private and the pod has no valid pull secret.",
     "Add an imagePullSecret to the pod or link one to its service account, and check the secret's registry host."),
    (r"toomanyrequests|rate limit", "the registry rate limit was hit",
     "The registry is throttling anonymous or free-tier pulls from this cluster's address.",
     "Pull with authenticated credentials, or mirror the image into your own registry."),
    (r"x509|certificate", "the registry's TLS certificate is not trusted",
     "The node doesn't trust the certificate the registry presents (private CA or expired certificate).",
     "Add the registry CA to the cluster's trusted CAs (OpenShift: image.config.openshift.io/cluster "
     "additionalTrustedCA) or fix the registry certificate."),
    (r"no such host|i/o timeout|connection refused|dial tcp|network is unreachable|TLS handshake timeout|EOF",
     "the node cannot reach the registry",
     "DNS, a firewall or a proxy stops the node from connecting to the registry.",
     "From the node, test name resolution and a connection to the registry; check proxy / mirror configuration."),
    (r"no matching manifest|exec format error|architecture", "the image is built for another CPU architecture",
     "The image has no variant for this node's architecture.", "Build or pull a multi-arch image."),
    (r"invalid reference format|InvalidImageName|couldn't parse image", "the image name is malformed",
     "The image reference isn't valid (stray character, uppercase repository, unresolved template variable).",
     "Fix the image field in the pod template."),
]


def image_advice(message: str) -> tuple[str, str, str]:
    for pattern, what, cause, fix in _IMAGE:
        if re.search(pattern, message or "", re.I):
            return what, cause, fix
    return ("the image can't be pulled", "The kubelet could not pull the image.",
            "Describe the pod: the Failed event carries the registry's exact answer.")


_SCHED = [
    (r"Insufficient memory", "no node has enough unreserved memory",
     "Every node's memory is already reserved by other pods' requests (reserved, not necessarily used).",
     "Lower this pod's memory request, free reservations (scale something down), or add a node."),
    (r"Insufficient cpu", "no node has enough unreserved CPU",
     "Every node's CPU is already reserved by other pods' requests.",
     "Lower this pod's CPU request, free reservations, or add a node."),
    (r"Insufficient (ephemeral-storage|nvidia|[a-z0-9./-]+)", "no node has a resource the pod requests",
     "The pod requests a resource no node can still provide.", "Check the requested resource and node capacity."),
    (r"untolerated taint|had taint|taints that the pod didn't tolerate", "every candidate node is tainted",
     "The nodes that could run it carry taints the pod does not tolerate.",
     "Add a matching toleration, or remove the taint if it is stale (e.g. a leftover maintenance taint)."),
    (r"node affinity|node selector|didn't match Pod's node", "its node selector / affinity matches no node",
     "No node has the labels the pod's nodeSelector or nodeAffinity requires.",
     "Compare the selector with `get nodes --show-labels`; fix the selector or label a node."),
    (r"anti-affinity", "pod anti-affinity leaves no node free",
     "Anti-affinity forbids sharing a node with certain pods, and every node already has one.",
     "Add a node, relax the rule to preferredDuringScheduling, or lower the replica count."),
    (r"pod affinity", "its pod affinity can't be satisfied", "No node runs the pods this one must be placed with.",
     "Check the affinity rule's label selector and topology key."),
    (r"unbound immediate PersistentVolumeClaims|persistentvolumeclaim .* not found|pod has unbound",
     "its volume claim is not bound", "The pod waits for a PersistentVolumeClaim that has no volume yet.",
     "Look at the claim: it shows why no volume was provisioned or bound."),
    (r"volume node affinity conflict", "its volume lives in another zone",
     "The bound volume can only attach to nodes in a zone where the pod can't be scheduled.",
     "Schedule the pod into the volume's zone or recreate the volume where the pod can run."),
    (r"Too many pods", "the nodes are at their pod limit", "Every candidate node already runs its maximum number of pods.",
     "Add a node or raise maxPods."),
    (r"were unschedulable|unschedulable", "the candidate nodes are cordoned",
     "Nodes that would fit are marked unschedulable (cordoned / draining).", "Uncordon a node or wait for the drain."),
    (r"max volume count", "the nodes can't attach more volumes", "Each candidate node has hit its attachable-volume limit.",
     "Spread volume-heavy pods across more nodes."),
]


def sched_advice(message: str) -> tuple[str, str, str]:
    for pattern, what, cause, fix in _SCHED:
        if re.search(pattern, message or "", re.I):
            return what, cause, fix
    return ("the scheduler found no node for it", "No node satisfies all of the pod's constraints.",
            "Read the scheduler's message: it lists how many nodes failed each check.")


def admission_advice(message: str, openshift: bool) -> tuple[str, str, str, str]:
    """-> (category, what, cause, fix) for a controller that can't create its pods."""
    m = message or ""
    if re.search(r"failed quota|must specify", m, re.I):
        return ("quota", "the project quota requires requests and limits the pod doesn't set",
                "The project's ResourceQuota counts CPU or memory, so every container must declare how much it asks for. "
                "This pod leaves some of them out, so it can't be counted and is refused.",
                "Set the missing requests / limits on the containers, or give the project a LimitRange with defaults.")
    if re.search(r"exceeded quota", m, re.I):
        return ("quota", "the project quota is exhausted",
                "Creating the pod would exceed the project's ResourceQuota (the message lists requested / used / limit).",
                "Raise the quota, lower the pod's requests and limits, or free quota by scaling something else down.")
    if re.search(r"security context constraint|unable to validate against any|PodSecurity|violates", m, re.I):
        return ("admission", "the pod is rejected by the security policy",
                "The pod spec asks for something its service account isn't allowed (" +
                ("no SecurityContextConstraint admits it" if openshift else "it violates the namespace's Pod Security level") +
                "): typically runAsUser/root, hostPath, hostNetwork, privileged or extra capabilities.",
                "Drop the privileged setting from the pod spec, or grant the workload's service account a policy that "
                "allows it" + (" (`oc adm policy add-scc-to-user <scc> -z <serviceaccount>`)." if openshift else "."))
    if re.search(r"admission webhook", m, re.I):
        hook = re.search(r'admission webhook "([^"]+)"', m)
        return ("admission", f"an admission webhook rejects the pod{' (' + hook.group(1) + ')' if hook else ''}",
                "A validating webhook denied the pod, or the webhook itself is unreachable and fails closed.",
                "Read the webhook's message. If it says the webhook could not be called, the webhook's own pods are down.")
    if re.search(r"minimum|maximum|limit ?range|must be less than or equal|must be greater", m, re.I):
        return ("admission", "the pod violates the project's LimitRange",
                "A container's request or limit is outside what the namespace's LimitRange allows.",
                "Adjust the container's requests/limits to fit, or change the LimitRange.")
    if re.search(r"serviceaccount.*not found|service account.*not found", m, re.I):
        return ("admission", "its service account doesn't exist",
                "The pod template names a ServiceAccount that is missing from the namespace.",
                "Create the ServiceAccount or fix serviceAccountName.")
    return ("admission", "the API server refuses to create its pods", "The controller's create request is being rejected.",
            "Read the message: it names the rule that refused the pod.")


def config_advice(message: str) -> tuple[str, str, str]:
    m = message or ""
    hit = re.search(r'(configmap|secret) "([^"]+)" not found', m, re.I)
    if hit:
        kind = "ConfigMap" if hit.group(1).lower() == "configmap" else "Secret"
        return (f"{kind} {hit.group(2)} is missing",
                f"The pod references {kind} \"{hit.group(2)}\", which does not exist in this namespace.",
                f"Create the {kind} (or fix its name in the pod template). The container starts as soon as it exists.")
    hit = re.search(r"couldn't find key (\S+) in (ConfigMap|Secret) (\S+)", m)
    if hit:
        return (f"key {hit.group(1)} is missing from {hit.group(2)} {hit.group(3).split('/')[-1]}",
                f"The {hit.group(2)} exists but has no key \"{hit.group(1)}\" that the pod's env references.",
                "Add the key, or fix the key name in the pod template (or mark the reference optional).")
    if re.search(r"runAsNonRoot|will run as root", m):
        return ("the image runs as root but the pod requires non-root",
                "securityContext.runAsNonRoot is set and the image's user is root (or non-numeric).",
                "Set a numeric non-root USER in the image or runAsUser in the pod spec.")
    if re.search(r"executable file not found|no such file or directory|exec:", m):
        return ("the container command can't be started", "The command or a mounted path it needs does not exist.",
                "Check command/args and volume mounts against the image.")
    return ("the container can't be created from its spec", "The kubelet could not build the container's configuration.",
            "Describe the pod: the message names the missing or invalid reference.")


def mount_advice(reason: str, message: str) -> tuple[str, str, str, str]:
    """-> (category, what, cause, fix) for a pod stuck in ContainerCreating."""
    m = message or ""
    hit = re.search(r'(secret|configmap) "([^"]+)" not found', m, re.I)
    if hit:
        kind = "Secret" if hit.group(1).lower() == "secret" else "ConfigMap"
        return ("config", f"a mounted {kind} ({hit.group(2)}) is missing",
                f"A volume mounts {kind} \"{hit.group(2)}\", which does not exist in this namespace.",
                f"Create the {kind} or fix its name. The pod starts by itself once the volume can be mounted.")
    if re.search(r"Multi-Attach", m):
        return ("storage", "its volume is still attached to another node",
                "A ReadWriteOnce volume can attach to one node at a time, and the previous pod's node still holds it "
                "(old pod not fully gone, or that node is down).",
                "Make sure the old pod is gone. If its node is dead, the volume detaches after the node is removed or "
                "the attachment is force-deleted.")
    if reason == "FailedAttachVolume" or re.search(r"AttachVolume|timed out waiting for the condition|Unable to attach", m):
        return ("storage", "its volume can't be attached or mounted",
                "The storage backend didn't attach the volume in time (CSI driver down, cloud API error, wrong zone).",
                "Check the CSI driver pods and the claim's events.")
    if reason == "FailedCreatePodSandBox" or re.search(r"sandbox|CNI|network", m, re.I):
        return ("network", "the pod network can't be set up",
                "The CNI plugin failed to give the pod a network: plugin pods down on that node, or the node's pod "
                "IP range is exhausted.",
                "Check the CNI / SDN pods on that node and its pod CIDR usage.")
    if reason == "FailedMount":
        return ("storage", "a volume can't be mounted", "The kubelet could not mount one of the pod's volumes.",
                "Read the mount error: it names the volume and the reason.")
    return ("availability", "it is stuck creating its containers", "The pod has a node but its containers never start.",
            "Describe the pod and read its newest events.")


def probe_advice(kind: str, message: str, listening: list[int] | None, port) -> tuple[str, str]:
    m = message or ""
    hint = ""
    if listening and port and str(port).isdigit() and int(port) not in listening:
        hint = f" The pod listens on {', '.join(map(str, listening[:6]))}, not on the probe's port {port}."
    if re.search(r"connection refused", m):
        return ("Nothing accepts connections on the probe's port: the app listens elsewhere, binds to localhost "
                "only, or hasn't finished starting." + hint,
                "Match the probe port to the port the app listens on (and bind to 0.0.0.0); give slow starters a "
                "startupProbe or a longer initialDelaySeconds.")
    code = re.search(r"statuscode: (\d+)", m)
    if code:
        c = int(code.group(1))
        if c == 404:
            return ("The probe path doesn't exist in the app (HTTP 404).", "Fix the probe's path.")
        if c in (401, 403):
            return (f"The probe endpoint requires authentication (HTTP {c}).",
                    "Probe an unauthenticated health endpoint.")
        return (f"The app answers the probe with HTTP {c}: it reports itself unhealthy, usually because a "
                "dependency it checks is down.",
                "Look at what the health endpoint checks (database, downstream service) and at the app's log.")
    if re.search(r"deadline exceeded|timeout|timed out", m, re.I):
        return ("The app doesn't answer the probe within its timeout (1 s by default): it is overloaded, "
                "CPU-throttled or blocked." + hint,
                "Check CPU against the limit and what the app is waiting on; raise timeoutSeconds if the endpoint is "
                "legitimately slow.")
    if re.search(r"no such host|dial tcp", m):
        return ("The probe can't connect to the pod." + hint, "Check the probe's host/port.")
    return (f"The {kind} probe keeps failing." + hint, "Describe the pod: the Unhealthy events carry the probe's output.")


# ---------------------------------------------------------------------------- generic signals (risk.py codes)
# code -> (category, cause, fix, [commands], severity override or None)
WORKLOAD = {
    "replicas": ("availability", "Fewer pods are ready than the workload asks for.",
                 "Open the pods below: each one shows why it isn't ready.",
                 ["{cli} -n {ns} get pods -o wide | grep {name}", "{cli} -n {ns} describe {ref}"], None),
    "unschedulable": ("scheduling", "The scheduler can't find a node for these pods.",
                      "Read the scheduler message: it says which constraint no node meets.",
                      ["{cli} -n {ns} get events --field-selector reason=FailedScheduling"], None),
    "evicted": ("node", "The kubelet evicted pods to reclaim memory or disk on their node.",
                "Check that node's pressure. Give pods realistic requests so the scheduler doesn't overpack nodes. "
                "Evicted pods stay listed until deleted.",
                ["{cli} -n {ns} get pods --field-selector=status.phase=Failed"], None),
    "restarts": ("crash", "Containers have been restarting.",
                 "Read the previous container's log and its last exit code.",
                 ["{cli} -n {ns} get pods | grep {name}"], None),
    "cpu": ("saturation", "Pods are at their CPU limit, so the kernel throttles them. Throttling shows up as "
            "latency, not as errors.",
            "Raise the CPU limit, add replicas, or find the hot path.", ["{cli} -n {ns} top pods | grep {name}"], None),
    "mem": ("saturation", "Pods are close to their memory limit. The next step is an OOM kill.",
            "Raise the limit, or find what grows (heap size vs limit, leak).",
            ["{cli} -n {ns} top pods --containers | grep {name}"], None),
    "hpa-max": ("saturation", "The autoscaler wants more replicas than maxReplicas allows.",
                "Raise maxReplicas (if nodes have room) or make each pod handle more.",
                ["{cli} -n {ns} describe hpa {name}"], None),
    "accept-queue": ("saturation", "Connections arrive faster than the app accepts them: its workers are all busy "
                     "or the process is stalled.",
                     "Add replicas or workers, and find what holds the workers (often a slow dependency).", [], None),
    "recvq": ("saturation", "The app isn't reading its sockets fast enough: it is CPU-starved or blocked.",
              "Check CPU throttling and what the app is waiting on.", [], None),
    "probe-fail": ("availability", "Connections to the pods are refused or time out.",
                   "Check that the app listens on the service's target port.", [], None),
    "probe-slow": ("saturation", "Pods are slow to accept new connections.", "Check CPU throttling and the accept queue.",
                   [], None),
    "single-replica": ("hygiene", "One replica serves several callers: any restart or node drain is an outage for them.",
                       "Run at least two replicas with a PodDisruptionBudget.", [], "info"),
}

EDGE = {
    "p95": ("latency", "The callee answers slowly. It is usually saturated, or waiting on a dependency of its own.",
            "Open the callee: check its CPU against the limit, its accept queue, and its own downstream calls."),
    "errors": ("latency", "The callee returns server errors (5xx).",
               "Check the callee's log and its dependencies. Requests in flight also fail while its pods restart."),
    "rtt": ("network", "The network path itself is slow (the kernel's minimum round-trip time), independent of the app.",
            "Compare with other callers of the same service, and look at the nodes on both ends."),
    "retrans": ("network", "Packets are being lost between the two ends.",
                "Check whether one node accounts for most retransmits (NIC errors, MTU mismatch on the overlay)."),
    "sendq": ("saturation", "The callee isn't reading what the caller sends: it is saturated or stalled.",
              "Open the callee and check CPU throttling and its accept queue."),
    "pending": ("network", "Connection attempts get no answer: nothing listens there, a NetworkPolicy or firewall drops "
                "them, or the target pods are gone.",
                "Check the service's endpoints and port, and the NetworkPolicies on both sides."),
    "probe-fail": ("network", "Test connections to the service fail.", "Check the service's endpoints and target port."),
    "connect-slow": ("network", "Opening a connection to the service is slow.", "Check the callee's accept queue and node."),
    "svc-no-pods": ("network", "The Service this call goes to has no pods behind it.",
                    "Fix the Service's selector or deploy the workload it should select."),
    "target-unready": ("availability", "Every pod behind this call is unready, so the call fails or times out.",
                       "Fix the callee."),
    "cross-zone": ("hygiene", "Caller and callee run in different zones, so every request pays an inter-zone hop.",
                   "Fine for resilience; co-locate (topology-aware routing) only if latency matters here."),
}
EDGE_INFO = {"cross-zone"}

HOST = {
    "notready": ("The kubelet stopped reporting: the machine is down, the kubelet or container runtime crashed, or "
                 "the node lost its network.",
                 "Check the machine. Its pods are only rescheduled after the eviction timeout (5 min by default), and "
                 "StatefulSet pods not until the node is confirmed gone.",
                 ["{cli} describe node {name}", "{cli} get pods -A -o wide --field-selector spec.nodeName={name}"]),
    "pressure:MemoryPressure": ("The node is nearly out of memory, so the kubelet evicts pods.",
                                "Find the pod using far more than it requested (often one without a memory limit).",
                                ["{cli} describe node {name}", "{cli} top pods -A --sort-by=memory | head"]),
    "pressure:DiskPressure": ("The node's disk is nearly full (images, container logs, emptyDir), so the kubelet evicts "
                              "pods and deletes images.",
                              "Free space: prune images, find pods writing large logs or emptyDir data.",
                              ["{cli} describe node {name}"]),
    "pressure:PIDPressure": ("The node is running out of process IDs.", "Find the pod leaking processes or threads.",
                             ["{cli} describe node {name}"]),
    "pressure:NetworkUnavailable": ("The node's pod network isn't configured.", "Check the CNI / SDN pods on this node.",
                                    ["{cli} describe node {name}"]),
    "node-cpu": ("Pods on this node use nearly all of its CPU, so they all compete for cycles.",
                 "Find the heaviest pod (often one without a CPU limit) and move or limit it.",
                 ["{cli} top pods -A --sort-by=cpu | head"]),
    "node-mem": ("Pods on this node use nearly all of its memory.", "Expect evictions next. Move or limit the largest pods.",
                 ["{cli} top pods -A --sort-by=memory | head"]),
    "node-mem-req": ("Memory requests reserve almost all of this node, whatever the pods actually use.",
                     "New or rescheduled pods won't fit here. Right-size requests or add capacity.",
                     ["{cli} describe node {name} | grep -A8 'Allocated resources'"]),
    "node-cpu-req": ("CPU requests reserve almost all of this node.", "Right-size requests or add capacity.",
                     ["{cli} describe node {name} | grep -A8 'Allocated resources'"]),
    "node-mem-limits": ("Memory limits add up to far more than the node has.",
                        "If pods grow toward their limits together, the kernel OOM-kills some. Bring limits closer to requests.",
                        []),
    "node-retrans": ("Pods on this node retransmit far more than pods elsewhere: packet loss at this node.",
                     "Check the node's NIC counters, MTU and the overlay network on it.", ["{cli} describe node {name}"]),
    "node-slow": ("The same calls are much slower from this node than from others.",
                  "Check the node's network path and CPU steal.", []),
    "agent-stale": ("The flow agent on this node stopped reporting, so there is no socket telemetry from it.",
                    "Check the galaxy-agent pod on this node.", []),
    "cordoned": ("The node is cordoned: running pods stay, nothing new is scheduled here.",
                 "Uncordon it when maintenance is over.", ["{cli} uncordon {name}"]),
}
HOST_INFO = {"agent-stale", "cordoned", "node-mem-limits"}


# Generic wording for fleet patterns ("N workloads: <label>").
PATTERN_LABELS = {
    "wl:replicas": "fewer pods ready than desired", "wl:unschedulable": "pods that can't be scheduled",
    "wl:evicted": "pods evicted by their node", "wl:restarts": "containers restarting", "wl:cpu": "CPU at its limit (throttled)",
    "wl:mem": "memory close to its limit", "wl:hpa-max": "autoscaler pinned at its maximum",
    "wl:accept-queue": "connections queueing faster than the app accepts them", "wl:recvq": "app not reading its sockets fast enough",
    "wl:probe-fail": "connections to the pods failing", "wl:probe-slow": "slow to accept connections",
    "wl:single-replica": "single replica serving several callers",
    "sizing:over": "reserving far more than they use",
    "call:p95": "slow calls", "call:errors": "calls returning server errors", "call:rtt": "slow network path",
    "call:retrans": "packet loss on calls", "call:sendq": "callee not reading what callers send",
    "call:pending": "connection attempts going unanswered", "call:probe-fail": "test connections failing",
    "call:connect-slow": "slow to connect", "call:svc-no-pods": "calls into a Service with no pods",
    "call:target-unready": "calls into workloads with no ready pods", "call:cross-zone": "calls crossing zones",
    "gaps": "hardening gaps (missing probes, requests, limits, pinned tags)", "svc-empty": "Services that select no pods",
    "svc-port": "Services pointing at a port the pods don't listen on", "netpol": "calls blocked by a NetworkPolicy",
    "entry-nosvc": "routes pointing at a Service that doesn't exist", "entry-port": "routes using a port the Service doesn't expose",
    "route-rejected": "routes not admitted", "route-unclaimed": "routes no router serves", "ingress-unclaimed": "Ingresses with no controller",
    "pvc-pending": "volume claims not bound", "pvc-lost": "volume claims that lost their volume", "pvc-missing": "missing volume claims",
    "quota": "project quotas nearly or fully used", "job": "failed jobs", "hpa": "autoscalers that can't scale",
    "cert-expired": "expired certificates", "cert-notready": "certificates not issued", "cert-expiring": "certificates about to expire",
    "node:notready": "nodes not ready", "node:node-cpu": "nodes with CPU saturated", "node:node-mem": "nodes with memory saturated",
    "node:node-mem-req": "nodes with memory reservations exhausted", "node:node-cpu-req": "nodes with CPU reservations exhausted",
    "node:node-retrans": "nodes with packet loss", "node:cordoned": "cordoned nodes", "node:agent-stale": "nodes whose flow agent stopped reporting",
}
