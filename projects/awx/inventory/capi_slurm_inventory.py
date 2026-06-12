#!/usr/bin/env python
"""AWX dynamic inventory for CAPI/Slinky workload clusters.

The script is designed for the stock AWX execution environment. It uses the
Python Kubernetes client and credentials injected by the CA4S AWX Kubernetes
credential type.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import tempfile
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

CAPI_GROUP = "cluster.x-k8s.io"
CAPI_VERSION = "v1beta2"
MACHINE_PLURAL = "machines"
CLUSTER_NAME_LABEL = "cluster.x-k8s.io/cluster-name"
NODE_TYPE_LABEL = "slinky.slurm.net/node-type"
DEFAULT_NAMESPACE = "default"
CONTROLLER_NODE_TYPE = "controller"
COMPUTE_NODE_TYPE = "compute"

_HOST_ENV = ("CA4S_K8S_HOST", "K8S_AUTH_HOST")
_TOKEN_ENV = (
    "CA4S_K8S_BEARER_TOKEN",
    "K8S_AUTH_BEARER_TOKEN",
    "K8S_AUTH_API_KEY",
)
_CA_CERT_ENV = ("CA4S_K8S_SSL_CA_CERT", "K8S_AUTH_SSL_CA_CERT")
_VERIFY_SSL_ENV = ("CA4S_K8S_VERIFY_SSL", "K8S_AUTH_VERIFY_SSL")


@dataclass(frozen=True)
class KubernetesAuth:
    host: str
    token: str
    ca_cert: str | None
    verify_ssl: bool


def _first_env(names: Iterable[str]) -> str | None:
    for name in names:
        value = os.environ.get(name)
        if value:
            return value
    return None


def _bool_env(names: Iterable[str], *, default: bool) -> bool:
    value = _first_env(names)
    if value is None:
        return default
    return value.lower() not in {"0", "false", "no", "off"}


def _load_auth_from_env() -> KubernetesAuth:
    host = _first_env(_HOST_ENV)
    token = _first_env(_TOKEN_ENV)
    if not host:
        raise RuntimeError(
            "missing Kubernetes API host; expected CA4S_K8S_HOST or K8S_AUTH_HOST"
        )
    if not token:
        raise RuntimeError(
            "missing Kubernetes bearer token; expected CA4S_K8S_BEARER_TOKEN, "
            "K8S_AUTH_BEARER_TOKEN, or K8S_AUTH_API_KEY"
        )
    return KubernetesAuth(
        host=host,
        token=token.removeprefix("Bearer ").strip(),
        ca_cert=_first_env(_CA_CERT_ENV),
        verify_ssl=_bool_env(_VERIFY_SSL_ENV, default=True),
    )


def _api_client(auth: KubernetesAuth) -> Any:
    from kubernetes import client

    configuration = client.Configuration()
    configuration.host = auth.host
    configuration.verify_ssl = auth.verify_ssl
    configuration.api_key = {"authorization": f"Bearer {auth.token}"}
    if auth.ca_cert:
        ca_file = tempfile.NamedTemporaryFile(mode="w", delete=False)
        ca_file.write(auth.ca_cert)
        ca_file.close()
        configuration.ssl_ca_cert = ca_file.name
    return client.ApiClient(configuration)


def _label_selector(args: argparse.Namespace) -> str | None:
    selectors: list[str] = []
    if args.cluster_name:
        selectors.append(f"{CLUSTER_NAME_LABEL}={args.cluster_name}")
    if args.label_selector:
        selectors.append(args.label_selector)
    return ",".join(selectors) or None


def _list_machines(api: Any, args: argparse.Namespace) -> list[dict[str, Any]]:
    label_selector = _label_selector(args)
    if args.all_namespaces:
        result = api.list_cluster_custom_object(
            CAPI_GROUP,
            CAPI_VERSION,
            MACHINE_PLURAL,
            label_selector=label_selector,
        )
    else:
        result = api.list_namespaced_custom_object(
            CAPI_GROUP,
            CAPI_VERSION,
            args.namespace,
            MACHINE_PLURAL,
            label_selector=label_selector,
        )
    return list(result.get("items", []))


def _sanitize_group(value: str) -> str:
    sanitized = re.sub(r"[^A-Za-z0-9_]", "_", value).strip("_").lower()
    return sanitized or "unknown"


def _address(machine: Mapping[str, Any], address_type: str) -> str | None:
    for address in machine.get("status", {}).get("addresses", []) or []:
        if address.get("type") == address_type and address.get("address"):
            return str(address["address"])
    return None


def _machine_hostname(machine: Mapping[str, Any]) -> str:
    metadata = machine.get("metadata", {})
    status = machine.get("status", {})
    node_ref = status.get("nodeRef") or {}
    return (
        _address(machine, "Hostname")
        or node_ref.get("name")
        or metadata.get("name")
        or "unknown"
    )


def _machine_hostvars(machine: Mapping[str, Any], *, node_type_label: str) -> dict[str, Any]:
    metadata = machine.get("metadata", {})
    labels = metadata.get("labels") or {}
    cluster_name = labels.get(CLUSTER_NAME_LABEL, "unknown")
    node_type = labels.get(node_type_label, "unknown")
    ansible_host = _address(machine, "ExternalIP") or _address(machine, "InternalIP")
    hostvars: dict[str, Any] = {
        "capi_cluster": cluster_name,
        "capi_machine": metadata.get("name"),
        "capi_namespace": metadata.get("namespace"),
        "node_type": node_type,
    }
    if ansible_host:
        hostvars["ansible_host"] = ansible_host
    return hostvars


def inventory_from_machines(
    machines: Iterable[Mapping[str, Any]],
    *,
    node_type_label: str = NODE_TYPE_LABEL,
    controller_node_type: str = CONTROLLER_NODE_TYPE,
    compute_node_type: str = COMPUTE_NODE_TYPE,
) -> dict[str, Any]:
    inventory: dict[str, Any] = {
        "_meta": {
            "hostvars": {
                "localhost": {
                    "ansible_connection": "local",
                    "ansible_python_interpreter": sys.executable,
                }
            }
        },
        "all": {"children": ["management", "slurm"]},
        "management": {"hosts": ["localhost"]},
        "slurm": {"children": []},
    }

    for machine in machines:
        metadata = machine.get("metadata", {})
        labels = metadata.get("labels") or {}
        node_type = labels.get(node_type_label)
        cluster_name = labels.get(CLUSTER_NAME_LABEL)
        if not node_type or not cluster_name:
            continue

        hostname = _machine_hostname(machine)
        hostvars = _machine_hostvars(machine, node_type_label=node_type_label)
        inventory["_meta"]["hostvars"][hostname] = hostvars

        cluster_group = f"cluster_{_sanitize_group(cluster_name)}"
        type_group = _sanitize_group(node_type)
        cluster_type_group = f"{type_group}_{_sanitize_group(cluster_name)}"

        inventory.setdefault(cluster_group, {"children": []})
        inventory.setdefault(type_group, {"children": []})
        inventory.setdefault(cluster_type_group, {"hosts": []})

        if cluster_group not in inventory["all"].setdefault("children", []):
            inventory["all"]["children"].append(cluster_group)
        if cluster_group not in inventory["slurm"].setdefault("children", []):
            inventory["slurm"]["children"].append(cluster_group)
        if cluster_type_group not in inventory[cluster_group].setdefault("children", []):
            inventory[cluster_group]["children"].append(cluster_type_group)
        if cluster_type_group not in inventory[type_group].setdefault("children", []):
            inventory[type_group]["children"].append(cluster_type_group)
        if type_group not in inventory["slurm"].setdefault("children", []):
            inventory["slurm"]["children"].append(type_group)
        if hostname not in inventory[cluster_type_group].setdefault("hosts", []):
            inventory[cluster_type_group]["hosts"].append(hostname)

        if node_type == controller_node_type:
            inventory.setdefault("controller", {"children": []})
            if cluster_type_group not in inventory["controller"].setdefault("children", []):
                inventory["controller"]["children"].append(cluster_type_group)
        if node_type == compute_node_type:
            inventory.setdefault("compute", {"children": []})
            if cluster_type_group not in inventory["compute"].setdefault("children", []):
                inventory["compute"]["children"].append(cluster_type_group)

    for group in inventory.values():
        if isinstance(group, dict):
            for key in ("children", "hosts"):
                if key in group:
                    group[key] = sorted(group[key])
    return inventory


def summary_from_machines(machines: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    inventory = inventory_from_machines(machines)
    hostvars = inventory["_meta"]["hostvars"]
    return {
        "clusters": sorted(
            {
                value["capi_cluster"]
                for key, value in hostvars.items()
                if key != "localhost" and value.get("capi_cluster")
            }
        ),
        "hosts": sorted(key for key in hostvars if key != "localhost"),
        "host_count": max(len(hostvars) - 1, 0),
        "groups": sorted(key for key in inventory if key != "_meta"),
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--list", action="store_true", help="emit AWX inventory JSON")
    parser.add_argument("--summary", action="store_true", help="emit a compact cluster summary")
    parser.add_argument("--host", help="emit hostvars for one host")
    parser.add_argument(
        "--namespace",
        default=os.environ.get("CA4S_CAPI_NAMESPACE", DEFAULT_NAMESPACE),
    )
    parser.add_argument("--all-namespaces", action="store_true")
    parser.add_argument("--cluster-name", default=os.environ.get("CA4S_CAPI_CLUSTER_NAME"))
    parser.add_argument("--label-selector", default=os.environ.get("CA4S_CAPI_LABEL_SELECTOR"))
    parser.add_argument(
        "--node-type-label",
        default=os.environ.get("CA4S_NODE_TYPE_LABEL", NODE_TYPE_LABEL),
    )
    parser.add_argument(
        "--controller-node-type",
        default=os.environ.get("CA4S_CONTROLLER_NODE_TYPE", CONTROLLER_NODE_TYPE),
    )
    parser.add_argument(
        "--compute-node-type",
        default=os.environ.get("CA4S_COMPUTE_NODE_TYPE", COMPUTE_NODE_TYPE),
    )
    return parser


def main() -> int:
    from kubernetes import client

    args = _parser().parse_args()
    auth = _load_auth_from_env()
    api = client.CustomObjectsApi(_api_client(auth))
    machines = _list_machines(api, args)
    inventory = inventory_from_machines(
        machines,
        node_type_label=args.node_type_label,
        controller_node_type=args.controller_node_type,
        compute_node_type=args.compute_node_type,
    )

    if args.host:
        hostvars = inventory["_meta"]["hostvars"].get(args.host, {})
        print(json.dumps(hostvars, indent=2, sort_keys=True))
    elif args.summary:
        print(json.dumps(summary_from_machines(machines), indent=2, sort_keys=True))
    else:
        print(json.dumps(inventory, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
