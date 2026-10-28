# Copyright (c) Microsoft Corporation.
# Licensed under the MIT license.

from __future__ import annotations

import json
import subprocess

import pytest

from stacks.workload_cluster.slurm_capacity import (
    CollectionError,
    Demand,
    calculate_demand,
    collect,
    render_metrics,
)


def number(value: int) -> dict:
    return {"set": True, "infinite": False, "number": value}


def partition(name="compute", maximum=None, state="UP") -> dict:
    return {
        "name": name,
        "maximums": {"nodes": number(maximum) if maximum is not None else {"infinite": True}},
        "partition": {"state": [state]},
    }


def job(**updates) -> dict:
    value = {
        "job_id": 1,
        "job_state": ["PENDING"],
        "partition": "compute",
        "state_reason": "Resources",
        "priority": number(1),
        "node_count": number(1),
        "required_nodes": "",
        "dependency": "",
        "array_job_id": number(0),
        "array_max_tasks": number(0),
        "array_task_string": "",
    }
    value.update(updates)
    return value


def node(name="worker-0", state=None, partitions=None) -> dict:
    return {
        "name": name,
        "state": state or ["ALLOCATED"],
        "partitions": partitions or ["compute"],
    }


@pytest.mark.parametrize(
    "busy,pending,expected",
    [(1, 1, 2), (3, 2, 5), (3, 0, 3), (0, 3, 3), (0, 0, 0)],
)
def test_busy_plus_pending_targets_total_capacity(busy, pending, expected):
    demand = calculate_demand(
        [job(job_id=i) for i in range(pending)],
        [node(f"worker-{i}") for i in range(busy)],
        [partition()], {"compute": 10},
    )["compute"]
    assert demand.busy == busy
    assert demand.pending == pending
    assert demand.busy + demand.pending == expected


def test_starting_queued_job_keeps_total_demand_constant():
    waiting = calculate_demand([job()], [node()], [partition()], {"compute": 10})
    running = calculate_demand(
        [job(job_state=["RUNNING"])], [node(), node("worker-1", ["MIXED"])],
        [partition()], {"compute": 10},
    )
    assert waiting["compute"].busy + waiting["compute"].pending == 2
    assert running["compute"].busy + running["compute"].pending == 2


def test_busy_nodes_are_unique_not_running_job_counts():
    demand = calculate_demand(
        [job(job_id=i, job_state=["RUNNING"]) for i in range(5)],
        [node(state=["ALLOCATED", "COMPLETING"]), node(),
         node("mixed", ["MIXED"]), node("idle", ["IDLE"])],
        [partition()], {"compute": 10},
    )["compute"]
    assert demand.busy == 2
    assert demand.pending == 0


def test_multinode_jobs_add_minimum_node_requests():
    demand = calculate_demand(
        [job(node_count=number(4)), job(job_id=2, node_count=number(2))],
        [node()], [partition()], {"compute": 10},
    )["compute"]
    assert demand.busy + demand.pending == 7


@pytest.mark.parametrize("reason", [
    "JobHeldUser", "JobHeldAdmin", "Dependency", "DependencyNeverSatisfied",
    "Licenses", "BadConstraints", "PartitionTimeLimit", "QOSMaxJobsPerUserLimit",
    "BeginTime", "ReqNodeNotAvail", "None",
])
def test_blocked_or_not_yet_evaluated_jobs_do_not_add_capacity(reason):
    demand = calculate_demand(
        [job(state_reason=reason)], [], [partition()], {"compute": 10},
    )["compute"]
    assert demand.pending == 0
    assert demand.excluded == {reason: 1}


@pytest.mark.parametrize("updates", [
    {"priority": number(0)}, {"dependency": "afterok:42"},
])
def test_hold_and_dependency_are_checked_even_with_capacity_reason(updates):
    demand = calculate_demand([job(**updates)], [], [partition()], {"compute": 10})
    assert demand["compute"].pending == 0


@pytest.mark.parametrize("reason", ["Resources", "Priority", "PartitionNodeLimit"])
def test_valid_node_shortages_are_included(reason):
    demand = calculate_demand(
        [job(state_reason=reason, node_count=number(2))], [node()],
        [partition()], {"compute": 10},
    )
    assert demand["compute"].pending == 2


