# Lint & format templates

All Node-based linters use **Node 24** + **pnpm** (Corepack) via
[`templates/common/node24.yml`](../../templates/common/node24.yml).

Override globally in the consumer:

```yaml
variables:
  NODE_IMAGE: 'node:24-bookworm'
  PNPM_VERSION: '12.7.0'
```

---

## `lint:prettier` — `templates/lint/prettier.yml`

**What:** Format check with Prettier 3 for JS/TS, JSON, YAML, Markdown, CSS, HTML, Vue.

**When:** Any repo with those file types. Does **not** require `package.json`.

**Example:**

```yaml
include:
  - project: 'homelab/pipeline-templates'
    ref: main
    file: [/templates/lint/prettier.yml]

lint:prettier:
  extends: .lint_prettier
```

**Variables:**

| Variable           | Default   | Meaning                       |
| ------------------ | --------- | ----------------------------- |
| `PRETTIER_VERSION` | `3.9.9`   | Prettier via `pnpm dlx` |
| `PRETTIER_GLOB`    | `.`       | Path to check                 |
| `PRETTIER_ARGS`    | _(empty)_ | Extra CLI flags               |

Commit a `.prettierrc.json` in the consumer (or rely on defaults).

---

## `lint:eslint` — `templates/lint/eslint.yml`

**What:** Runs `pnpm run lint` (override with `ESLINT_CMD`).

**Requires:** `package.json` (+ lockfile recommended).

```yaml
include:
  - project: 'homelab/pipeline-templates'
    ref: main
    file: [/templates/lint/eslint.yml]

lint:eslint:
  extends: .lint_eslint
```

| Variable      | Default         |
| ------------- | --------------- |
| `ESLINT_CMD`  | `pnpm run lint` |
| `PNPM_FROZEN` | `true`          |

---

## `lint:yaml` — `templates/lint/yaml.yml`

**What:** `yamllint` (syntax/style). Pair with Prettier for formatting.

Uses `.yamllint.yaml` if present; otherwise relaxed defaults (`line-length: 200`).

---

## `lint:json` — `templates/lint/json.yml`

**What:** `jq empty` syntax validation for `*.json`. Formatting → Prettier.

---

## `lint:markdown` — `templates/lint/markdown.yml`

**What:** `markdownlint-cli` via `pnpm dlx`.

Optional configs: `.markdownlint.json`, `.markdownlint.yaml`, `.markdownlint-cli2.jsonc`.

---

## `lint:shell` — `templates/lint/shell.yml`

**What:** ShellCheck on `*.sh` / `*.bash`.

---

## `lint:editorconfig` — `templates/lint/editorconfig.yml`

**What:** [editorconfig-checker](https://github.com/editorconfig-checker/editorconfig-checker) for plain text / mixed types (`.txt`, `.toml`, `.env.example`, README, …).

Commit a root `.editorconfig`. Override `rules:` if you want it on every pipeline.

---

## `lint:terraform` / `lint:ansible`

See existing IaC templates under `templates/lint/terraform.yml` and `templates/lint/ansible.yml` (fmt + validate / ansible-lint).

---

## Suggested bundle for a docs/infra repo

```yaml
include:
  - project: 'homelab/pipeline-templates'
    ref: main
    file:
      - /templates/lint/yaml.yml
      - /templates/lint/json.yml
      - /templates/lint/markdown.yml
      - /templates/lint/shell.yml
      - /templates/lint/prettier.yml
      - /templates/security/gitleaks.yml

stages: [lint, validate]

lint:yaml:
  extends: .lint_yaml
lint:json:
  extends: .lint_json
lint:markdown:
  extends: .lint_markdown
lint:shell:
  extends: .lint_shell
lint:prettier:
  extends: .lint_prettier
security:gitleaks:
  extends: .security_gitleaks
```
