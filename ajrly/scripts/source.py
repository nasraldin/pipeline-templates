#!/usr/bin/env python3
"""Verify GitHub source metadata and check out a precise commit without URL credentials."""
import argparse
import base64
import json
import os
import re
import subprocess
import time
import urllib.parse
import urllib.request
from pathlib import Path

REPOSITORY = "nasraldin/ajrly-platform"
SHA = re.compile(r"[0-9a-f]{40}\Z")
REF = re.compile(r"refs/(?:heads/[A-Za-z0-9_./-]+|pull/[1-9][0-9]*/head)\Z")


def validate_event(payload: dict, github_metadata: dict) -> dict:
    repository = payload.get("repository")
    sha = payload.get("sha", "")
    ref = payload.get("ref", "")
    event = payload.get("event")
    if repository != REPOSITORY or github_metadata.get("repository") != repository:
        raise ValueError("unexpected source repository")
    if not SHA.fullmatch(sha) or not REF.fullmatch(ref) or ".." in ref or "//" in ref or ref.endswith("/"):
        raise ValueError("invalid source SHA or ref")
    if event not in ("push", "pull_request", "workflow_dispatch") or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", payload.get("event_id", "")):
        raise ValueError("invalid source event")
    if github_metadata.get("sha") != sha or github_metadata.get("ref") != ref:
        raise ValueError("GitHub source ref has changed; dispatch the current commit")
    result = {"repository": repository, "sha": sha, "ref": ref,
              "event": event, "event_id": payload["event_id"], "trusted": False}
    if event == "pull_request":
        number = str(payload.get("pr_number", ""))
        if not number.isdigit() or ref != "refs/pull/" + number + "/head":
            raise ValueError("invalid pull request identity")
        if github_metadata.get("head_repository") != repository:
            raise ValueError("fork pull requests require a separately approved unprivileged path")
        target_sha = github_metadata.get("target_sha", "")
        target_ref = github_metadata.get("target_ref", "")
        if not SHA.fullmatch(target_sha) or not REF.fullmatch(target_ref) or not target_ref.startswith("refs/heads/"):
            raise ValueError("invalid pull request target")
        if payload.get("target_sha") != target_sha or payload.get("target_ref") != target_ref:
            raise ValueError("GitHub pull request target has changed; dispatch the current target")
        result.update(pr_number=number, target_sha=target_sha, target_ref=target_ref)
    elif ref.startswith("refs/heads/"):
        result["trusted"] = github_metadata.get("protected") is True
    else:
        raise ValueError("branch event requires a branch ref")
    return result


def github_metadata(payload: dict) -> dict:
    if payload.get("repository") != REPOSITORY:
        raise ValueError("unexpected source repository")
    token = os.environ.get("AJRLY_GITHUB_READ_TOKEN", "")
    def get(path):
        headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
        if token:
            headers["Authorization"] = "Bearer " + token
        request = urllib.request.Request("https://api.github.com/repos/" + REPOSITORY + path, headers=headers)
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.load(response)
    result = {"repository": REPOSITORY, "ref": payload.get("ref")}
    if payload.get("event") == "pull_request":
        number = str(payload.get("pr_number", ""))
        if not re.fullmatch(r"[1-9][0-9]*", number):
            raise ValueError("invalid PR number")
        pr = get("/pulls/" + number)
        if pr["state"] != "open":
            raise ValueError("pull request is no longer open")
        result.update(sha=pr["head"]["sha"], head_repository=(pr["head"].get("repo") or {}).get("full_name"),
                      target_sha=pr["base"]["sha"], target_ref="refs/heads/" + pr["base"]["ref"])
    else:
        ref = payload.get("ref", "")
        if not ref.startswith("refs/heads/") or not REF.fullmatch(ref):
            raise ValueError("invalid branch")
        branch = get("/branches/" + urllib.parse.quote(ref[len("refs/heads/"):], safe=""))
        result.update(sha=branch["commit"]["sha"], protected=branch.get("protected") is True)
    return result


def git_environment(token: str) -> dict:
    environment = dict(os.environ)
    if token:
        environment.update(GIT_CONFIG_COUNT="1", GIT_CONFIG_KEY_0="http.https://github.com/.extraheader",
                           GIT_CONFIG_VALUE_0="Authorization: Basic " + base64.b64encode(("x-access-token:" + token).encode()).decode())
    return environment


