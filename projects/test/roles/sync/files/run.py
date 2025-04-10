#!/usr/bin/python3.11
import os
import tempfile

current_dir = os.path.dirname(os.path.abspath(__file__))
ping_yml_path = os.path.join(current_dir, 'ping.yml')

with tempfile.TemporaryDirectory() as tempdir:
    # run ansible-playbook cli
    pass
