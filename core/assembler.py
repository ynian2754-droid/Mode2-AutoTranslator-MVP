"""Reassemble translated units into one complete document."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from .document_model import is_structured_document, node_map, node_unit_ids
from .exceptions import PipelineError
from .utils import now_iso, sha256_text


EXPORTABLE_STATUSES = {"passed", "accepted_risk", "user_modified"}


class AssemblyError(PipelineError):
    """The project cannot be safely assembled or exported."""


class DocumentAssembler:
    """Validate a document manifest and write its translated text atomically."""

    def __init__(self, runtime_dir: Path | str) -> None:
        self.runtime_dir = Path(runtime_dir).resolve()
        self.output_root = (self.runtime_dir / "output").resolve()

    def readiness(self, state: dict[str, Any]) -> dict[str, Any]:
        units = state.get("units") if isinstance(state.get("units"), list) else []
        invalid_units = bool(units) and any(not isinstance(unit, dict) for unit in units)
        translated_units = (
            0
            if invalid_units
            else sum(bool(str(unit.get("translation") or "").strip()) for unit in units)
        )
        completed_units = (
            0
            if invalid_units
            else sum(unit.get("status") in EXPORTABLE_STATUSES for unit in units)
        )
        result: dict[str, Any] = {
            "ready": False,
            "reason": None,
            "total_units": len(units),
            "translated_units": translated_units,
            "completed_units": completed_units,
            "output": state.get("output") or {},
        }
        if not units:
            result["reason"] = "尚未导入源文件。"
            return result
        if invalid_units:
            result["reason"] = "项目包含无效翻译单元，无法安全输出。"
            return result
        try:
            self._validate_manifest(state)
        except AssemblyError as exc:
            result["reason"] = str(exc)
            return result
        if translated_units != len(units):
            result["reason"] = f"还有 {len(units) - translated_units} 个单元没有译文。"
            return result
        if completed_units != len(units):
            result["reason"] = f"还有 {len(units) - completed_units} 个单元未通过校验或未接受风险。"
            return result
        result["ready"] = True
        result["reason"] = "所有单元均已完成，可输出完整文档。"
        return result

    def assemble_text(self, state: dict[str, Any]) -> str:
        readiness = self.readiness(state)
        if not readiness["ready"]:
            raise AssemblyError(str(readiness["reason"] or "当前项目尚不可输出。"))
        units = state.get("units") or []
        units_by_id = {str(unit.get("id")): unit for unit in units}
        parts = (state.get("document") or {}).get("parts") or []
        output: list[str] = []
        seen: set[str] = set()
        for part in parts:
            if not isinstance(part, dict):
                raise AssemblyError("文档 manifest 包含无效片段。")
            part_type = part.get("type")
            if part_type == "literal":
                literal = part.get("text")
                if not isinstance(literal, str):
                    raise AssemblyError("文档 manifest 的固定文本无效。")
                output.append(literal)
                continue
            if part_type != "unit":
                raise AssemblyError("文档 manifest 包含未知片段类型。")
            unit_id = str(part.get("unit_id") or "")
            if unit_id in seen:
                raise AssemblyError(f"文档 manifest 重复引用单元：{unit_id}")
            unit = units_by_id.get(unit_id)
            if unit is None:
                raise AssemblyError(f"文档 manifest 引用了不存在的单元：{unit_id}")
            seen.add(unit_id)
            output.append(str(unit.get("translation") or ""))
        if seen != set(units_by_id):
            missing = sorted(set(units_by_id) - seen)
            raise AssemblyError(f"文档 manifest 缺少单元：{', '.join(missing)}")
        return "".join(output)

    def assemble_nodes(self, state: dict[str, Any]) -> list[dict[str, Any]]:
        """Materialize translated text back into the persisted document nodes."""
        readiness = self.readiness(state)
        if not readiness["ready"]:
            raise AssemblyError(str(readiness["reason"] or "当前项目尚不可输出。"))
        units = state.get("units") or []
        units_by_id = {str(unit.get("id")): unit for unit in units}
        document = state.get("document") or {}
        if not is_structured_document(document):
            ordered_units = [unit for _order, unit in sorted(self._unit_order_pairs(units))]
            return [
                {
                    "id": "legacy-document",
                    "order": 1,
                    "type": "raw",
                    "source": "",
                    "translated_text": self.assemble_text(state),
                    "separator_before": "",
                    "unit_ids": [str(unit.get("id")) for unit in ordered_units],
                    "attributes": {"legacy": True},
                }
            ]

        nodes = node_map(document)
        reading_order = [str(value) for value in document.get("reading_order") or []]
        if not reading_order or len(reading_order) != len(nodes) or set(reading_order) != set(nodes):
            raise AssemblyError("结构化文档的 reading_order 与 nodes 不一致。")
        materialized: list[dict[str, Any]] = []
        seen_units: set[str] = set()
        for order, node_id in enumerate(reading_order, 1):
            node = nodes[node_id]
            unit_ids = node_unit_ids(node)
            translated_parts: list[str] = []
            for unit_id in unit_ids:
                if unit_id in seen_units:
                    raise AssemblyError(f"结构化文档重复引用单元：{unit_id}")
                unit = units_by_id.get(unit_id)
                if unit is None:
                    raise AssemblyError(f"结构化文档引用了不存在的单元：{unit_id}")
                seen_units.add(unit_id)
                translated_parts.append(str(unit.get("translation") or ""))
            materialized.append(
                {
                    "id": node_id,
                    "order": order,
                    "type": str(node.get("type") or "paragraph"),
                    "source": str(node.get("source") or ""),
                    "translated_text": "".join(translated_parts),
                    "separator_before": str(node.get("separator_before") or ""),
                    "unit_ids": unit_ids,
                    "attributes": dict(node.get("attributes") or {})
                    if isinstance(node.get("attributes"), dict)
                    else {},
                }
            )
        if seen_units != set(units_by_id):
            missing = sorted(set(units_by_id) - seen_units)
            raise AssemblyError(f"结构化文档缺少单元：{', '.join(missing)}")
        return materialized

    def trace_map(self, state: dict[str, Any]) -> dict[str, Any]:
        """Return a stable source-node-to-unit-to-output mapping."""
        nodes = self.assemble_nodes(state)
        return {
            "schema_version": 1,
            "source_sha256": str((state.get("document") or {}).get("source_sha256") or ""),
            "unit_count": len(state.get("units") or []),
            "nodes": [
                {
                    "node_id": node["id"],
                    "output_order": node["order"],
                    "type": node["type"],
                    "unit_ids": list(node["unit_ids"]),
                }
                for node in nodes
            ],
        }

    def export(self, state: dict[str, Any], *, output_format: str | None = None) -> dict[str, Any]:
        content = self.assemble_text(state)
        self.output_root.mkdir(parents=True, exist_ok=True)
        filename = self._output_filename(state, output_format)
        target = (self.output_root / filename).resolve()
        if target.parent != self.output_root:
            raise AssemblyError("输出路径必须位于当前项目的 output 目录内。")
        temporary = target.with_name(f".{target.name}.tmp")
        try:
            temporary.write_text(content, encoding="utf-8", newline="\n")
            temporary.replace(target)
            trace_path = self.write_trace_map(target, self.trace_map(state))
        except OSError as exc:
            raise AssemblyError(f"完整文档写入失败：{exc}") from exc
        return {
            "format": output_format or self._default_output_format(state),
            "path": target.relative_to(self.runtime_dir).as_posix(),
            "trace_map_path": trace_path.relative_to(self.runtime_dir).as_posix(),
            "filename": target.name,
            "sha256": sha256_text(content),
            "exported_at": now_iso(),
            "included_unit_count": len(state.get("units") or []),
            "size_bytes": target.stat().st_size,
        }

    @staticmethod
    def source_title(state: dict[str, Any]) -> str:
        """The source document's file stem, before sanitizing."""
        source_file = (state.get("project") or {}).get("source_file") or {}
        source_name = str(
            source_file.get("name")
            or (state.get("document") or {}).get("source_name")
            or "document"
        )
        return Path(source_name).stem or "document"

    @classmethod
    def source_stem(cls, state: dict[str, Any]) -> str:
        """The sanitized stem every exporter derives its target name from."""
        return re.sub(r"[^0-9A-Za-z\u4e00-\u9fff._-]+", "_", cls.source_title(state)).strip("._") or "document"

    def write_trace_map(self, target: Path, trace: dict[str, Any]) -> Path:
        """Write one trace map beside its artifact through a same-directory temp file.

        Only the write and the replace live here. Every exporter keeps its own
        error type, message and post-write checks around the call.
        """
        trace_path = target.with_name(f"{target.name}.map.json")
        temporary = trace_path.with_name(f".{trace_path.name}.tmp")
        temporary.write_text(
            json.dumps(trace, ensure_ascii=False, indent=2),
            encoding="utf-8",
            newline="\n",
        )
        temporary.replace(trace_path)
        return trace_path

    def output_path(self, output_metadata: dict[str, Any] | None) -> Path | None:
        relative_path = str((output_metadata or {}).get("path") or "")
        if not relative_path:
            return None
        candidate = (self.runtime_dir / Path(relative_path)).resolve()
        if self.output_root not in candidate.parents or not candidate.is_file():
            return None
        return candidate

    def _validate_manifest(self, state: dict[str, Any]) -> None:
        document = state.get("document")
        if not isinstance(document, dict) or document.get("schema_version") not in {1, 2}:
            raise AssemblyError("项目缺少文档组装结构，请重新导入源文件。")
        units = state.get("units")
        if not isinstance(units, list) or not units:
            raise AssemblyError("项目中没有可组装的翻译单元。")
        if any(not isinstance(unit, dict) for unit in units):
            raise AssemblyError("项目包含无效翻译单元。")
        parts = document.get("parts")
        if not isinstance(parts, list) or not parts:
            raise AssemblyError("项目缺少文档组装片段，请重新导入源文件。")
        for part in parts:
            if not isinstance(part, dict) or part.get("type") not in {"unit", "literal"}:
                raise AssemblyError("文档 manifest 包含未知片段类型。")
            if part.get("type") == "literal" and not isinstance(part.get("text"), str):
                raise AssemblyError("文档 manifest 的固定文本无效。")
        unit_ids = [str(unit.get("id") or "") for unit in units]
        if any(not unit_id for unit_id in unit_ids) or len(set(unit_ids)) != len(unit_ids):
            raise AssemblyError("项目翻译单元 ID 重复或无效。")
        unit_orders = self._unit_orders(units)
        orders = [order for order, _unit_id in unit_orders]
        if sorted(orders) != list(range(1, len(units) + 1)):
            raise AssemblyError("翻译单元序号不连续，无法安全组装。")
        ordered_ids = [
            unit_id
            for unit_id in (
                str(part.get("unit_id") or "")
                for part in parts
                if isinstance(part, dict) and part.get("type") == "unit"
            )
        ]
        if ordered_ids != [unit_id for _order, unit_id in sorted(unit_orders)]:
            raise AssemblyError("文档组装顺序与翻译单元顺序不一致。")
        if len(ordered_ids) != len(set(ordered_ids)):
            raise AssemblyError("文档 manifest 重复引用翻译单元。")
        if set(ordered_ids) != set(unit_ids):
            raise AssemblyError("文档 manifest 与翻译单元列表不一致。")
        if document.get("schema_version") == 2:
            nodes = document.get("nodes")
            reading_order = document.get("reading_order")
            if not isinstance(nodes, list) or not nodes:
                raise AssemblyError("结构化文档缺少 nodes。")
            if not isinstance(reading_order, list) or not reading_order:
                raise AssemblyError("结构化文档缺少 reading_order。")
            node_ids = []
            mapped_units: list[str] = []
            for node in nodes:
                if not isinstance(node, dict) or not str(node.get("id") or ""):
                    raise AssemblyError("结构化文档包含无效节点。")
                node_ids.append(str(node["id"]))
                if node.get("type") not in {
                    "section", "heading", "paragraph", "list", "list_item", "blockquote",
                    "table", "code", "image", "page_break", "literal", "raw",
                }:
                    raise AssemblyError("结构化文档包含未知节点类型。")
                mapped_units.extend(node_unit_ids(node))
            if len(set(node_ids)) != len(node_ids):
                raise AssemblyError("结构化文档节点 ID 重复。")
            if [str(value) for value in reading_order] != node_ids:
                raise AssemblyError("结构化文档 reading_order 顺序无效。")
            if len(mapped_units) != len(set(mapped_units)) or set(mapped_units) != set(unit_ids):
                raise AssemblyError("结构化文档节点与翻译单元映射不完整。")

    @staticmethod
    def _unit_orders(units: list[dict[str, Any]]) -> list[tuple[int, str]]:
        result: list[tuple[int, str]] = []
        for fallback, unit in enumerate(units, 1):
            try:
                order = int(unit.get("order"))
            except (TypeError, ValueError):
                order = fallback
            result.append((order, str(unit.get("id") or "")))
        return result

    @staticmethod
    def _unit_order_pairs(units: list[dict[str, Any]]) -> list[tuple[int, dict[str, Any]]]:
        result: list[tuple[int, dict[str, Any]]] = []
        for fallback, unit in enumerate(units, 1):
            try:
                order = int(unit.get("order"))
            except (TypeError, ValueError):
                order = fallback
            result.append((order, unit))
        return result

    @classmethod
    def _output_filename(cls, state: dict[str, Any], output_format: str | None = None) -> str:
        file_format = output_format or cls._default_output_format(state)
        suffix = ".txt" if file_format == "text" else ".md"
        return f"{cls.source_stem(state)}_translated{suffix}"

    @staticmethod
    def _default_output_format(state: dict[str, Any]) -> str:
        return "text" if str((state.get("document") or {}).get("format") or "") == "text" else "markdown"
