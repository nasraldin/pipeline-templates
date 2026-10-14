#!/usr/bin/env python3
"""Verified pnpm-store dependency archive keying, restore, and producer publish API.

Ordinary CI jobs only restore from a canonical read-only prefix after exact-source
verification. publish_store is a pure library for protected platform producers; it
does not authorize writes from caller trust flags.
"""
from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import platform
import re
import shutil
import stat
import tarfile
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Iterable, Mapping, Optional, Protocol, Tuple

CACHE_SCHEMA = 1
KEY_PREFIX = f"dep-v{CACHE_SCHEMA}-"
MAX_COMPRESSED_BYTES = 2 * 1024 ** 3
MAX_AGE_SECONDS = 7 * 24 * 3600
STORE_DIRNAME = ".pnpm-store"
DEFAULT_PREFIX = "canonical/"
SOURCE_MANIFEST = ".ajrly-source.json"
PATCH_DIRNAME = "patches"
KEY_RE = re.compile(rf"^{re.escape(KEY_PREFIX)}[0-9a-f]{{64}}$")
IMMUTABLE_TEMPLATES_REF_RE = re.compile(r"^[0-9a-f]{40}$")
PRIVATE_NPM_HOST_SUFFIX = ".ajrly.com"
PRIVATE_NPM_HOST = "npm.ajrly.com"

TEMPLATES_SCRIPT_RELATIVE = {
    "source": "ajrly/scripts/source.py",
    "dependency_cache": "ajrly/scripts/dependency_cache.py",
}


class S3Reader(Protocol):
    def fetch_bytes(self, object_key: str) -> bytes: ...
    def fetch(self, object_key: str, destination: Path) -> int: ...


class S3Writer(Protocol):
    def put(self, object_key: str, data: bytes, *, content_type: str = "") -> None: ...


@dataclass(frozen=True)
class RestoreResult:
    status: str  # hit | miss | rejected
    bytes: int = 0
    duration: float = 0.0
    reason: str = ""


def require_immutable_templates_ref(ref: str) -> str:
    """Reject mutable branch tips; canary script fetch must use an exact 40-char SHA."""
    cleaned = (ref or "").strip()
    if not IMMUTABLE_TEMPLATES_REF_RE.fullmatch(cleaned):
        raise ValueError("templates_ref_not_immutable")
    return cleaned


def templates_script_raw_url(project_api: str, ref: str, script: str = "dependency_cache.py") -> str:
    """Path helper for GitLab raw file fetch of an ajrly/scripts module (immutable SHA only)."""
    pin = require_immutable_templates_ref(ref)
    if "/" in script or script.startswith("."):
        relative = script
    else:
        relative = f"ajrly/scripts/{script}"
    encoded = urllib.parse.quote(relative, safe="")
    base = project_api.rstrip("/")
    return f"{base}/repository/files/{encoded}/raw?ref={urllib.parse.quote(pin, safe='')}"


def verify_templates_file_metadata(
    server_meta: Mapping[str, object],
    *,
    expected_ref: str,
    caller_trust: Optional[Mapping[str, object]] = None,
) -> Dict[str, object]:
    """Trust GitLab repository-file API metadata; ignore caller-written trust fields."""
    del caller_trust  # Explicitly unused: never authorize from caller-supplied digests.
    pin = require_immutable_templates_ref(expected_ref)
    commit_id = str(server_meta.get("commit_id") or "").strip()
    content_sha = str(server_meta.get("content_sha256") or "").strip()
    if commit_id != pin:
        raise ValueError("templates_ref_metadata_mismatch")
    if not re.fullmatch(r"[0-9a-f]{64}", content_sha):
        raise ValueError("templates_ref_metadata_mismatch")
    return {
        "file_name": str(server_meta.get("file_name") or ""),
        "ref": pin,
        "commit_id": commit_id,
        "last_commit_id": str(server_meta.get("last_commit_id") or ""),
        "content_sha256": content_sha,
        "size": int(server_meta.get("size") or 0),
    }


def fetch_templates_file_metadata(
    project_api: str,
    ref: str,
    script: str = "dependency_cache.py",
    *,
    job_token: str = "",
    opener=None,
) -> Dict[str, object]:
    """GET repository/files metadata for the immutable pin (server authority)."""
    pin = require_immutable_templates_ref(ref)
    if "/" in script or script.startswith("."):
        relative = script
    else:
        relative = f"ajrly/scripts/{script}"
    encoded = urllib.parse.quote(relative, safe="")
    url = f"{project_api.rstrip('/')}/repository/files/{encoded}?ref={urllib.parse.quote(pin, safe='')}"
    headers = {}
    token = (job_token or "").strip()
    if token:
        headers["JOB-TOKEN"] = token
    request = urllib.request.Request(url, headers=headers, method="GET")
    open_url = opener or urllib.request.urlopen
    with open_url(request, timeout=30) as response:
        payload = json.loads(response.read().decode("utf-8"))
    return verify_templates_file_metadata(payload, expected_ref=pin)


def detect_platform() -> str:
    machine = platform.machine().lower().replace("amd64", "x86_64").replace("arm64", "aarch64")
    system = platform.system().lower()
    return f"{system}-{machine}"


