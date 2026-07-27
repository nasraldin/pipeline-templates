#!/usr/bin/env bash
# Build Terraform -target flags from TF_TARGET_GUESTS.
set -euo pipefail

TF_TARGET_GUESTS="${TF_TARGET_GUESTS:-}"
TF_TARGET_KIND="${TF_TARGET_KIND:-vm}"
flags=()

if [[ -z "${TF_TARGET_GUESTS// /}" ]]; then
  echo "export TF_TARGET_FLAGS=''"
  echo "export TF_TARGET_MODE='full'"
  exit 0
fi

IFS=',' read -ra guests <<<"${TF_TARGET_GUESTS}"
for raw in "${guests[@]}"; do
  g="$(echo "${raw}" | xargs)"
  [[ -z "${g}" ]] && continue
  case "${TF_TARGET_KIND}" in
  vm) flags+=(-target="module.vm[\"${g}\"]") ;;
  ct) flags+=(-target="proxmox_virtual_environment_container.ct[\"${g}\"]") ;;
  *) echo "error: unknown TF_TARGET_KIND=${TF_TARGET_KIND}" >&2; exit 1 ;;
  esac
done

escaped=$(printf '%q ' "${flags[@]}")
echo "export TF_TARGET_FLAGS=${escaped}"
echo "export TF_TARGET_MODE='targeted'"
