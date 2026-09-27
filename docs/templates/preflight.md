# Preflight — template dry-run validation

Every pipeline on this repo starts with a **preflight** stage that dry-runs
each reusable template through GitLab’s [CI Lint API](https://docs.gitlab.com/api/lint/)
(`dry_run=true`, `include_jobs=true`) and compares the result to golden
snapshots under `ci/preflight/expected/`.

## What runs

| Job                           | Scope                                                     |
| ----------------------------- | --------------------------------------------------------- |
| `preflight:root-and-examples` | Root `.gitlab-ci.yml` + all `examples/*.gitlab-ci.yml`    |
| `preflight:template` (matrix) | One parallel job per entry in `ci/preflight/catalog.json` |

Stage order: **preflight → lint → validate**. Lint/security dogfood jobs only
run after preflight succeeds.

## Catalog

`ci/preflight/catalog.json` lists:

- **templates** — fixture consumer YAML (include + forced `rules: when: on_success`)
  and expected merged job keys / scheduled job name / stage / script markers
- **examples** — rewrite `project: homelab/pipeline-templates` → `local:`, force
  jobs so gated `rules:` still schedule in dry-run, assert expected job names

## Local run

```bash
export GITLAB_TOKEN=…          # Owner/Maintainer PAT with api scope
export GITLAB_API_URL=http://192.168.68.12/api/v4
export GITLAB_PROJECT_PATH=homelab/pipeline-templates
export GITLAB_REF=main         # branch/tag only (raw SHAs are rejected)

python3 ci/preflight/validate.py                 # all
python3 ci/preflight/validate.py --id lint-yaml  # one template
python3 ci/preflight/validate.py --examples-only
python3 ci/preflight/validate.py --write-expected  # refresh goldens after intentional changes
```

In GitLab CI the jobs use `CI_JOB_TOKEN` automatically. If Job-Token is denied
for `POST /ci/lint`, set a masked `GITLAB_TOKEN` CI/CD variable on the project.

## After changing a template

1. Update the template YAML.
2. If job name/stage/script markers changed, edit `ci/preflight/catalog.json`.
3. Run `python3 ci/preflight/validate.py --write-expected`.
4. Commit the updated `ci/preflight/expected/<id>.json` files with the template change.

## Reuse in other repos

```yaml
include:
  - project: 'homelab/pipeline-templates'
    ref: main
    file: [/templates/preflight/dry-run.yml]

stages: [preflight, lint, …]

# Point catalog/validate at this templates project, or vendor a subset.
```
