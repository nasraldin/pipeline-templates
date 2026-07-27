# Container scanning templates

Single-responsibility GitLab CI jobs for image supply-chain checks. Include only
what each repo needs.

## Templates

| File | Job | Stage | Purpose |
| ---- | --- | ----- | ------- |
| `templates/container/build.yml` | `container:build` | build | Build and push to GitLab Container Registry |
| `templates/container/trivy-image-scan.yml` | `container:trivy-image-scan` | scan | Trivy CVE gate on built image |
| `templates/container/gitlab-container-scanning.yml` | `container:gitlab-scan` | scan | GitLab native container scanning (Security tab) |
| `templates/container/harbor-push.yml` | `container:harbor-push` | publish | Manual retag/push to Harbor after scans |
| `templates/security/trivy-filesystem.yml` | `security:trivy-fs-scan` | validate | Repo/IaC scan (no Dockerfile required) |

## App repo with Dockerfile

```yaml
include:
  - project: homelab/pipeline-templates
    ref: main
    file:
      - templates/container/build.yml
      - templates/container/trivy-image-scan.yml
      - templates/container/gitlab-container-scanning.yml
      - templates/container/harbor-push.yml

stages:
  - build
  - scan
  - publish
```

Pipeline order:

```text
container:build
    ├── container:trivy-image-scan   (fail on CRITICAL,HIGH by default)
    └── container:gitlab-scan        (GitLab Security → Vulnerability Report)
            └── container:harbor-push (manual; needs scans when present)
```

See [examples/container-pipeline.gitlab-ci.yml](../examples/container-pipeline.gitlab-ci.yml).

## Infra / GitOps repo (no image)

Include filesystem scanning only:

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
        - ansible/**/*
        - platform/**/*
```

## Variables

| Variable | Default | Used by |
| -------- | ------- | ------- |
| `CONTAINER_IMAGE` | `$CI_REGISTRY_IMAGE:$CI_COMMIT_SHORT_SHA` | build, scan, harbor-push |
| `TRIVY_SEVERITY` | `CRITICAL,HIGH` | Trivy jobs |
| `TRIVY_EXIT_CODE` | `1` | Trivy jobs (`0` = report only) |
| `TRIVY_SCANNERS` | `vuln,secret,misconfig` | filesystem scan |
| `TRIVY_SCAN_PATH` | `.` | filesystem scan |
| `HARBOR_REGISTRY` | `harbor.nasraldin.com` | harbor-push |
| `HARBOR_PROJECT` | _(required)_ | harbor-push |
| `HARBOR_USERNAME` | _(required)_ | harbor-push |
| `HARBOR_PASSWORD` | _(required)_ | harbor-push |

GitLab Container Scanning uses `CS_IMAGE` (set automatically from `CONTAINER_IMAGE`).

## Pick only what you need

| Repo type | Include |
| --------- | ------- |
| Container app | `build` + `trivy-image-scan` + `gitlab-container-scanning` + optional `harbor-push` |
| Harbor-only (no GitLab report) | `build` + `trivy-image-scan` + `harbor-push` |
| Terraform/Ansible/GitOps | `trivy-filesystem` only |
| Report-only Trivy | Set `TRIVY_EXIT_CODE=0` on the scan job |

## GitLab requirements

- **Container Scanning** needs GitLab Ultimate or an equivalent security license on
  self-managed. If unavailable, omit `gitlab-container-scanning.yml` and keep Trivy.
- **Trivy** runs on any runner with Docker executor or sufficient memory for image pull.

## Related

- [Supply chain design](https://nasraldin.github.io/homelab/security/supply-chain-and-policies/) — Cosign, Harbor, Kyverno roadmap
- [Home lab CI pipelines](https://nasraldin.github.io/dev-homelab/ci/pipelines.html) — infra/gitops selective pipelines
