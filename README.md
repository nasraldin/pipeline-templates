# pipeline-templates

Reusable **GitLab CI** job templates for the homelab. Consumer repos
`include` these files — do not copy job bodies into each project.

Hosted on GitLab: `homelab/pipeline-templates` (`http://192.168.68.12/homelab/pipeline-templates`).

## Quick include

```yaml
include:
  - project: 'homelab/pipeline-templates'
    ref: main
    file:
      - /templates/lint/yaml.yml
      - /templates/lint/prettier.yml
      - /templates/security/gitleaks.yml
```

Paths are rooted at the templates project (`/templates/...`).

## Defaults (Node)

| Item            | Value                                     |
| --------------- | ----------------------------------------- |
| Node            | **24** (LTS image `node:24-bookworm`)     |
| Package manager | **pnpm** via Corepack (`PNPM_VERSION=10`) |

Shared base: [`templates/common/node24.yml`](templates/common/node24.yml).

## Template catalog

| Area      | Template                                   | Job / hidden                 | Docs                                                       |
| --------- | ------------------------------------------ | ---------------------------- | ---------------------------------------------------------- |
| Lint      | `templates/lint/prettier.yml`              | `lint:prettier`              | [docs/templates/lint.md](docs/templates/lint.md)           |
| Lint      | `templates/lint/eslint.yml`                | `lint:eslint`                | same                                                       |
| Lint      | `templates/lint/yaml.yml`                  | `lint:yaml`                  | same                                                       |
| Lint      | `templates/lint/json.yml`                  | `lint:json`                  | same                                                       |
| Lint      | `templates/lint/markdown.yml`              | `lint:markdown`              | same                                                       |
| Lint      | `templates/lint/shell.yml`                 | `lint:shell`                 | same                                                       |
| Lint      | `templates/lint/editorconfig.yml`          | `lint:editorconfig`          | same                                                       |
| Lint      | `templates/lint/terraform.yml`             | `lint:terraform`             | same                                                       |
| Lint      | `templates/lint/ansible.yml`               | `lint:ansible`               | same                                                       |
| Node      | `templates/node/test.yml`                  | `node:test`                  | [docs/templates/node.md](docs/templates/node.md)           |
| Node      | `templates/node/build.yml`                 | `node:build`                 | same                                                       |
| Security  | `templates/security/gitleaks.yml`          | `security:gitleaks`          | [docs/templates/security.md](docs/templates/security.md)   |
| Security  | `templates/security/osv-scanner.yml`       | `security:osv-scanner`       | same                                                       |
| Security  | `templates/security/snyk.yml`              | `security:snyk`              | same                                                       |
| Security  | `templates/security/trivy-filesystem.yml`  | `security:trivy-fs-scan`     | same                                                       |
| Quality   | `templates/quality/sonarqube.yml`          | `quality:sonarqube`          | [docs/templates/quality.md](docs/templates/quality.md)     |
| Container | `templates/container/build-push.yml`       | `container:build-push`       | [docs/templates/container.md](docs/templates/container.md) |
| Container | `templates/container/trivy-image-scan.yml` | `container:trivy-image-scan` | same                                                       |
| Container | `templates/container/syft-sbom.yml`        | `container:syft-sbom`        | same                                                       |
| Container | `templates/container/cosign-sign.yml`      | `container:cosign-sign`      | same                                                       |
| IaC       | `templates/terraform/*.yml`                | plan/apply/destroy           | [docs/usage.md](docs/usage.md)                             |
| IaC       | `templates/ansible/*.yml`                  | check/apply                  | same                                                       |
| GitOps    | `templates/gitops/*.yml`                   | helm/kubeconform/argocd      | same                                                       |

## Multi-registry image push

```yaml
variables:
  PUSH_REGISTRIES: 'gitlab,dockerhub,harbor' # pick any combo
  HARBOR_PROJECT: 'library'
  # DOCKERHUB_USERNAME / DOCKERHUB_TOKEN
  # HARBOR_USERNAME / HARBOR_PASSWORD
```

See [docs/templates/container.md](docs/templates/container.md).

## This repo’s own CI

On every MR and push to `main`: YAML/shell/markdown/JSON/Prettier + Gitleaks.

## Safety

- Prefer extending hidden jobs (`.lint_yaml`) when you need custom `rules:` / `needs:`.
- Never filter Terraform `var.vms` for selective apply — use `-target` only.
- Destroy requires `TF_TARGET_GUESTS` unless `TF_ALLOW_FULL_DESTROY=true`.

Full usage notes: [docs/usage.md](docs/usage.md).
