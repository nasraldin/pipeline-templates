#!/usr/bin/env python3
"""Preflight: dry-run every pipeline template via GitLab CI Lint and assert expected output.

Usage:
  python3 ci/preflight/validate.py                  # all templates + examples + root
  python3 ci/preflight/validate.py --id lint-yaml   # one catalog entry
  python3 ci/preflight/validate.py --examples-only
  python3 ci/preflight/validate.py --root-only
  python3 ci/preflight/validate.py --write-expected # refresh golden JSON snapshots

Auth (first match):
  CI_JOB_TOKEN  → Job-Token header (preferred in GitLab CI)
  GITLAB_TOKEN / PRIVATE_TOKEN → PRIVATE-TOKEN header

Env:
  CI_API_V4_URL / GITLAB_API_URL  (default http://192.168.68.12/api/v4)
  CI_PROJECT_ID / GITLAB_PROJECT_ID / GITLAB_PROJECT_PATH
  CI_COMMIT_REF_NAME / GITLAB_REF  (branch/tag for include resolution; default main)
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
CATALOG_PATH = Path(__file__).resolve().parent / "catalog.json"
EXPECTED_DIR = Path(__file__).resolve().parent / "expected"


def api_base() -> str:
    # Prefer explicit GITLAB_API_URL (LAN) over CI_API_V4_URL (often Cloudflare).
    return (
        os.environ.get("GITLAB_API_URL")
        or os.environ.get("CI_API_V4_URL")
        or "http://192.168.68.12/api/v4"
    ).rstrip("/")


def project_id() -> str:
    return (
        os.environ.get("CI_PROJECT_ID")
        or os.environ.get("GITLAB_PROJECT_ID")
        or os.environ.get("GITLAB_PROJECT_PATH")
        or "homelab/pipeline-templates"
    )


def auth_headers() -> dict[str, str]:
    # Prefer PAT for CI Lint — Job-Token often cannot call POST /ci/lint (404).
    token = os.environ.get("GITLAB_TOKEN") or os.environ.get("PRIVATE_TOKEN")
    if token:
        return {"PRIVATE-TOKEN": token}
    job = os.environ.get("CI_JOB_TOKEN")
    if job:
        return {"JOB-TOKEN": job}
    raise SystemExit(
        "Missing GITLAB_TOKEN (or CI_JOB_TOKEN) for GitLab CI Lint API"
    )



def lint_ref() -> str:
    # Branch/tag only — raw SHAs return "Reference not found" on this GitLab.
    return (
        os.environ.get("CI_COMMIT_REF_NAME")
        or os.environ.get("GITLAB_REF")
        or "main"
    )


def ci_lint(content: str) -> dict[str, Any]:
    pid = urllib.request.quote(str(project_id()), safe="")
    url = f"{api_base()}/projects/{pid}/ci/lint"
    payload = {
        "content": content,
        "dry_run": True,
        "include_jobs": True,
        "ref": lint_ref(),
    }
    body = json.dumps(payload).encode()
    req = urllib.request.Request(
        url,
        data=body,
        headers={**auth_headers(), "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            return json.load(resp)
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise SystemExit(f"CI Lint HTTP {exc.code}: {detail}") from exc


def yaml_quote(value: str) -> str:
    if re.search(r'[:#\[\]{}",\'\n]', value) or value == "":
        return json.dumps(value)
    return value


def build_template_fixture(entry: dict[str, Any]) -> str:
    lines: list[str] = [
        f"# preflight fixture: {entry['id']}",
        "include:",
        f"  - local: {entry['template']}",
        "stages:",
    ]
    for stage in entry["stages"]:
        lines.append(f"  - {stage}")
    lines.append("")
    for job in entry["jobs"]:
        lines.append(f"{job['name']}:")
        lines.append(f"  extends: {job['extends']}")
        lines.append(f"  stage: {job['stage']}")
        lines.append("  rules:")
        lines.append("    - when: on_success")
        # Clear template needs (e.g. container:harbor-push → container:build)
        lines.append("  needs: []")
        if job.get("script"):
            lines.append("  script:")
            for cmd in job["script"]:
                lines.append(f"    - {yaml_quote(cmd)}")
        lines.append("")
    return "\n".join(lines)


def rewrite_example_to_local(text: str) -> str:
    """Rewrite include: project:homelab/pipeline-templates → local paths for linting."""
    # Replace project include blocks with local includes (line-safe, not DOTALL).
    pattern = re.compile(
        r"(?m)^(\s*)-\s+project:\s*[\"']?homelab/pipeline-templates[\"']?\s*\n"
        r"(?:\1\s+ref:\s*[^\n]+\n)?"
        r"\1\s+file:\s*\n"
        r"((?:\1\s+-\s+[^\n]+\n)+)"
    )

    def repl(match: re.Match[str]) -> str:
        indent = match.group(1)
        files_block = match.group(2)
        files = re.findall(r"-\s+/?([^\s]+\.yml)", files_block)
        out = [f"{indent}- local: {fpath.lstrip('/')}" for fpath in files]
        return "\n".join(out) + "\n"

    return pattern.sub(repl, text)


def expand_local_includes(content: str, base: Path = ROOT) -> str:
    """Inline `include: local:` from the workspace; keep template/project includes."""
    match = re.search(r"(?m)^include:\n((?:[ \t].+\n)*)", content)
    if not match:
        return content

    block = match.group(0)
    # Split include entries on top-level "- " lines inside the include block.
    entries = re.split(r"(?m)(?=^[ \t]+- )", block)
    header, *items = entries
    if not items:
        return content

    local_blobs: list[str] = []
    kept: list[str] = []
    for item in items:
        local_match = re.search(r"local:\s*[\"']?([^\"'\n]+)[\"']?", item)
        if local_match:
            rel = local_match.group(1).lstrip("/")
            path = base / rel
            if not path.is_file():
                raise FileNotFoundError(f"local include not found: {rel}")
            nested = expand_local_includes(path.read_text(encoding="utf-8"), base)
            local_blobs.append(f"# --- begin include {rel} ---\n{nested}\n# --- end include {rel} ---\n")
        else:
            kept.append(item)

    rest = content[: match.start()] + content[match.end() :]
    kept_block = ""
    if kept:
        kept_block = "include:\n" + "".join(kept)
        if not kept_block.endswith("\n"):
            kept_block += "\n"

    return "".join(local_blobs) + kept_block + rest


def merged_top_level_keys(merged_yaml: str | None) -> list[str]:
    keys: list[str] = []
    skip = {
        "stages",
        "variables",
        "workflow",
        "default",
        "include",
        "image",
        "services",
        "cache",
        "before_script",
        "after_script",
    }
    for line in (merged_yaml or "").splitlines():
        # GitLab quotes hidden job names: ".lint_yaml":
        m = re.match(r'^"?([\w./:-]+)"?:\s*$', line)
        if m:
            key = m.group(1)
            if key not in skip:
                keys.append(key)
    return keys


def job_script_blob(job: dict[str, Any]) -> str:
    parts: list[str] = []
    for field in ("before_script", "script", "after_script"):
        val = job.get(field) or []
        if isinstance(val, list):
            parts.extend(str(x) for x in val)
        elif val:
            parts.append(str(val))
    return "\n".join(parts)


def assert_template(entry: dict[str, Any], data: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    if not data.get("valid"):
        errors.append(f"invalid CI: {data.get('errors')}")
        return errors

    merged = merged_top_level_keys(data.get("merged_yaml"))
    for key in entry.get("expected_merged") or []:
        if key not in merged:
            errors.append(f"missing merged key {key!r}; have={merged}")

    pipeline_jobs = {j["name"]: j for j in (data.get("jobs") or [])}
    for job_spec in entry["jobs"]:
        name = job_spec["name"]
        if name not in pipeline_jobs:
            errors.append(
                f"expected scheduled job {name!r}; got={sorted(pipeline_jobs)}"
            )
            continue
        got = pipeline_jobs[name]
        if got.get("stage") != job_spec["stage"]:
            errors.append(
                f"{name}: stage want={job_spec['stage']!r} got={got.get('stage')!r}"
            )
        blob = job_script_blob(got)
        for needle in job_spec.get("script_contains") or []:
            if needle not in blob:
                errors.append(
                    f"{name}: script missing {needle!r}"
                )
    return errors


def assert_example(entry: dict[str, Any], data: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    if not data.get("valid"):
        errors.append(f"invalid CI: {data.get('errors')}")
        return errors
    merged = set(merged_top_level_keys(data.get("merged_yaml")))
    for name in entry.get("expected_jobs") or []:
        if name not in merged and not any(
            j.get("name") == name for j in (data.get("jobs") or [])
        ):
            # Prefer merged keys (jobs may be filtered by rules).
            if name not in merged:
                errors.append(
                    f"expected job key {name!r} in merged YAML; have={sorted(merged)}"
                )
    return errors


def snapshot_from_result(
    entry_id: str, kind: str, data: dict[str, Any]
) -> dict[str, Any]:
    return {
        "id": entry_id,
        "kind": kind,
        "valid": data.get("valid"),
        "errors": data.get("errors") or [],
        "warnings": data.get("warnings") or [],
        "merged_keys": merged_top_level_keys(data.get("merged_yaml")),
        "jobs": [
            {
                "name": j.get("name"),
                "stage": j.get("stage"),
                "when": j.get("when"),
                "allow_failure": j.get("allow_failure"),
            }
            for j in (data.get("jobs") or [])
        ],
        "includes": [
            i.get("location") for i in (data.get("includes") or []) if i.get("location")
        ],
    }


def write_expected(entry_id: str, snap: dict[str, Any]) -> None:
    EXPECTED_DIR.mkdir(parents=True, exist_ok=True)
    path = EXPECTED_DIR / f"{entry_id}.json"
    path.write_text(json.dumps(snap, indent=2) + "\n", encoding="utf-8")


def compare_expected(entry_id: str, snap: dict[str, Any]) -> list[str]:
    path = EXPECTED_DIR / f"{entry_id}.json"
    if not path.exists():
        return [
            f"missing golden expected file {path.relative_to(ROOT)} "
            f"(run with --write-expected)"
        ]
    golden = json.loads(path.read_text(encoding="utf-8"))
    errors: list[str] = []
    for field in ("valid", "merged_keys", "jobs"):
        if golden.get(field) != snap.get(field):
            errors.append(
                f"expected/{entry_id}.json field {field!r} mismatch\n"
                f"  want={json.dumps(golden.get(field), indent=2)}\n"
                f"  got ={json.dumps(snap.get(field), indent=2)}"
            )
    return errors


def load_catalog() -> dict[str, Any]:
    return json.loads(CATALOG_PATH.read_text(encoding="utf-8"))


def run_root(write: bool) -> list[tuple[str, list[str]]]:
    raw = (ROOT / ".gitlab-ci.yml").read_text(encoding="utf-8")
    # Expand workspace locals so new preflight includes validate before push.
    content = expand_local_includes(raw)
    data = ci_lint(content)
    snap = snapshot_from_result("root-gitlab-ci", "root", data)
    if write:
        write_expected("root-gitlab-ci", snap)
    errors = []
    if not data.get("valid"):
        errors.append(f"root .gitlab-ci.yml invalid: {data.get('errors')}")
    else:
        errors.extend(compare_expected("root-gitlab-ci", snap))
    return [("root-gitlab-ci", errors)]


def run_templates(
    catalog: dict[str, Any], only_id: str | None, write: bool
) -> list[tuple[str, list[str]]]:
    results: list[tuple[str, list[str]]] = []
    for entry in catalog["templates"]:
        if only_id and entry["id"] != only_id:
            continue
        fixture = expand_local_includes(build_template_fixture(entry))
        data = ci_lint(fixture)
        snap = snapshot_from_result(entry["id"], "template", data)
        if write:
            write_expected(entry["id"], snap)
        errors = assert_template(entry, data)
        if data.get("valid"):
            errors.extend(compare_expected(entry["id"], snap))
        results.append((entry["id"], errors))
    return results


def run_examples(
    catalog: dict[str, Any], only_id: str | None, write: bool
) -> list[tuple[str, list[str]]]:
    results: list[tuple[str, list[str]]] = []
    for entry in catalog.get("examples") or []:
        if only_id and entry["id"] != only_id:
            continue
        raw = (ROOT / entry["path"]).read_text(encoding="utf-8")
        content = (
            rewrite_example_to_local(raw)
            if entry.get("rewrite_project_to_local", True)
            else raw
        )
        # Force rules for jobs defined in the example so dry-run is non-empty
        # when templates use exists:/if: gates. Append overrides only for named expected jobs.
        force_lines = ["", "# preflight: force scheduling for dry-run"]
        for job in entry.get("force_jobs") or []:
            force_lines.append(f"{job['name']}:")
            force_lines.append(f"  extends: {job['extends']}")
            force_lines.append(f"  stage: {job['stage']}")
            force_lines.append("  rules:")
            force_lines.append("    - when: on_success")
            force_lines.append("  needs: []")
        content = content + "\n".join(force_lines) + "\n"
        # Expand locals from the workspace so example dry-runs match this checkout.
        content = expand_local_includes(content)

        data = ci_lint(content)
        snap = snapshot_from_result(entry["id"], "example", data)
        if write:
            write_expected(entry["id"], snap)
        errors = assert_example(entry, data)
        if data.get("valid"):
            errors.extend(compare_expected(entry["id"], snap))
        results.append((entry["id"], errors))
    return results


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--id", help="Run a single catalog/example/root id")
    parser.add_argument("--examples-only", action="store_true")
    parser.add_argument("--root-only", action="store_true")
    parser.add_argument("--templates-only", action="store_true")
    parser.add_argument(
        "--write-expected",
        action="store_true",
        help="Refresh ci/preflight/expected/*.json golden snapshots",
    )
    parser.add_argument(
        "--list",
        action="store_true",
        help="Print catalog ids (for CI parallel:matrix)",
    )
    args = parser.parse_args()
    catalog = load_catalog()

    if args.list:
        ids = [t["id"] for t in catalog["templates"]]
        print("\n".join(ids))
        return 0

    os.chdir(ROOT)
    results: list[tuple[str, list[str]]] = []

    run_all = not (args.examples_only or args.root_only or args.templates_only or args.id)

    if args.root_only or run_all or args.id in (None, "root-gitlab-ci"):
        if not args.examples_only and not args.templates_only:
            if args.id in (None, "root-gitlab-ci") or args.root_only:
                results.extend(run_root(args.write_expected))

    if args.templates_only or run_all or (
        args.id and any(t["id"] == args.id for t in catalog["templates"])
    ):
        if not args.examples_only and not args.root_only:
            results.extend(run_templates(catalog, args.id, args.write_expected))

    if args.examples_only or run_all or (
        args.id and any(e["id"] == args.id for e in catalog.get("examples") or [])
    ):
        if not args.templates_only and not args.root_only:
            results.extend(run_examples(catalog, args.id, args.write_expected))

    failed = 0
    for entry_id, errors in results:
        if errors:
            failed += 1
            print(f"FAIL  {entry_id}")
            for err in errors:
                for line in err.splitlines():
                    print(f"      {line}")
        else:
            print(f"PASS  {entry_id}")

    print(
        f"\n{len(results) - failed}/{len(results)} passed"
        + (" (wrote expected)" if args.write_expected else "")
    )
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