def detect_libc() -> str:
    system = platform.system().lower()
    if system != "linux":
        return system
    try:
        with open("/etc/os-release", encoding="utf-8") as handle:
            text = handle.read().lower()
        if "alpine" in text:
            return "musl"
    except OSError:
        pass
    for path in (Path("/lib/libc.musl-x86_64.so.1"), Path("/lib/ld-musl-x86_64.so.1"),
                 Path("/lib/libc.musl-aarch64.so.1"), Path("/lib/ld-musl-aarch64.so.1")):
        if path.exists():
            return "musl"
    return "gnu"


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _file_digest_or_empty(path: Path) -> str:
    if not path.is_file():
        return _sha256_bytes(b"")
    return _sha256_file(path)


def _patches_digest(root: Path) -> str:
    patch_dir = root / PATCH_DIRNAME
    digest = hashlib.sha256()
    if not patch_dir.is_dir():
        digest.update(b"<missing-patches>\n")
        return digest.hexdigest()
    for path in sorted(p for p in patch_dir.rglob("*") if p.is_file()):
        relative = path.relative_to(patch_dir).as_posix()
        digest.update(relative.encode())
        digest.update(b"\0")
        digest.update(_sha256_file(path).encode())
        digest.update(b"\n")
    return digest.hexdigest()


def dependency_key(root: Path, scope: str, toolchain: Mapping[str, str]) -> str:
    """Hash schema/platform/libc/Node/pnpm plus lockfile/workspace/patch inputs."""
    root = Path(root)
    if not scope or scope.startswith("-") or ".." in scope or scope.startswith("/"):
        raise ValueError("invalid dependency scope")
    node = str(toolchain.get("node", "")).strip()
    pnpm = str(toolchain.get("pnpm", "")).strip()
    if not node or not pnpm:
        raise ValueError("toolchain requires node and pnpm versions")
    material = [
        f"schema={CACHE_SCHEMA}",
        f"platform={detect_platform()}",
        f"libc={detect_libc()}",
        f"node={node}",
        f"pnpm={pnpm}",
        f"scope={scope}",
        f"lock={_file_digest_or_empty(root / 'pnpm-lock.yaml')}",
        f"workspace={_file_digest_or_empty(root / 'pnpm-workspace.yaml')}",
        f"patches={_patches_digest(root)}",
    ]
    return KEY_PREFIX + _sha256_bytes("\n".join(material).encode())


def object_prefix(prefix: str = DEFAULT_PREFIX) -> str:
    cleaned = (prefix or DEFAULT_PREFIX).strip()
    if cleaned.startswith("/") or ".." in cleaned.split("/"):
        raise ValueError("invalid object prefix")
    return cleaned if cleaned.endswith("/") else cleaned + "/"


def manifest_object_key(key: str, prefix: str = DEFAULT_PREFIX) -> str:
    if not KEY_RE.fullmatch(key):
        raise ValueError("invalid dependency key")
    return object_prefix(prefix) + key + ".manifest.json"


def archive_object_key(key: str, prefix: str = DEFAULT_PREFIX) -> str:
    if not KEY_RE.fullmatch(key):
        raise ValueError("invalid dependency key")
    return object_prefix(prefix) + key + ".tar.gz"


def free_disk_bytes(path: Path) -> int:
    usage = shutil.disk_usage(path)
    return int(usage.free)


def require_source_verification(root: Path) -> None:
    manifest = root / SOURCE_MANIFEST
    if not manifest.is_file():
        raise ValueError("missing_source_verification")
    payload = json.loads(manifest.read_text(encoding="utf-8"))
    sha = str(payload.get("sha", ""))
    if not re.fullmatch(r"[0-9a-f]{40}", sha):
        raise ValueError("missing_source_verification")


def registry_config(env: Mapping[str, str]) -> Dict[str, object]:
    """Safe registry precedence: private install registry + install-only token."""
    private = (env.get("AJRLY_NPM_REGISTRY") or "").strip()
    public = (env.get("PNPM_CONFIG_REGISTRY") or "https://registry.npmjs.org/").strip()
    install_token = (env.get("AJRLY_NPM_INSTALL_TOKEN") or "").strip()
    if private:
        return {
            "registry": private if private.endswith("/") else private + "/",
            "auth_token": install_token,
            "private": True,
            # Never fall back to publisher NPM_TOKEN for installs.
            "network_concurrency": str(env.get("PNPM_CONFIG_NETWORK_CONCURRENCY") or "4"),
        }
    return {
        "registry": public,
        "auth_token": "",
        "private": False,
        "network_concurrency": str(env.get("PNPM_CONFIG_NETWORK_CONCURRENCY") or "4"),
    }


def format_restore_summary(result: RestoreResult, *, key: str = "") -> str:
    key_part = key[:18] + "…" if key else ""
    reason = f" reason={result.reason}" if result.reason else ""
    return (
        f"dependency-cache {result.status} key={key_part} "
        f"bytes={result.bytes} duration_s={result.duration:.3f}{reason}"
    )


def acceptance_class_for_restore(status: str, *, local_store_warm: bool = False) -> str:
    """Classify canary measurements so local same-job warm is never a persistent hit.

    - persistent_canonical_restore: verified S3 archive restore (producer present)
    - miss_no_fabricated_hit: restore miss/rejected/disabled; no hit claimed
    - local_store_warm_not_persistent: pnpm filled/reused store inside the same job only
    """
    normalized = (status or "").strip().lower()
    if normalized == "hit":
        return "persistent_canonical_restore"
    if local_store_warm:
        return "local_store_warm_not_persistent"
    return "miss_no_fabricated_hit"


