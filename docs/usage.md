# Pipeline template usage

Include from GitLab project `homelab/pipeline-templates` (LAN:
`http://192.168.68.12`).

```yaml
include:
  - project: 'homelab/pipeline-templates'
    ref: main
    file:
      - /templates/lint/yaml.yml
```

## Tool download cache

CLI binaries must not be re-fetched from GitHub on every job. Use
version-keyed GitLab cache under `.ci-tools/`:

| Template | What |
| -------- | ---- |
| `templates/common/tool-cache.yml` | Shared `.tool_cache` (`.ci-tools/` paths + version key) |
| `templates/talos/tools.yml` | `.talos_tools_base` — extend + install via consumer scripts |

Cache key changes when `versions.env` or version variables change → download
once, then **cache hit** only. Consumer install scripts should use
`--connect-timeout` / `--max-time 600` with resume so large assets (e.g.
talosctl) can finish from slow mirrors.

Detailed per-area docs:

| Doc                                                | Topic                                                       |
| -------------------------------------------------- | ----------------------------------------------------------- |
| [templates/preflight.md](templates/preflight.md)   | CI Lint dry-run matrix + golden expected outputs            |
| [templates/iac-gitops.md](templates/iac-gitops.md) | Terraform init/validate/plan + Helm lint/template/dry-run   |
| [templates/lint.md](templates/lint.md)             | Prettier, ESLint, YAML, JSON, Markdown, Shell, EditorConfig |
| [templates/node.md](templates/node.md)             | pnpm test/build (Node 24)                                   |
| [templates/security.md](templates/security.md)     | Gitleaks, OSV, Snyk, Trivy FS                               |
| [templates/quality.md](templates/quality.md)       | SonarQube                                                   |
| [templates/container.md](templates/container.md)   | Multi-registry build/push + scan                            |
| [container-scanning.md](container-scanning.md)     | Legacy Harbor/Cosign notes                                  |

## Selective Terraform / Ansible runs

| Variable           | Example               | Effect                                         |
| ------------------ | --------------------- | ---------------------------------------------- |
| `TF_TARGET_GUESTS` | `infra-01`            | Terraform `-target=module.vm["infra-01"]` only |
| `ANSIBLE_PLAYBOOK` | `playbooks/infra.yml` | Single playbook                                |
| `ANSIBLE_LIMIT`    | `docker-01`           | Single host                                    |
| `GITOPS_COMPONENT` | `keycloak`            | Validate one platform directory                |

Automatic path detection: `scripts/detect-changed-services.sh` + `maps/*.yml`.

## App repo (Node + container)

See [examples/node-app.gitlab-ci.yml](../examples/node-app.gitlab-ci.yml) and
[examples/multi-registry.gitlab-ci.yml](../examples/multi-registry.gitlab-ci.yml).

## Safety

- Never filter `var.vms` for selective apply — use `-target` only.
- `resource_group` serialises terraform/ansible apply jobs.
- Destroy requires `TF_TARGET_GUESTS` unless `TF_ALLOW_FULL_DESTROY=true`.
