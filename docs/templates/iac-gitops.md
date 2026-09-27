# Terraform & GitOps templates

## Terraform

| Template                           | Job                           | Stage      | Role                                       |
| ---------------------------------- | ----------------------------- | ---------- | ------------------------------------------ |
| `templates/terraform/init.yml`     | `terraform:init`              | `init`     | `terraform init` (+ artifact `.terraform`) |
| `templates/terraform/validate.yml` | `terraform:validate`          | `validate` | `terraform validate`                       |
| `templates/terraform/plan.yml`     | `terraform:plan` (+targeted)  | `plan`     | plan → `tfplan` / `tfplan.txt`             |
| `templates/terraform/apply.yml`    | `terraform:apply` (+targeted) | `apply`    | manual apply                               |
| `templates/terraform/destroy.yml`  | `terraform:destroy:targeted`  | `apply`    | targeted destroy only                      |
| `templates/lint/terraform.yml`     | `lint:terraform`              | `lint`     | `fmt -check` + offline init + validate     |

### Recommended consumer stages

```yaml
stages: [lint, init, validate, plan, apply]

include:
  - project: 'homelab/pipeline-templates'
    ref: main
    file:
      - /templates/lint/terraform.yml
      - /templates/terraform/init.yml
      - /templates/terraform/validate.yml
      - /templates/terraform/plan.yml
      - /templates/terraform/apply.yml

lint:terraform:
  extends: .lint_terraform

terraform:init:
  extends: .terraform_init

terraform:validate:
  extends: .terraform_validate
  needs: [terraform:init]

terraform:plan:
  extends: .terraform_plan
  needs: [terraform:validate]

terraform:apply:
  extends: .terraform_apply
  needs: [terraform:plan]
```

### Variables

| Variable                | Default     | Meaning                                        |
| ----------------------- | ----------- | ---------------------------------------------- |
| `TF_DIR`                | `terraform` | Working directory                              |
| `TF_BACKEND`            | `true`      | `init` only — set `false` for `-backend=false` |
| `TF_INIT_ARGS`          | _(empty)_   | Extra `terraform init` flags                   |
| `TF_INIT_IF_MISSING`    | `true`      | `validate` runs offline init if needed         |
| `TF_TARGET_GUESTS`      | —           | Enables `*:targeted` jobs                      |
| `TF_ALLOW_FULL_DESTROY` | `false`     | Required for non-targeted destroy              |

Plan still skips the live Proxmox plan when `TF_VAR_proxmox_*` are unset (init/validate still run).

---

## Helm / GitOps

| Template                             | Job                    | Stage      | Role                                               |
| ------------------------------------ | ---------------------- | ---------- | -------------------------------------------------- |
| `templates/gitops/helm-lint.yml`     | `gitops:helm-lint`     | `lint`     | Real `helm lint` on local `Chart.yaml` trees       |
| `templates/gitops/helm-template.yml` | `gitops:helm-template` | `validate` | `helm template` → artifact `helm-rendered/`        |
| `templates/gitops/helm-dry-run.yml`  | `gitops:helm-dry-run`  | `validate` | `helm upgrade --install --dry-run=client\|server`  |
| `templates/gitops/values-lint.yml`   | `gitops:values-lint`   | `lint`     | Parse Argo `apps.yaml` / `values.yaml` (no charts) |
| `templates/gitops/kubeconform.yml`   | `gitops:kubeconform`   | `validate` | Schema-check plain manifests                       |
| `templates/gitops/argocd-diff.yml`   | `gitops:argocd-diff`   | `validate` | `argocd app diff --local`                          |

### Helm example (local chart)

```yaml
variables:
  HELM_CHART: charts/myapp
  HELM_RELEASE_NAME: myapp
  HELM_NAMESPACE: apps
  HELM_VALUES_FILES: charts/myapp/values.yaml values-prod.yaml
  HELM_DRY_RUN_MODE: client

include:
  - project: 'homelab/pipeline-templates'
    ref: main
    file:
      - /templates/gitops/helm-lint.yml
      - /templates/gitops/helm-template.yml
      - /templates/gitops/helm-dry-run.yml

gitops:helm-lint:
  extends: .gitops_helm_lint
gitops:helm-template:
  extends: .gitops_helm_template
gitops:helm-dry-run:
  extends: .gitops_helm_dry_run
```

For Argo-only trees (values + apps, no `Chart.yaml`), use `gitops:values-lint` + `gitops:kubeconform`.
