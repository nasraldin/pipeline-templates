# Quality templates

## `quality:sonarqube` — `templates/quality/sonarqube.yml`

**What:** SonarScanner CLI against your SonarQube server.

**Requires CI variables:**

| Variable         | Example                                                |
| ---------------- | ------------------------------------------------------ |
| `SONAR_HOST_URL` | `http://192.168.68.112` (LAN) or public URL when ready |
| `SONAR_TOKEN`    | analysis token (masked)                                |

Optional:

| Variable            | Default                 |
| ------------------- | ----------------------- |
| `SONAR_PROJECT_KEY` | `$CI_PROJECT_PATH_SLUG` |
| `SONAR_EXTRA_ARGS`  | _(empty)_               |

```yaml
include:
  - project: 'homelab/pipeline-templates'
    ref: main
    file: [/templates/quality/sonarqube.yml]

stages: [quality]

quality:sonarqube:
  extends: .sonarqube_scan
```

Prefer injecting `SONAR_*` from Infisical into GitLab CI vars rather than committing tokens.

Job runs only when both `SONAR_TOKEN` and `SONAR_HOST_URL` are set.
