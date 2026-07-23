#!/usr/bin/env bash
#
# vm-bootstrap.sh - bring up the CAPS management VM from a bare Ubuntu box and run
# `pulumi up`. Milestone-2 provisioning: this runs ON the per-run VM that the
# pipeline creates INSIDE the run's resource group. It installs the caps-pulumi
# toolchain (Docker, Go, kind, cloud-provider-kind, ctlptl, Pulumi, kubectl/helm),
# checks out caps-pulumi, sets up the venv, then runs `pulumi up`.
#
# Pulumi discovers + reuses THIS VM's resource group via IMDS
# (useDiscoveredResourceGroup: true), so the workload cluster lands in the one
# tracked RG and `az group delete <rg>` (in the pipeline's Cleanup job) is the
# catch-all teardown -- it removes this VM + kind + all workload resources.
#
# Runs as ROOT (from cloud-init or `az vm run-command invoke`).
#
# Env (all optional; defaults shown):
#   REPO_URL       git URL for caps-pulumi (default the GitHub repo).
#   REPO_BRANCH    branch to check out (default capz-phase1-dev).
#   REPO_DIR       checkout dir (default /opt/caps-pulumi).
#   SKIP_SETUP_REPO  when set, skip the git clone/fetch (REPO_DIR is already populated,
#                  e.g. scp'd from the pipeline agent that checked out the private repo).
#   GIT_TOKEN      token for a private/https clone (x-access-token). Optional.
#   PULUMI_STACK   Pulumi stack to update (default azurebyo).
#   TENANT_NAME    workload-cluster tenant key in Pulumi.<stack>.yaml (default caps-self).
#   CONTROL_PLANE_VM_SIZE / WORKER_VM_SIZE / WORKER_REPLICAS
#                  OPTIONAL per-run overrides for the tenant's control-plane / worker
#                  SKUs + worker count. Unset => the values committed in
#                  Pulumi.<stack>.yaml are used. `pulumi config set --path` type-infers
#                  the replica count as an int (StrictPositiveInt in the config model).
#   GO_VERSION     Go toolchain to install (default 1.23.4).
#   ORAS_VERSION   oras CLI version for the capz-artifact OCI build (default 1.2.0).
#   CAPZ_FORK_URL / CAPZ_FORK_BRANCH / CAPZ_FORK_DIR
#                  public CAPZ VMSS-Flex fork checked out for the Pulumi customImages build;
#                  CAPZ_FORK_DIR must match Pulumi.<stack>.yaml sourcePath (/home/azureuser/capz-vmss-flex).
#   PULUMI_CONFIG_PASSPHRASE   default empty (matches the current mgmt VM).
#   ARM_CLIENT_ID / ARM_CLIENT_SECRET / ARM_TENANT_ID / ARM_SUBSCRIPTION_ID
#                  OPTIONAL service-principal override. Normally UNSET: the VM's
#                  attached UAMI (Option A) is used via IMDS. Only set these to force
#                  service-principal auth instead of the managed identity.
#
# TODO(caps): Milestone-2 first cut. Confirm before wiring live:
#   * base image (ubuntu 24.04 LTS gen2), VM SKU, disk size, egress/NAT for pulls;
#   * IDENTITY = attached UAMI (Zheyu chose Option A): the pipeline creates the VM
#     with `az vm create --assign-identity <UAMI>`, so Pulumi/CAPZ authenticate with
#     the VM's managed identity via IMDS (and useDiscoveredResourceGroup reads the RG
#     name from IMDS instance metadata). Leave the ARM_* env below UNSET normally.
#   * where the caps-pulumi checkout comes from (GitHub clone here vs. pre-baked).
set -euo pipefail

log() { printf '\n\033[1;36m== %s\033[0m\n' "$*"; }

