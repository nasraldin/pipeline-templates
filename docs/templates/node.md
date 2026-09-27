# Node templates (pnpm + Node 24)

Shared base: [`templates/common/node24.yml`](../../templates/common/node24.yml).

---

## `node:test` — `templates/node/test.yml`

Runs `pnpm install` then `pnpm run test` (override with `NODE_TEST_CMD`).

```yaml
include:
  - project: 'homelab/pipeline-templates'
    ref: main
    file: [/templates/node/test.yml]

stages: [test]

node:test:
  extends: .node_test
```

| Variable        | Default         |
| --------------- | --------------- |
| `NODE_TEST_CMD` | `pnpm run test` |
| `PNPM_FROZEN`   | `true`          |

Skips cleanly if there is no `package.json`.

---

## `node:build` — `templates/node/build.yml`

Runs `pnpm run build`. Publishes `dist/` as artifacts by default.

```yaml
include:
  - project: 'homelab/pipeline-templates'
    ref: main
    file: [/templates/node/build.yml]

stages: [build]

node:build:
  extends: .node_build
```

| Variable                    | Default                                                   |
| --------------------------- | --------------------------------------------------------- |
| `NODE_BUILD_CMD`            | `pnpm run build`                                          |
| `NODE_BUILD_ARTIFACT_PATHS` | `dist` (documented; artifact path is `dist/` in template) |

---

## Full app example

```yaml
include:
  - project: 'homelab/pipeline-templates'
    ref: main
    file:
      - /templates/lint/prettier.yml
      - /templates/lint/eslint.yml
      - /templates/node/test.yml
      - /templates/node/build.yml
      - /templates/security/gitleaks.yml
      - /templates/security/osv-scanner.yml

stages: [lint, test, build, validate]

lint:prettier:
  extends: .lint_prettier
lint:eslint:
  extends: .lint_eslint
node:test:
  extends: .node_test
node:build:
  extends: .node_build
  needs: [node:test]
security:gitleaks:
  extends: .security_gitleaks
security:osv-scanner:
  extends: .security_osv_scanner
```