def _normalize_registry_host(value: str) -> str:
    text = (value or "").strip()
    if not text:
        return ""
    if "://" in text:
        return urllib.parse.urlparse(text).netloc.lower()
    return text.split("/")[0].lower()


def _is_private_npm_host(host: str) -> bool:
    cleaned = _normalize_registry_host(host)
    return cleaned == PRIVATE_NPM_HOST or cleaned.endswith(PRIVATE_NPM_HOST_SUFFIX)


def format_restore_report(
    result: RestoreResult,
    *,
    key: str = "",
    phase: str = "",
    install_elapsed_s: Optional[float] = None,
    upstream_hosts: Optional[Iterable[str]] = None,
    configured_registry: str = "",
    observed_registry_hosts: Optional[Iterable[str]] = None,
    local_store_warm: bool = False,
    store_bytes: Optional[int] = None,
    npm_request_estimate: Optional[int] = None,
    pnpm_progress: Optional[Mapping[str, int]] = None,
) -> dict:
    """Machine-readable restore metrics for canary cold/warm/sources-only phases.

    Never includes secrets, signed URLs, or credential material.

    ``configured_registry`` is pnpm/env config only. ``observed_registry_hosts`` is
    actual network/source evidence. Caller ``--upstream-host`` alone must not set
    ``npm_only`` (config check ≠ observed host traffic).
    """
    del upstream_hosts  # Legacy CLI flag retained for argv compat; never treated as observation.
    configured = (configured_registry or "").strip()
    observed = sorted({
        _normalize_registry_host(host)
        for host in (observed_registry_hosts or [])
        if _normalize_registry_host(host)
    })
    report = {
        "phase": phase or "restore",
        "status": result.status,
        "reason": result.reason or "",
        "bytes": int(result.bytes),
        "restore_duration_s": round(float(result.duration), 6),
        "key_prefix": (key[:18] + "…") if key else "",
        "key_schema": KEY_PREFIX.rstrip("-"),
        "max_compressed_bytes": MAX_COMPRESSED_BYTES,
        "configured_registry": configured,
        "observed_registry_hosts": observed,
        "acceptance_class": acceptance_class_for_restore(
            result.status, local_store_warm=local_store_warm
        ),
        "local_store_warm": bool(local_store_warm),
    }
    if observed:
        report["npm_only"] = all(_is_private_npm_host(host) for host in observed)
    if install_elapsed_s is not None:
        report["install_elapsed_s"] = round(float(install_elapsed_s), 6)
    if store_bytes is not None:
        report["store_bytes"] = int(store_bytes)
    # Never publish a zero package-count heuristic as upstream request proof.
    if npm_request_estimate is not None and int(npm_request_estimate) > 0:
        report["npm_request_estimate"] = int(npm_request_estimate)
    if pnpm_progress is not None:
        progress = {
            key_name: int(pnpm_progress[key_name])
            for key_name in ("resolved", "reused", "downloaded", "added")
            if key_name in pnpm_progress
        }
        report["pnpm_progress"] = progress
    return report


def parse_pnpm_progress_line(line: str) -> Optional[Dict[str, int]]:
    """Parse a pnpm ``Progress: resolved N, reused N, downloaded N, added N`` line."""
    match = re.search(
        r"Progress:\s*resolved\s+(\d+),\s*reused\s+(\d+),\s*downloaded\s+(\d+),\s*added\s+(\d+)",
        line or "",
    )
    if not match:
        return None
    return {
        "resolved": int(match.group(1)),
        "reused": int(match.group(2)),
        "downloaded": int(match.group(3)),
        "added": int(match.group(4)),
    }


def closure_timing_ratio_report(phases: Iterable[Mapping[str, object]]) -> dict:
    """Summarize cold-miss vs warm restore+install timings without claiming whole-pipeline 25%."""
    rows = [dict(phase) for phase in phases]
    cold = next((row for row in rows if str(row.get("phase")) == "cold"), None)
    warms = [row for row in rows if str(row.get("phase", "")).startswith("warm")]
    if cold is None or not warms:
        raise ValueError("closure_timing_requires_cold_and_warm")
    cold_install = float(cold.get("install_elapsed_s") or 0.0)
    warm_installs = [float(row.get("install_elapsed_s") or 0.0) for row in warms]
    warm_restores = [float(row.get("restore_duration_s") or 0.0) for row in warms]
    warm_install_mean = sum(warm_installs) / len(warm_installs)
    warm_restore_mean = sum(warm_restores) / len(warm_restores)
    warm_total_mean = warm_install_mean + warm_restore_mean
    if cold_install <= 0:
        raise ValueError("closure_timing_invalid_cold_install")
    reduction = (cold_install - warm_total_mean) / cold_install
    install_values = [cold_install, *warm_installs]
    mean_install = sum(install_values) / len(install_values)
    variance = sum((value - mean_install) ** 2 for value in install_values) / len(install_values)
    return {
        "cold_install_s": round(cold_install, 6),
        "warm_install_mean_s": round(warm_install_mean, 6),
        "warm_restore_mean_s": round(warm_restore_mean, 6),
        "warm_total_mean_s": round(warm_total_mean, 6),
        "install_wall_reduction_ratio": round(reduction, 6),
        "variance": {
            "install_elapsed_s": round(variance, 6),
            "warm_install_spread_s": round(max(warm_installs) - min(warm_installs), 6),
            "warm_restore_spread_s": round(max(warm_restores) - min(warm_restores), 6),
        },
        "whole_pipeline_25pct_claim": False,
        "objective_target_status": "pending",
        "phases": rows,
    }


