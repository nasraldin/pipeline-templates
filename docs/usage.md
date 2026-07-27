# Pipeline template usage

## lab-home-k8s

```bash
# Local (same as CI)
cd terraform
TF_ACTION=plan TF_TARGET_GUESTS=infra-01 ./scripts/ci-run.sh

cd ../ansible
ANSIBLE_PLAYBOOK=playbooks/infra.yml ANSIBLE_LIMIT=infra-01 ./scripts/ci-run.sh
```

## lab-home-gitops

Push to `platform/keycloak/**` only → GitLab runs keycloak-scoped validate jobs
(see `maps/lab-home-gitops-services.yml`).

## App repos

### Container image (build → scan → publish)

```yaml
include:
  - project: homelab/pipeline-templates
    ref: main
    file:
      - templates/lint/yaml.yml
      - templates/container/build.yml
      - templates/container/trivy-image-scan.yml
      - templates/container/gitlab-container-scanning.yml
      - templates/container/harbor-push.yml

stages:
  - lint
  - build
  - scan
  - publish
```

Set `HARBOR_PROJECT`, `HARBOR_USERNAME`, and `HARBOR_PASSWORD` for Harbor push.
See [container-scanning.md](container-scanning.md).

### Quality gate only (no image)

```yaml
include:
  - project: homelab/pipeline-templates
    ref: main
    file:
      - templates/lint/yaml.yml
      - templates/quality/sonarqube.yml
```

Set `SONAR_HOST_URL` and `SONAR_TOKEN` in CI/CD variables.
