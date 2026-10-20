"""Python side of the Workflow Developer custom component (ui/static/wfd): the stage/step tree and the log console.

The component is plain HTML/JS shipped with the package (no build step); it is declared lazily so importing this
module needs no Streamlit runtime. Contracts: WORKFLOW_DEVELOPER_PLAN.md §2 (snapshot) and §3 (this API)."""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Any

STATIC = Path(__file__).parent / "static" / "wfd"


@lru_cache(maxsize=1)
def _component():
    import streamlit.components.v1 as components

    return components.declare_component("pkf_wfd", path=str(STATIC))


def tree(snapshot: dict, *, key: str, height: int | None = None, fill: bool = False) -> str | None:
    """Render the run as collapsible stages; returns the step id the user clicked, else None (the component keeps its
    expand/collapse state across reruns; the Python side decides what a click means). fill=True (0.7.0): the page sizes
    the iframe with CSS and the tree scrolls inside it."""
    extra = {"fill": True} if fill else {}
    value: Any = _component()(view="tree", snapshot=snapshot, height=height, key=key, default=None, **extra)
    return value if isinstance(value, str) and value else None


def console(lines: list[dict], *, key: str, title: str = "", height: int = 360, follow: bool = True, fill: bool = False,
            query: str = "", numbers: bool = False) -> None:
    """A terminal-like panel of log lines ({ts, level, source, text}); renders incrementally and auto-scrolls when
    `follow` is on and the user is at the bottom. fill=True (0.7.0): the page sizes the iframe with CSS. query (0.8.0):
    only the lines containing it are shown (case-insensitive); numbers: line numbers."""
    extra = {"fill": True} if fill else {}
    if query:
        extra["query"] = str(query)
    if numbers:
        extra["numbers"] = True
    _component()(view="console", lines=list(lines), title=title, height=int(height), follow=bool(follow), key=key, default=None, **extra)