def _cleanup(path: Path) -> None:
    if path.is_symlink() or path.is_file():
        path.unlink(missing_ok=True)
    elif path.is_dir():
        shutil.rmtree(path, ignore_errors=True)


def _safe_member_path(name: str) -> Path:
    if not name or name.endswith("\x00"):
        raise ValueError("unsafe_archive_member")
    normalized = name.replace("\\", "/")
    if normalized.startswith("/") or normalized.startswith("../") or "/../" in f"/{normalized}/":
        raise ValueError("unsafe_archive_member")
    parts = Path(normalized).parts
    if any(part in ("", "..") for part in parts) or Path(normalized).is_absolute():
        raise ValueError("unsafe_archive_member")
    return Path(*parts)


def _is_within_directory(path: Path, root: Path) -> bool:
    """Reject path-prefix tricks (``/tmp/a`` must not accept ``/tmp/a-evil``)."""
    try:
        path.resolve(strict=False).relative_to(root.resolve(strict=False))
        return True
    except ValueError:
        return False


def _validate_member(member: tarfile.TarInfo) -> Path:
    relative = _safe_member_path(member.name)
    if member.isdev() or member.ischr() or member.isblk() or member.isfifo():
        raise ValueError("unsafe_archive_member")
    if member.islnk() or member.issym():
        target = member.linkname or ""
        if not target or target.startswith("/") or "\\" in target:
            raise ValueError("unsafe_archive_member")
        target_path = Path(target)
        if target_path.is_absolute() or ".." in target_path.parts:
            raise ValueError("unsafe_archive_member")
        # Resolve relative to the member directory and require in-store target.
        resolved = (relative.parent / target_path)
        try:
            _safe_member_path(resolved.as_posix())
        except ValueError as error:
            raise ValueError("unsafe_archive_member") from error
    elif not (member.isfile() or member.isdir()):
        raise ValueError("unsafe_archive_member")
    return relative


def _extract_verified(archive_path: Path, destination: Path, *, expected_expanded: int) -> int:
    destination.mkdir(parents=True, exist_ok=True)
    destination_resolved = destination.resolve(strict=False)
    expanded = 0
    with tarfile.open(archive_path, mode="r:gz") as tar:
        for member in tar.getmembers():
            relative = _validate_member(member)
            target = destination / relative
            if not _is_within_directory(target, destination):
                raise ValueError("unsafe_archive_member")
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            if member.issym():
                # Symlinks are recorded but must not escape the extract root.
                link = Path(member.linkname)
                if link.is_absolute() or ".." in link.parts:
                    raise ValueError("unsafe_archive_member")
                if not _is_within_directory(target.parent / link, destination):
                    raise ValueError("unsafe_archive_member")
                if target.exists() or target.is_symlink():
                    target.unlink()
                target.symlink_to(member.linkname)
                continue
            if member.islnk():
                link_target = destination / _safe_member_path(member.linkname)
                if not _is_within_directory(link_target, destination):
                    raise ValueError("unsafe_archive_member")
                if not link_target.exists():
                    raise ValueError("unsafe_archive_member")
                os.link(link_target, target)
                continue
            source = tar.extractfile(member)
            if source is None:
                raise ValueError("unsafe_archive_member")
            # Count *actual* bytes written; never trust member.size alone (tar bombs).
            written = 0
            with source, target.open("wb") as handle:
                while True:
                    chunk = source.read(1024 * 1024)
                    if not chunk:
                        break
                    written += len(chunk)
                    expanded += len(chunk)
                    if expanded > expected_expanded:
                        raise ValueError("expanded_mismatch")
                    handle.write(chunk)
            if written != int(member.size):
                raise ValueError("expanded_mismatch")
            mode = member.mode & 0o755
            if mode:
                target.chmod(mode)
    if expanded != expected_expanded:
        raise ValueError("expanded_mismatch")
    # Final promotion guard: every extracted path must remain under destination.
    for path in destination.rglob("*"):
        if not _is_within_directory(path, destination_resolved):
            raise ValueError("unsafe_archive_member")
    return expanded


def _read_manifest(reader: S3Reader, key: str, prefix: str) -> dict:
    raw = reader.fetch_bytes(manifest_object_key(key, prefix))
    manifest = json.loads(raw.decode("utf-8"))
    required = (
        "schema", "key", "toolchain", "scope", "created_at",
        "archive_sha256", "compressed_bytes", "expanded_bytes",
    )
    missing = [field for field in required if field not in manifest]
    if missing:
        raise ValueError("corrupt_manifest")
    if int(manifest.get("schema", -1)) != CACHE_SCHEMA:
        raise ValueError("unsupported_manifest_schema")
    if manifest.get("key") != key:
        raise ValueError("manifest_key_mismatch")
    if not re.fullmatch(r"[0-9a-f]{64}", str(manifest.get("archive_sha256", ""))):
        raise ValueError("corrupt_manifest")
    if not isinstance(manifest.get("toolchain"), dict):
        raise ValueError("corrupt_manifest")
    try:
        created_at = float(manifest["created_at"])
        compressed = int(manifest["compressed_bytes"])
        expanded = int(manifest["expanded_bytes"])
    except (TypeError, ValueError) as error:
        raise ValueError("corrupt_manifest") from error
    if created_at <= 0 or compressed <= 0 or expanded < 0:
        raise ValueError("corrupt_manifest")
    return manifest


