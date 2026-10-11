# Ajrly CI templates

GitHub remains the source repository. A GitLab coordination pipeline includes
`ajrly/pipeline.yml` at an immutable revision and supplies the same revision as
`AJRLY_TEMPLATES_REF` for trusted source-checkout tooling. Application code is
fetched at the exact dispatched SHA rather than the coordination project's SHA.

Required inputs are `AJRLY_SOURCE_REPOSITORY` (`nasraldin/ajrly-platform`),
`AJRLY_SOURCE_SHA`, `AJRLY_SOURCE_REF`, `AJRLY_EVENT`, and `AJRLY_EVENT_ID`.
PR events additionally supply `AJRLY_PR_NUMBER`. A dedicated read credential
`AJRLY_GITHUB_READ_TOKEN` supplies access to the private source repository.
The coordination project must be on this project's CI job-token allowlist.

`common/` defines verified checkout, Node 26.11.1 / pnpm 12.10.1, and bounded
resources. `checks/`, `tests/`, and `build/` run existing monorepo commands.
Integration PostGIS and Redis are disposable containers in the job pod and use
localhost. `security/` provides opt-in scanning through `RUN_SECURITY` (enabled
by default). `quality/`, `images/`, and `release/` are restricted to a separate
trusted pipeline mode and explicit enable flags; publication credentials must
never be placed in the ordinary checks coordinator.

The runner tag is `ajrly-kubernetes`. Managers and job pods use
`gitlab-runners`. There is no global lock on check/build jobs. Resource quota
and per-profile requests determine whether a job can fit immediately; twenty
runner slots do not promise twenty concurrent large builds.

Run `python3 -m unittest discover -s ajrly/tests` in an environment with PyYAML,
then `python3 ci/preflight/validate.py --id ajrly-monorepo` with the existing
GitLab lint authentication variables. The preflight expands the local includes
and checks the consumer job graph against its saved snapshot.
