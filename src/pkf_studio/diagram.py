"""The pipeline's data flow as Graphviz DOT (rendered by st.graphviz_chart in the browser; no binary needed).

trigger → events → source → package ×N → transformation steps → destination, one node per component with its steps.
Every label is escaped, so names and titles from specs or the AI cannot break the graph."""
from __future__ import annotations

COLORS = {"trigger": "#E8F0FE", "source": "#E6F4EA", "transformation": "#FEF7E0", "destination": "#FCE8E6", "data": "#F1F3F4"}


def esc(s) -> str:
    return str(s).replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n").replace("<", "‹").replace(">", "›").replace("{", "(").replace("}", ")")


def _label(kind: str, name: str, steps: list[dict], mode: str | None) -> str:
    rows = [f"{kind.upper()}", name] + [f"• {s.get('title') or s.get('id')}  [{s.get('op')}]" for s in steps[:12]]
    if len(steps) > 12:
        rows.append(f"… {len(steps) - 12} more steps")
    if not steps:
        rows.append("(plan not written yet)" if mode == "requirement" else "(runs on its schedule)" if kind == "trigger" else "(no steps)")
    return "\\l".join(esc(r) for r in rows) + "\\l"


def dot(pipeline: str, comps: list[dict]) -> str:
    """comps: [{kind, name, steps: [{id, op, title}], mode, schedule?}] in flow order."""
    out = ["digraph pkf {", '  rankdir=LR; bgcolor="transparent";',
           '  node [shape=box, style="rounded,filled", fontname="Helvetica", fontsize=10];', '  edge [fontname="Helvetica", fontsize=9];',
           f'  label="{esc(pipeline)}"; labelloc=t; fontname="Helvetica";']
    by = {c["kind"]: c for c in comps}
    order = [k for k in ("trigger", "source", "transformation", "destination") if k in by]
    for k in order:
        c = by[k]
        out.append(f'  {k} [label="{_label(k, c["name"], c.get("steps") or [], c.get("mode"))}", fillcolor="{COLORS[k]}"];')
    edges = {("trigger", "source"): "events", ("source", "transformation"): "package ×N", ("source", "destination"): "package ×N",
             ("transformation", "destination"): "package"}
    for a, b in zip(order, order[1:]):
        out.append(f'  {a} -> {b} [label="{esc(edges.get((a, b), ""))}"];')
    if "trigger" in by and by["trigger"].get("schedule"):
        out.append(f'  schedule [shape=note, label="{esc("schedule: " + by["trigger"]["schedule"])}", fillcolor="{COLORS["data"]}"];')
        out.append("  schedule -> trigger [style=dashed, arrowhead=none];")
    out.append("}")
    return "\n".join(out)
