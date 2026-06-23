#!/usr/bin/python3.11

# Copyright (c) Microsoft Corporation.
# Licensed under the MIT license.

import os
import subprocess
import tempfile
import yaml
import json

current_dir = os.path.dirname(os.path.abspath(__file__))
sync_yml_path = os.path.join(current_dir, 'sync.yml')

with tempfile.TemporaryDirectory() as tempdir:
    # copy the /runner/.kube/config file to tempdir
    kube_config_path = os.path.join(tempdir, 'config')
    subprocess.run(
        [
            'cp',
            '/runner/.kube/config',
            kube_config_path,
        ],
        stdout=subprocess.PIPE,
    )
    # replace all occurences of 127.0.0.1 with host.docker.internal
    # TODO: this is a kind-specific hack, generalize this
    with open(kube_config_path, 'r') as file:
        content = file.read()
    
    modified_content = content.replace('127.0.0.1', 'host.docker.internal')
    
    with open(kube_config_path, 'w') as file:
        file.write(modified_content)

    # run ansible-playbook cli

    result = subprocess.run(
        [
            'ansible-playbook',
            sync_yml_path,
            '-i',
            'localhost,',
            # TODO: find a way to avoid hardcoding this
            '-e',
            'ansible_python_interpreter=/usr/bin/python3.11',
            '-e',
            f'output_path={tempdir}',
        ],
        env=dict(
            os.environ,
            # TODO: this is a kind-specific hack to get around the cert issue for host.docker.internal
            K8S_AUTH_VERIFY_SSL="false",
            K8S_AUTH_KUBECONFIG=kube_config_path,
        ),
        stdout=subprocess.PIPE,
    )

    with open(os.path.join(tempdir, 'output.yaml'), 'rb') as temp_file:
        # parse temp_file as yaml
        data = yaml.safe_load(temp_file)
        # for dev purposes on CAPD clusters, we take the following simplifying assumptions:
        # - only 1 node under the MachineDeployment/MachineSet, acting as the slurm scheduler node
        # - only 1 `MachinePool`, under which all nodes are slurm compute nodes
        scheduler_nodes = [node for node in data['resources'] if any(r['kind'] == 'MachineSet' for r in node['metadata']['ownerReferences'])]
        compute_nodes = [node for node in data['resources'] if any(r['kind'] == 'MachinePool' for r in node['metadata']['ownerReferences'])]
        ip = lambda x: [addr['address'] for addr in x['status']['addresses'] if addr['type'] == 'ExternalIP'][0]
        hostname = lambda x: [addr['address'] for addr in x['status']['addresses'] if addr['type'] == 'Hostname'][0]
        # https://docs.ansible.com/ansible-tower/latest/html/administration/scm-inv-source.html#custom-dynamic-inventory-scripts
        output_json = {
            "_meta": {
                "hostvars": {
                    hostname(node): {"ansible_host": ip(node)} for node in scheduler_nodes + compute_nodes
                }
            },
            "all": {
                "children": ["compute", "scheduler"],
            },
            "scheduler": {
                "hosts": [hostname(node) for node in scheduler_nodes],
            },
            "compute": {
                "hosts": [hostname(node) for node in compute_nodes],
            },
        }
        print(json.dumps(output_json, indent=2))