@pytest.mark.parametrize("maximum,limit,state", [(1, 10, "UP"), (None, 1, "UP"), (None, 10, "DOWN")])
def test_unsatisfiable_partition_limits_do_not_trigger_provisioning(maximum, limit, state):
    demand = calculate_demand(
        [job(state_reason="PartitionNodeLimit", node_count=number(2))], [],
        [partition(maximum=maximum, state=state)], {"compute": limit},
    )["compute"]
    assert demand.pending == 0
    assert demand.excluded == {"PartitionLimit": 1}


def test_named_node_request_cannot_be_satisfied_by_new_workers():
    demand = calculate_demand(
        [job(required_nodes="worker-0")], [], [partition()], {"compute": 10},
    )["compute"]
    assert demand.pending == 0
    assert demand.excluded == {"RequiredNodes": 1}


def test_multiple_partitions_do_not_duplicate_one_job():
    demand = calculate_demand(
        [job(partition="compute,gpu")],
        [node("gpu-0", partitions=["gpu"])],
        [partition(), partition("gpu")], {"compute": 10, "gpu": 10},
    )
    assert demand["compute"].pending == 1
    assert demand["gpu"].pending == 0
    assert demand["gpu"].busy == 1


@pytest.mark.parametrize(
    "expression,concurrency,active,expected",
    [("1-3%2", 2, 0, 2), ("1-3%2", 2, 1, 1),
     ("1-3%2", 2, 2, 0), ("1-5:2,8,10-11", 0, 0, 6)],
)
def test_pending_arrays_respect_concurrency(expression, concurrency, active, expected):
    jobs = [
        job(array_job_id=number(42), array_task_string=expression,
            array_max_tasks=number(concurrency)),
        *[job(job_id=i, job_state=["RUNNING"], array_job_id=number(42)) for i in range(active)],
    ]
    demand = calculate_demand(jobs, [], [partition()], {"compute": 10})
    assert demand["compute"].pending == expected


def test_expanded_array_tasks_share_one_concurrency_budget():
    jobs = [job(job_id=i, array_job_id=number(42), array_max_tasks=number(2)) for i in range(4)]
    assert calculate_demand(jobs, [], [partition()], {"compute": 10})["compute"].pending == 2


@pytest.mark.parametrize("count", [
    {"set": False, "infinite": False, "number": 0},
    {"set": True, "infinite": True, "number": 0},
    number(0), number(-1), number(1.5),
])
def test_invalid_eligible_requests_fail_instead_of_reporting_zero(count):
    with pytest.raises(CollectionError):
        calculate_demand([job(node_count=count)], [], [partition()], {"compute": 10})


def test_missing_partition_fails_collection():
    with pytest.raises(CollectionError, match="partitions are missing"):
        calculate_demand([], [], [], {"compute": 10})


@pytest.mark.parametrize("failure", [
    subprocess.TimeoutExpired(["squeue"], 5),
    subprocess.CalledProcessError(1, ["squeue"]),
    OSError("squeue unavailable"),
])
def test_command_failures_are_not_zero_demand(monkeypatch, failure):
    def fail(*args, **kwargs):
        raise failure

    monkeypatch.setattr(subprocess, "run", fail)
    expected_error = CollectionError if isinstance(failure, subprocess.CalledProcessError) else type(failure)
    with pytest.raises(expected_error):
        collect({"compute": 10})


@pytest.mark.parametrize("response", [{"errors": [{"description": "Slurm unavailable"}]}, {"jobs": {}}])
def test_slurm_errors_and_invalid_schema_are_not_zero_demand(monkeypatch, response):
    monkeypatch.setattr(
        subprocess, "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(args, 0, json.dumps(response)),
    )
    with pytest.raises(CollectionError):
        collect({"compute": 10})


def test_metrics_expose_capacity_and_exclusion_reason():
    demand = Demand(busy=2, pending=3)
    demand.excluded["Dependency"] = 1
    metrics = render_metrics({"compute": demand})
    assert 'ca4s_slurm_busy_workers{partition="compute"} 2\n' in metrics
    assert 'ca4s_slurm_pending_workers{partition="compute"} 3\n' in metrics
    assert 'ca4s_slurm_worker_demand{partition="compute"} 5\n' in metrics
    assert 'ca4s_slurm_excluded_pending_jobs{partition="compute",reason="Dependency"} 1\n' in metrics
    assert 'ca4s_slurm_worker_demand{partition="compute"} 0\n' in render_metrics({"compute": Demand()})
