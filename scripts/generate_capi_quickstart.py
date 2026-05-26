#!/usr/bin/env python3
"""Generate capi-quickstart.yaml using clusterctl and apply project-specific customizations.

Conventions (v0.1 CAPS):
  - 1 MachineDeployment (controller / Slurm head)
  - 1 MachinePool (compute) optionally managed by Cluster Autoscaler

Autoscaling Mode (default):
  - Enabled via --autoscale (default True; disable with --no-autoscale)
  - MachinePool `replicas` omitted so Cluster Autoscaler can manage size
    - Annotations set:
            * `cluster.x-k8s.io/cluster-api-autoscaler-node-group-min-size=1`
            * `cluster.x-k8s.io/cluster-api-autoscaler-node-group-max-size=10`
        (Defaults; adjust manually or extend script for flags later)

Non-Autoscale Mode (--no-autoscale):
  - MachinePool `replicas` set explicitly from --machinepool-replicas
  - Autoscaler annotation removed if previously present (idempotent rollback)

Other Features:
  - Default Kubernetes version baked in as DEFAULT_K8S_VERSION (override with --kubernetes-version)
  - SSH enablement on node bootstrap (preKubeadmCommands, idempotent public key injection)
    - Insecure HTTP registry enablement for host.docker.internal:5000 (containerd config.toml patch, idempotent)
  - Role labels: MachineDeployment=controller, MachinePool=compute
  - Round-trip YAML preservation with ruamel.yaml
  - Idempotent mutations (safe to re-run after manual edits)

Exit codes:
  0 success
  2 SSH key issues
  3 ruamel.yaml missing

"""
from __future__ import annotations
import argparse
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import List, Any, Optional

# --- YAML Library (ruamel) --- #
try:
    from ruamel.yaml import YAML
    from ruamel.yaml.comments import CommentedMap, CommentedSeq
except ImportError:  # pragma: no cover
    print("ERROR: ruamel.yaml is required. Install with: pip install ruamel.yaml", file=sys.stderr)
    raise SystemExit(3)

yaml_rt = YAML(typ='rt')  # round-trip
yaml_rt.preserve_quotes = True

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_OUTPUT = REPO_ROOT / "capi-quickstart.yaml"
# Default Kubernetes version for the workload (CAPD) cluster. Keep in sync with
# the kindest/node image pinned in pulumi/ctlptl/ctlptl_cluster.py.
DEFAULT_K8S_VERSION = "v1.34.0"
DEFAULT_SSH_KEY_PATH = Path.home() / ".ssh" / "id_rsa.pub"

SSH_COMMANDS_TEMPLATE = [
    "apt update -y",
    "apt install -y openssh-server",
    "mkdir -p /etc/ssh",
    # authorized key cmd inserted dynamically after this line
    'echo "PermitRootLogin yes" >> /etc/ssh/sshd_config',
    'echo "PubkeyAuthentication yes" >> /etc/ssh/sshd_config',
    'echo "AuthorizedKeysFile /etc/ssh/authorized_keys .ssh/authorized_keys .ssh/authorized_keys2" >> /etc/ssh/sshd_config',
    "systemctl enable ssh",
    "systemctl start ssh",
]

MACHINEPOOL_DEFAULT_CLASS = "default-worker"
MACHINEPOOL_DEFAULT_NAME = "mp-0"

LABEL_KEY = "slinky.slurm.net/node-type"
LABEL_VALUE_CONTROLLER = "controller"
LABEL_VALUE_COMPUTE = "compute"

AUTOSCALER_MIN_ANNOTATION = "cluster.x-k8s.io/cluster-api-autoscaler-node-group-min-size"
AUTOSCALER_MIN_VALUE = "1"
AUTOSCALER_MAX_ANNOTATION = "cluster.x-k8s.io/cluster-api-autoscaler-node-group-max-size"
AUTOSCALER_MAX_VALUE = "10"

# TODO(host-registry-port): hardcoded for now to match the legacy ctlptl.yaml
# behavior. Now that ctlptl.yaml uses ${RANDOM_PORT}, the registry's host port
# is chosen by Pulumi at create-time (see pulumi/). This constant
# should accept the port via CLI flag / env var (e.g. CA4S_HOST_REGISTRY_PORT)
# and the bootstrap stack should pass `pulumi stack output random_port` into
# it so the CAPD containerd mirror matches the registry's actual port.
DEFAULT_HOST_REGISTRY = "host.docker.internal:5000"

# Track style/preamble for multi-doc YAML
_MULTI_DOC_STYLE: dict[str, Any] = {
    "preamble": "",          # leading comments / whitespace before first doc
    "leading_sep": False,     # whether file began with ---\n explicitly
}

