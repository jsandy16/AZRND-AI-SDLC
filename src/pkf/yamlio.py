"""YAML read/write that keeps comments and key order (round-trip), so people and tools can both edit specs."""
from __future__ import annotations

import io
from pathlib import Path
from typing import Any

from ruamel.yaml import YAML
from ruamel.yaml.comments import CommentedMap, CommentedSeq


def _yaml() -> YAML:
    y = YAML()  # round-trip
    y.preserve_quotes = True
    y.width = 120
    y.indent(mapping=2, sequence=4, offset=2)
    return y


def _no_aliases(text: str) -> None:
    """Spec files may not use YAML anchors/aliases: expansion is unbounded (billion laughs) and hides values from review."""
    from ruamel.yaml.events import AliasEvent

    for ev in YAML(typ="safe", pure=True).parse(text):
        if isinstance(ev, AliasEvent):
            raise ValueError("YAML aliases (*name) are not allowed in spec files")


def load(path: Path) -> Any:
    text = Path(path).read_text(encoding="utf-8")
    _no_aliases(text)
    return _yaml().load(text)


def loads(text: str) -> Any:
    _no_aliases(text)
    return _yaml().load(text)


def dump(data: Any, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    buf = io.StringIO()
    _yaml().dump(data, buf)
    path.write_text(buf.getvalue(), encoding="utf-8", newline="\n")


def dumps(data: Any) -> str:
    buf = io.StringIO()
    _yaml().dump(data, buf)
    return buf.getvalue()


def to_plain(node: Any) -> Any:
    """Commented structures -> plain dict/list (for pydantic)."""
    if isinstance(node, (CommentedMap, dict)):
        return {str(k): to_plain(v) for k, v in node.items()}
    if isinstance(node, (CommentedSeq, list)):
        return [to_plain(v) for v in node]
    return node
