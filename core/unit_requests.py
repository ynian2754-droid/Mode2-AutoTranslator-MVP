"""Source context, frozen references, and unit request construction.

Callers hold the project's original lock. Review construction writes its
reference at the original build point; this module neither locks nor saves.
"""

from __future__ import annotations

import copy
from typing import Any, Callable, Mapping, Protocol

from core import concept_automation, project_settings, unit_state
from core.project_state import ProjectStateCell
from core.quality_state import quality_unit_sources
from core.quality_support import (
    build_reference_snapshot, normalize_quality_support,
    select_reference_candidates, select_reference_cards, terminology_rules,
)
from core.translation_context import build_translation_context
from providers.base import ReviewRequest, TranslationRequest


class PromptSettingsPort(Protocol):
    def prompt_for_task(self, task: str) -> str: ...



def unit_translation_context(state: Mapping[str, Any], unit: Mapping[str, Any]) -> dict[str, str]:
    """The previous/next SOURCE context of one unit under the saved budget."""

    previous_words, next_words = project_settings.configured_context_words_for_state(state)
    return build_translation_context(
        state["units"],
        unit["id"],
        previous_words=previous_words,
        next_words=next_words,
    )

def structure_role(state: Mapping[str, Any], unit: dict[str, Any]) -> tuple[str, str]:
    """Read the structural role of a unit from the document layer only."""
    document = state.get("document")
    nodes = (document or {}).get("nodes") if isinstance(document, dict) else None
    if not isinstance(nodes, list):
        return "", ""
    node_id = str(unit.get("node_id") or "")
    for node in nodes:
        if not isinstance(node, dict) or str(node.get("id") or "") != node_id:
            continue
        attributes = node.get("attributes") if isinstance(node.get("attributes"), dict) else {}
        role = attributes.get("layout_role")
        if isinstance(role, str) and role.strip():
            return role.strip(), "document_node.layout_role"
        node_type = str(node.get("type") or "")
        if node_type:
            return node_type, "document_node.type"
    return "", ""

def freeze_reference(
    state: Mapping[str, Any],
    unit: dict[str, Any],
    *,
    adjacent_texts: list[str],
) -> dict[str, Any]:
    """Freeze one reference snapshot for the current unit.

        An empty snapshot is still a real snapshot.  This matters when a
        translation starts before the first concept card is approved: the
        automatic review must reuse the empty reference rather than resolving
        a newer set of cards after the model call returns.  Empty snapshots
        are deliberately omitted from provider context below, so legacy
        projects keep receiving the same messages as before.
        """
    support = normalize_quality_support(state.get("quality_support"))
    previous_words, next_words = project_settings.configured_context_words_for_state(state)
    context_budget = {
        "previous_context_words": previous_words,
        "next_context_words": next_words,
    }
    unit_sources = quality_unit_sources(state.get("units") or [])
    mode = concept_automation.reference_mode(state.get("project"))
    candidate_cards = select_reference_candidates(
        support,
        unit_id=str(unit.get("id") or ""),
        unit_sources=unit_sources,
        mode=mode,
    )
    term_rules = terminology_rules(candidate_cards, str(unit.get("source") or ""))
    selection = select_reference_cards(
        support,
        source_text=str(unit.get("source") or ""),
        adjacent_texts=adjacent_texts,
        candidate_cards=candidate_cards,
        conflicted_expressions={
            str(expression).casefold()
            for item in term_rules["conflicts"]
            for expression in item.get("expressions") or [item["expression"]]
        },
    )
    role, role_source = structure_role(state, unit)
    snapshot = build_reference_snapshot(
        approved_version=support["approved_version"],
        selection=selection,
        context_budget=context_budget,
        structure_role=role,
        structure_role_source=role_source,
        term_rules=term_rules,
    )
    snapshot["reference_mode"] = mode
    snapshot["reference_revision"] = int(
        concept_automation.automation_of(support).get("reference_revision") or 0
    )
    snapshot["frozen_empty"] = not bool(candidate_cards)
    return snapshot

def unit_reference_texts(state: Mapping[str, Any], unit: dict[str, Any]) -> list[str]:
    context = unit_translation_context(state, unit)
    return [
        str(context[key])
        for key in ("previous_context", "next_context")
        if isinstance(context.get(key), str) and context[key].strip()
    ]

def store_reference(
    unit: dict[str, Any],
    snapshot: dict[str, Any] | None,
    *,
    kind: str,
    is_new_reference: bool = False,
    clock: Callable[[], str],
) -> None:
    """Attach a frozen snapshot to the revision that actually used it."""
    if snapshot is None:
        return
    reference = unit.get("quality_reference")
    if not isinstance(reference, dict):
        reference = {}
    entry = {
        "translation_revision": int(unit.get("translation_revision") or 0),
        "approved_version": int(snapshot.get("approved_version") or 0),
        "card_count": int(snapshot.get("card_count") or 0),
        "snapshot": snapshot,
        "at": clock(),
    }
    if kind == "review":
        entry["is_new_reference"] = bool(is_new_reference)
    reference[kind] = entry
    unit["quality_reference"] = reference

