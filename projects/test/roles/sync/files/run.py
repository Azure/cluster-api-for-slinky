#!/usr/bin/python3.11
import os
import subprocess
import tempfile

current_dir = os.path.dirname(os.path.abspath(__file__))
ping_yml_path = os.path.join(current_dir, 'ping.yml')

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
            ping_yml_path,
            '-i',
            'localhost,',
            # TODO: find a way to avoid hardcoding this
            '-e',
            'ansible_python_interpreter=/usr/bin/python3.11',
        ],
        env=dict(
            os.environ,
            # TODO: this is a kind-specific hack to get around the cert issue for host.docker.internal
            K8S_AUTH_VERIFY_SSL="false",
            K8S_AUTH_KUBECONFIG=kube_config_path,
        ),
        stdout=subprocess.PIPE,

    )
    # result = subprocess.run(
    #     [
    #         'ls',
    #         '-al',
    #         '/runner/.kube',
    #     ], stdout=subprocess.PIPE,
    # )
    print(result.stdout.decode('utf-8'))