# ---------------- Utility Functions ---------------- #

def run_clusterctl(cluster_name: str, flavor: str, k8s_version: str, cp_replicas: int, md_replicas: int) -> str:
    cmd = [
        "clusterctl", "generate", "cluster", cluster_name,
        "--flavor", flavor,
        f"--kubernetes-version", k8s_version,
        f"--control-plane-machine-count={cp_replicas}",
        f"--worker-machine-count={md_replicas}",
    ]
    with tempfile.NamedTemporaryFile("w+", delete=False) as tmp:
        subprocess.run(cmd, check=True, stdout=tmp)
        tmp_path = tmp.name
    try:
        return Path(tmp_path).read_text()
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass


def load_documents(yaml_text: str) -> List[CommentedMap]:
    from io import StringIO
    # Capture preamble (everything before first '---' that precedes a doc start)
    preamble = []
    leading_sep = False
    lines = yaml_text.splitlines(keepends=True)
    consumed = 0
    for idx, line in enumerate(lines):
        if line.startswith('---'):
            leading_sep = True
            consumed = idx + 1
            break
        # If we reach a line that looks like the start of a YAML document (apiVersion:, kind:, etc.) we stop
        if line.lstrip().startswith(('apiVersion:', 'kind:', 'metadata:', '# Source:')):
            consumed = idx
            break
        preamble.append(line)
        consumed = idx + 1
    _MULTI_DOC_STYLE['preamble'] = ''.join(preamble)
    _MULTI_DOC_STYLE['leading_sep'] = leading_sep

    # Build the remaining text for ruamel loader (include separator if we stopped at it)
    remaining = ''.join(lines[consumed:])
    docs: List[CommentedMap] = []
    for doc in yaml_rt.load_all(StringIO(remaining)):
        if doc is None:
            continue
        if isinstance(doc, CommentedMap):
            docs.append(doc)
        elif isinstance(doc, dict):
            cm = CommentedMap(); cm.update(doc); docs.append(cm)
    return docs


def dump_documents(docs: List[CommentedMap]) -> str:
    from io import StringIO
    parts: List[str] = []
    for i, doc in enumerate(docs):
        buf = StringIO(); yaml_rt.dump(doc, buf)
        text = buf.getvalue().rstrip()  # stable trimming
        parts.append(text)
    body = '\n---\n'.join(parts) + '\n'
    if _MULTI_DOC_STYLE.get('leading_sep'):
        body = '---\n' + body
    preamble = _MULTI_DOC_STYLE.get('preamble', '')
    # If preamble exists ensure it ends with single newline
    if preamble and not preamble.endswith('\n'):
        preamble += '\n'
    return f"{preamble}{body}"  # preamble (may be empty) + documents


# ---------------- Mutation Functions ---------------- #

def ensure_ssh_on_kubeadm_config_templates(docs: List[CommentedMap], public_key: str) -> int:
    modified = 0
    for doc in docs:
        if doc.get("kind") != "KubeadmConfigTemplate":
            continue
        api = doc.get("apiVersion", "")
        if not isinstance(api, str) or not api.startswith("bootstrap.cluster.x-k8s.io/"):
            continue
        spec = doc.setdefault("spec", CommentedMap())
        template = spec.setdefault("template", CommentedMap())
        tmpl_spec = template.setdefault("spec", CommentedMap())
        existing = tmpl_spec.get("preKubeadmCommands")
        if existing is None:
            existing_list: List[str] = []
        elif isinstance(existing, list):
            existing_list = [c for c in existing if isinstance(c, str)]
        else:
            continue  # malformed

        authorized_key_cmd = f'echo "{public_key}" > /etc/ssh/authorized_keys'
        filtered = [c for c in existing_list if not ("/etc/ssh/authorized_keys" in c and c != authorized_key_cmd)]

        desired_order: List[str] = []
        for cmd in SSH_COMMANDS_TEMPLATE:
            desired_order.append(cmd)
            if cmd == "mkdir -p /etc/ssh":
                desired_order.append(authorized_key_cmd)

        final_cmds: List[str] = []
        for cmd in desired_order:
            if cmd not in final_cmds:
                final_cmds.append(cmd)
        for cmd in filtered:
            if cmd not in final_cmds:
                final_cmds.append(cmd)

        if final_cmds != existing_list:
            tmpl_spec["preKubeadmCommands"] = final_cmds
            modified += 1
    return modified


