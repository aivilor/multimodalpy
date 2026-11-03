"""Text helpers: name normalisation, edit distances and slugs.

Private module.
"""

from __future__ import annotations

import logging
import re
import unicodedata
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Names
# ---------------------------------------------------------------------------
def normalize_name(value: object) -> str:
    """Normalize text for accent-/case-insensitive comparisons."""
    text = "" if value is None else str(value)
    text = unicodedata.normalize("NFKD", text)
    text = text.encode("ascii", "ignore").decode("ascii")
    text = re.sub(r"[^a-zA-Z0-9]+", " ", text)
    return re.sub(r"\s+", " ", text).strip().lower()


def slugify(value: str) -> str:
    """Generate a filesystem-safe slug from value."""
    slug = normalize_name(value).replace(" ", "_")
    return slug or "area"


# ---------------------------------------------------------------------------
# Edit distances
# ---------------------------------------------------------------------------
def levenshtein_distance(left: str, right: str) -> int:
    """Levenshtein edit distance between two normalized strings."""
    if left == right:
        return 0
    if not left:
        return len(right)
    if not right:
        return len(left)

    previous = list(range(len(right) + 1))
    for i, left_char in enumerate(left, start=1):
        current = [i]
        for j, right_char in enumerate(right, start=1):
            insert_cost = current[j - 1] + 1
            delete_cost = previous[j] + 1
            replace_cost = previous[j - 1] + (left_char != right_char)
            current.append(min(insert_cost, delete_cost, replace_cost))
        previous = current
    return previous[-1]


def levenshtein_similarity(left: str, right: str) -> float:
    """0-1 similarity score based on the Levenshtein distance."""
    max_length = max(len(left), len(right))
    if max_length == 0:
        return 1.0
    return 1 - (levenshtein_distance(left, right) / max_length)


def _typo_distance(left: str, right: str) -> int:
    """Edit distance counting adjacent transpositions as one edit (OSA)."""
    if left == right:
        return 0
    rows = len(left) + 1
    cols = len(right) + 1
    dist = [[0] * cols for _ in range(rows)]
    for i in range(rows):
        dist[i][0] = i
    for j in range(cols):
        dist[0][j] = j
    for i in range(1, rows):
        for j in range(1, cols):
            cost = left[i - 1] != right[j - 1]
            dist[i][j] = min(
                dist[i - 1][j] + 1,
                dist[i][j - 1] + 1,
                dist[i - 1][j - 1] + cost,
            )
            if (
                i > 1
                and j > 1
                and left[i - 1] == right[j - 2]
                and left[i - 2] == right[j - 1]
            ):
                dist[i][j] = min(dist[i][j], dist[i - 2][j - 2] + 1)
    return dist[-1][-1]


def _typo_similarity(left: str, right: str) -> tuple[float, int]:
    """0-1 similarity (and distance) based on :func:`_typo_distance`."""
    max_length = max(len(left), len(right))
    if max_length == 0:
        return 1.0, 0
    distance = _typo_distance(left, right)
    return 1 - distance / max_length, distance
