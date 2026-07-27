#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TF_DIR="${TF_DIR:-${ROOT}/../terraform}"
cd "${TF_DIR}"
eval "$("${ROOT}/scripts/ci-targets.sh")"
TF_ACTION="${TF_ACTION:-plan}"
TF_AUTO_APPROVE="${TF_AUTO_APPROVE:-false}"
echo "==> TF_ACTION=${TF_ACTION} TF_TARGET_MODE=${TF_TARGET_MODE}"
case "${TF_ACTION}" in
plan)
  # shellcheck disable=SC2086
  terraform plan ${TF_TARGET_FLAGS} -out=tfplan
  terraform show -no-color tfplan | tee tfplan.txt
  ;;
apply)
  extra=(-input=false)
  [[ "${TF_AUTO_APPROVE}" == "true" ]] && extra+=(-auto-approve)
  # shellcheck disable=SC2086
  terraform apply "${extra[@]}" ${TF_TARGET_FLAGS}
  ;;
destroy)
  if [[ "${TF_TARGET_MODE}" != "targeted" && "${TF_ALLOW_FULL_DESTROY:-false}" != "true" ]]; then
    echo "error: refuse full destroy without TF_TARGET_GUESTS" >&2
    exit 1
  fi
  extra=(-input=false)
  [[ "${TF_AUTO_APPROVE}" == "true" ]] && extra+=(-auto-approve)
  # shellcheck disable=SC2086
  terraform destroy "${extra[@]}" ${TF_TARGET_FLAGS}
  ;;
*) echo "error: TF_ACTION must be plan|apply|destroy" >&2; exit 1 ;;
esac