def build_registry_command(registry: str) -> str:
    """
    Return a single shell line that safely appends a mirror stanza if absent.
    """
    return (
        f"if ! grep -q SLINKY-REGISTRY-START /etc/containerd/config.toml; then "
        f"[ -f /etc/containerd/config.toml ] || containerd config default > /etc/containerd/config.toml; "
        f"{{ echo '# SLINKY-REGISTRY-START'; "
        f"echo '[plugins.\"io.containerd.grpc.v1.cri\".registry.mirrors.\"{registry}\"]'; "
        f"echo '  endpoint = [\"http://{registry}\"]'; "
        f"echo '# SLINKY-REGISTRY-END'; }} >> /etc/containerd/config.toml; "
        f"systemctl restart containerd; fi"
    )


def ensure_insecure_registry(docs: List[CommentedMap], registry: str = DEFAULT_HOST_REGISTRY) -> int:
    """Inject containerd config patch commands to enable pulling from an HTTP registry.

    Strategy:
      - Ensures /etc/containerd/config.toml exists (generates default if missing)
      - Appends a mirror stanza for the registry inside config.toml if not already present
      - Uses a sentinel comment # SLINKY-REGISTRY-START for idempotency
      - Restarts containerd only on first injection
    Returns count of modified KubeadmConfigTemplate docs.
    """
    added = 0
    cmd = build_registry_command(registry)
    for doc in docs:
        if doc.get("kind") != "KubeadmConfigTemplate":
            continue
        api = doc.get("apiVersion", "")
        if not isinstance(api, str) or not api.startswith("bootstrap.cluster.x-k8s.io/"):
            continue
        spec = doc.setdefault("spec", CommentedMap())
        template = spec.setdefault("template", CommentedMap())
        tmpl_spec = template.setdefault("spec", CommentedMap())
        existing = tmpl_spec.get("preKubeadmCommands")
        if existing is None:
            existing_list: List[str] = []
        elif isinstance(existing, list):
            existing_list = [c for c in existing if isinstance(c, str)]
        else:
            continue
        # If any existing command already references sentinel or registry assume configured
        already = any("SLINKY-REGISTRY-START" in c or registry in c for c in existing_list)
        if not already:
            existing_list.append(cmd)
            tmpl_spec["preKubeadmCommands"] = existing_list
            added += 1
    return added


def ensure_machine_pool(
    docs: List[CommentedMap],
    replicas: int,
    autoscale: bool,
    pool_name: str = MACHINEPOOL_DEFAULT_NAME,
    pool_class: str = MACHINEPOOL_DEFAULT_CLASS,
) -> bool:
    mutated = False
    for doc in docs:
        if doc.get("kind") != "Cluster":
            continue
        spec = doc.setdefault("spec", CommentedMap())
        topo = spec.setdefault("topology", CommentedMap())
        workers = topo.setdefault("workers", CommentedMap())
        pools = workers.setdefault("machinePools", CommentedSeq())
        if not isinstance(pools, list):
            continue
        entry = None
        for p in pools:
            if isinstance(p, dict) and p.get("name") == pool_name:
                entry = p
                break
        if entry is None:
            entry = CommentedMap()
            entry["class"] = pool_class
            entry["name"] = pool_name
            pools.append(entry)
            mutated = True
        meta = entry.setdefault("metadata", CommentedMap())
        annotations = meta.setdefault("annotations", CommentedMap())
        if autoscale:
            if "replicas" in entry:
                del entry["replicas"]
                mutated = True
            if annotations.get(AUTOSCALER_MIN_ANNOTATION) != AUTOSCALER_MIN_VALUE:
                annotations[AUTOSCALER_MIN_ANNOTATION] = AUTOSCALER_MIN_VALUE
                mutated = True
            if annotations.get(AUTOSCALER_MAX_ANNOTATION) != AUTOSCALER_MAX_VALUE:
                annotations[AUTOSCALER_MAX_ANNOTATION] = AUTOSCALER_MAX_VALUE
                mutated = True
        else:
            if entry.get("replicas") != replicas:
                entry["replicas"] = replicas
                mutated = True
            if AUTOSCALER_MIN_ANNOTATION in annotations:
                del annotations[AUTOSCALER_MIN_ANNOTATION]
                mutated = True
            if AUTOSCALER_MAX_ANNOTATION in annotations:
                del annotations[AUTOSCALER_MAX_ANNOTATION]
                mutated = True
            if not annotations:
                meta.pop("annotations", None)
        if not meta:
            entry.pop("metadata", None)
    return mutated


