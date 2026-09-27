"""Deterministic integrity layer for Mode 2 pure-agent workflows.

The model is treated as an untrusted, replaceable worker.  This module owns
content-addressed artifacts, tamper detection, executor calibration, state
diagnostics, recovery, and final DOCX verification.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import time
import zipfile
from contextlib import contextmanager
from pathlib import Path, PurePosixPath
from typing import Any, Iterator
from xml.etree import ElementTree as ET


INTEGRITY_VERSION = 1
NS_W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
NS_R = "http://schemas.openxmlformats.org/package/2006/relationships"


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def ensure_integrity_state(state: dict[str, Any]) -> dict[str, Any]:
    data = state.setdefault("integrity", {})
    data.setdefault("schema_version", INTEGRITY_VERSION)
    data.setdefault("revision", 0)
    data.setdefault("artifacts", {})
    data.setdefault("accepted", {})
    data.setdefault("attempts", {})
    data.setdefault("decisions", [])
    data.setdefault("executor_profiles", {})
    data.setdefault("final_artifacts", {})
    return data


def bump_revision(state: dict[str, Any]) -> int:
    data = ensure_integrity_state(state)
    data["revision"] = int(data.get("revision", 0)) + 1
    return data["revision"]


@contextmanager
def project_lock(book_root: Path, *, timeout: float = 20.0) -> Iterator[None]:
    """Portable best-effort exclusive lock for state-changing commands."""
    lock = book_root / "agent_work" / ".mode2.lock"
    lock.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + timeout
    descriptor: int | None = None
    while descriptor is None:
        try:
            descriptor = os.open(str(lock), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(descriptor, json.dumps({"pid": os.getpid(), "at": time.time()}).encode("ascii"))
        except FileExistsError:
            try:
                age = time.time() - lock.stat().st_mtime
                if age > 3600:
                    lock.unlink(missing_ok=True)
                    continue
            except OSError:
                pass
            if time.monotonic() >= deadline:
                raise TimeoutError(f"项目正被另一个 Mode 2 进程使用：{lock}")
            time.sleep(0.1)
    try:
        yield
    finally:
        if descriptor is not None:
            os.close(descriptor)
        lock.unlink(missing_ok=True)


def _atomic_bytes(path: Path, value: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_bytes(value)
    temporary.replace(path)


def object_path(book_root: Path, digest: str, suffix: str = ".md") -> Path:
    return book_root / "agent_work" / "objects" / "sha256" / digest[:2] / f"{digest}{suffix}"


def store_artifact(
    book_root: Path,
    state: dict[str, Any],
    value: bytes,
    *,
    kind: str,
    job_id: str,
    source_path: str | None = None,
) -> dict[str, Any]:
    data = ensure_integrity_state(state)
    digest = sha256_bytes(value)
    suffix = Path(source_path).suffix if source_path else ".bin"
    if not suffix or len(suffix) > 10:
        suffix = ".bin"
    destination = object_path(book_root, digest, suffix)
    if destination.exists() and sha256_file(destination) != digest:
        raise ValueError(f"内容仓库对象损坏：{destination}")
    if not destination.exists():
        _atomic_bytes(destination, value)
        try:
            destination.chmod(stat.S_IREAD)
        except OSError:
            pass
    record = {
        "sha256": digest,
        "kind": kind,
        "job_id": job_id,
        "path": destination.relative_to(book_root).as_posix(),
        "bytes": len(value),
        "source_path": source_path,
    }
    data["artifacts"][digest] = record
    return record


def register_attempt(
    book_root: Path,
    state: dict[str, Any],
    *,
    job_id: str,
    result_path: Path,
    accepted: bool,
    executor_id: str,
    packet_sha256: str | None = None,
    reason: str | None = None,
) -> dict[str, Any]:
    data = ensure_integrity_state(state)
    artifact = store_artifact(
        book_root,
        state,
        result_path.read_bytes(),
        kind="translation_result",
        job_id=job_id,
        source_path=str(result_path),
    )
    attempts = data["attempts"].setdefault(job_id, [])
    attempt = {
        "attempt": len(attempts) + 1,
        "artifact_sha256": artifact["sha256"],
        "accepted": bool(accepted),
        "executor_id": executor_id,
        "packet_sha256": packet_sha256,
        "reason": reason,
    }
    attempts.append(attempt)
    if accepted:
        data["accepted"][job_id] = artifact["sha256"]
    bump_revision(state)
    return attempt


def accepted_artifact(book_root: Path, state: dict[str, Any], job_id: str) -> Path | None:
    data = ensure_integrity_state(state)
    digest = data["accepted"].get(job_id)
    if not digest:
        return None
    record = data["artifacts"].get(digest)
    if not record:
        raise ValueError(f"已接受成果缺少对象记录：{job_id}")
    path = book_root / record["path"]
    if not path.is_file():
        raise ValueError(f"已接受成果对象丢失：{job_id}")
    actual = sha256_file(path)
    if actual != digest:
        raise ValueError(f"已接受成果对象被修改：{job_id}；期望 {digest}，实际 {actual}")
    return path


def verify_accepted_artifacts(book_root: Path, state: dict[str, Any]) -> list[dict[str, str]]:
    issues: list[dict[str, str]] = []
    data = ensure_integrity_state(state)
    for job_id in sorted(data["accepted"]):
        try:
            accepted_artifact(book_root, state, job_id)
        except ValueError as exc:
            issues.append({"rule": "accepted_artifact_invalid", "job_id": job_id, "detail": str(exc)})
    return issues


def migrate_archives(book_root: Path, state: dict[str, Any], *, apply: bool) -> dict[str, Any]:
    plan: list[dict[str, Any]] = []
    for unit in state.get("units", []):
        if unit.get("status") not in {"complete", "quality_pass", "legacy_unverified"}:
            continue
        archive = book_root / "agent_work" / "translate" / "outbox" / f"{unit['id']}_translated.md"
        item = {"unit": unit["id"], "path": str(archive), "exists": archive.is_file()}
        if archive.is_file():
            item["sha256"] = sha256_file(archive)
            if apply:
                attempt = register_attempt(
                    book_root,
                    state,
                    job_id=unit["id"],
                    result_path=archive,
                    accepted=unit.get("status") in {"complete", "quality_pass"},
                    executor_id="legacy-import",
                    reason="v4_read_only_migration",
                )
                item["artifact_sha256"] = attempt["artifact_sha256"]
        plan.append(item)
    return {"mode": "apply" if apply else "plan", "archives": plan, "missing": [x["unit"] for x in plan if not x["exists"]]}


def executor_profile(state: dict[str, Any], executor_id: str | None) -> dict[str, Any]:
    key = (executor_id or "unidentified-project-local").strip() or "unidentified-project-local"
    profiles = ensure_integrity_state(state)["executor_profiles"]
    return profiles.setdefault(
        key,
        {
            "executor_id": key,
            "tier": "standard",
            "translation_successes": 0,
            "review_successes": 0,
            "failures": 0,
            "success_streak": 0,
            "target_words": 9000,
            "max_words": 12000,
            "review_target_words": 18000,
            "capacity_failures": 0,
        },
    )


def record_executor_outcome(
    state: dict[str, Any],
    executor_id: str | None,
    *,
    stage: str,
    ok: bool,
    reason: str | None = None,
    capacity_qualifying: bool = True,
) -> dict[str, Any]:
    profile = executor_profile(state, executor_id)
    if ok and capacity_qualifying:
        key = "review_successes" if stage == "review" else "translation_successes"
        profile[key] = int(profile.get(key, 0)) + 1
        profile["success_streak"] = int(profile.get("success_streak", 0)) + 1
    elif ok:
        # Tiny structural/miscellaneous packets prove protocol compliance, not
        # output capacity. Keep them visible without allowing a two-word page
        # marker or delta review to promote the executor to the strong tier.
        profile["non_capacity_successes"] = int(profile.get("non_capacity_successes", 0)) + 1
    else:
        profile["failures"] = int(profile.get("failures", 0)) + 1
        profile["success_streak"] = 0
        profile["last_failure"] = reason
    capacity_failure = str(reason or "").casefold() in {
        "truncated", "protocol_error", "output_limit", "context_limit", "capacity_failure"
    }
    if not ok and capacity_failure:
        profile["capacity_failures"] = int(profile.get("capacity_failures", 0)) + 1
    if int(profile.get("capacity_failures", 0)) and int(profile.get("success_streak", 0)) < 2:
        profile.update({"tier": "conservative", "target_words": 4000, "max_words": 5000, "review_target_words": 7000})
    elif profile["translation_successes"] >= 2 and profile["review_successes"] >= 1 and int(profile.get("success_streak", 0)) >= 3:
        profile.update({"tier": "strong", "target_words": 12000, "max_words": 15000, "review_target_words": 22000})
    else:
        profile.update({"tier": "standard", "target_words": 9000, "max_words": 12000, "review_target_words": 18000})
    return profile


def plan_conservation(state: dict[str, Any]) -> dict[str, Any]:
    expected = [block for info in state.get("chapters", {}).values() for block in info.get("block_ids", [])]
    translated = [block for unit in state.get("units", []) for block in unit.get("block_ids", [])]
    preserved = [
        item.get("block_id") if isinstance(item, dict) else item
        for item in state.get("preserved_blocks", [])
    ]
    preserved = [item for item in preserved if item]
    reused = [
        item.get("block_id") if isinstance(item, dict) else item
        for item in state.get("reused_translation_blocks", [])
    ]
    reused = [item for item in reused if item]
    assigned = translated + preserved + reused
    expected_counts = {item: expected.count(item) for item in set(expected)}
    assigned_counts = {item: assigned.count(item) for item in set(assigned)}
    missing = [item for item in expected if assigned_counts.get(item, 0) == 0]
    duplicate = sorted(item for item, count in assigned_counts.items() if count > 1)
    unknown = sorted(item for item in assigned_counts if item not in expected_counts)
    return {
        "ok": not (missing or duplicate or unknown),
        "source_blocks": len(expected),
        "assigned_blocks": len(assigned),
        "translation_blocks": len(translated),
        "locally_preserved_blocks": len(preserved),
        "reused_translation_blocks": len(reused),
        "missing": list(dict.fromkeys(missing)),
        "duplicate": duplicate,
        "unknown": unknown,
    }


def derive_layers(state: dict[str, Any]) -> dict[str, Any]:
    units = state.get("units", [])
    chapters = list(state.get("chapters", {}))
    completeness = state.get("completeness", {})
    translation_done = (
        bool(chapters)
        and all(item.get("status") in {"quality_pass", "complete"} for item in units)
        and completeness.get("ok", True)
        and not completeness.get("catastrophic", False)
    )
    work_scopes = [
        item for item in state.get("work_scopes", [])
        if int(item.get("reviewable_word_count", 0)) > 0
    ]
    if work_scopes:
        reviews = state.get("reviews", {}).get("scopes", {})
        review_done = all(
            reviews.get(item["id"], {}).get("status") in {"pass", "accepted_with_risk"}
            and float(reviews.get(item["id"], {}).get("coverage_percent", 100.0)) >= 100.0
            for item in work_scopes
        )
    else:
        reviews = state.get("reviews", {}).get("chapters", {})
        review_done = bool(chapters) and all(
            reviews.get(chapter, {}).get("status") in {"pass", "accepted_with_risk"}
            and float(reviews.get(chapter, {}).get("coverage_percent", 100.0)) >= 100.0
            for chapter in chapters
        )
    deferred = [item["id"] for item in units if item.get("status") == "deferred"]
    layers = {
        "source_plan": "pass" if plan_conservation(state)["ok"] else "fail",
        "translation": "pass" if translation_done else ("blocked" if deferred else "pending"),
        "review": "pass" if review_done else "pending",
        "assets": state.get("release_checks", {}).get("assets", "not_run"),
        "docx": state.get("release_checks", {}).get("docx", "not_run"),
    }
    layers["release"] = "ready" if all(layers[key] == "pass" for key in ("source_plan", "translation", "review", "assets", "docx")) else "not_ready"
    return layers


def diagnose(book_root: Path, state: dict[str, Any]) -> dict[str, Any]:
    conservation = plan_conservation(state)
    artifact_issues = verify_accepted_artifacts(book_root, state)
    layers = derive_layers(state)
    issues: list[dict[str, Any]] = []
    if not conservation["ok"]:
        issues.append({"rule": "source_plan_not_conservative", **conservation})
    issues.extend(artifact_issues)
    data = ensure_integrity_state(state)
    for unit in state.get("units", []):
        digest = data["accepted"].get(unit.get("id"))
        if not digest:
            continue
        mirror = book_root / "agent_work" / "translate" / "outbox" / f"{unit['id']}_translated.md"
        if not mirror.is_file() or sha256_file(mirror) != digest:
            issues.append({"rule": "compatibility_archive_diverged", "unit": unit["id"], "expected_sha256": digest})
    current = state.get("current_unit")
    if current and not any(item.get("id") == current and item.get("status") == "in_progress" for item in state.get("units", [])):
        issues.append({"rule": "stale_current_unit", "unit": current})
    if state.get("status") == "translation_complete_review_pending" and layers["translation"] != "pass":
        issues.append({"rule": "status_claims_translation_complete_but_units_disagree"})
    next_action = "agent recover" if artifact_issues else ("agent review next" if layers["translation"] == "pass" and layers["review"] != "pass" else "agent next")
    return {"ok": not issues, "layers": layers, "conservation": conservation, "issues": issues, "next_action": next_action}


def restore_compatibility_archives(book_root: Path, state: dict[str, Any]) -> dict[str, Any]:
    restored: list[str] = []
    for unit in state.get("units", []):
        path = accepted_artifact(book_root, state, unit["id"])
        if path is None:
            continue
        archive = book_root / "agent_work" / "translate" / "outbox" / f"{unit['id']}_translated.md"
        if not archive.is_file() or sha256_file(archive) != sha256_file(path):
            _atomic_bytes(archive, path.read_bytes())
            restored.append(unit["id"])
        unit["status"] = "quality_pass"
    state["current_unit"] = None
    bump_revision(state)
    return {"restored": restored, "layers": derive_layers(state)}


def _relationship_targets(zf: zipfile.ZipFile, rel_name: str, base: str) -> list[str]:
    if rel_name not in zf.namelist():
        return []
    root = ET.fromstring(zf.read(rel_name))
    targets: list[str] = []
    for rel in root.findall(f"{{{NS_R}}}Relationship"):
        if rel.get("TargetMode") == "External":
            continue
        target = rel.get("Target") or ""
        combined = PurePosixPath(base) / target
        parts: list[str] = []
        for part in combined.parts:
            if part == "..":
                if parts:
                    parts.pop()
            elif part != ".":
                parts.append(part)
        targets.append("/".join(parts))
    return targets


def verify_docx(path: Path, *, source_manifest: Path | None = None) -> dict[str, Any]:
    issues: list[dict[str, Any]] = []
    media: list[dict[str, Any]] = []
    try:
        with zipfile.ZipFile(path) as zf:
            bad = zf.testzip()
            if bad:
                issues.append({"rule": "zip_crc_error", "part": bad})
            names = set(zf.namelist())
            for name in names:
                if name.endswith((".xml", ".rels")):
                    try:
                        ET.fromstring(zf.read(name))
                    except ET.ParseError as exc:
                        issues.append({"rule": "xml_invalid", "part": name, "detail": str(exc)})
            for target in _relationship_targets(zf, "word/_rels/document.xml.rels", "word"):
                if target not in names:
                    issues.append({"rule": "relationship_target_missing", "target": target})
            document = ET.fromstring(zf.read("word/document.xml"))
            paragraph_note_refs: set[str] = set()
            paragraph_note_defs: list[str] = []
            for paragraph in document.iter(f"{{{NS_W}}}p"):
                value = "".join(node.text or "" for node in paragraph.iter(f"{{{NS_W}}}t"))
                labels = re.findall(r"〔注([^〕]+)〕", value)
                paragraph_note_refs.update(labels)
                properties = paragraph.find(f"{{{NS_W}}}pPr")
                style_node = properties.find(f"{{{NS_W}}}pStyle") if properties is not None else None
                style_value = style_node.get(f"{{{NS_W}}}val", "") if style_node is not None else ""
                if style_value == "Mode2ParagraphNote":
                    paragraph_note_defs.extend(labels[:1])
            paragraph_note_def_set = set(paragraph_note_defs)
            if paragraph_note_refs != paragraph_note_def_set:
                issues.append({
                    "rule": "paragraph_note_reference_definition_mismatch",
                    "references": sorted(paragraph_note_refs),
                    "definitions": sorted(paragraph_note_def_set),
                })
            footnote_refs = {node.get(f"{{{NS_W}}}id") for node in document.iter(f"{{{NS_W}}}footnoteReference")}
            endnote_refs = {node.get(f"{{{NS_W}}}id") for node in document.iter(f"{{{NS_W}}}endnoteReference")}
            footnote_defs: set[str] = set()
            endnote_defs: set[str] = set()
            if "word/footnotes.xml" in names:
                root = ET.fromstring(zf.read("word/footnotes.xml"))
                footnote_defs = {node.get(f"{{{NS_W}}}id") for node in root.iter(f"{{{NS_W}}}footnote") if int(node.get(f"{{{NS_W}}}id", "0")) > 0}
            if "word/endnotes.xml" in names:
                root = ET.fromstring(zf.read("word/endnotes.xml"))
                endnote_defs = {node.get(f"{{{NS_W}}}id") for node in root.iter(f"{{{NS_W}}}endnote") if int(node.get(f"{{{NS_W}}}id", "0")) > 0}
            if footnote_refs != footnote_defs:
                issues.append({"rule": "footnote_reference_definition_mismatch", "references": sorted(footnote_refs), "definitions": sorted(footnote_defs)})
            if endnote_refs != endnote_defs:
                issues.append({"rule": "endnote_reference_definition_mismatch", "references": sorted(endnote_refs), "definitions": sorted(endnote_defs)})
            try:
                from PIL import Image, ImageStat
                from io import BytesIO
                for name in sorted(x for x in names if x.startswith("word/media/")):
                    value = zf.read(name)
                    record: dict[str, Any] = {"part": name, "sha256": sha256_bytes(value), "bytes": len(value)}
                    try:
                        image = Image.open(BytesIO(value))
                        record["size"] = list(image.size)
                        gray = image.convert("L")
                        mean = float(ImageStat.Stat(gray).mean[0])
                        record["mean_luma"] = round(mean, 2)
                        if image.width <= 2 or image.height <= 2:
                            issues.append({"rule": "embedded_image_tiny", "part": name, "size": list(image.size)})
                        if mean < 3.0:
                            issues.append({"rule": "embedded_image_black", "part": name, "mean_luma": round(mean, 2)})
                    except Exception as exc:
                        issues.append({"rule": "embedded_image_unreadable", "part": name, "detail": str(exc)})
                    media.append(record)
            except ImportError:
                issues.append({"rule": "image_decoder_unavailable", "severity": "advisory"})
    except (OSError, zipfile.BadZipFile, KeyError, ET.ParseError) as exc:
        issues.append({"rule": "docx_package_invalid", "detail": str(exc)})

    if source_manifest and source_manifest.is_file():
        manifest = json.loads(source_manifest.read_text(encoding="utf-8-sig"))
        embedded = {item["sha256"] for item in media}
        for item in manifest.get("objects", []):
            digest = item.get("sha256")
            if digest and not item.get("missing_asset") and digest not in embedded:
                issues.append({"rule": "latest_asset_not_embedded", "object_id": item.get("id"), "sha256": digest})
    hard = [item for item in issues if item.get("severity") != "advisory"]
    return {
        "ok": not hard,
        "path": str(path),
        "issues": issues,
        "media": media,
        "summary": {
            "hard_failures": len(hard),
            "media": len(media),
            "paragraph_note_references": len(paragraph_note_refs) if 'paragraph_note_refs' in locals() else 0,
            "paragraph_note_definitions": len(paragraph_note_def_set) if 'paragraph_note_def_set' in locals() else 0,
        },
    }
