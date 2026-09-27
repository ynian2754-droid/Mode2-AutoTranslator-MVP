"""Durable local project state and imported source-file storage."""

from __future__ import annotations

import json
import os
import re
import time
import uuid
from pathlib import Path

#: The replacement step can lose a race with a reader that already holds
#: ``project.json`` open: Windows refuses to replace a file whose handle was not
#: opened for delete sharing, and such a reader closes it again shortly after.
#: Only those two sharing families are absorbed, and only a bounded number of
#: times -- a genuinely denied write must still fail and keep the old file.
_REPLACE_ATTEMPTS = 4
_REPLACE_DELAYS = (0.05, 0.1, 0.2)
_TRANSIENT_WINERRORS = frozenset({5, 32, 33})  # ACCESS_DENIED / SHARING / LOCK violation


def _transient_replace_error(exc: BaseException) -> bool:
    """Whether ``exc`` is a Windows sharing/lock error that may clear by itself.

    Anything else -- disk full, a bad path, a serialisation error, or an access
    denial without a Windows error number we recognise -- is permanent here:
    this module never guesses that an unclassified failure is recoverable.
    """

    if os.name != "nt" or not isinstance(exc, PermissionError):
        return False
    return getattr(exc, "winerror", None) in _TRANSIENT_WINERRORS


class ProjectStore:
    """Persist one active project atomically under the runtime directory."""

    def __init__(self, runtime_dir: Path | str) -> None:
        self.runtime_dir = Path(runtime_dir)
        self.state_path = self.runtime_dir / "project.json"

    def load(self) -> dict | None:
        if not self.state_path.is_file():
            return None
        try:
            data = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        return data if isinstance(data, dict) else None

    def save(self, state: dict) -> None:
        """Serialise ``state`` completely, then publish it with one atomic replace.

        The scratch file is unique to this call, so two saves can never share
        (or delete) each other's scratch file, and a failed save can only ever
        clean up its own. Writing order between business operations is *not*
        established here: that stays the caller's lock, as before.
        """

        self.runtime_dir.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(state, ensure_ascii=False, indent=2)
        temporary = self._temporary_path()
        try:
            temporary.write_text(payload, encoding="utf-8")
            self._replace_with_retry(temporary, self.state_path)
        finally:
            self._discard_temporary(temporary)

    def _temporary_path(self) -> Path:
        return self.state_path.with_name(f"{self.state_path.name}.{uuid.uuid4().hex}.tmp")

    def _replace_with_retry(self, temporary: Path, target: Path) -> None:
        """Replace ``target``, retrying only a transient sharing failure.

        The scratch file is already written, so a retry repeats nothing else:
        no model call, no other business step, and no second serialisation.
        """

        for attempt in range(_REPLACE_ATTEMPTS):
            try:
                temporary.replace(target)
                return
            except OSError as exc:
                last_attempt = attempt == _REPLACE_ATTEMPTS - 1
                if last_attempt or not _transient_replace_error(exc):
                    raise
                time.sleep(_REPLACE_DELAYS[attempt])

    @staticmethod
    def _discard_temporary(temporary: Path) -> None:
        """Remove this call's scratch file; never mask the original failure."""

        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass

    def backup_state(self, state: dict, *, reason: str) -> str:
        """Write an immutable JSON snapshot before an explicit destructive reset."""

        runtime_root = self.runtime_dir.resolve()
        backup_root = (runtime_root / "backups").resolve()
        if runtime_root != backup_root.parent:
            raise ValueError("项目备份路径越界。")
        safe_reason = re.sub(r"[^A-Za-z0-9_-]+", "_", reason).strip("_") or "backup"
        backup_root.mkdir(parents=True, exist_ok=True)
        target = backup_root / f"project-{safe_reason}-{uuid.uuid4().hex[:12]}.json"
        temporary = target.with_suffix(".json.tmp")
        temporary.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(target)
        return target.relative_to(runtime_root).as_posix()

    def save_source_file(self, project_id: str, original_name: str, content: bytes) -> str:
        safe_name = self._safe_name(original_name)
        runtime_root = self.runtime_dir.resolve()
        project_root = (runtime_root / "sources" / project_id).resolve()
        if runtime_root not in project_root.parents:
            raise ValueError("源文件保存路径越界。")
        project_root.mkdir(parents=True, exist_ok=True)
        target = project_root / f"{uuid.uuid4().hex[:12]}_{safe_name}"
        temporary = target.with_suffix(target.suffix + ".tmp")
        temporary.write_bytes(content)
        temporary.replace(target)
        return target.relative_to(runtime_root).as_posix()

    @staticmethod
    def _safe_name(original_name: str) -> str:
        name = Path(original_name or "source").name
        name = re.sub(r"[^A-Za-z0-9._-]+", "_", name).strip("._")
        return name or "source"
