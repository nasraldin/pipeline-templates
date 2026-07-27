# pipeline-templates

Reusable GitLab CI job templates for the home lab. Consumer repos
include these files — do not copy job definitions into each project.

## Usage

```yaml
include:
  - project: homelab/pipeline-templates
    ref: main
    file:
      - templates/lint/yaml.yml
      - templates/terraform/plan.yml
```

## Selective runs

| Variable           | Example               | Effect                                         |
| ------------------ | --------------------- | ---------------------------------------------- |
| `TF_TARGET_GUESTS` | `infra-01`            | Terraform `-target=module.vm["infra-01"]` only |
| `ANSIBLE_PLAYBOOK` | `playbooks/infra.yml` | Single playbook                                |
| `ANSIBLE_LIMIT`    | `docker-01`           | Single host                                    |
| `GITOPS_COMPONENT` | `keycloak`            | Validate one platform directory                |

## Container scanning

| Template                                    | Job                           | When to include                        |
| ------------------------------------------- | ----------------------------- | -------------------------------------- |
| `templates/security/gitleaks.yml`           | `security:gitleaks`           | All repos (secret leak scan)           |
| `templates/security/trivy-filesystem.yml`   | `security:trivy-fs-scan`      | Infra/GitOps (no image)                |
| `templates/container/build.yml`             | `container:build`             | Build + push to GitLab registry        |
| `templates/container/harbor-build-push.yml` | `container:harbor-build-push` | Build + push directly to Harbor        |
| `templates/container/trivy-image-scan.yml`  | `container:trivy-image-scan`  | CVE gate after build                   |
| `templates/container/syft-sbom.yml`         | `container:syft-sbom`         | SBOM artifact after build              |
| `templates/container/cosign-sign.yml`       | `container:cosign-sign`       | Sign image (Cosign key pair)           |
| `templates/container/container-scan.yml`    | `container:container-scan`    | GitLab Security report                 |
| `templates/container/harbor-push.yml`       | `container:harbor-push`       | Retag GitLab image → Harbor (no build) |

See [docs/container-scanning.md](docs/container-scanning.md).

Automatic path detection: `scripts/detect-changed-services.sh` + `maps/*.yml`.

## Safety

- Never filter `var.vms` for selective apply — use `-target` only.
- `resource_group` serialises terraform/ansible apply jobs.
- Destroy requires `TF_TARGET_GUESTS` unless `TF_ALLOW_FULL_DESTROY=true`.

See [docs/usage.md](docs/usage.md).