REPO_URL="${REPO_URL:-https://github.com/Azure/cluster-api-provider-for-slinky.git}"
REPO_BRANCH="${REPO_BRANCH:-capz-phase1-dev}"
REPO_DIR="${REPO_DIR:-/opt/caps-pulumi}"
GIT_TOKEN="${GIT_TOKEN:-}"
PULUMI_STACK="${PULUMI_STACK:-azurebyo}"
TENANT_NAME="${TENANT_NAME:-caps-self}"
CONTROL_PLANE_VM_SIZE="${CONTROL_PLANE_VM_SIZE:-}"
WORKER_VM_SIZE="${WORKER_VM_SIZE:-}"
WORKER_REPLICAS="${WORKER_REPLICAS:-}"
GO_VERSION="${GO_VERSION:-1.23.4}"
ORAS_VERSION="${ORAS_VERSION:-1.2.0}"
# CAPZ VMSS-Flex fork built by the Pulumi customImages/capzArtifact resources; the stack's
# Pulumi.<stack>.yaml pins sourcePath + sourceRef=origin/arsdragonfly/md-vmss, so the source
# must be checked out at CAPZ_FORK_DIR before `pulumi up`.
CAPZ_FORK_URL="${CAPZ_FORK_URL:-https://github.com/arsdragonfly/cluster-api-provider-azure.git}"
CAPZ_FORK_BRANCH="${CAPZ_FORK_BRANCH:-arsdragonfly/md-vmss}"
CAPZ_FORK_DIR="${CAPZ_FORK_DIR:-/home/azureuser/capz-vmss-flex}"

export PULUMI_CONFIG_PASSPHRASE="${PULUMI_CONFIG_PASSPHRASE:-}"
export DEBIAN_FRONTEND=noninteractive
# az vm run-command runs as root in a minimal, non-login shell where HOME is unset, so Go
# can't derive GOPATH/GOMODCACHE/GOCACHE ("neither GOMODCACHE nor GOPATH is set") and pulumi
# can't find ~/.pulumi. Pin them explicitly.
export HOME="${HOME:-/root}"
export GOPATH="${GOPATH:-${HOME}/go}"
export GOMODCACHE="${GOMODCACHE:-${GOPATH}/pkg/mod}"
export GOCACHE="${GOCACHE:-${HOME}/.cache/go-build}"
export GOBIN=/usr/local/bin
export PATH="/usr/local/go/bin:/usr/local/bin:${GOPATH}/bin:${HOME}/.pulumi/bin:${PATH}"

install_base_packages() {
  log "apt: base packages (git, curl, jq, python venv)"
  apt-get update -y
  apt-get install -y git curl jq python3-venv python3-pip ca-certificates
}

install_docker() {
  log "install Docker + Docker Hub mirror (README recommendation)"
  apt-get install -y docker.io docker-buildx
  mkdir -p /etc/docker
  # Route host-side docker.io pulls (kindest/node, CAPD node, envoy) via the mirror.
  echo '{"registry-mirrors":["https://mirror.gcr.io"]}' > /etc/docker/daemon.json
  systemctl enable --now docker
}

install_go() {
  log "install Go ${GO_VERSION}"
  if command -v go >/dev/null 2>&1 && go version | grep -q "go${GO_VERSION} "; then
    return 0
  fi
  curl -fsSL "https://go.dev/dl/go${GO_VERSION}.linux-amd64.tar.gz" -o /tmp/go.tgz
  rm -rf /usr/local/go
  tar -C /usr/local -xzf /tmp/go.tgz
  rm -f /tmp/go.tgz
}

install_go_tools() {
  log "go install kind + cloud-provider-kind + ctlptl (-> ${GOBIN})"
  go install sigs.k8s.io/kind@latest
  go install sigs.k8s.io/cloud-provider-kind@latest
  go install github.com/tilt-dev/ctlptl/cmd/ctlptl@latest
}

install_pulumi() {
  log "install Pulumi"
  curl -fsSL https://get.pulumi.com | sh
}

install_oras() {
  log "install oras ${ORAS_VERSION} (OCI artifact CLI used by the capz-artifact build)"
  command -v oras >/dev/null 2>&1 && return 0
  curl -fsSL "https://github.com/oras-project/oras/releases/download/v${ORAS_VERSION}/oras_${ORAS_VERSION}_linux_amd64.tar.gz" -o /tmp/oras.tgz
  tar -xzf /tmp/oras.tgz -C /usr/local/bin oras
  rm -f /tmp/oras.tgz
}

install_kubectl_helm() {
  log "install kubectl + helm"
  local kver
  kver="$(curl -fsSL https://dl.k8s.io/release/stable.txt)"
  curl -fsSL "https://dl.k8s.io/release/${kver}/bin/linux/amd64/kubectl" -o /usr/local/bin/kubectl
  chmod +x /usr/local/bin/kubectl
  curl -fsSL https://raw.githubusercontent.com/helm/helm/main/scripts/get-helm-3 | bash
}

set_inotify() {
  log "raise inotify limits (CAPD/kind create many watches)"
  cat > /etc/sysctl.d/99-ca4s-inotify.conf <<'EOF'
fs.inotify.max_user_watches=1048576
fs.inotify.max_user_instances=8192
EOF
  sysctl --system >/dev/null
}