def ensure_topology_labels(docs):
    changed = False
    for doc in docs:
        if doc.get("kind") != "Cluster":
            continue
        spec = doc.get("spec") or {}
        topo = spec.get("topology") or {}
        workers = topo.get("workers") or {}
        # machineDeployments -> controller label
        mds = workers.get("machineDeployments") or []
        for md in mds:
            if not isinstance(md, dict):
                continue
            meta = md.setdefault("metadata", {})
            labels = meta.setdefault("labels", {})
            if labels.get(LABEL_KEY) != LABEL_VALUE_CONTROLLER:
                labels[LABEL_KEY] = LABEL_VALUE_CONTROLLER
                changed = True
        # machinePools -> compute label
        mps = workers.get("machinePools") or []
        for mp in mps:
            if not isinstance(mp, dict):
                continue
            meta = mp.setdefault("metadata", {})
            labels = meta.setdefault("labels", {})
            if labels.get(LABEL_KEY) != LABEL_VALUE_COMPUTE:
                labels[LABEL_KEY] = LABEL_VALUE_COMPUTE
                changed = True
    return changed


# ---------------- CLI Parsing ---------------- #

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Generate customized capi-quickstart.yaml")
    parser.add_argument("--cluster-name", default="capi-quickstart", help="Cluster name (default: capi-quickstart)")
    parser.add_argument("--flavor", default="development", help="clusterctl flavor to use (default: development)")
    parser.add_argument("--kubernetes-version", dest="k8s_version", default=DEFAULT_K8S_VERSION, help=f"Kubernetes version (default: {DEFAULT_K8S_VERSION})")
    parser.add_argument("--control-plane-replicas", type=int, default=1, help="Control plane replicas (default: 1)")
    parser.add_argument("--machinedeployment-replicas", type=int, default=1, help="MachineDeployment replicas (default: 1)")
    parser.add_argument("--machinepool-replicas", type=int, default=1, help="MachinePool replicas (default: 1)")
    parser.add_argument("--machinepool-name", default=MACHINEPOOL_DEFAULT_NAME, help=f"MachinePool name (default: {MACHINEPOOL_DEFAULT_NAME})")
    parser.add_argument("--machinepool-class", default=MACHINEPOOL_DEFAULT_CLASS, help=f"MachinePool class (default: {MACHINEPOOL_DEFAULT_CLASS})")
    parser.add_argument("--ssh-public-key", type=Path, help="Path to SSH public key file; if omitted, uses ~/.ssh/id_rsa.pub")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT, help=f"Output file (default: {DEFAULT_OUTPUT})")
    parser.add_argument("--autoscale", dest="autoscale", action=argparse.BooleanOptionalAction, default=True, help="Enable autoscaler mode (omit MachinePool replicas & add autoscaler annotations). Use --no-autoscale to set fixed replicas.")
    parser.add_argument("--enable-host-registry", dest="enable_host_registry", action=argparse.BooleanOptionalAction, default=True, help="Inject containerd config to allow HTTP pulls from host.docker.internal:5000 (default enabled). Use --no-enable-host-registry to skip.")
    return parser.parse_args()


def read_ssh_key(path_arg: Optional[Path]) -> str:
    path = path_arg or DEFAULT_SSH_KEY_PATH
    if not path.exists():
        print(f"ERROR: SSH public key not found at {path}. Provide --ssh-public-key.", file=sys.stderr)
        raise SystemExit(2)
    key = path.read_text().strip()
    if not key or " " not in key:
        print("ERROR: SSH public key file seems invalid (no spaces).", file=sys.stderr)
        raise SystemExit(2)
    return key


# ---------------- Main ---------------- #

def main() -> int:
    args = parse_args()
    public_key = read_ssh_key(args.ssh_public_key)

    raw_yaml = run_clusterctl(
        cluster_name=args.cluster_name,
        flavor=args.flavor,
        k8s_version=args.k8s_version,
        cp_replicas=args.control_plane_replicas,
        md_replicas=args.machinedeployment_replicas,
    )
    docs = load_documents(raw_yaml)

    ssh_modified = ensure_ssh_on_kubeadm_config_templates(docs, public_key)
    registry_modified = 0
    if args.enable_host_registry:
        registry_modified = ensure_insecure_registry(docs, DEFAULT_HOST_REGISTRY)
    mp_modified = ensure_machine_pool(
        docs,
        replicas=args.machinepool_replicas,
        autoscale=args.autoscale,
        pool_name=args.machinepool_name,
        pool_class=args.machinepool_class,
    )
    labels_changed = ensure_topology_labels(docs)

    final_yaml = dump_documents(docs)
    args.output.write_text(final_yaml)
    print(
        f"Wrote {args.output} (SSH modified: {ssh_modified}, registry modified: {registry_modified}, machinePool changed: {mp_modified}, labels changed: {labels_changed}, autoscale={args.autoscale}, host-registry={args.enable_host_registry})"
    )
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
