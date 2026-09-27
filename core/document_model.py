"""Small, JSON-friendly document model shared by assembly and exporters."""

from __future__ import annotations

from typing import Any


DOCUMENT_SCHEMA_VERSION = 2

NODE_TYPES = {
    "section",
    "heading",
    "paragraph",
    "list",
    "list_item",
    "blockquote",
    "table",
    "code",
    "image",
    "page_break",
    "literal",
    "raw",
}


def empty_document(
    *,
    document_format: str | None = None,
    source_name: str | None = None,
    source_sha256: str = "",
) -> dict[str, Any]:
    """Return the v2 document envelope without changing the v1 unit protocol."""
    return {
        "schema_version": DOCUMENT_SCHEMA_VERSION,
        "format": document_format,
        "source_name": source_name,
        "source_sha256": source_sha256,
        "unit_count": 0,
        # Kept for the existing linear assembler and old project readers.
        "parts": [],
        "nodes": [],
        "reading_order": [],
        "assets": [],
    }


def is_structured_document(document: Any) -> bool:
    return (
        isinstance(document, dict)
        and document.get("schema_version") == DOCUMENT_SCHEMA_VERSION
        and isinstance(document.get("nodes"), list)
        and isinstance(document.get("reading_order"), list)
    )


def node_map(document: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Index valid nodes by ID without silently accepting duplicate IDs."""
    nodes = document.get("nodes")
    if not isinstance(nodes, list):
        return {}
    result: dict[str, dict[str, Any]] = {}
    for node in nodes:
        if not isinstance(node, dict):
            continue
        node_id = str(node.get("id") or "")
        if node_id and node_id not in result:
            result[node_id] = node
    return result


def node_unit_ids(node: dict[str, Any]) -> list[str]:
    values = node.get("unit_ids")
    if not isinstance(values, list):
        return []
    return [str(value) for value in values if str(value)]