def restore_store(
    root: Path,
    key: str,
    reader: S3Reader,
    *,
    scope: str = "full",
    toolchain: Optional[Mapping[str, str]] = None,
    prefix: str = DEFAULT_PREFIX,
    staging_dir: Optional[Path] = None,
    now: Optional[float] = None,
) -> RestoreResult:
    """Restore a verified pnpm store after exact checkout. Read-only S3 consumer."""
    started = time.monotonic()
    root = Path(root)
    staging: Optional[Path] = None
    try:
        require_source_verification(root)
        if toolchain is None:
            raise ValueError("toolchain_required")
        expected = dependency_key(root, scope, toolchain)
        if key != expected:
            return RestoreResult("rejected", 0, time.monotonic() - started, "caller_selected_key")
        try:
            manifest = _read_manifest(reader, key, prefix)
        except FileNotFoundError:
            return RestoreResult("miss", 0, time.monotonic() - started, "not_found")
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
            reason = str(error) if str(error) in {
                "unsupported_manifest_schema", "manifest_key_mismatch"
            } else "corrupt_manifest"
            return RestoreResult("rejected", 0, time.monotonic() - started, reason)

        created_at = float(manifest.get("created_at", 0))
        current = time.time() if now is None else now
        if created_at <= 0 or current - created_at > MAX_AGE_SECONDS:
            return RestoreResult("rejected", 0, time.monotonic() - started, "expired")

        compressed = int(manifest.get("compressed_bytes", 0))
        expanded_declared = int(manifest.get("expanded_bytes", 0))
        if compressed <= 0 or compressed > MAX_COMPRESSED_BYTES:
            return RestoreResult("rejected", 0, time.monotonic() - started, "compressed_too_large")
        if expanded_declared < 0:
            return RestoreResult("rejected", 0, time.monotonic() - started, "invalid_expanded_bytes")
        if expanded_declared > free_disk_bytes(root):
            return RestoreResult("rejected", 0, time.monotonic() - started, "expanded_exceeds_free_disk")
        if str(manifest.get("scope", scope)) != scope:
            return RestoreResult("rejected", 0, time.monotonic() - started, "scope_mismatch")

        parent = Path(staging_dir) if staging_dir else root / ".ci-tmp" / "dependency-cache"
        parent.mkdir(parents=True, exist_ok=True)
        staging = Path(tempfile.mkdtemp(prefix="restore-", dir=str(parent)))
        archive_path = staging / "store.tar.gz"
        extract_dir = staging / "extract"
        extract_dir.mkdir()

        try:
            written = reader.fetch(archive_object_key(key, prefix), archive_path)
        except FileNotFoundError:
            _cleanup(staging)
            return RestoreResult("miss", 0, time.monotonic() - started, "not_found")

        if written != compressed or archive_path.stat().st_size != compressed:
            _cleanup(staging)
            return RestoreResult("rejected", written, time.monotonic() - started, "compressed_size_mismatch")
        digest = _sha256_file(archive_path)
        if digest != str(manifest.get("archive_sha256", "")):
            _cleanup(staging)
            return RestoreResult("rejected", written, time.monotonic() - started, "checksum_mismatch")

        try:
            _extract_verified(archive_path, extract_dir, expected_expanded=expanded_declared)
        except ValueError as error:
            _cleanup(staging)
            reason = str(error) if str(error) in {
                "unsafe_archive_member", "expanded_mismatch"
            } else "unsafe_archive_member"
            return RestoreResult("rejected", written, time.monotonic() - started, reason)

        store = root / STORE_DIRNAME
        final_staging = root / f".{STORE_DIRNAME}.promoting"
        _cleanup(final_staging)
        # Refuse to promote if staging escaped the extract root (symlink tricks).
        extract_resolved = extract_dir.resolve(strict=False)
        for path in extract_dir.rglob("*"):
            if not _is_within_directory(path, extract_resolved):
                _cleanup(staging)
                return RestoreResult(
                    "rejected", written, time.monotonic() - started, "unsafe_archive_member",
                )
        shutil.move(str(extract_dir), str(final_staging))
        if store.exists() or store.is_symlink():
            if store.is_symlink() or store.is_file():
                store.unlink()
            else:
                # Never follow a pre-existing store symlink into attacker-controlled paths.
                shutil.rmtree(store)
        os.replace(final_staging, store)
        _cleanup(staging)
        return RestoreResult("hit", written, time.monotonic() - started, "")
    except ValueError as error:
        if staging is not None:
            _cleanup(staging)
        reason = str(error)
        if reason == "missing_source_verification":
            return RestoreResult("rejected", 0, time.monotonic() - started, reason)
        return RestoreResult("rejected", 0, time.monotonic() - started, reason or "error")


def _iter_store_files(store: Path) -> Iterable[Path]:
    for path in sorted(store.rglob("*")):
        if path.is_symlink() or path.is_file():
            yield path
        elif path.is_dir():
            continue
        else:
            raise ValueError(f"unsupported store entry: {path}")


