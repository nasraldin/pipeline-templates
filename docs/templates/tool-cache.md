# Tool download cache

Reusable GitLab CI cache for CLI binaries under `.ci-tools/`. Download from
GitHub (or similar) **only on cache miss** or when the version key changes.

## Templates

| File                                                                       | Exports                                                                                                  |
| -------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------- |
| [`templates/common/tool-cache.yml`](../../templates/common/tool-cache.yml) | `.tool_cache` — `cache.paths: [.ci-tools/]`, key from `TOOL_CACHE_PREFIX` + `versions.env`               |
| [`templates/talos/tools.yml`](../../templates/talos/tools.yml)             | `.talos_tools_base` (extends `.tool_cache`, Alpine image) + default `TALHELPER_VERSION` / `SOPS_VERSION` |

Kubeconform and EditorConfig templates embed the same pattern inline (version
variable → cache key → download into `.ci-tools/` on miss).

## Consumer pattern (Talos)

```yaml
include:
  - project: homelab/pipeline-templates
    ref: main
    file:
      - templates/talos/tools.yml

variables:
  TALHELPER_VERSION: 'v3.1.17'
  SOPS_VERSION: 'v3.13.3'
  KUBECTL_VERSION: 'v1.37.0' # pin; do not curl stable.txt
  TOOL_CACHE_PREFIX: 'talos-tools-${TALHELPER_VERSION}-${SOPS_VERSION}'

talos:genconfig:
  extends: .talos_tools_base
  before_script:
    - sh scripts/ci-install-talos-tools.sh # lives in the consumer repo
```

Install scripts must:

1. Write binaries under `${CI_PROJECT_DIR}/.ci-tools/`.
2. `ln -sf` into `/usr/local/bin`.
3. Log `cache hit:` / `cache miss:`.
4. Use `--connect-timeout` and a generous `--max-time` (600s+) with resume for large assets.

Example consumer: `homelab/homelab-cluster-infra` (`scripts/ci-install-*.sh`, [docs/ci.md](https://gitlab.nasraldin.com/homelab/homelab-cluster-infra/-/blob/main/docs/ci.md)).

## Runner shared cache

Ansible defaults keep `gitlab_runner_cache_enabled: false` until AIStor S3 is
wired. Without S3, GitLab stores cache **locally on the runner host** (`No URL
provided` in job logs). Enable S3 when you need cache hits across hosts.
