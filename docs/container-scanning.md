# Container scanning templates

Single-responsibility GitLab CI jobs for image supply-chain checks. Include only
what each repo needs.

## Templates

| File | Job | Stage | Purpose |
| ---- | --- | ----- | ------- |
| `templates/container/build.yml` | `container:build` | build | Build and push to GitLab Container Registry |
| `templates/container/harbor-build-push.yml` | `container:harbor-build-push` | build | Build and push directly to Harbor |
| `templates/container/trivy-image-scan.yml` | `container:trivy-image-scan` | scan | Trivy CVE gate on built image |
| `templates/container/container-scan.yml` | `container:container-scan` | scan | GitLab native container scanning (Security tab) |
| `templates/container/harbor-push.yml` | `container:harbor-push` | publish | Retag/push GitLab registry image to Harbor (no build) |
| `templates/security/trivy-filesystem.yml` | `security:trivy-fs-scan` | validate | Repo/IaC scan (no Dockerfile required) |

## App repo — GitLab registry flow

Use when you want GitLab Security reports and optional Harbor promotion.

```yaml
include:
  - project: homelab/pipeline-templates
    ref: main
    file:
      - templates/container/build.yml
      - templates/container/trivy-image-scan.yml
      - templates/container/container-scan.yml
      - templates/container/harbor-push.yml

stages:
  - build
  - scan
  - publish
```

```text
container:build
    ├── container:trivy-image-scan
    └── container:container-scan
            └── container:harbor-push   (manual; retag only)
```

## App repo — Harbor-only flow

Use when the app pushes straight to Harbor (no GitLab Container Registry).

```yaml
include:
  - project: homelab/pipeline-templates
    ref: main
    file:
      - templates/container/harbor-build-push.yml
      - templates/container/trivy-image-scan.yml

stages:
  - build
  - scan

container:trivy-image-scan:
  extends: .container_trivy_image_scan
  needs:
    - job: container:harbor-build-push
```

`harbor-build-push` sets `CONTAINER_IMAGE` to the Harbor tag so Trivy scans the
same image. Add Harbor registry credentials to the Trivy job if the runner cannot
pull from Harbor anonymously.

## Infra / GitOps repo (no image)

```yaml
include:
  - project: homelab/pipeline-templates
    ref: main
    file:
      - templates/security/trivy-filesystem.yml

stages:
  - validate

security:trivy-fs-scan:
  extends: .security_trivy_filesystem
  variables:
    TRIVY_SCANNERS: "misconfig,secret"
  rules:
    - changes:
        - terraform/**/*
        - platform/**/*
```

## Variables

| Variable | Default | Used by |
| -------- | ------- | ------- |
| `CONTAINER_IMAGE` | GitLab or Harbor tag (see template) | build, scan, harbor-push |
| `TRIVY_SEVERITY` | `CRITICAL,HIGH` | Trivy jobs |
| `TRIVY_EXIT_CODE` | `1` | Trivy jobs (`0` = report only) |
| `TRIVY_SCANNERS` | `vuln,secret,misconfig` | filesystem scan |
| `TRIVY_SCAN_PATH` | `.` | filesystem scan |
| `HARBOR_REGISTRY` | `harbor.nasraldin.com` | harbor jobs |
| `HARBOR_PROJECT` | _(required)_ | harbor jobs |
| `HARBOR_USERNAME` | _(required)_ | harbor jobs |
| `HARBOR_PASSWORD` | _(required)_ | harbor jobs |

GitLab container scanning uses `CS_IMAGE` (set from `CONTAINER_IMAGE`).

## Pick only what you need

| Repo type | Include |
| --------- | ------- |
| GitLab registry app | `build` + `trivy-image-scan` + `container-scan` + optional `harbor-push` |
| Harbor-only app | `harbor-build-push` + `trivy-image-scan` |
| Promote after scan | `build` + scans + `harbor-push` |
| Terraform/Ansible/GitOps | `trivy-filesystem` only |
| Report-only Trivy | Set `TRIVY_EXIT_CODE=0` on the scan job |

## GitLab requirements

- **Container scanning** needs GitLab Ultimate or equivalent security license on
  self-managed. If unavailable, omit `container-scan.yml` and keep Trivy.
- **Trivy** runs on any runner that can pull the target image.

## Related

- [Supply chain design](https://nasraldin.github.io/homelab/security/supply-chain-and-policies/)
- [Home lab CI pipelines](https://nasraldin.github.io/dev-homelab/ci/pipelines.html)
