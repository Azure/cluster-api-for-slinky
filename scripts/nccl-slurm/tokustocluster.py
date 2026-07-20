#!/usr/bin/env python3
"""
Upload NCCL/benchmark telemetry JSON to Azure Data Explorer (Kusto).

Ported verbatim from hpc-image-val2 headnode/tokustocluster.py (logic unchanged;
it is already tested and understands the CommType_s="nccl" dashboard schema that
process-nccl-slurm.sh emits). Only this header was adapted for the CAPS flow.

Usage:
    python tokustocluster.py <cluster> <database> <table_name> <json_file> \
        [--client-id <client_id>] [--error | --ghr]

Example:
    python tokustocluster.py mycluster mydb ImagePerf nccl_telemetry/allreduce_123.json \
        --client-id xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx

Identity split (Zheyu open item):
  * --client-id is the REPORTING/telemetry managed identity, used only to ingest
    into Kusto (ManagedIdentityCredential(client_id=...)). It is DISTINCT from the
    infra-creation UAMI that Pulumi/CAPZ uses to create the workload VMs.
  * With no --client-id it falls back to AzureCliCredential for local dev.
  * Never hardcode the client id — pass it at runtime (KUSTO_CLIENT_ID in
    process-nccl-slurm.sh). The reporting MI needs Kusto DB ingest RBAC.
  * TODO(caps): document how the reporting MI is provisioned vs the infra UAMI.

Requires (install on the runner, NOT the Pulumi venv):
    pip install pandas azure-identity azure-kusto-data azure-kusto-ingest
"""

import sys
import json
import os
import argparse

from datetime import datetime, timezone
import pandas as pd
from azure.kusto.data import KustoConnectionStringBuilder, DataFormat
from azure.kusto.ingest import QueuedIngestClient, IngestionProperties
from azure.identity import ManagedIdentityCredential


def load_json_file(json_file: str) -> dict:
    if not os.path.exists(json_file):
        print(f"##[error]JSON file {json_file} not found")
        sys.exit(1)

    if os.path.getsize(json_file) == 0:
        print(f"##[error]JSON file {json_file} is empty")
        sys.exit(1)

    with open(json_file, "r", encoding="utf-8") as f:
        return json.load(f)


def flatten_json(raw: dict) -> dict:
    payload = {
        "TimeGenerated": datetime.now(timezone.utc).isoformat(),
        "ResourceGroup": raw.get("ResourceGroup"),
        "Timestamp_s": raw.get("Timestamp"),
        "InitTimestamp_s": raw.get("InitTimestamp"),
        "CommType_s": raw.get("CommType"),
        "TestType_s": raw.get("TestType"),
        "Benchmark_s": raw.get("Benchmark"),
        "Processes_s": raw.get("Processes"),
        "Image_s": raw.get("Image"),
        "Location_s": raw.get("Location"),
        "VmType_s": raw.get("VmType"),
        "VmId_g": raw.get("VmId"),
        "HWInfo_s": raw.get("HWInfo"),
        "communicator_version_s": raw["communicator"].get("version"),
    }

    data = raw.get("communicator", {}).get("data", {})
    for key in ["iteration_1", "iteration_2", "iteration_3", "iteration_4", "iteration_5", "iteration_avg"]:
        if key in data:
            payload[f"communicator_data_{key}_s"] = data[key]

    return payload


def flatten_error_json(raw: dict) -> dict:
    payload = {
        "TimeGenerated": datetime.now(timezone.utc).isoformat(),
        "Code_s": raw.get("Code"),
        "Message": raw.get("Message"),
        "Timestamp_s": raw.get("Timestamp"),
        "Compute_s": raw.get("Compute"),
    }
    return payload


def flatten_ghr_json(raw: dict) -> dict:
    """Flatten GHR fault nodes JSON to Kusto format."""
    payload = {
        "TimeGenerated": datetime.now(timezone.utc).isoformat(),
        "PipelineId": raw.get("pipelineId"),
        "VmIp": raw.get("vmIp"),
        "ImpactCategory": raw.get("impactCategory"),
        "ImpactDescription": raw.get("impactDescription"),
        "PhysicalHostName": raw.get("physicalHostName"),
        "WorkloadImpactResponse": raw.get("workloadImpactResponse"),
    }
    return payload


def upload_to_kusto(cluster: str, database: str, client_id: str, table_name: str, json_file: str, data_type: str = "telemetry"):
    print(f"##[debug]Kusto Cluster: {cluster}")
    print(f"##[debug]Kusto Database: {database}")
    print(f"##[debug]Kusto Table name: {table_name}")
    print(f"##[debug]Mode: {data_type}")
    print(f"##[section]Uploading {json_file} to Azure Data Explorer cluster {cluster}")

    raw = load_json_file(json_file)
    print(f"##[debug]{json.dumps(raw, indent=2)}")

    if data_type == "error":
        payload = flatten_error_json(raw)
    elif data_type == "ghr":
        payload = flatten_ghr_json(raw)
    else:
        payload = flatten_json(raw)

    df = pd.DataFrame([payload])

    if client_id:
        credential = ManagedIdentityCredential(client_id=client_id)
    else:
        from azure.identity import AzureCliCredential
        credential = AzureCliCredential()
    kcsb = KustoConnectionStringBuilder.with_azure_token_credential(f"https://{cluster}.kusto.windows.net", credential)
    ingest_client = QueuedIngestClient(kcsb)

    ingestion_props = IngestionProperties(
        database=database,
        table=table_name.replace("-", "_")  # Replace hyphens with underscores like original script
    )

    # Ingest data
    ingest_client.ingest_from_dataframe(df, ingestion_properties=ingestion_props)

    print(f"##[section]Ingestion request sent to Kusto table: {table_name}")


def main():
    parser = argparse.ArgumentParser(
        description="Upload JSON data to Azure Data Explorer (Kusto)"
    )
    parser.add_argument("cluster", help="Kusto endpoint name")
    parser.add_argument("database", help="Kusto database name")
    parser.add_argument("table_name", help="Kusto table name")
    parser.add_argument("json_file", help="Path to JSON file")
    parser.add_argument("--client-id", dest="client_id", default=None, help="Managed Identity client ID for authentication (optional)")
    data_type_group = parser.add_mutually_exclusive_group()
    data_type_group.add_argument(
        "-e", "--error",
        action="store_true",
        help="Upload as error message"
    )
    data_type_group.add_argument(
        "-g", "--ghr",
        action="store_true",
        help="Upload as GHR fault nodes data"
    )

    args = parser.parse_args()

    # Determine data type
    if args.error:
        data_type = "error"
    elif args.ghr:
        data_type = "ghr"
    else:
        data_type = "telemetry"

    upload_to_kusto(
        cluster=args.cluster,
        database=args.database,
        client_id=args.client_id,
        table_name=args.table_name,
        json_file=args.json_file,
        data_type=data_type
    )


if __name__ == "__main__":
    main()