def is_affected(files: list, project: str) -> bool:
    return any(not filename.startswith("apps/") or filename.startswith(project.rstrip("/") + "/")
               or (project == "apps/backend/worker" and filename.startswith("apps/backend/api/")) for filename in files)


def git_fetch(destination: Path, arguments: list, token: str = "") -> None:
    command = ["git", "-C", str(destination), "-c", "http.version=HTTP/1.1", "fetch", "--no-tags", *arguments]
    for attempt in range(3):
        try:
            subprocess.run(command, text=True, capture_output=True, check=True,
                           env=git_environment(token), timeout=120)
            return
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as error:
            message = error.stderr or "git transport timeout"
            if isinstance(message, bytes):
                message = message.decode("utf-8", errors="replace")
            for secret in (token, base64.b64encode(("x-access-token:" + token).encode()).decode() if token else ""):
                if secret:
                    message = message.replace(secret, "[REDACTED]")
            message = re.sub(r"(?i)authorization:[^\r\n]*", "Authorization: [REDACTED]", message)
            message = re.sub(r"https://[^/\s]+@", "https://[REDACTED]@", message)
            transient = isinstance(error, subprocess.TimeoutExpired) or bool(re.search(
                r"curl (?:6|7|16|18|28|35|52|55|56|92)\b|HTTP (?:429|5\d\d)|returned error: (?:429|5\d\d)|early EOF|remote end hung up",
                message, re.IGNORECASE))
            if not transient or attempt == 2:
                raise RuntimeError("git fetch failed: " + message.strip()[:500]) from None
            print("Transient Git transport failure; retrying the same fetch", flush=True)
            time.sleep(2 ** (attempt + 1))


def checkout_exact(url: str, ref: str, sha: str, destination: Path, token: str = "", full_history: bool = False) -> None:
    if not SHA.fullmatch(sha) or ref.startswith("-") or ":" in ref:
        raise ValueError("invalid checkout identity")
    destination = Path(destination)
    destination.mkdir(parents=True, exist_ok=True)
    environment = git_environment(token)
    def git(*args):
        return subprocess.check_output(["git", "-C", str(destination), *args], text=True,
                                       stderr=subprocess.DEVNULL, env=environment).strip()
    git("init", "-q")
    git("remote", "add", "origin", url)
    git_fetch(destination, [*([] if full_history else ["--depth=2"]), "origin", ref], token)
    if git("rev-parse", "FETCH_HEAD") != sha:
        raise ValueError("fetched source does not match dispatched SHA")
    git("checkout", "--detach", "-q", sha)
    if git("rev-parse", "HEAD") != sha:
        raise ValueError("checkout SHA mismatch")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=["checkout", "verify", "affected"])
    parser.add_argument("--destination", default=".")
    parser.add_argument("--path")
    args = parser.parse_args()
    if args.command == "affected":
        manifest = json.loads(Path(args.destination, ".ajrly-source.json").read_text())
        base = manifest.get("target_sha", "HEAD^")
        try:
            files = subprocess.check_output(["git", "-C", args.destination, "diff", "--name-only", base, "HEAD"],
                                            text=True, stderr=subprocess.DEVNULL).splitlines()
        except subprocess.CalledProcessError:
            print("true")
            return
        print("true" if is_affected(files, args.path or "") else "false")
        return
    payload = {key: os.environ.get("AJRLY_" + env, "") for key, env in
               (("repository", "SOURCE_REPOSITORY"), ("sha", "SOURCE_SHA"), ("ref", "SOURCE_REF"),
                ("event", "EVENT"), ("event_id", "EVENT_ID"), ("pr_number", "PR_NUMBER"),
                ("target_sha", "TARGET_SHA"), ("target_ref", "TARGET_REF"))}
    manifest = validate_event(payload, github_metadata(payload))
    if args.command == "checkout":
        checkout_exact("https://github.com/" + REPOSITORY + ".git", manifest["ref"], manifest["sha"], Path(args.destination),
                       os.environ.get("AJRLY_GITHUB_READ_TOKEN", ""),
                       full_history=os.environ.get("AJRLY_SOURCE_FULL_HISTORY") == "true")
        if manifest.get("target_ref"):
            git_fetch(Path(args.destination), ["--depth=1", "origin", manifest["target_sha"]],
                      os.environ.get("AJRLY_GITHUB_READ_TOKEN", ""))
        Path(args.destination, ".ajrly-source.json").write_text(json.dumps(manifest, sort_keys=True) + "\n")
    else:
        print(json.dumps(manifest, sort_keys=True))


if __name__ == "__main__":
    main()
