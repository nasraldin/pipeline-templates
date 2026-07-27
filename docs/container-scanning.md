# Container and supply-chain scanning templates

Single-responsibility GitLab CI jobs for image and repo security checks. Include
only what each repo needs.

## Templates

| File                                        | Job                           | Stage    | Purpose                                               |
| ------------------------------------------- | ----------------------------- | -------- | ----------------------------------------------------- |
| `templates/security/gitleaks.yml`           | `security:gitleaks`           | validate | Secret leak scan (git tree / history)                 |
| `templates/security/trivy-filesystem.yml`   | `security:trivy-fs-scan`      | validate | Repo/IaC scan (no Dockerfile required)                |
| `templates/container/build.yml`             | `container:build`             | build    | Build and push to GitLab Container Registry           |
| `templates/container/harbor-build-push.yml` | `container:harbor-build-push` | build    | Build and push directly to Harbor                     |
| `templates/container/trivy-image-scan.yml`  | `container:trivy-image-scan`  | scan     | Trivy CVE gate on built image                         |
| `templates/container/syft-sbom.yml`         | `container:syft-sbom`         | scan     | SBOM (SPDX JSON artifact)                             |
| `templates/container/container-scan.yml`    | `container:container-scan`    | scan     | GitLab native container scanning (Security tab)       |
| `templates/container/cosign-sign.yml`       | `container:cosign-sign`       | publish  | Cosign sign (key pair from CI / Infisical)            |
| `templates/container/harbor-push.yml`       | `container:harbor-push`       | publish  | Retag/push GitLab registry image to Harbor (no build) |

## App repo — recommended Harbor supply-chain flow

```text
gitleaks → harbor-build-push → trivy-image-scan + syft-sbom → cosign-sign
```

```yaml
include:
  - project: homelab/pipeline-templates
    ref: main
    file:
      - templates/security/gitleaks.yml
      - templates/container/harbor-build-push.yml
      - templates/container/trivy-image-scan.yml
      - templates/container/syft-sbom.yml
      - templates/container/cosign-sign.yml
```

See [examples/harbor-only-pipeline.gitlab-ci.yml](../examples/harbor-only-pipeline.gitlab-ci.yml).

Cosign keys: store in Infisical project `pipelines` / path `/cosign`, then map to
GitLab CI variables `COSIGN_PRIVATE_KEY` + `COSIGN_PASSWORD` (or `infisical run`).

## App repo — GitLab registry flow

```yaml
include:
  - project: homelab/pipeline-templates
    ref: main
    file:
      - templates/security/gitleaks.yml
      - templates/container/build.yml
      - templates/container/trivy-image-scan.yml
      - templates/container/syft-sbom.yml
      - templates/container/container-scan.yml
      - templates/container/cosign-sign.yml
      - templates/container/harbor-push.yml

stages:
  - validate
  - build
  - scan
  - publish
```

## Infra / GitOps repo (no image)

```yaml
include:
  - project: homelab/pipeline-templates
    ref: main
    file:
      - templates/security/gitleaks.yml
      - templates/security/trivy-filesystem.yml

stages:
  - validate

security:trivy-fs-scan:
  extends: .security_trivy_filesystem
  variables:
    TRIVY_SCANNERS: "misconfig,secret"
```

`lab-home-k8s` and `lab-home-gitops` include both Gitleaks and Trivy FS.

## Variables

| Variable             | Default                             | Used by                        |
| -------------------- | ----------------------------------- | ------------------------------ |
| `CONTAINER_IMAGE`    | GitLab or Harbor tag (see template) | build, scan, cosign, harbor    |
| `TRIVY_SEVERITY`     | `CRITICAL,HIGH`                     | Trivy jobs                     |
| `TRIVY_EXIT_CODE`    | `1`                                 | Trivy jobs (`0` = report only) |
| `TRIVY_SCANNERS`     | `vuln,secret,misconfig`             | filesystem scan                |
| `TRIVY_SCAN_PATH`    | `.`                                 | filesystem scan                |
| `GITLEAKS_EXIT_CODE` | `1`                                 | gitleaks (`0` = report only)   |
| `GITLEAKS_CONFIG`    | optional `.gitleaks.toml`           | gitleaks                       |
| `SYFT_OUTPUT`        | `sbom.spdx.json`                    | syft                           |
| `COSIGN_PRIVATE_KEY` | _(required for sign)_               | cosign                         |
| `COSIGN_PASSWORD`    | optional                            | cosign                         |
| `HARBOR_REGISTRY`    | `harbor.nasraldin.com`              | harbor / cosign auth           |
| `HARBOR_PROJECT`     | _(required)_                        | harbor jobs                    |
| `HARBOR_USERNAME`    | _(required)_                        | harbor jobs                    |
| `HARBOR_PASSWORD`    | _(required)_                        | harbor jobs                    |

## Pick only what you need

| Repo type                | Include                                                                 |
| ------------------------ | ----------------------------------------------------------------------- |
| Harbor-only app          | gitleaks + harbor-build-push + trivy + syft + cosign                    |
| GitLab registry app      | gitleaks + build + trivy + syft + optional container-scan + cosign      |
| Terraform/Ansible/GitOps | gitleaks + trivy-filesystem                                             |
| Report-only              | Set `TRIVY_EXIT_CODE=0` / `GITLEAKS_EXIT_CODE=0`                        |

## Related

- [Supply chain (curriculum)](https://nasraldin.github.io/homelab/security/supply-chain-and-policies/)
- [Supply chain (dev-homelab)](https://nasraldin.github.io/dev-homelab/architecture/supply-chain)
- [Home lab CI pipelines](https://nasraldin.github.io/dev-homelab/ci/pipelines.html)