class UnitRequests:
    def __init__(
        self,
        cell: ProjectStateCell,
        settings: PromptSettingsPort,
        clock: Callable[[], str],
    ) -> None:
        self.cell = cell
        self.settings = settings
        self.clock = clock

    def translation_locked(self, unit: dict[str, Any]) -> tuple[TranslationRequest, dict[str, Any] | None]:
        unit_state.ensure_unit_feedback_fields(unit)
        context: dict[str, Any] = {
            "force_review": bool(unit.get("demo_force_review")),
            "project_id": self.cell.state["project"]["id"],
        }
        context.update(unit_translation_context(self.cell.state, unit))
        # Freeze the reference snapshot once per translation request. The
        # automatic review of this same translation reuses exactly this object.
        snapshot = freeze_reference(self.cell.state,
            unit,
            adjacent_texts=[
                str(context[key])
                for key in ("previous_context", "next_context")
                if isinstance(context.get(key), str) and context[key].strip()
            ],
        )
        if snapshot.get("cards"):
            context["concept_references"] = [
                str(card["text"]) for card in snapshot.get("cards") or []
            ]
            context["concept_reference_version"] = snapshot["approved_version"]
        if snapshot.get("term_rules"):
            context["terminology_rules"] = copy.deepcopy(snapshot["term_rules"])
        feedback = unit.get("pending_translation_feedback")
        revision = unit["translation_revision"]
        manual_reference = unit.get("user_edited_translation")
        if isinstance(manual_reference, str) and manual_reference.strip():
            # A saved manual translation is request-local reference material.
            # It is never used as the source text and never shared with other
            # Units; a successful new AI translation consumes it below.
            context["user_edited_translation"] = manual_reference.strip()
        if isinstance(feedback, dict):
            suggestions = unit_state.normalize_suggestions(feedback.get("suggestions"))
            source_revision = feedback.get("source_revision")
            # The record describes the last *saved* draft; `revision` is the
            # attempt that replaces it. The version check stays — the record must
            # belong to an earlier revision than this attempt — but it no longer
            # demands "exactly one revision back": a repeated failure keeps the
            # same draft (and its feedback) while the attempt numbers move on,
            # and its suggestions stay valid until a new draft is saved.
            earlier_draft = (
                isinstance(source_revision, int)
                and not isinstance(source_revision, bool)
                and 0 <= source_revision < revision
            )
            if earlier_draft and suggestions:
                context["validation_suggestions"] = suggestions
                previous_translation = feedback.get("previous_translation")
                if isinstance(previous_translation, str) and previous_translation.strip():
                    context["previous_translation"] = previous_translation
        request = TranslationRequest(
            unit_id=unit["id"],
            source_text=unit["source"],
            source_sha256=unit["source_sha256"],
            source_language=self.cell.state["config"]["source_language"],
            target_language=self.cell.state["config"]["target_language"],
            context=context,
            # Resolved under the pipeline lock when the request is built, so
            # this attempt keeps its prompt even if the selection changes
            # while the model call runs.
            system_prompt=self.settings.prompt_for_task("unit_translation"),
        )
        return request, snapshot

    def review_locked(
        self,
        unit: dict[str, Any],
        snapshot: dict[str, Any] | None = None,
    ) -> ReviewRequest:
        """Build one review request.

        The automatic review after a translation receives the same frozen
        snapshot that the translation used. A user-triggered re-review freezes
        the latest approved cards and is flagged as a new reference version.
        """
        context: dict[str, Any] = {"project_id": self.cell.state["project"]["id"]}
        # Limited adjacent SOURCE text only: never other units' translations
        # and never the browser draft. Direction is preserved so the reviewer
        # can tell preceding from following context; flattening the two into a
        # single list lost the direction when one side was empty.
        translation_context = unit_translation_context(self.cell.state, unit)
        previous_source = str(translation_context.get("previous_context") or "").strip()
        next_source = str(translation_context.get("next_context") or "").strip()
        adjacent: list[str] = []
        review_source_context: dict[str, str] = {}
        if previous_source:
            adjacent.append(previous_source)
            review_source_context["previous"] = previous_source
        if next_source:
            adjacent.append(next_source)
            review_source_context["next"] = next_source
        if not adjacent:
            own_source = str(unit.get("source") or "").strip()
            if own_source:
                # Fall back to the unit's own source so the reviewer always has
                # at least one verifiable text snippet, clearly labelled as the
                # current unit rather than a neighbour.
                adjacent.append(own_source)
                review_source_context["current"] = own_source
        if review_source_context:
            context["review_source_context"] = review_source_context
        if snapshot is None:
            snapshot = freeze_reference(self.cell.state, unit, adjacent_texts=adjacent)
            is_new = True
        else:
            is_new = False
        store_reference(unit, snapshot, kind="review", is_new_reference=is_new, clock=self.clock)
        if snapshot.get("cards"):
            context["concept_references"] = [
                str(card["text"]) for card in snapshot.get("cards") or []
            ]
            context["concept_reference_version"] = snapshot["approved_version"]
            context["concept_reference_is_new"] = is_new
        if snapshot.get("term_rules"):
            context["terminology_rules"] = copy.deepcopy(snapshot["term_rules"])
        role, role_source = structure_role(self.cell.state, unit)
        if role:
            context["structure_role"] = role
            context["structure_role_source"] = role_source
        return ReviewRequest(
            unit_id=unit["id"],
            source_text=unit["source"],
            translated_text=unit["translation"],
            source_sha256=unit["source_sha256"],
            source_language=self.cell.state["config"]["source_language"],
            target_language=self.cell.state["config"]["target_language"],
            context=context,
            system_prompt=self.settings.prompt_for_task("unit_review"),
        )

