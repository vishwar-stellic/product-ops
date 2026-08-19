"""Canonical "tracked" project milestones - lifecycle checkpoints every
project is expected to have. Used to:

- filter the milestones table shown on the web dashboard and in the Notion
  export down to just these five (see `notion_report.py`; the equivalent
  list is duplicated in `static/app.js:KEY_MILESTONE_NAMES` since that runs
  client-side and can't import this module), and
- let `milestone_setup.add_tracked_milestones_for_team` create whichever of
  the five a project is missing (see `cli.py --add-tracked-milestones`).
"""

import re
from typing import Any, Dict, List, Optional

KEY_MILESTONE_NAMES = ["Product: Define", "Design: Shape", "Design: Refine", "Early Access", "Public Launch"]

_NON_ALNUM_RE = re.compile(r"[^a-z0-9]")


def normalize_milestone_name(name: Optional[str]) -> str:
    """Lowercase, punctuation/whitespace-stripped form used for fuzzy
    matching, so e.g. "product define", "Product - Define", and
    "PRODUCT: DEFINE" all match "Product: Define"."""
    return _NON_ALNUM_RE.sub("", (name or "").lower())


def match_key_milestones_map(milestones: List[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    """Canonical name -> the first existing milestone that fuzzy-matches it
    (only canonical names with a match are included)."""
    targets = [(name, normalize_milestone_name(name)) for name in KEY_MILESTONE_NAMES]
    by_target: Dict[str, Dict[str, Any]] = {}
    for milestone in milestones:
        norm = normalize_milestone_name(milestone.get("name"))
        for name, target_norm in targets:
            if name in by_target:
                continue
            if norm == target_norm or target_norm in norm or norm in target_norm:
                by_target[name] = milestone
                break
    return by_target


def match_key_milestones(milestones: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Existing milestones that fuzzy-match a canonical name, one per
    canonical name (first match wins), in canonical order."""
    by_target = match_key_milestones_map(milestones)
    return [by_target[name] for name in KEY_MILESTONE_NAMES if name in by_target]


def missing_key_milestone_names(milestones: List[Dict[str, Any]]) -> List[str]:
    """Canonical names with no fuzzy-matching milestone yet, in canonical order."""
    by_target = match_key_milestones_map(milestones)
    return [name for name in KEY_MILESTONE_NAMES if name not in by_target]


def resolve_milestone_names(requested: List[str]) -> List[str]:
    """Map user-provided milestone name(s) (e.g. from `--milestones` on the
    CLI) to their canonical form, fuzzy-matched the same way existing
    milestones are. Returns the matches in canonical order, deduplicated.

    Raises `ValueError` (listing the offending input and the valid options)
    if any requested name doesn't match one of `KEY_MILESTONE_NAMES`.
    """
    targets = [(name, normalize_milestone_name(name)) for name in KEY_MILESTONE_NAMES]
    resolved = set()
    unmatched = []
    for raw in requested:
        norm = normalize_milestone_name(raw)
        match = next(
            (name for name, target_norm in targets if norm == target_norm or target_norm in norm or norm in target_norm),
            None,
        )
        if match:
            resolved.add(match)
        else:
            unmatched.append(raw)

    if unmatched:
        raise ValueError(
            f"Unrecognized milestone name(s): {', '.join(unmatched)}. "
            f"Valid options: {', '.join(KEY_MILESTONE_NAMES)}"
        )

    return [name for name in KEY_MILESTONE_NAMES if name in resolved]
