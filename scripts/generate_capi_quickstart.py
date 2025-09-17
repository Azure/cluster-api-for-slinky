#!/usr/bin/env python3
"""Generate capi-quickstart.yaml using clusterctl and apply project-specific customizations.

In the current v0.1 version of CAPS, we hold the following conventions for the k8s workload cluster:
  - 1 MachineDeployment with 1 replica, dedicated to being the Slurm head node
  - 1 MachinePool with x replicas, acting as Slurm/k8s compute nodes

This script automates regeneration of a Cluster API quickstart manifest and applies
custom modifications (SSH enablement + machinePool). Useful when upgrading clusterctl/CAPI.

Dependencies:
  - clusterctl in PATH
  - Python 3.9+
  - ruamel.yaml (pip install ruamel.yaml)  # switched from PyYAML to preserve comments/formatting
  - An SSH public key (~/.ssh/id_rsa.pub by default) (TODO: instead of reading from file, bridge the SSH key with awx-operator)

What it does:
  1. Determines Kubernetes version (flag or parsed from kind-config.yaml so that workload cluster has same k8s version as management cluster for convenience)
  2. Runs `clusterctl generate cluster` with provided replica counts
  3. Parses all YAML documents (round-trip) preserving order, comments, formatting where possible
  4. Adds/updates preKubeadmCommands in each KubeadmConfigTemplate to install & configure SSH
  5. Ensures a machinePool entry (default name mp-0) with desired replicas exists in Cluster topology
  6. Writes final YAML to capi-quickstart.yaml (configurable) with preserved style

Idempotency:
  - Re-running will not duplicate SSH commands; authorized key line is updated if key changes.
  - MachinePool entry is created once and replicas updated if changed.

Examples:
  ./scripts/generate_capi_quickstart.py --ssh-public-key ~/.ssh/id_rsa.pub
  ./scripts/generate_capi_quickstart.py --kubernetes-version v1.34.0 \
      --machinedeployment-replicas 1 --machinepool-replicas 2
  ./scripts/generate_capi_quickstart.py --machinepool-name mp-workers --machinepool-replicas 4

Exit codes:
  0 success
  1 missing kubernetes version determination
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
KIND_CONFIG = REPO_ROOT / "kind-config.yaml"
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

# Track style/preamble for multi-doc YAML
_MULTI_DOC_STYLE: dict[str, Any] = {
    "preamble": "",          # leading comments / whitespace before first doc
    "leading_sep": False,     # whether file began with ---\n explicitly
}

# ---------------- Utility Functions ---------------- #

def detect_k8s_version(kind_config: Path) -> Optional[str]:
    if not kind_config.exists():
        return None
    for line in kind_config.read_text().splitlines():
        line = line.strip()
        if line.startswith("image:") and "kindest/node:v" in line:
            part = line.split("kindest/node:", 1)[1]
            tag = part.split("@", 1)[0]
            if tag.startswith("v"):
                return tag
    return None


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


def ensure_machine_pool(docs: List[CommentedMap], replicas: int, pool_name: str = MACHINEPOOL_DEFAULT_NAME, pool_class: str = MACHINEPOOL_DEFAULT_CLASS) -> bool:
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
        if entry:
            if entry.get("replicas") != replicas:
                entry["replicas"] = replicas
                mutated = True
        else:
            new_entry = CommentedMap()
            new_entry["class"] = pool_class
            new_entry["name"] = pool_name
            new_entry["replicas"] = replicas
            pools.append(new_entry)
            mutated = True
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
    parser.add_argument("--kubernetes-version", dest="k8s_version", help="Explicit Kubernetes version (e.g. v1.34.0); if omitted, parsed from kind-config.yaml")
    parser.add_argument("--control-plane-replicas", type=int, default=1, help="Control plane replicas (default: 1)")
    parser.add_argument("--machinedeployment-replicas", type=int, default=1, help="MachineDeployment replicas (default: 1)")
    parser.add_argument("--machinepool-replicas", type=int, default=2, help="MachinePool replicas (default: 2)")
    parser.add_argument("--machinepool-name", default=MACHINEPOOL_DEFAULT_NAME, help=f"MachinePool name (default: {MACHINEPOOL_DEFAULT_NAME})")
    parser.add_argument("--machinepool-class", default=MACHINEPOOL_DEFAULT_CLASS, help=f"MachinePool class (default: {MACHINEPOOL_DEFAULT_CLASS})")
    parser.add_argument("--ssh-public-key", type=Path, help="Path to SSH public key file; if omitted, uses ~/.ssh/id_rsa.pub")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT, help=f"Output file (default: {DEFAULT_OUTPUT})")
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
    k8s_version = args.k8s_version or detect_k8s_version(KIND_CONFIG)
    if not k8s_version:
        print("ERROR: Could not determine Kubernetes version (provide --kubernetes-version)", file=sys.stderr)
        return 1
    public_key = read_ssh_key(args.ssh_public_key)

    raw_yaml = run_clusterctl(
        cluster_name=args.cluster_name,
        flavor=args.flavor,
        k8s_version=k8s_version,
        cp_replicas=args.control_plane_replicas,
        md_replicas=args.machinedeployment_replicas,
    )
    docs = load_documents(raw_yaml)

    ssh_modified = ensure_ssh_on_kubeadm_config_templates(docs, public_key)
    mp_modified = ensure_machine_pool(
        docs,
        replicas=args.machinepool_replicas,
        pool_name=args.machinepool_name,
        pool_class=args.machinepool_class,
    )
    labels_changed = ensure_topology_labels(docs)

    final_yaml = dump_documents(docs)
    args.output.write_text(final_yaml)
    print(
        f"Wrote {args.output} (SSH modified docs: {ssh_modified}, machinePool changed: {mp_modified}, labels changed: {labels_changed})"
    )
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
