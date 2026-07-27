#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ANSIBLE_DIR="${ANSIBLE_DIR:-${ROOT}/../ansible}"
cd "${ANSIBLE_DIR}"
ANSIBLE_PLAYBOOK="${ANSIBLE_PLAYBOOK:-playbooks/infra.yml}"
ANSIBLE_LIMIT="${ANSIBLE_LIMIT:-}"
ANSIBLE_SECRETS="${ANSIBLE_SECRETS:-}"
ANSIBLE_CHECK="${ANSIBLE_CHECK:-false}"
args=(ansible-playbook -i inventory/hosts.yml "${ANSIBLE_PLAYBOOK}")
[[ -n "${ANSIBLE_LIMIT}" ]] && args+=(--limit "${ANSIBLE_LIMIT}")
[[ -n "${ANSIBLE_SECRETS}" && -f "${ANSIBLE_SECRETS}" ]] && args+=(-e "@${ANSIBLE_SECRETS}")
[[ "${ANSIBLE_CHECK}" == "true" ]] && args+=(--check --diff)
echo "==> ${args[*]}"
exec "${args[@]}"
