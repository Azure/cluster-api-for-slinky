# Cluster API Provider HPC

Kubernetes-native declarative infrastructure for [Slinky](https://github.com/SlinkyProject)-based converged Slurm and Kubernetes clusters.

## What is the Cluster API Provider HPC (CAPH)

The [Cluster API](https://github.com/kubernetes-sigs/cluster-api) (CAPI) brings declarative, Kubernetes-style APIs to cluster creation, configuration and management.

CAPZ enables efficient management at scale of Slinky-based converged Slurm and Kubernetes clusters, with the nodes dual-managed by both Ansible AWX for Slurm workloads and CAPI providers (Cluster API Provider Docker/Azure/vCluster/etc.) for containerized workloads on Kubernetes, with slurm-bridge bridging Slurm and Kubernetes for fair-share scheduling across both orchestrators.

![CAPH architecture](docs/images/architecture.svg)

## Getting started

Currently only local clusters powered by Cluster API Provider Docker (CAPD) is supported.

Create a local Kubernetes cluster as the management cluster.

```bash
envsubst < kind-config.yaml | kind create cluster --config -
kubectl cluster-info --context kind-kind
```

To work around an AWX missing feature regarding using manual projects (see [this](https://github.com/ansible/awx/issues/1288) and [this](https://forum.ansible.com/t/how-to-import-inventory-files-from-ansible/28655/7)), we need to install a Git server, e.g. Gitea. set your own administrator username (cannot be `admin`), password and email in the following `values.yaml`:

```yaml
redis-cluster:
  enabled: false
redis:
  enabled: false
postgresql:
  enabled: false
postgresql-ha:
  enabled: false

persistence:
  enabled: false

gitea:
  config:
    database:
      DB_TYPE: sqlite3
    session:
      PROVIDER: memory
    cache:
      ADAPTER: memory
    queue:
      TYPE: level
  admin:
    username: "your username"
    password: "your password"
    email: "your email"
```

Then deploy Gitea using this `values.yaml`:

```bash
helm repo add gitea-charts https://dl.gitea.com/charts/
helm install gitea gitea-charts/gitea -f values.yaml
```

Upload your cluster-api-provider-hpc repo into this Gitea instance.

Install AWX Operator.

```bash
kubectl apply -f https://raw.githubusercontent.com/kubernetes/ingress-nginx/main/deploy/static/provider/kind/deploy.yaml
helm repo add awx-operator https://ansible-community.github.io/awx-operator-helm/
helm install my-awx-operator awx-operator/awx-operator -n awx --create-namespace -f awx.yaml
# display your AWX admin password
kubectl get secret awx-admin-password -n awx -o jsonpath="{.data.password}" | base64 --decode ; echo
```

Using your AWX admin username and password, log in to AWX portal at `localhost:32000`.

Go to `Resources -> Credentials` and add your Gitea username and password as a credential of type `Source Control`.

Go to `Resources -> Projects` and add a project of Source Control Type `Git`. The Source Control URL should be `http://host.docker.internal:3000/<your gitea admin username>/cluster-api-provider-hpc.git`, with appropriate branch/tag/commit name and Source Control Credential pointing to the credential you added in the last step.

Go to `Resources -> Inventories` and add an inventory. Go to `Sources` of this inventory, add an inventory source `Sourced from a project`, and choose the Project you added in the last step, with Inventory file set as `projects/test/roles/sync/files/run.py`. For Update options, select `Overwrite` and `Update on launch`.

Go to `Resources -> Templates` and add a job template, with Job Type of `Run`, Inventory and project set to what you created in previous steps, and Playbook set to `projects/test/run.yml`.

Go to `Resources -> Credentials` and add another credential of type `Machine`, containing your SSH private key (and passphrase if you have any). This SSH Key would be used by Ansible to access pseudo-nodes bootstrapped by CAPD.

Install [CAPD](https://github.com/kubernetes-sigs/cluster-api/blob/main/test/infrastructure/docker/README.md).
```bash
export CLUSTER_TOPOLOGY=true
clusterctl init --infrastructure docker
```

open `capi-quickstart.yaml` and modify the SSH public key (corresponding to the private key in AWX) inside `preKubeCommands` (around line 329).

Create the CAPD cluster. wait for the cluster to be ready.
```bash
kubectl apply -f capi-quickstart.yaml
clusterctl get kubeconfig capi-quickstart > capi-quickstart.kubeconfig
# https://cluster-api.sigs.k8s.io/clusterctl/developers#fix-kubeconfig-when-using-docker-desktop-and-clusterctl
# Point the kubeconfig to the exposed port of the load balancer, rather than the inaccessible container IP.
sed -i -e "s/server:.*/server: https:\/\/$(docker port capi-quickstart-lb 6443/tcp | sed "s/0.0.0.0/127.0.0.1/")/g" ./capi-quickstart.kubeconfig
# CAPD nodes won't be ready until we install CNI
kubectl --kubeconfig=./capi-quickstart.kubeconfig apply -f https://raw.githubusercontent.com/projectcalico/calico/v3.29.3/manifests/calico.yaml
```

Run the job in AWX. The job should now output the hostnames of all pseudo-nodes of the CAPD cluster (which are Docker containers under the hood), and the inventory's host list should also contain those hosts. This means that AWX is now aware of these nodes and Ansible can bootstrap Slurm/SSSD/Storage/networking/etc.

## Contributing

This project welcomes contributions and suggestions.  Most contributions require you to agree to a
Contributor License Agreement (CLA) declaring that you have the right to, and actually do, grant us
the rights to use your contribution. For details, visit https://cla.opensource.microsoft.com.

When you submit a pull request, a CLA bot will automatically determine whether you need to provide
a CLA and decorate the PR appropriately (e.g., status check, comment). Simply follow the instructions
provided by the bot. You will only need to do this once across all repos using our CLA.

This project has adopted the [Microsoft Open Source Code of Conduct](https://opensource.microsoft.com/codeofconduct/).
For more information see the [Code of Conduct FAQ](https://opensource.microsoft.com/codeofconduct/faq/) or
contact [opencode@microsoft.com](mailto:opencode@microsoft.com) with any additional questions or comments.

## Trademarks

This project may contain trademarks or logos for projects, products, or services. Authorized use of Microsoft 
trademarks or logos is subject to and must follow 
[Microsoft's Trademark & Brand Guidelines](https://www.microsoft.com/en-us/legal/intellectualproperty/trademarks/usage/general).
Use of Microsoft trademarks or logos in modified versions of this project must not cause confusion or imply Microsoft sponsorship.
Any use of third-party trademarks or logos are subject to those third-party's policies.
