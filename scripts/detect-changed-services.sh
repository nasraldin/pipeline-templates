#!/usr/bin/env bash
# Emit changed service keys for selective CI (dotenv format).
# Usage: detect-changed-services.sh maps/<services>.yml [base-ref]
set -euo pipefail

MAP_FILE="${1:?map file required}"
BASE_REF="${2:-${CI_MERGE_REQUEST_DIFF_BASE_SHA:-origin/main}}"
CHANGED=$(git diff --name-only "${BASE_REF}"...HEAD 2>/dev/null || git diff --name-only HEAD~1)
CURRENT=""
TF_TARGET=""
COMPONENT=""
SERVICES=()
TF_TARGETS=()
GITOPS_COMPONENTS=()

path_matches() {
  local glob="$1"
  local pattern="${glob//\*\*/.*}"
  echo "${CHANGED}" | grep -qE "^${pattern}" 2>/dev/null
}

while IFS= read -r line; do
  [[ -z "${line}" || "${line}" =~ ^# ]] && continue
  if [[ "${line}" =~ ^([a-zA-Z0-9_-]+):$ ]]; then
    CURRENT="${BASH_REMATCH[1]}"
    TF_TARGET=""
    COMPONENT=""
    continue
  fi
  if [[ -z "${CURRENT}" ]]; then
    continue
  fi
  if [[ "${line}" =~ ^[[:space:]]+tf_target:[[:space:]]*(.+)$ ]]; then
    TF_TARGET="${BASH_REMATCH[1]}"
    continue
  fi
  if [[ "${line}" =~ ^[[:space:]]+component:[[:space:]]*(.+)$ ]]; then
    COMPONENT="${BASH_REMATCH[1]}"
    continue
  fi
  if [[ "${line}" =~ ^[[:space:]]+-[[:space:]] ]]; then
    path="${line#*- }"
    path="${path// /}"
    if path_matches "${path}"; then
      SERVICES+=("${CURRENT}")
      [[ -n "${TF_TARGET}" ]] && TF_TARGETS+=("${TF_TARGET}")
      [[ -n "${COMPONENT}" ]] && GITOPS_COMPONENTS+=("${COMPONENT}")
    fi
  fi
done < "${MAP_FILE}"

UNIQUE=$(printf '%s\n' "${SERVICES[@]}" | sort -u | tr '\n' ',' | sed 's/,$//')
echo "CHANGED_SERVICES=${UNIQUE}"

if ((${#TF_TARGETS[@]} > 0)); then
  TF_UNIQUE=$(printf '%s\n' "${TF_TARGETS[@]}" | sort -u | tr '\n' ',' | sed 's/,$//')
  echo "TF_TARGET_GUESTS=${TF_UNIQUE}"
fi

if ((${#GITOPS_COMPONENTS[@]} > 0)); then
  COMP_UNIQUE=$(printf '%s\n' "${GITOPS_COMPONENTS[@]}" | sort -u | tr '\n' ',' | sed 's/,$//')
  echo "GITOPS_COMPONENT=${COMP_UNIQUE}"
fi