def _archive_store(store: Path, archive_path: Path) -> Tuple[int, int]:
    expanded = 0
    with tarfile.open(archive_path, mode="w:gz", compresslevel=6) as tar:
        for path in _iter_store_files(store):
            relative = path.relative_to(store).as_posix()
            _safe_member_path(relative)
            if path.is_symlink():
                target = os.readlink(path)
                if target.startswith("/") or ".." in Path(target).parts:
                    raise ValueError("store contains external symlink")
                info = tarfile.TarInfo(name=relative)
                info.type = tarfile.SYMTYPE
                info.linkname = target
                info.mtime = int(path.stat(follow_symlinks=False).st_mtime)
                tar.addfile(info)
                continue
            size = path.stat().st_size
            expanded += size
            info = tarfile.TarInfo(name=relative)
            info.size = size
            info.mtime = int(path.stat().st_mtime)
            info.mode = stat.S_IMODE(path.stat().st_mode)
            with path.open("rb") as handle:
                tar.addfile(info, handle)
    compressed = archive_path.stat().st_size
    if compressed > MAX_COMPRESSED_BYTES:
        raise ValueError(f"compressed archive exceeds {MAX_COMPRESSED_BYTES} bytes")
    return compressed, expanded


def publish_store(
    root: Path,
    key: str,
    writer: S3Writer,
    *,
    scope: str = "full",
    toolchain: Optional[Mapping[str, str]] = None,
    prefix: str = DEFAULT_PREFIX,
) -> dict:
    """Pure producer API. Authorization is the caller's responsibility (platform main)."""
    root = Path(root)
    if toolchain is None:
        raise ValueError("toolchain requires node and pnpm versions")
    expected = dependency_key(root, scope, toolchain)
    if key != expected:
        raise ValueError("caller_selected_key")
    store = root / STORE_DIRNAME
    if not store.is_dir():
        raise ValueError("pnpm store is missing")
    with tempfile.TemporaryDirectory(prefix="dependency-publish-") as tmp:
        archive_path = Path(tmp) / "store.tar.gz"
        compressed, expanded = _archive_store(store, archive_path)
        digest = _sha256_file(archive_path)
        manifest = {
            "schema": CACHE_SCHEMA,
            "key": key,
            "toolchain": {"node": str(toolchain["node"]), "pnpm": str(toolchain["pnpm"])},
            "scope": scope,
            "created_at": time.time(),
            "archive_sha256": digest,
            "compressed_bytes": compressed,
            "expanded_bytes": expanded,
        }
        writer.put(
            manifest_object_key(key, prefix),
            (json.dumps(manifest, sort_keys=True) + "\n").encode("utf-8"),
            content_type="application/json",
        )
        writer.put(
            archive_object_key(key, prefix),
            archive_path.read_bytes(),
            content_type="application/gzip",
        )
        return manifest


class StdlibS3Client:
    """Minimal SigV4 S3 client using stdlib only (read + optional write)."""

    def __init__(
        self,
        *,
        endpoint: str,
        bucket: str,
        access_key: str,
        secret_key: str,
        region: str = "us-east-1",
        allow_write: bool = False,
    ):
        if not endpoint.startswith("https://"):
            raise ValueError("dependency cache endpoint must use https")
        if not bucket or not access_key or not secret_key:
            raise ValueError("incomplete S3 credentials")
        self.endpoint = endpoint.rstrip("/")
        self.bucket = bucket
        self.access_key = access_key
        self.secret_key = secret_key
        self.region = region
        self.allow_write = allow_write

    def _url(self, object_key: str) -> str:
        quoted = urllib.parse.quote(object_key, safe="/")
        return f"{self.endpoint}/{self.bucket}/{quoted}"

    def _sign(self, method: str, object_key: str, payload_hash: str) -> Dict[str, str]:
        now = datetime.now(timezone.utc)
        amz_date = now.strftime("%Y%m%dT%H%M%SZ")
        date_stamp = now.strftime("%Y%m%d")
        host = urllib.parse.urlparse(self.endpoint).netloc
        canonical_uri = "/" + self.bucket + "/" + urllib.parse.quote(object_key, safe="/")
        canonical_headers = f"host:{host}\nx-amz-content-sha256:{payload_hash}\nx-amz-date:{amz_date}\n"
        signed_headers = "host;x-amz-content-sha256;x-amz-date"
        canonical_request = "\n".join([
            method, canonical_uri, "", canonical_headers, signed_headers, payload_hash,
        ])
        credential_scope = f"{date_stamp}/{self.region}/s3/aws4_request"
        string_to_sign = "\n".join([
            "AWS4-HMAC-SHA256",
            amz_date,
            credential_scope,
            _sha256_bytes(canonical_request.encode()),
        ])

        def sign(key: bytes, msg: str) -> bytes:
            return hmac.new(key, msg.encode(), hashlib.sha256).digest()

        signing_key = sign(
            sign(sign(sign(("AWS4" + self.secret_key).encode(), date_stamp), self.region), "s3"),
            "aws4_request",
        )
        signature = hmac.new(signing_key, string_to_sign.encode(), hashlib.sha256).hexdigest()
        authorization = (
            "AWS4-HMAC-SHA256 "
            f"Credential={self.access_key}/{credential_scope}, "
            f"SignedHeaders={signed_headers}, Signature={signature}"
        )
        return {
            "Host": host,
            "x-amz-date": amz_date,
            "x-amz-content-sha256": payload_hash,
            "Authorization": authorization,
        }

    def fetch_bytes(self, object_key: str) -> bytes:
        headers = self._sign("GET", object_key, _sha256_bytes(b""))
        request = urllib.request.Request(self._url(object_key), headers=headers, method="GET")
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                return response.read()
        except urllib.error.HTTPError as error:
            if error.code == 404:
                raise FileNotFoundError(object_key) from None
            raise RuntimeError(f"s3 get failed with HTTP {error.code}") from None

    def fetch(self, object_key: str, destination: Path) -> int:
        data = self.fetch_bytes(object_key)
        destination.write_bytes(data)
        return len(data)

    def put(self, object_key: str, data: bytes, *, content_type: str = "") -> None:
        if not self.allow_write:
            raise PermissionError("canonical dependency cache client is read-only")
        payload_hash = _sha256_bytes(data)
        headers = self._sign("PUT", object_key, payload_hash)
        if content_type:
            headers["Content-Type"] = content_type
        headers["Content-Length"] = str(len(data))
        request = urllib.request.Request(self._url(object_key), data=data, headers=headers, method="PUT")
        try:
            with urllib.request.urlopen(request, timeout=120) as response:
                response.read()
        except urllib.error.HTTPError as error:
            raise RuntimeError(f"s3 put failed with HTTP {error.code}") from None


