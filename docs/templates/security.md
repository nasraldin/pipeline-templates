# Security templates

---

## `security:gitleaks` — `templates/security/gitleaks.yml`

**What:** Secret leak scan (API keys, passwords, tokens) on the working tree.

**Default:** `GITLEAKS_NO_GIT=true` (scan files, not full history).

```yaml
include:
  - project: 'homelab/pipeline-templates'
    ref: main
    file: [/templates/security/gitleaks.yml]

security:gitleaks:
  extends: .security_gitleaks
```

| Variable             | Default | Meaning                             |
| -------------------- | ------- | ----------------------------------- |
| `GITLEAKS_EXIT_CODE` | `1`     | Fail pipeline on findings           |
| `GITLEAKS_NO_GIT`    | `true`  | `gitleaks dir` vs `gitleaks detect` |
| `GITLEAKS_CONFIG`    | —       | Path to custom config               |

Optional repo file: `.gitleaks.toml`.

---

## `security:osv-scanner` — `templates/security/osv-scanner.yml`

**What:** [OSV-Scanner](https://google.github.io/osv-scanner/) on lockfiles / manifests (pnpm, npm, Go, Python, …).

```yaml
include:
  - project: 'homelab/pipeline-templates'
    ref: main
    file: [/templates/security/osv-scanner.yml]

security:osv-scanner:
  extends: .security_osv_scanner
```

| Variable              | Default                       |
| --------------------- | ----------------------------- |
| `OSV_SCANNER_VERSION` | `v2.0.1`                      |
| `OSV_ARGS`            | _(empty)_                     |
| `OSV_EXIT_CODE`       | `1` (set `0` for report-only) |

No API token required.

---

## `security:snyk` — `templates/security/snyk.yml`

**What:** Snyk SCA (`snyk test`). **Requires** `SNYK_TOKEN` (masked CI variable).

```yaml
include:
  - project: 'homelab/pipeline-templates'
    ref: main
    file: [/templates/security/snyk.yml]

variables:
  SNYK_CMD: 'snyk test --severity-level=high'

security:snyk:
  extends: .security_snyk
```

| Variable     | Default                           |
| ------------ | --------------------------------- |
| `SNYK_TOKEN` | **required**                      |
| `SNYK_CMD`   | `snyk test --severity-level=high` |
| `SNYK_FAIL`  | `true`                            |
| `SNYK_IMAGE` | `snyk/snyk:node`                  |

Job is skipped when `SNYK_TOKEN` is unset.

---

## `security:trivy-fs-scan` — `templates/security/trivy-filesystem.yml`

Filesystem / config CVE scan (no container image). Prefer for infra/GitOps repos.
See template file for variables.

---

## Recommended security bundle

```yaml
include:
  - project: 'homelab/pipeline-templates'
    ref: main
    file:
      - /templates/security/gitleaks.yml
      - /templates/security/osv-scanner.yml
      # - /templates/security/snyk.yml   # when SNYK_TOKEN is set

security:gitleaks:
  extends: .security_gitleaks
security:osv-scanner:
  extends: .security_osv_scanner
```