setup_repo() {
  log "checkout caps-pulumi (${REPO_BRANCH})"
  local url="$REPO_URL"
  if [[ -n "$GIT_TOKEN" && "$REPO_URL" == https://* ]]; then
    url="https://x-access-token:${GIT_TOKEN}@${REPO_URL#https://}"
  fi
  if [[ -d "$REPO_DIR/.git" ]]; then
    git -C "$REPO_DIR" fetch --depth 1 origin "$REPO_BRANCH"
    git -C "$REPO_DIR" checkout -B "$REPO_BRANCH" "origin/$REPO_BRANCH"
  else
    git clone --depth 1 --branch "$REPO_BRANCH" "$url" "$REPO_DIR"
  fi
}

setup_capz_fork() {
  log "checkout CAPZ VMSS-Flex fork -> ${CAPZ_FORK_DIR} (${CAPZ_FORK_BRANCH})"
  # Pulumi's customImages/capzArtifact resources run `git -C <sourcePath> rev-parse
  # <sourceRef>` and docker-build the CAPZ controller from it, so the PUBLIC fork must be
  # checked out at the sourcePath the stack config pins.
  mkdir -p "$(dirname "$CAPZ_FORK_DIR")"
  if [[ ! -d "$CAPZ_FORK_DIR/.git" ]]; then
    git clone --filter=blob:none "$CAPZ_FORK_URL" "$CAPZ_FORK_DIR"
  fi
  git -C "$CAPZ_FORK_DIR" fetch origin "$CAPZ_FORK_BRANCH"
}

setup_venv() {
  log "python venv + requirements (pinned as ../.venv by pulumi/Pulumi.yaml)"
  python3 -m venv "$REPO_DIR/.venv"
  "$REPO_DIR/.venv/bin/pip" install --upgrade pip
  # requirements.txt uses editable installs whose paths are relative to the repo root
  # (e.g. -e ./pulumi/sdks/local); pip resolves them against the CWD, so run from $REPO_DIR.
  ( cd "$REPO_DIR" && "$REPO_DIR/.venv/bin/pip" install -r requirements.txt )
}

pulumi_up() {
  log "pulumi up -s ${PULUMI_STACK} (IMDS discovers THIS VM's resource group)"
  # Local state backend: the VM is ephemeral per run and teardown is `az group
  # delete`, so no shared/remote Pulumi state is needed.
  pulumi login --local
  cd "$REPO_DIR/pulumi"
  # Create/select the stack in the local backend; config lives in Pulumi.<stack>.yaml.
  pulumi stack select --create "$PULUMI_STACK"
  # The tenant's parameters -- including useDiscoveredResourceGroup: true so THIS VM's
  # RG is reused -- are committed in Pulumi.<stack>.yaml. Optionally override the
  # control-plane / worker SKUs + worker count per run; `--path` type-infers the int.
  local base="ca4s-infra:initStack.tenants.workloadClusters[\"${TENANT_NAME}\"].parameters"
  if [[ -n "$CONTROL_PLANE_VM_SIZE" ]]; then
    pulumi config set --path "${base}.controlPlaneVmSize" "$CONTROL_PLANE_VM_SIZE"
  fi
  if [[ -n "$WORKER_VM_SIZE" ]]; then
    pulumi config set --path "${base}.workerVmSize" "$WORKER_VM_SIZE"
  fi
  if [[ -n "$WORKER_REPLICAS" ]]; then
    pulumi config set --path "${base}.workerReplicas" "$WORKER_REPLICAS"
  fi
  pulumi up -s "$PULUMI_STACK" --yes --non-interactive
}

main() {
  install_base_packages
  install_docker
  install_go
  install_go_tools
  install_pulumi
  install_kubectl_helm
  install_oras
  set_inotify
  if [[ -n "${SKIP_SETUP_REPO:-}" ]]; then
    log "SKIP_SETUP_REPO set - using pre-provisioned repo at ${REPO_DIR}"
  else
    setup_repo
  fi
  setup_venv
  setup_capz_fork
  pulumi_up
  log "bootstrap complete"
  # Success sentinel: the pipeline's `az vm run-command invoke` returns success even
  # when this script fails, so it greps for this exact last line to decide pass/fail.
  echo "CAPS_BOOTSTRAP_SUCCESS"
}

main "$@"