def reader_from_env(env: Optional[Mapping[str, str]] = None) -> StdlibS3Client:
    env = env or os.environ
    return StdlibS3Client(
        endpoint=(env.get("AJRLY_DEPENDENCY_CACHE_ENDPOINT") or "").strip(),
        bucket=(env.get("AJRLY_DEPENDENCY_CACHE_BUCKET") or "").strip(),
        access_key=(env.get("AJRLY_DEPENDENCY_CACHE_ACCESS_KEY") or "").strip(),
        secret_key=(env.get("AJRLY_DEPENDENCY_CACHE_SECRET_KEY") or "").strip(),
        region=(env.get("AJRLY_DEPENDENCY_CACHE_REGION") or "us-east-1").strip(),
        allow_write=False,
    )


def toolchain_from_env(env: Optional[Mapping[str, str]] = None) -> Dict[str, str]:
    env = env or os.environ
    return {
        "node": (env.get("AJRLY_NODE_VERSION") or "").strip(),
        "pnpm": (env.get("AJRLY_PNPM_VERSION") or "").strip(),
    }


def configure_install_registry(root: Path, env: Optional[Mapping[str, str]] = None) -> Dict[str, object]:
    """Write install-only auth for private registry without logging the token."""
    env = dict(env or os.environ)
    cfg = registry_config(env)
    if not cfg["private"]:
        return cfg
    registry = str(cfg["registry"])
    host = urllib.parse.urlparse(registry).netloc
    token = str(cfg["auth_token"])
    if not token:
        raise ValueError("AJRLY_NPM_INSTALL_TOKEN required for private registry installs")
    npmrc = root / ".npmrc"
    lines = []
    if npmrc.exists():
        for line in npmrc.read_text(encoding="utf-8").splitlines():
            if line.startswith(f"//{host}/:_authToken=") or line.startswith("registry="):
                continue
            lines.append(line)
    lines.append(f"registry={registry}")
    lines.append(f"//{host}/:_authToken={token}")
    npmrc.write_text("\n".join(lines) + "\n", encoding="utf-8")
    # Prefer offline store contents when present; miss still cold-installs.
    os.environ["PNPM_CONFIG_REGISTRY"] = registry
    os.environ["PNPM_CONFIG_NETWORK_CONCURRENCY"] = str(cfg["network_concurrency"])
    return {**cfg, "auth_token": "[set]"}


