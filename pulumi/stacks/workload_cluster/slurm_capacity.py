# Copyright (c) Microsoft Corporation.
# Licensed under the MIT license.

"""Expose Slurm worker demand using the controller image's CLI and Python."""

from __future__ import annotations

import argparse
from collections import Counter
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import logging
import re
import subprocess
from typing import Any


_BUSY_STATES = {"ALLOCATED", "MIXED", "COMPLETING"}
_CAPACITY_REASONS = {"Resources", "Priority", "PartitionNodeLimit"}
_LOG = logging.getLogger(__name__)


class CollectionError(ValueError):
    """Slurm returned an invalid or unsuccessful capacity snapshot."""


@dataclass
class Demand:
    busy: int = 0
    pending: int = 0
    excluded: Counter[str] = field(default_factory=Counter)


def _number(value: Any) -> int:
    if not isinstance(value, dict) or not value.get("set") or value.get("infinite"):
        raise CollectionError(f"expected a finite Slurm number, got {value!r}")
    number = value.get("number")
    if type(number) is not int or number < 0:
        raise CollectionError(f"expected a nonnegative Slurm integer, got {number!r}")
    return number


def _array_size(expression: str) -> int:
    tasks = expression.split("%", 1)[0]
    count = 0
    for part in tasks.split(","):
        match = re.fullmatch(r"(\d+)(?:-(\d+)(?::([1-9]\d*))?)?", part)
        if match is None:
            raise CollectionError(f"invalid Slurm array task expression: {expression!r}")
        start = int(match[1])
        end = int(match[2]) if match[2] is not None else start
        if end < start:
            raise CollectionError(f"invalid Slurm array task range: {expression!r}")
        count += (end - start) // int(match[3] or 1) + 1
    return count


def _array_id(job: dict[str, Any]) -> int:
    return _number(job["array_job_id"])


def calculate_demand(
    jobs: list[dict[str, Any]],
    nodes: list[dict[str, Any]],
    partitions: list[dict[str, Any]],
    limits: dict[str, int],
) -> dict[str, Demand]:
    result = {name: Demand() for name in limits}
    partition_info = {partition["name"]: partition for partition in partitions}
    missing = limits.keys() - partition_info.keys()
    if missing:
        raise CollectionError(f"configured Slurm partitions are missing: {sorted(missing)}")
    busy = {name: set() for name in limits}
    for node in nodes:
        if _BUSY_STATES.intersection(node["state"]):
            for name in set(node["partitions"]).intersection(limits):
                busy[name].add(node["name"])
    for name in result:
        result[name].busy = len(busy[name])

    active_arrays: Counter[int] = Counter()
    for job in jobs:
        if {"RUNNING", "CONFIGURING", "COMPLETING"}.intersection(job["job_state"]):
            active_arrays[_array_id(job)] += 1
    array_remaining: dict[int, int] = {}
    for job in jobs:
        if "PENDING" not in job["job_state"]:
            continue
        candidates = [name for name in job["partition"].split(",") if name in limits]
        if not candidates:
            continue
        count = _array_size(job["array_task_string"]) if job["array_task_string"] else 1
        reason = job["state_reason"]
        if reason not in _CAPACITY_REASONS or job["dependency"] or _number(job["priority"]) == 0:
            result[candidates[0]].excluded[reason] += count
            continue
        if job["required_nodes"]:
            result[candidates[0]].excluded["RequiredNodes"] += count
            continue
        requested = _number(job["node_count"])
        if requested == 0:
            raise CollectionError(f"pending job {job['job_id']} requests zero nodes")
        selected = None
        for name in candidates:
            partition = partition_info[name]
            maximum = partition["maximums"]["nodes"]
            maximum_nodes = limits[name] if maximum.get("infinite") else _number(maximum)
            if "UP" in partition["partition"]["state"] and requested <= min(limits[name], maximum_nodes):
                selected = name
                break
        if selected is None:
            result[candidates[0]].excluded["PartitionLimit"] += count
            continue
        array = _array_id(job)
        concurrency = _number(job["array_max_tasks"])
        if array and concurrency:
            available = array_remaining.setdefault(array, max(0, concurrency - active_arrays[array]))
            count = min(count, available)
            array_remaining[array] -= count
        result[selected].pending += count * requested
    return result


def _read_slurm(command: list[str], key: str) -> list[dict[str, Any]]:
    output = subprocess.run(
        command, check=True, capture_output=True, text=True, timeout=5,
    )
    data = json.loads(output.stdout)
    if not isinstance(data, dict):
        raise CollectionError(f"{command[0]} returned a non-object JSON response")
    if data.get("errors"):
        raise CollectionError(f"{command[0]} returned Slurm errors: {data['errors']!r}")
    records = data.get(key)
    if not isinstance(records, list):
        raise CollectionError(f"{command[0]} response is missing {key!r}")
    return records


def collect(limits: dict[str, int]) -> dict[str, Demand]:
    jobs = _read_slurm(["squeue", "--all", "--json"], "jobs")
    nodes = _read_slurm(["scontrol", "show", "nodes", "--json"], "nodes")
    partitions = _read_slurm(["scontrol", "show", "partitions", "--json"], "partitions")
    try:
        return calculate_demand(jobs, nodes, partitions, limits)
    except (KeyError, TypeError) as error:
        raise CollectionError(f"unexpected Slurm JSON schema: {error}") from error


def render_metrics(demand: dict[str, Demand]) -> str:
    lines = []
    for metric, help_text, value in (
        ("busy_workers", "Distinct allocated, mixed or completing Slurm workers.", lambda item: item.busy),
        ("pending_workers", "Node requests of eligible pending Slurm jobs.", lambda item: item.pending),
        ("worker_demand", "Busy workers plus eligible pending worker demand.", lambda item: item.busy + item.pending),
    ):
        name = f"ca4s_slurm_{metric}"
        lines += [f"# HELP {name} {help_text}", f"# TYPE {name} gauge"]
        for partition, item in sorted(demand.items()):
            labels = '{partition=' + json.dumps(partition) + '}'
            lines.append(f"{name}{labels} {value(item)}")
    name = "ca4s_slurm_excluded_pending_jobs"
    lines += [f"# HELP {name} Pending jobs excluded from capacity demand.", f"# TYPE {name} gauge"]
    for partition, item in sorted(demand.items()):
        for reason, count in sorted(item.excluded.items()):
            labels = '{partition=' + json.dumps(partition) + ',reason=' + json.dumps(reason) + '}'
            lines.append(f"{name}{labels} {count}")
    return "\n".join(lines) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--limits", required=True, help="JSON partition-to-max-workers map")
    args = parser.parse_args()
    limits = json.loads(args.limits)
    if not isinstance(limits, dict) or not limits or any(
        not isinstance(name, str) or not name or type(value) is not int or value < 1
        for name, value in limits.items()
    ):
        parser.error("--limits must contain partition names and positive integer limits")
    logging.basicConfig(level=logging.INFO)

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            if self.path == "/healthz":
                self.send_response(200)
                self.end_headers()
                return
            if self.path != "/metrics":
                self.send_error(404)
                return
            try:
                body = render_metrics(collect(limits)).encode()
            except (CollectionError, OSError, subprocess.SubprocessError, json.JSONDecodeError):
                _LOG.exception("Slurm capacity collection failed")
                self.send_error(503, "Slurm capacity collection failed")
                return
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; version=0.0.4; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    ThreadingHTTPServer(("0.0.0.0", 9091), Handler).serve_forever()


if __name__ == "__main__":
    main()
