# Container templates

## `container:build-push` — `templates/container/build-push.yml`

**What:** `docker build` once, then push the same image to **one or more** registries.

### `PUSH_REGISTRIES`

Comma-separated list:

| Value                     | Destination               | Required CI vars                                                                      |
| ------------------------- | ------------------------- | ------------------------------------------------------------------------------------- |
| `gitlab`                  | GitLab Container Registry | built-in `CI_REGISTRY_*`                                                              |
| `dockerhub` / `docker.io` | Docker Hub                | `DOCKERHUB_USERNAME`, `DOCKERHUB_TOKEN`                                               |
| `harbor`                  | Harbor (lab registry)     | `HARBOR_USERNAME`, `HARBOR_PASSWORD`, `HARBOR_PROJECT` (+ optional `HARBOR_REGISTRY`) |
| `custom`                  | Any registry              | `CUSTOM_REGISTRY`, `CUSTOM_REGISTRY_USER`, `CUSTOM_REGISTRY_PASSWORD`                 |

Examples:

```yaml
variables:
  PUSH_REGISTRIES: 'gitlab'

variables:
  PUSH_REGISTRIES: 'gitlab,dockerhub'

variables:
  PUSH_REGISTRIES: 'gitlab,harbor,dockerhub'
```

### Image naming

| Variable               | Default                                                   |
| ---------------------- | --------------------------------------------------------- |
| `IMAGE_NAME`           | `$CI_PROJECT_NAME`                                        |
| `IMAGE_TAG`            | `$CI_COMMIT_SHORT_SHA`                                    |
| `CONTAINER_CONTEXT`    | `.`                                                       |
| `CONTAINER_DOCKERFILE` | `Dockerfile`                                              |
| `GITLAB_IMAGE`         | `$CI_REGISTRY_IMAGE:$IMAGE_TAG`                           |
| `DOCKERHUB_IMAGE`      | `docker.io/$DOCKERHUB_USERNAME/$IMAGE_NAME:$IMAGE_TAG`    |
| `HARBOR_IMAGE`         | `$HARBOR_REGISTRY/$HARBOR_PROJECT/$IMAGE_NAME:$IMAGE_TAG` |
| `CUSTOM_IMAGE`         | `$CUSTOM_REGISTRY/$IMAGE_NAME:$IMAGE_TAG`                 |
| `HARBOR_REGISTRY`      | `harbor.nasraldin.com`                                    |

Writes dotenv artifact `CONTAINER_IMAGE=<first pushed ref>` for Trivy / Cosign / Syft jobs.

### Example pipeline

```yaml
include:
  - project: 'homelab/pipeline-templates'
    ref: main
    file:
      - /templates/security/gitleaks.yml
      - /templates/container/build-push.yml
      - /templates/container/trivy-image-scan.yml
      - /templates/container/syft-sbom.yml

variables:
  PUSH_REGISTRIES: 'gitlab,harbor'
  HARBOR_PROJECT: 'library'

stages: [validate, build, scan]

security:gitleaks:
  extends: .security_gitleaks

container:build-push:
  extends: .container_build_push

container:trivy-image-scan:
  extends: .container_trivy_image_scan
  needs: [container:build-push]

container:syft-sbom:
  extends: .container_syft_sbom
  needs: [container:build-push]
```

### Legacy aliases

- `templates/container/build.yml` → GitLab-only wrapper (`PUSH_REGISTRIES=gitlab`)
- `templates/container/harbor-build-push.yml` → Harbor-only (kept for older consumers)

Prefer **`build-push.yml`** for new projects.

---

## Scan / SBOM / sign

| Template               | Purpose                                 |
| ---------------------- | --------------------------------------- |
| `trivy-image-scan.yml` | CVE gate on `$CONTAINER_IMAGE`          |
| `syft-sbom.yml`        | SBOM artifact                           |
| `cosign-sign.yml`      | Sign image (needs Cosign key CI vars)   |
| `container-scan.yml`   | GitLab native container scanning report |

Details and Harbor-only flows: [container-scanning.md](../container-scanning.md).
