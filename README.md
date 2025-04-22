# Cluster API Provider HPC

Kubernetes-native declarative infrastructure for [Slinky](https://github.com/SlinkyProject)-based hybrid Slurm and Kubernetes clusters.

## What is the Cluster API Provider HPC (CAPH)

The [Cluster API](https://github.com/kubernetes-sigs/cluster-api) (CAPI) brings declarative, Kubernetes-style APIs to cluster creation, configuration and management.

CAPZ enables efficient management at scale of Slinky-based hybrid Slurm and Kubernetes clusters, with the nodes dual-managed by both Ansible AWX for Slurm workloads and CAPI providers (Cluster API Provider Docker/Azure/vCluster/etc.) for containerized workloads on Kubernetes, with slurm-bridge bridging Slurm and Kubernetes for fair-share scheduling across both orchestrators.

![CAPH architecture](docs/images/architecture.svg)

## Getting started

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