def main(argv: Optional[Iterable[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Ajrly dependency store cache helper")
    parser.add_argument(
        "command",
        choices=[
            "key", "restore", "configure-registry", "script-url", "report",
            "require-immutable-ref", "file-metadata", "timing-ratio",
        ],
    )
    parser.add_argument("--root", default=".")
    parser.add_argument("--scope", default="full")
    parser.add_argument("--key", default="")
    parser.add_argument("--ref", default="")
    parser.add_argument("--script", default="dependency_cache.py")
    parser.add_argument("--phase", default="")
    parser.add_argument("--json", action="store_true", dest="as_json")
    parser.add_argument("--install-elapsed", type=float, default=None)
    parser.add_argument("--upstream-host", action="append", default=[])
    parser.add_argument("--configured-registry", default="")
    parser.add_argument("--observed-registry-host", action="append", default=[])
    parser.add_argument("--pnpm-progress", default="")
    parser.add_argument("--timing-phases-json", default="")
    parser.add_argument("--local-store-warm", action="store_true", default=False)
    parser.add_argument("--store-bytes", type=int, default=None)
    parser.add_argument("--npm-request-estimate", type=int, default=None)
    args = parser.parse_args(list(argv) if argv is not None else None)
    root = Path(args.root)

    def _progress_from_args() -> Optional[Dict[str, int]]:
        raw = (args.pnpm_progress or "").strip()
        if not raw:
            return None
        if raw.startswith("{"):
            payload = json.loads(raw)
            return {k: int(payload[k]) for k in ("resolved", "reused", "downloaded", "added") if k in payload}
        parsed = parse_pnpm_progress_line(raw)
        if parsed is None:
            raise SystemExit("invalid --pnpm-progress value")
        return parsed

    if args.command == "require-immutable-ref":
        ref = args.ref or os.environ.get("AJRLY_TEMPLATES_REF", "")
        print(require_immutable_templates_ref(ref))
        return 0

    if args.command == "file-metadata":
        api = os.environ.get("CI_API_V4_URL", "http://192.168.68.12/api/v4")
        project = os.environ.get("AJRLY_TEMPLATES_PROJECT_ID", "1")
        ref = args.ref or os.environ.get("AJRLY_TEMPLATES_REF", "")
        meta = fetch_templates_file_metadata(
            f"{api}/projects/{project}",
            ref,
            args.script,
            job_token=os.environ.get("CI_JOB_TOKEN", ""),
        )
        print(json.dumps(meta, sort_keys=True))
        return 0

    if args.command == "timing-ratio":
        raw = (args.timing_phases_json or "").strip()
        if not raw:
            raise SystemExit("--timing-phases-json is required")
        phases = json.loads(raw)
        print(json.dumps(closure_timing_ratio_report(phases), sort_keys=True))
        return 0

    if args.command == "script-url":
        api = os.environ.get("CI_API_V4_URL", "http://192.168.68.12/api/v4")
        project = os.environ.get("AJRLY_TEMPLATES_PROJECT_ID", "1")
        ref = args.ref or os.environ.get("AJRLY_TEMPLATES_REF", "")
        if not ref:
            raise SystemExit("AJRLY_TEMPLATES_REF is required")
        print(templates_script_raw_url(f"{api}/projects/{project}", ref, args.script))
        return 0

    if args.command == "configure-registry":
        cfg = configure_install_registry(root)
        print(json.dumps({"registry": cfg["registry"], "private": cfg["private"],
                          "network_concurrency": cfg["network_concurrency"]}, sort_keys=True))
        return 0

    toolchain = toolchain_from_env()
    if args.command == "key":
        print(dependency_key(root, args.scope, toolchain))
        return 0

    if args.command == "report":
        # Canary helper: emit metrics JSON from an already-observed restore result line
        # or synthesize a structured miss when cache is disabled. No S3 writes.
        status = (os.environ.get("AJRLY_DEPENDENCY_CACHE_LAST_STATUS") or "miss").strip()
        reason = (os.environ.get("AJRLY_DEPENDENCY_CACHE_LAST_REASON") or "").strip()
        try:
            bytes_read = int(os.environ.get("AJRLY_DEPENDENCY_CACHE_LAST_BYTES") or "0")
        except ValueError:
            bytes_read = 0
        try:
            duration = float(os.environ.get("AJRLY_DEPENDENCY_CACHE_LAST_DURATION") or "0")
        except ValueError:
            duration = 0.0
        key = args.key.strip() or (os.environ.get("AJRLY_DEPENDENCY_CACHE_LAST_KEY") or "").strip()
        result = RestoreResult(status, bytes_read, duration, reason)
        configured = args.configured_registry.strip() or (
            os.environ.get("AJRLY_NPM_REGISTRY") or os.environ.get("PNPM_CONFIG_REGISTRY") or ""
        ).strip()
        print(json.dumps(format_restore_report(
            result,
            key=key,
            phase=args.phase,
            install_elapsed_s=args.install_elapsed,
            upstream_hosts=args.upstream_host,
            configured_registry=configured,
            observed_registry_hosts=args.observed_registry_host,
            local_store_warm=args.local_store_warm,
            store_bytes=args.store_bytes,
            npm_request_estimate=args.npm_request_estimate,
            pnpm_progress=_progress_from_args(),
        ), sort_keys=True))
        return 0

    # restore
    if os.environ.get("AJRLY_DEPENDENCY_CACHE_ENABLED", "false").lower() not in {"1", "true", "yes"}:
        result = RestoreResult("miss", 0, 0.0, "disabled")
        if args.as_json:
            configured = args.configured_registry.strip() or (
                os.environ.get("AJRLY_NPM_REGISTRY") or os.environ.get("PNPM_CONFIG_REGISTRY") or ""
            ).strip()
            print(json.dumps(format_restore_report(
                result, phase=args.phase or "restore", install_elapsed_s=args.install_elapsed,
                upstream_hosts=args.upstream_host,
                configured_registry=configured,
                observed_registry_hosts=args.observed_registry_host,
                local_store_warm=args.local_store_warm,
                store_bytes=args.store_bytes,
                npm_request_estimate=args.npm_request_estimate,
                pnpm_progress=_progress_from_args(),
            ), sort_keys=True))
        else:
            print(format_restore_summary(result))
        return 0
    computed = dependency_key(root, args.scope, toolchain)
    supplied = args.key.strip()
    key = supplied or computed
    prefix = os.environ.get("AJRLY_DEPENDENCY_CACHE_PREFIX", DEFAULT_PREFIX)
    reader = reader_from_env()
    result = restore_store(
        root, key, reader, scope=args.scope, toolchain=toolchain, prefix=prefix,
    )
    if args.as_json:
        configured = args.configured_registry.strip() or (
            os.environ.get("AJRLY_NPM_REGISTRY") or os.environ.get("PNPM_CONFIG_REGISTRY") or ""
        ).strip()
        print(json.dumps(format_restore_report(
            result,
            key=key,
            phase=args.phase or "restore",
            install_elapsed_s=args.install_elapsed,
            upstream_hosts=args.upstream_host,
            configured_registry=configured,
            observed_registry_hosts=args.observed_registry_host,
            local_store_warm=args.local_store_warm,
            store_bytes=args.store_bytes,
            npm_request_estimate=args.npm_request_estimate,
            pnpm_progress=_progress_from_args(),
        ), sort_keys=True))
    else:
        print(format_restore_summary(result, key=key))
    # Miss/rejected are non-fatal: cold install through the configured registry.
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
