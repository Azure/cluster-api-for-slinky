from __future__ import annotations

import json

from stacks.workload_cluster.workload_cluster_storage import (
    _local_path_config_data,
)


def test_local_path_config_uses_node_local_stopgap_storage() -> None:
    data = _local_path_config_data()
    config = json.loads(data["config.json"])

    assert config == {
        "nodePathMap": [
            {
                "node": "DEFAULT_PATH_FOR_NON_LISTED_NODES",
                "paths": ["/opt/local-path-provisioner"],
            }
        ]
    }
    assert 'mkdir -m 0777 -p "$VOL_DIR"' in data["setup"]
    assert 'rm -rf "$VOL_DIR"' in data["teardown"]