"""The long Studio actions, run by the job runner (python -m pkf_studio.runner). Every framework step is a `pkf --json`
subprocess in the pipeline's worktree; every result is logged and committed.

S1  analyze            document text → pipelines/<p>.yaml + pseudocode, then the split (pkf plan pipeline)
    resplit            the requirement was edited: split again
S2  start_g2           ledger row for G1; re-split only if the requirement changed during G1
S3  start_g3           ledger row for G2; pkf approve-plan pipeline (writes the component files to fill in)
    plan_components    pkf plan for every component that needs an AI plan (checkpoint commit after each)
S4  start_g4           ledger row for G3 + approve-plan per component (or, G3 skipped, continue from the G3 branch);
                       pkf code where plans have custom steps; pkf test pipeline; pkf build --draft (writes nothing:
                       dags/ only ever holds approved code); tests.json (version 2, the S4 status); capability summary
    code_component     regenerate one component's code (or one custom step);  test_build: tests + build again
S5  start_g5           ledger row for G4 + approve-code; build; prepare_env
    prepare_env        Terraform render + plan (guarded), policy checks, dry run, deploy notes
S6  preview_run        a new run branch + `pkf run --dry-run --plan-out` into the git-ignored cache (nothing committed)
    run_local          the Move (custom delivery: only with the preview's planned.json) or a one-click run: apply the
                       approved saved plan; G5 ledger row; started.json; pkf run; files.csv + report.json; record PR
    publish_run_record a record that could not be pushed, or a Move that never finished (published as failed)

A framework step that reports `ok: false` makes the job a success *with problems* (runner.Ctx.problem), so the user
sees what failed and the next action instead of a silent "succeeded". Every stage job (@stage_job) pushes its branch at
the end — success, failure or crash — so GitHub's copy never lags behind this computer.
"""
from __future__ import annotations

import ast
import functools
import json
import re
import subprocess
from pathlib import Path

import yaml

from . import STUDIO_DIR, infra
from . import ledger as L
from .capabilities import report as cap_report
from .runner import ActionError, Ctx, problem_text
from .scan import scan, secrets_only
from .service import Studio, StudioError, branch_for, dags_problem, unfilled

NEEDS_PLAN = ("needs_plan", "plan_stale", "plan_tampered")
CUSTOM_OPS = ("custom", "custom_delivery")
MAX_FAILING = 50


def _open(ctx: Ctx) -> Studio:
    try:
        s = Studio.open(ctx.root)
    except StudioError as exc:
        raise ActionError(str(exc)) from None
    if not s.configured:
        raise ActionError("the project is not set up on the base branch yet (Setup)")
    ctx.studio = s
    return s


# ------------------------------------------------------------------------------------------------------------ push (K2)
def _push_quiet(s: Studio, pipeline: str) -> str | None:
    """Push the worktree's branch; None when pushed, else what went wrong (never raises)."""
    return s.push_quiet(pipeline)


def _push_at_end(ctx: Ctx) -> None:
    s = getattr(ctx, "studio", None)
    if s is None:
        return
    branch = ""
    try:
        if not (s.worktree_path(ctx.pipeline) / ".git").exists():
            return
        branch = s.worktree_branch(ctx.pipeline) or ""
        if not re.fullmatch(rf"pkf/{re.escape(ctx.pipeline)}/g[1-5]", branch):
            return
        if not _has_stage_work(s, ctx.pipeline, branch):
            ctx.log(f"  not pushed: {branch} has no work of its stage yet")
            return
        msg = _push_quiet(s, ctx.pipeline)
    except Exception as exc:  # noqa: BLE001 - the push never hides the job's own result
        msg = f"could not push {branch or 'the branch'}: {type(exc).__name__}: {exc}"
    if msg:
        ctx.problem(msg)
    else:
        ctx.log(f"  pushed {branch}")


def _has_stage_work(s: Studio, pipeline: str, branch: str) -> bool:
    """False for a brand-new stage branch that is not on GitHub and has no commit beyond the base (a Start that failed
    before its first commit): it is not published (R-phase1 #5). Unknown → True (push as before)."""
    try:
        if s.git.sha(f"origin/{branch}"):
            return True
        head = s.git.head(s.worktree_path(pipeline))
        base = s.git.sha(s.base_ref)
        return not (head and base and (head == base or s.git.is_ancestor(head, base)))
    except Exception:  # noqa: BLE001
        return True


def stage_job(fn):
    """A stage job pushes its branch when it ends (success, ActionError or crash); a failed push is a problem (K2)."""
    @functools.wraps(fn)
    def wrapper(ctx: Ctx, *args, **kwargs):
        try:
            return fn(ctx, *args, **kwargs)
        finally:
            _push_at_end(ctx)
    return wrapper


def _commit(ctx: Ctx, s: Studio, msg: str) -> str | None:
    """Commit the job's work. On a stage branch Studio.commit leaves files outside the pipeline's own uncommitted
    (they would block the gate); the job names them (R-phase1 #2)."""
    try:
        sha = s.commit(ctx.pipeline, msg)
    except StudioError as exc:
        raise ActionError(str(exc)) from None
    ctx.log(f"  commit {sha[:7]}: {msg}" if sha else "  (nothing to commit)")
    left = s.left_uncommitted_message(ctx.pipeline)
    if left:
        ctx.problem(left)
    return sha


# ------------------------------------------------------------------------------------------------------ isolation (0.6.1)
def own_paths(wt: Path, pipeline: str) -> tuple[str, ...]:
    """Every path prefix that belongs to `pipeline` in this worktree (its specs, its components, reused ones included)."""
    read = _reader(wt)
    conn = (L._yaml(read, f"pipelines/{pipeline}.yaml") or {}).get("connection")
    return tuple(L.pipeline_paths(pipeline, L.components(read, pipeline), conn))


def split_issues(issues: list, wt: Path, pipeline: str) -> tuple[list[dict], list[dict]]:
    """(errors in this pipeline's files or in no file, errors in other pipelines' files)."""
    own = own_paths(wt, pipeline)
    mine, others = [], []
    for i in issues or []:
        if not isinstance(i, dict) or i.get("severity", "error") != "error":
            continue
        f = str(i.get("file") or "").replace("\\", "/")
        (mine if not f or f.startswith(own) else others).append(i)
    return mine, others


def validate_own(ctx: Ctx, wt: Path) -> dict:
    """`pkf validate` judged on this pipeline's own files. Before 0.6.1 any invalid spec on the base branch — e.g. another
    pipeline half-way through its gates in another session — failed this pipeline's S3/S4. Errors in other pipelines'
    files are logged and do not stop this one; this pipeline's own errors fail the job exactly as before."""
    doc = ctx.pkf("validate", cwd=wt, quiet=True)
    if doc.get("ok"):
        return doc
    mine, others = split_issues(doc.get("issues") or [], wt, ctx.pipeline)
    if others:
        ctx.log("  ignored (other pipelines' files; they do not stop this pipeline): "
                + "; ".join(f"{i.get('file')}: {i.get('msg')}" for i in others[:8])[:1500])
    if mine:
        msgs = "; ".join(str(i.get("msg", "")) for i in mine)[:800]
        raise ActionError(f"pkf validate failed: {msgs}")
    return {**doc, "ok": True, "ignored_other_pipelines": len(others)}


def _sync_shared_stacks(ctx: Ctx, s: Studio, wt: Path, buckets: list[str]) -> list[str]:
    """Before rendering the dev environment: when the base branch changed a shared bucket stack this pipeline uses
    (another pipeline's G5 on the same bucket merged since this branch started), merge the base branch into the stage
    branch, so the stack is rendered with every pipeline's current entries and the G5 PR neither conflicts with nor
    reverts them (0.6.1). A conflict inside infra/buckets/ takes the base's version (the stack is rendered again right
    after); any other conflict aborts the merge and fails the job with the files. Returns the files that changed."""
    if not buckets:
        return []
    base = s.base_ref
    mb = s.git.run("merge-base", "HEAD", base, cwd=wt, check=False).strip()
    if not mb:
        return []
    paths = [f"infra/buckets/{b}/" for b in buckets]
    changed = [f for f in s.git.run("diff", "--name-only", mb, base, "--", *paths, cwd=wt, check=False).splitlines() if f]
    if not changed:
        return []
    _commit(ctx, s, f"S5 {ctx.pipeline}: edits")
    env = {**s._git_env(), "GIT_MERGE_AUTOEDIT": "no"}
    msg = f"S5 {ctx.pipeline}: {s.base} merged (shared bucket stack changed: {', '.join(sorted({f.split('/')[2] for f in changed}))})"
    with s.git.lock():
        s.git.clear_stale_index_lock(wt)
        try:
            s.git.run("merge", "--no-ff", "-m", msg, base, cwd=wt, env=env)
        except Exception as exc:  # noqa: BLE001 - a conflict: resolve inside infra/buckets/, else give up cleanly
            unmerged = [f for f in s.git.run("diff", "--name-only", "--diff-filter=U", cwd=wt, check=False).splitlines() if f]
            resolved = False
            if unmerged and all(f.startswith("infra/buckets/") for f in unmerged):
                try:
                    for f in unmerged:   # the base's side; a file the base deleted is deleted here too
                        if subprocess.run(["git", "cat-file", "-e", f"{base}:{f}"], cwd=str(wt), capture_output=True).returncode == 0:
                            s.git.run("checkout", "--theirs", "--", f, cwd=wt)
                            s.git.run("add", "--", f, cwd=wt)
                        else:
                            s.git.run("rm", "-q", "-f", "--", f, cwd=wt)
                    s.git.run("commit", "--no-edit", "-m", msg, cwd=wt, env=env)
                    resolved = True
                except Exception:  # noqa: BLE001
                    resolved = False
            if not resolved:
                if (wt / ".git").exists():
                    s.git.run("merge", "--abort", cwd=wt, check=False)   # never leave the worktree mid-merge
                why = (f"conflicts in {', '.join(unmerged[:8])}" if unmerged else
                       f"git refused it ({str(exc).strip().splitlines()[-1][:200] if str(exc).strip() else 'unknown reason'})")
                raise ActionError(f"the base branch changed the shared bucket stack, and merging it into {s.worktree_branch(ctx.pipeline)} "
                                  f"failed: {why} — commit or move away local files, or Reopen G5, then Render & plan again") from None
    ctx.log(f"  {s.base} merged into the stage branch: the shared bucket stack changed there ({', '.join(changed[:6])})")
    return changed


def _status(ctx: Ctx, wt: Path) -> dict:
    doc = ctx.pkf("status", ctx.pipeline, cwd=wt, quiet=True)
    rows = (doc.get("data") or {}).get("pipelines") or [{}]
    return rows[0]


def _owner(s: Studio) -> str:
    me = s.me()
    return (me.github or me.email.split("@")[0]) if me else "data-engineering"


def _stage(ctx: Ctx, s: Studio, gate: str, start: str | None = None) -> Path:
    try:
        wt = s.open_stage(ctx.pipeline, gate, start=start)
    except StudioError as exc:
        raise ActionError(str(exc)) from None
    ctx.log(f"  worktree {wt.name} on {branch_for(ctx.pipeline, gate)}")
    return wt


def _ledger(ctx: Ctx, s: Studio, gate: str) -> dict:
    try:
        row = s.ledger_row(ctx.pipeline, gate)
    except StudioError as exc:
        raise ActionError(str(exc)) from None
    s.write_ledger(ctx.pipeline, row)
    ctx.log(f"  ledger: {gate} approved by " + ", ".join(f"@{a['login']} ({a['role']})" for a in row["approvers"]) +
            (" — self-asserted (dev)" if row["self_asserted"] else ""))
    return row


def _unmerged_warning(ctx: Ctx, s: Studio, gate: str) -> None:
    """C20: commits made on the gate's branch after its PR was merged (on GitHub) are not in the base: keep them as an
    archive ref and say how to include them."""
    p = ctx.pipeline
    try:
        n = int(s.unmerged_local_commits(p, gate) or 0)
        if not n:
            return
        branch = branch_for(p, gate)
        pr = s.prs(p, 0).get(gate) or {}
        ref = s.archive(p, gate)
        ctx.log(f"  kept {branch} as {ref}")
    except Exception as exc:  # noqa: BLE001 - a warning never fails the stage
        ctx.log(f"  could not check {gate}'s branch for unmerged commits: {exc}")
        return
    ctx.problem(f"{n} commit{'s' if n != 1 else ''} on g{gate[1:]} {'were' if n != 1 else 'was'} not in the merged PR "
                f"#{pr.get('number')}: Reopen {gate} to include them (they are kept as {ref})")


# ------------------------------------------------------------------------------------------------------------ S1
@stage_job
def analyze(ctx: Ctx) -> dict:
    s = _open(ctx)
    wt = _stage(ctx, s, "G1")
    rdir = wt / "requirements" / ctx.pipeline
    text_file = rdir / "extracted.txt"
    if not text_file.exists():
        raise ActionError("import or paste the requirement first")
    meta = yaml.safe_load((rdir / "meta.yaml").read_text(encoding="utf-8")) if (rdir / "meta.yaml").exists() else {}
    findings = scan(text_file.read_text(encoding="utf-8"), {p.email for p in s.team.people})
    if secrets_only(findings):
        raise ActionError("the requirement contains what looks like secrets: " +
                          "; ".join(f"line {f.line}: {f.what}" for f in secrets_only(findings)[:5]) + " — remove them first")
    if any(f.kind == "pii" for f in findings) and not (meta or {}).get("pii_confirmed"):
        raise ActionError("the requirement may contain personal data; remove it or confirm it is not personal data (Requirements tab)")
    ctx.pkf("analyze-requirements", text_file.relative_to(wt).as_posix(), "--name", ctx.pipeline, "--owner", _owner(s),
            "--source-file", (meta or {}).get("original_name") or "pasted text", cwd=wt, check=True)
    split = ctx.pkf("plan", "pipeline", ctx.pipeline, cwd=wt)
    _commit(ctx, s, f"S1 {ctx.pipeline}: requirement analysed and split")
    return {"split_ok": split.get("ok"), "issues": split.get("issues", [])}


@stage_job
def resplit(ctx: Ctx) -> dict:
    s = _open(ctx)
    wt = _stage(ctx, s, "G1")
    _commit(ctx, s, f"S1 {ctx.pipeline}: requirement edited")
    split = ctx.pkf("plan", "pipeline", ctx.pipeline, cwd=wt)
    _commit(ctx, s, f"S1 {ctx.pipeline}: split again")
    return {"split_ok": split.get("ok"), "issues": split.get("issues", [])}


# ------------------------------------------------------------------------------------------------------------ S2
@stage_job
def start_g2(ctx: Ctx) -> dict:
    s = _open(ctx)
    _stage(ctx, s, "G2")
    wt = s.worktree_path(ctx.pipeline)
    _ledger(ctx, s, "G1")
    _unmerged_warning(ctx, s, "G1")
    st = _status(ctx, wt)
    if (st.get("split") or {}).get("stage") in ("needs_split", "split_stale", "split_tampered"):
        ctx.log("  the requirement changed during G1: splitting again")
        ctx.pkf("plan", "pipeline", ctx.pipeline, cwd=wt, check=True)
    _commit(ctx, s, f"S2 {ctx.pipeline}: G1 recorded; design ready for review")
    return {"split": st.get("split")}


# ------------------------------------------------------------------------------------------------------------ S3
@stage_job
def start_g3(ctx: Ctx) -> dict:
    s = _open(ctx)
    wt = _stage(ctx, s, "G3")
    row = _ledger(ctx, s, "G2")
    _unmerged_warning(ctx, s, "G2")
    ctx.pkf("approve-plan", "pipeline", ctx.pipeline, *s.evidence_args(row), cwd=wt, check=True)
    _commit(ctx, s, f"S3 {ctx.pipeline}: G2 recorded; component files written")
    return {"unfilled": unfilled(wt, ctx.pipeline)}


@stage_job
def plan_components(ctx: Ctx) -> dict:
    s = _open(ctx)
    wt = _stage(ctx, s, "G3")
    _commit(ctx, s, f"S3 {ctx.pipeline}: environment values")
    left = unfilled(wt, ctx.pipeline)
    if left:
        raise ActionError("fill in every <REQUIRED> value first: " + "; ".join(left[:8]))
    validate_own(ctx, wt)
    planned = []
    for c in _status(ctx, wt).get("components", []):
        if c["stage"] in NEEDS_PLAN:
            doc = ctx.pkf("plan", c["kind"], c["name"], cwd=wt)
            planned.append({"kind": c["kind"], "name": c["name"], "ok": doc.get("ok")})
            _commit(ctx, s, f"S3 {ctx.pipeline}: plan {c['kind']} {c['name']}")
    comps = _status(ctx, wt).get("components", [])
    # quiet (K1 R3): at S3 custom steps are not coded yet, so `needs_code` examples are expected; only `fail` is a problem
    ex = ctx.pkf("test", "pipeline", ctx.pipeline, cwd=wt, quiet=True)
    examples = (ex.get("data") or {}).get("examples")
    for e in examples or []:
        if e.get("status") == "fail":
            ctx.problem(f"the pipeline example '{e.get('example')}' fails with these plans: {e.get('detail')} — fix the plans "
                        "(Re-plan), or reopen G1 to fix the example")
    if not ex.get("ok") and examples is None:
        ctx.problem(problem_text(("test", "pipeline", ctx.pipeline), ex))
    rv = wt / "review" / ctx.pipeline
    rv.mkdir(parents=True, exist_ok=True)
    (rv / "examples.json").write_text(json.dumps({"ok": ex.get("ok"), "examples": examples, "issues": ex.get("issues")},
                                                 indent=2, default=str) + "\n", encoding="utf-8", newline="\n")
    _commit(ctx, s, f"S3 {ctx.pipeline}: plans")
    needs_review = [c for c in comps if c.get("mode") == "requirement"]
    return {"planned": planned, "components": comps, "no_plans": not needs_review, "examples_ok": ex.get("ok")}


@stage_job
def plan_component(ctx: Ctx, kind: str, name: str) -> dict:
    s = _open(ctx)
    wt = _stage(ctx, s, "G3")
    doc = ctx.pkf("plan", kind, name, cwd=wt)
    _commit(ctx, s, f"S3 {ctx.pipeline}: re-plan {kind} {name}")
    return {"ok": doc.get("ok")}


# ------------------------------------------------------------------------------------------------------------ S4
@stage_job
def start_g4(ctx: Ctx) -> dict:
    s = _open(ctx)
    prs = s.prs(ctx.pipeline, 0)
    g3 = prs.get("G3")
    if g3 and g3.get("merged_at"):
        wt = _stage(ctx, s, "G4")
        row = _ledger(ctx, s, "G3")
        _unmerged_warning(ctx, s, "G3")
        for c in _status(ctx, wt).get("components", []):
            if c["stage"] == "plan_proposed":
                ctx.pkf("approve-plan", c["kind"], c["name"], *s.evidence_args(row), cwd=wt, check=True)
        _commit(ctx, s, f"S4 {ctx.pipeline}: G3 recorded; plans approved")
    else:
        if s.worktree_branch(ctx.pipeline) != branch_for(ctx.pipeline, "G3"):
            raise ActionError("G3 is not approved, and the worktree is not on the plans branch")
        wt = s.worktree_path(ctx.pipeline)
        comps = _status(ctx, wt).get("components", [])
        if any(c.get("mode") == "requirement" for c in comps):
            raise ActionError("a component has an AI plan: request G3 approval first")
        left = unfilled(wt, ctx.pipeline)
        if left:
            raise ActionError("fill in every <REQUIRED> value first: " + "; ".join(left[:8]))
        validate_own(ctx, wt)
        _commit(ctx, s, f"S3 {ctx.pipeline}: no AI plans needed")
        head = s.git.head(wt)
        wt = _stage(ctx, s, "G4", start=head)
        ctx.log("  G3 skipped: no component needs an AI plan")
    return build_code(ctx, s, wt)


def _lock(wt: Path, kind: str, name: str) -> dict:
    try:
        return yaml.safe_load((wt / L.FOLDERS[kind] / f"{name}.lock.yaml").read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError):
        return {}


def _custom_steps(lock: dict) -> list[str]:
    return [x["id"] for x in ((lock.get("plan") or {}).get("steps") or []) if isinstance(x, dict) and x.get("op") in CUSTOM_OPS]


def _coded_functions(path: Path) -> set[str] | None:
    try:
        return {n.name for n in ast.parse(path.read_text(encoding="utf-8")).body if isinstance(n, ast.FunctionDef)}
    except (OSError, SyntaxError, ValueError):
        return None


def _missing_function(lock: dict) -> bool:
    """The code file lacks a step function (the coder stopped part-way): S4 codes the component again (A8)."""
    return any("missing custom step function" in str((p or {}).get("msg", "")) for p in ((lock.get("code") or {}).get("problems") or []))


def _code(ctx: Ctx, wt: Path, kind: str, name: str, step: str | None = None, all_steps: bool = False) -> list[str]:
    """`pkf code` for one component (one step with `step`, every step with `all_steps`, else the missing or failing
    steps); returns its problems (also recorded on the job)."""
    args = ["code", kind, name, *(["--step", step] if step else []), *(["--all"] if all_steps else [])]
    doc = ctx.pkf(*args, cwd=wt, quiet=True)
    if doc.get("ok"):
        return []
    msg = problem_text(args, doc)
    ctx.problem(msg)
    return [msg]


def _draft_build(ctx: Ctx, s: Studio, wt: Path, quiet: bool = False) -> dict:
    """`pkf build <p> --draft`: every check of the build (specs, examples end to end with the current code, DAG
    compiles), accepting code that is generated but not approved yet, and writing nothing (K3)."""
    p = ctx.pipeline
    doc = ctx.pkf("build", p, "--draft", cwd=wt, quiet=True)
    if not doc.get("ok") and not quiet:
        ctx.problem(problem_text(("build", p, "--draft"), doc))
    return doc




def _failing(lock: dict) -> list[dict]:
    code = lock.get("code") or {}
    out = [{"step": (p.get("loc") if p.get("loc") not in (None, "file") else None), "case": None, "example": None,
            "detail": str(p.get("msg", ""))} for p in code.get("problems") or [] if isinstance(p, dict)]
    out += [{"step": t.get("step"), "case": t.get("case"), "example": t.get("example"), "detail": t.get("detail")}
            for t in code.get("tests") or [] if isinstance(t, dict) and t.get("status") not in ("pass", "shown")]
    return out[:MAX_FAILING]


def _component_row(wt: Path, c: dict, ai_issues: list[str]) -> dict:
    kind, name = c["kind"], c["name"]
    lock = _lock(wt, kind, name) if c.get("mode") == "requirement" else {}
    rel = f"{L.FOLDERS[kind]}/{name}.py"
    return {"kind": kind, "name": name, "stage": c.get("stage"), "mode": c.get("mode"), "next": c.get("next"),
            "custom_steps": _custom_steps(lock), "code_file": rel if (wt / rel).exists() else None,
            "failing": _failing(lock) if lock.get("code") else [], "ai_issues": list(ai_issues)}


def build_code(ctx: Ctx, s: Studio, wt: Path) -> dict:
    ai: dict[tuple[str, str], list[str]] = {}
    for c in _status(ctx, wt).get("components", []):
        if c["stage"] == "plan_approved" or (c["stage"] == "code_failing" and _missing_function(_lock(wt, c["kind"], c["name"]))):
            ai[(c["kind"], c["name"])] = _code(ctx, wt, c["kind"], c["name"])
            _commit(ctx, s, f"S4 {ctx.pipeline}: code {c['kind']} {c['name']}")
    return test_build_in(ctx, s, wt, ai)


def test_build_in(ctx: Ctx, s: Studio, wt: Path, ai: dict | None = None) -> dict:
    """Tests, draft build and the S4 status: review/<p>/tests.json version 2 (K5) and the capability summary."""
    p = ctx.pipeline
    comps = _status(ctx, wt).get("components", [])
    # a component whose coder already failed in this job is reported once (its `pkf code` problem); the follow-up
    # tests and the draft build fail for the same reason, so they add no second problem (R-phase1 #9)
    reported = any((ai or {}).values())
    for c in comps:
        if c["stage"] in ("code_changed", "code_failing"):
            ctx.pkf("test", c["kind"], c["name"], cwd=wt, quiet=bool((ai or {}).get((c["kind"], c["name"]))))
    ex = ctx.pkf("test", "pipeline", p, cwd=wt, quiet=reported)
    build = _draft_build(ctx, s, wt, quiet=reported)
    comps = _status(ctx, wt).get("components", [])
    rows = [_component_row(wt, c, (ai or {}).get((c["kind"], c["name"]), [])) for c in comps]
    missing = []
    for r in rows:
        if r["custom_steps"]:
            have = _coded_functions(wt / r["code_file"]) if r["code_file"] else set()
            todo = [i for i in r["custom_steps"] if have is not None and i not in have]
            if todo:
                missing.append(f"{r['kind']} {r['name']}: {', '.join(todo)}")
    differ = bool(s.dags_diff(p))   # e.g. a 0.4.0 build committed on g4 (R10): Restore dags/ from main
    if differ:
        ctx.problem(dags_problem(s.base))
    rv = wt / "review" / p
    rv.mkdir(parents=True, exist_ok=True)
    code_files = [r["code_file"] for r in rows if r["code_file"]]
    (rv / "capabilities.md").write_text(cap_report(wt, code_files, missing), encoding="utf-8", newline="\n")
    tests = {"version": 2, "build_mode": "draft", "build_ok": bool(build.get("ok")), "build_issues": build.get("issues") or [],
             "examples_ok": bool(ex.get("ok")), "examples": (ex.get("data") or {}).get("examples"), "components": rows,
             "dags_differ": differ, "problems": list(ctx.problems)}
    (rv / "tests.json").write_text(json.dumps(tests, indent=2, default=str) + "\n", encoding="utf-8", newline="\n")
    _commit(ctx, s, f"S4 {p}: tests, draft build and capability summary")
    return {"examples_ok": ex.get("ok"), "build_ok": build.get("ok"), "components": _status(ctx, wt).get("components", []),
            "dags_differ": differ}


@stage_job
def code_component(ctx: Ctx, kind: str, name: str, step: str | None = None, all_steps: bool = False) -> dict:
    """Regenerate (AI): one component's missing or failing steps, one step (`step`), or every step (`all_steps`)."""
    s = _open(ctx)
    wt = _stage(ctx, s, "G4")
    issues = _code(ctx, wt, kind, name, step=step, all_steps=all_steps)
    _commit(ctx, s, f"S4 {ctx.pipeline}: code {kind} {name}" + (f" step {step}" if step else "") + " again")
    return test_build_in(ctx, s, wt, {(kind, name): issues})


@stage_job
def test_build(ctx: Ctx) -> dict:
    """Tests and build again — and code for any component whose code is still missing (a failed first attempt)."""
    s = _open(ctx)
    wt = _stage(ctx, s, "G4")
    _commit(ctx, s, f"S4 {ctx.pipeline}: edits")
    return build_code(ctx, s, wt)


# ------------------------------------------------------------------------------------------------------------ S5
@stage_job
def start_g5(ctx: Ctx) -> dict:
    s = _open(ctx)
    wt = _stage(ctx, s, "G5")
    row = _ledger(ctx, s, "G4")
    _unmerged_warning(ctx, s, "G4")
    for c in _status(ctx, wt).get("components", []):
        if c["stage"] == "code_generated":
            ctx.pkf("approve-code", c["kind"], c["name"], *s.evidence_args(row), cwd=wt, check=True)
        elif c["stage"] == "approved" and c.get("mode") == "requirement" and _manual_code_approval(_lock(wt, c["kind"], c["name"])):
            # §6.8: a code approval recorded by hand (`pkf approve-code --by …`) carries no GitHub evidence: re-record it
            # with the G4 approval's evidence (the lock is in no gate's input hash)
            ctx.log(f"  {c['kind']} {c['name']}: the code approval was recorded by hand; recording it again with the G4 evidence")
            ctx.pkf("approve-code", c["kind"], c["name"], *s.evidence_args(row), cwd=wt, check=True)
    ctx.pkf("build", ctx.pipeline, cwd=wt, check=True)
    _commit(ctx, s, f"S5 {ctx.pipeline}: G4 recorded; code approved; DAG built")
    return prepare_env_in(ctx, s, wt)


@stage_job
def prepare_env(ctx: Ctx) -> dict:
    s = _open(ctx)
    wt = _stage(ctx, s, "G5")
    return prepare_env_in(ctx, s, wt)


def _manual_code_approval(lock: dict) -> bool:
    ca = lock.get("code_approval")
    return bool(lock.get("code") and isinstance(ca, dict) and not ca.get("evidence"))


def _local_state(s: Studio) -> bool:
    return not s.cfg.terraform_backend.get("bucket") and not s.strict and len(s.team.people) <= 1


def prepare_env_in(ctx: Ctx, s: Studio, wt: Path) -> dict:
    p = ctx.pipeline
    try:
        f = infra.facts(wt, p, "dev", s.cfg.aws_region, s.cfg.aws_account_id)
    except (infra.InfraError, Exception) as exc:  # noqa: BLE001 - facts come from the specs; report what is wrong
        raise ActionError(f"could not read the infrastructure facts: {exc}") from None
    read_wt = _reader(wt)
    no_aws = None
    try:
        ident = preflight(ctx, s, read_wt)   # D15: before the dry run; the caller ARN is logged
    except ActionError as exc:
        ident, no_aws = None, str(exc)
    if ident is not None and _custom_delivery(read_wt, p):
        f["source_versioning"] = _source_versioning(f, s.cfg.aws_region)
        if f["source_versioning"] is None:
            f["warnings"].append(f"could not read the versioning of {f['source'].get('bucket')}: deletes and overwrites may be irreversible")
    _sync_shared_stacks(ctx, s, wt, sorted({n["bucket"] for n in f.get("notifications") or []}))   # 0.6.1
    others = [n for n in infra.repo_notifications(wt) if n["pipeline"] != p]
    manifest = infra.render(wt, p, f, _owner(s), f"{s.owner}/{s.repo}", s.cfg.tags, s.cfg.terraform_backend, others + f["notifications"])
    for w in manifest["warnings"]:
        ctx.log(f"  warning: {w}")
    policy = json.loads((wt / "infra" / p / "iam-policy.json").read_text(encoding="utf-8"))
    check = infra.policy_check(policy, [tuple(c) for c in manifest["runtime_calls"]], s.cfg.aws_region)
    (wt / "infra" / p / "policy-check.json").write_text(json.dumps(check, indent=2, default=str) + "\n", encoding="utf-8", newline="\n")
    ctx.log(f"  policy: validate {check['validate'] if isinstance(check['validate'], str) else str(len(check['validate'])) + ' finding(s)'}; "
            f"simulate {check['simulate'] if isinstance(check['simulate'], str) else check['simulate']}")
    plans, problems = [], []
    if manifest["needs_apply"]:
        if not s.cfg.aws_account_id:
            raise ActionError("pin the AWS account id in the project settings (Setup) before planning infrastructure")
        try:
            plans.append(infra.plan_stack(ctx.root, wt / "infra" / p / "envs" / "dev", f"{p}/dev", ctx.log, _local_state(s)))
        except infra.InfraError as exc:
            problems.append(str(exc))
        for b in manifest["buckets"]:
            stack = wt / "infra" / "buckets" / b
            try:
                import boto3

                foreign = infra.foreign_notifications(boto3.client("s3", region_name=s.cfg.aws_region), b,
                                                      {n["queue_arn"] for n in infra.repo_notifications(wt)})
            except Exception as exc:  # noqa: BLE001
                problems.append(f"could not read the notification configuration of {b}: {exc}")
                continue
            if foreign:
                (stack / "handoff.json").write_text(json.dumps({"bucket": b, "reason": "the bucket has notifications this repository does not own",
                                                                "foreign": foreign, "add": [n for n in infra.repo_notifications(wt) if n["bucket"] == b]},
                                                               indent=2) + "\n", encoding="utf-8", newline="\n")
                ctx.log(f"  {b} is shared ({len(foreign)} foreign entries): wrote handoff.json for its owner; not planned")
                continue
            if not s.cfg.terraform_backend.get("bucket"):
                problems.append(f"bucket stack {b} needs the S3 state backend (project settings: terraform_backend.bucket)")
                continue
            try:
                plans.append(infra.plan_stack(ctx.root, stack, f"buckets/{b}", ctx.log, False))
            except infra.InfraError as exc:
                problems.append(str(exc))
    if no_aws:   # no usable credentials: the dry run cannot work; G5 readiness asks for an ok one (D9)
        dry = {"ok": False, "issues": [{"msg": no_aws, "code": "no_credentials", "severity": "error"}], "data": {}}
    else:
        dry = ctx.pkf("run", p, "--dry-run", cwd=wt, quiet=True)
    if not dry.get("ok"):
        msgs = "; ".join(str(i.get("msg", "")) for i in (dry.get("issues") or [])[:3] if isinstance(i, dict)) or "failed"
        ctx.problem(f"the dry run failed: {msgs}"[:600])
    rv = wt / "review" / p
    rv.mkdir(parents=True, exist_ok=True)
    (rv / "dry-run.json").write_text(json.dumps({"ok": dry.get("ok"), "issues": dry.get("issues"),
                                                 "dry_run": (dry.get("data") or {}).get("dry_run"),
                                                 "caller_arn": (ident or {}).get("Arn"),
                                                 "source_versioning": f.get("source_versioning")}, indent=2, default=str) + "\n",
                                     encoding="utf-8", newline="\n")
    (rv / "deploy.md").write_text(deploy_notes(p, manifest), encoding="utf-8", newline="\n")
    _commit(ctx, s, f"S5 {p}: dev environment rendered and planned; dry run")
    if problems:
        raise ActionError("the dev environment is not ready: " + " | ".join(problems))
    return {"needs_apply": manifest["needs_apply"], "plans": plans, "dry_run_ok": dry.get("ok"), "warnings": manifest["warnings"],
            "source_versioning": f.get("source_versioning")}


def _reader(wt: Path):
    def read(rel: str) -> str | None:
        try:
            return (Path(wt) / rel).read_text(encoding="utf-8")
        except OSError:
            return None
    return read


def _source_versioning(f: dict, region: str) -> str | None:
    """The source bucket's versioning ("Enabled", "Suspended" or "Off"); None when it cannot be read (D1)."""
    bucket = (f.get("source") or {}).get("bucket")
    if (f.get("source") or {}).get("type") != "s3" or not bucket:
        return None
    try:
        import boto3

        return boto3.client("s3", region_name=region).get_bucket_versioning(Bucket=bucket).get("Status") or "Off"
    except Exception:  # noqa: BLE001 - recorded as unknown, with a warning
        return None


def deploy_notes(p: str, manifest: dict) -> str:
    return "\n".join([
        f"# Deploying `{p}`", "",
        "1. Apply the approved dev plan (pkf Studio S6 does this).",
        f"2. Copy `dags/pkf_{p}.py` and `dags/pkf/{p}/` into the Airflow DAGs folder (the bundle's 04_code/dags).",
        "3. Install the runtime on the workers: `pip install 'package-factory[runtime]'` (same version as the build).",
        "4. Give the Airflow worker role the policy in `infra/<p>/iam-policy.json` (05_infra/iam-policy.json).",
        "5. Set PKF_STAGING_URL=s3://<bucket>/<prefix> on the workers (with a lifecycle rule) unless Airflow runs on one "
        "machine: every task of a run must reach the same staging.",
        "6. Unpause the DAG; the first run should match the dry run in 07_runs/dry-run.json.", "",
        f"Resources: {'see the Terraform plan' if manifest['needs_apply'] else manifest.get('note', 'none')}.", ""])


# ------------------------------------------------------------------------------------------------------------ S6
def _custom_delivery(read, p: str) -> bool:
    """The pipeline's destination ends with a `custom_delivery` step (it moves or deletes files itself)."""
    for kind, name in L.components(read, p):
        if kind != "destination":
            continue
        for rel in (f"{L.FOLDERS[kind]}/{name}.lock.yaml", f"{L.FOLDERS[kind]}/{name}.yaml"):
            try:
                doc = yaml.safe_load(read(rel) or "") or {}
            except yaml.YAMLError:
                continue
            steps = ((doc.get("plan") or {}).get("steps") if rel.endswith(".lock.yaml") else doc.get("steps")) or []
            if any(isinstance(x, dict) and x.get("op") == "custom_delivery" for x in steps):
                return True
    return False


def _spec(read, kind: str, name: str) -> dict:
    try:
        return yaml.safe_load(read(f"{L.FOLDERS[kind]}/{name}.yaml") or "") or {}
    except yaml.YAMLError:
        return {}


def uses_aws(read, p: str) -> bool:
    """An `s3` source or destination, or an SQS trigger: the pipeline needs AWS credentials (D15). Local-folder
    pipelines never call STS."""
    for kind, name in L.components(read, p):
        spec = _spec(read, kind, name)
        if kind in ("source", "destination") and spec.get("type") == "s3":
            return True
        if kind == "trigger":
            lock = {}
            try:
                lock = yaml.safe_load(read(f"{L.FOLDERS[kind]}/{name}.lock.yaml") or "") or {}
            except yaml.YAMLError:
                pass
            steps = ((lock.get("plan") or {}).get("steps") or []) + (spec.get("steps") or [])
            if "queue_url" in (spec.get("params") or {}) or any(isinstance(x, dict) and x.get("op") == "sqs" for x in steps):
                return True
    return False


PIN_ACCOUNT = "pin the AWS account id in Setup (recommended in dev, required for Terraform)"


def preflight(ctx: Ctx, s: Studio, read=None) -> dict | None:
    """Who the AWS credentials are, before anything touches AWS (S5 dry run, S6 preview and run; D15, E13). None when
    the pipeline does not use AWS; ActionError with the fix when the credentials are missing, expired or for another
    account. Logs the caller ARN."""
    if not uses_aws(read or s.read_base, ctx.pipeline):
        return None
    if not s.cfg.aws_account_id:
        ctx.problem(PIN_ACCOUNT)
    ok, msg, ident = infra.aws_identity(s.cfg.aws_region, s.cfg.aws_account_id)
    if not ok:
        raise ActionError(msg)
    ctx.log(f"  AWS credentials: {msg}")
    return ident


def _gates_done(s: Studio, p: str) -> None:
    from .stages import derive

    st = derive(s, p, s.prs(p, 0))
    bad = [g for g in st["gates"] if g.gate != "RUN" and not g.done]
    if bad:
        raise ActionError(f"{bad[0].gate} {bad[0].title} is {bad[0].state}: every gate must be approved (and current) before a run")


def _open_run_branch(s: Studio, p: str, branch: str | None = None) -> tuple[Path, str]:
    return s.open_run_branch(p, branch=branch)


def _cache(ctx: Ctx, run: str) -> Path:
    """Where a preview's planned.json / preview.json live: the main clone's git-ignored cache, never in git (S4)."""
    return ctx.root / STUDIO_DIR / "cache" / "runs" / ctx.pipeline / run


def _run_name(branch: str) -> str:
    return branch.rsplit("/", 1)[-1]


def run_branches(s: Studio, p: str) -> list[dict]:
    """Local run branches of the pipeline: {"branch", "sha", "run", "on_origin", "started", "report"}."""
    out = []
    refs = s.git.run("for-each-ref", "--format=%(refname:short) %(objectname)", f"refs/heads/pkf/{p}/", check=False)
    for line in refs.splitlines():
        b, _, sha = line.strip().partition(" ")
        if not re.fullmatch(rf"pkf/{re.escape(p)}/run-[0-9a-z]+", b):
            continue
        run = _run_name(b)
        rd = f"review/{p}/runs/{run}"
        out.append({"branch": b, "sha": sha, "run": run, "on_origin": bool(s.git.sha(f"origin/{b}")),
                    "started": s.read_at(sha, f"{rd}/started.json") is not None,
                    "report": s.read_at(sha, f"{rd}/report.json") is not None})
    return out


def _archive(ctx: Ctx, s: Studio, p: str, b: dict) -> str:
    """A local archive ref for a crashed move's tip (K8): never pushed, never deleted by the Studio."""
    return archive_ref(ctx, s, p, b["run"], b["sha"], b["branch"])


def archive_ref(ctx: Ctx, s: Studio, p: str, name: str, sha: str, what: str) -> str:
    """refs/pkf-archive/<p>/<name>-<UTC time> → `sha` (K8), unless an archive ref already points there."""
    have = s.git.run("for-each-ref", "--format=%(refname)", "--points-at", sha, f"refs/pkf-archive/{p}/", check=False).split()
    if have:
        return have[0]
    ref = s.git.archive(p, name, sha)
    ctx.log(f"  kept {what} as {ref}")
    return ref


def _crashed_report(rd: Path) -> dict:
    return {"ok": False, "issues": [{"msg": "the run did not finish (crashed); run.log shows how far it got", "code": "run_crashed"}],
            "files_csv": None, "log": "run.log" if (rd / "run.log").exists() else None}


def _finish_crashed(ctx: Ctx, s: Studio, p: str, wt: Path, branch: str) -> None:
    """The worktree is on a crashed move's branch: commit its run.log and the "did not finish" report.json (R7)."""
    rd = wt / "review" / p / "runs" / _run_name(branch)
    rd.mkdir(parents=True, exist_ok=True)
    _write_json(rd / "report.json", _crashed_report(rd))
    _commit(ctx, s, f"S6 {p}: the move did not finish (crashed); recorded as failed")


def _cleanup_previews(ctx: Ctx, s: Studio, p: str, keep: str) -> list[str]:
    """After the worktree moved to the new run branch: delete the local run branches that are not on GitHub and were
    never moved (no started.json: previews only), with their cache folders. A branch with started.json is a move and
    is never deleted (R7); one without report.json is a crashed move: archived and listed for Publish run record."""
    deleted = []
    for b in run_branches(s, p):
        if b["branch"] == keep or b["on_origin"]:
            continue
        if b["started"]:
            if not b["report"]:
                ref = _archive(ctx, s, p, b)
                ctx.problem(f"an earlier Move did not finish ({b['branch']}); it is kept (as {ref}): Workflow → Publish run "
                            "record records it as failed")
            continue
        if b["report"]:
            continue   # a finished run whose record was not published (D11): Publish run record
        with s.git.lock():
            s.git.run("branch", "-D", b["branch"], check=False)
        import shutil

        shutil.rmtree(_cache(ctx, b["run"]), ignore_errors=True)
        deleted.append(b["branch"])
    if deleted:
        ctx.log(f"  removed {len(deleted)} earlier preview(s) that were never moved: {', '.join(deleted)}")
    return deleted


def _write_json(path: Path, doc) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(doc, indent=2, default=str) + "\n", encoding="utf-8", newline="\n")


def _sha256(path: Path) -> str:
    import hashlib

    return hashlib.sha256(path.read_bytes()).hexdigest()


def _deletes_sources(wt: Path, p: str) -> bool:
    """The destination's approved code deletes objects (a move): its capability summary lists a delete call."""
    from .capabilities import summarize

    for kind, name in L.components(lambda q: (wt / q).read_text(encoding="utf-8") if (wt / q).exists() else None, p):
        f = wt / L.FOLDERS[kind] / f"{name}.py"
        if kind == "destination" and f.exists():
            return any(d.startswith("delete_object") for d in summarize(f.read_text(encoding="utf-8")).get("deletes", []))
    return False


def _run_args(params: dict | None, date: str | None) -> list[str]:
    return [*sum((["--param", f"{k}={v}"] for k, v in (params or {}).items()), []), *(["--date", date] if date else [])]


def _new_second(s: Studio, p: str) -> None:
    """Run branches are named by the second (open_run_branch): never reuse an existing one (e.g. a crashed move)."""
    import datetime as dt
    import time

    names = {b["branch"] for b in run_branches(s, p)}
    for _ in range(20):
        if f"pkf/{p}/run-{dt.datetime.now(dt.timezone.utc):%Y%m%dt%H%M%S}" not in names:
            return
        time.sleep(0.1)


PREVIEW_MAX_AGE_H = 24
NEEDS_PREVIEW = "this pipeline moves or deletes files: Preview this run first, then Move"


def preview_run(ctx: Ctx, params: dict | None = None, date: str | None = None, max_packages: int | None = None) -> dict:
    """S6 "Preview this run" (D1, K6): a new run branch, then `pkf run --dry-run --plan-out` into the git-ignored cache.
    Commits nothing and never pushes. The Move (`run_local` with `planned`) delivers exactly these files."""
    import datetime as dt

    s = _open(ctx)
    p = ctx.pipeline
    _gates_done(s, p)
    ident = preflight(ctx, s)                       # before any branch is made (E13)
    cur = s.worktree_branch(p) or ""
    if re.fullmatch(rf"pkf/{re.escape(p)}/run-[0-9a-z]+", cur):
        b = next((x for x in run_branches(s, p) if x["branch"] == cur), None)
        if b and b["started"] and not b["report"] and not b["on_origin"]:
            # the worktree still holds a crashed move (its run.log is not committed): keep it, record it as failed
            ref = _archive(ctx, s, p, b)
            _finish_crashed(ctx, s, p, s.worktree_path(p), cur)
            ctx.problem(f"an earlier Move did not finish ({cur}); it is kept (as {ref}) and recorded as failed: Workflow → "
                        "Publish run record")
    _new_second(s, p)
    try:
        wt, branch = s.open_run_branch(p)
    except StudioError as exc:
        raise ActionError(str(exc)) from None
    ctx.log(f"  worktree {wt.name} on {branch}")
    run = _run_name(branch)
    _cleanup_previews(ctx, s, p, keep=branch)
    cache = _cache(ctx, run)
    cache.mkdir(parents=True, exist_ok=True)
    planned = cache / "planned.json"
    n = int(max_packages) if max_packages else None
    args = ["run", p, "--dry-run", "--plan-out", str(planned), *(["--max-packages", str(n)] if n else []), *_run_args(params, date)]
    doc = ctx.pkf(*args, cwd=wt, timeout=3600, quiet=True)
    if not doc.get("ok") or not planned.exists():
        raise ActionError("the preview failed: " + problem_text(args[:3], doc).split(": ", 1)[-1])
    plan = json.loads(planned.read_text(encoding="utf-8"))
    files = plan.get("files") or []
    by_prefix: dict[str, int] = {}
    for f in files:
        k = str(f.get("target_key") or "")
        top = k.split("/", 1)[0] + "/" if "/" in k else k
        by_prefix[top] = by_prefix.get(top, 0) + 1
    custom = plan.get("delivery") == "custom_delivery" or _custom_delivery(s.read_base, p)
    packages = {(f.get("package"), f.get("index")) for f in files}
    deletes = len(files) if custom and _deletes_sources(wt, p) else 0
    src = next((_spec(s.read_base, k, nm) for k, nm in L.components(s.read_base, p) if k == "source"), {})
    out = {"branch": branch, "run": run, "planned": str(planned), "files": len(files), "deletes": deletes,
           "by_prefix": dict(sorted(by_prefix.items())), "error_files": by_prefix.get("error/", 0),
           "max_packages": plan.get("max_packages", n), "packages": len(packages),
           "one_file_each": bool(files) and len(packages) == len(files),
           "source_bucket": (src.get("connection") or {}).get("bucket"), "delivery": plan.get("delivery"),
           "run_id": plan.get("run_id"), "planned_sha256": _sha256(planned)}
    _write_json(cache / "preview.json", {**out, "created_at": dt.datetime.now(dt.timezone.utc).isoformat(),
                                         "caller_arn": (ident or {}).get("Arn"), "rows": files[:500],
                                         "dry_run": (doc.get("data") or {}).get("dry_run")})
    ctx.log(f"  preview: {len(files)} file(s) in {out['packages']} package(s); {deletes} source object(s) would be deleted")
    return out


def _load_preview(ctx: Ctx, planned: str) -> tuple[Path, dict, dict]:
    import datetime as dt

    path = Path(planned)
    base = (ctx.root / STUDIO_DIR / "cache" / "runs" / ctx.pipeline).resolve()
    try:
        path.resolve().relative_to(base)
    except ValueError:
        raise ActionError("the planned files must come from Preview this run") from None
    try:
        plan = json.loads(path.read_text(encoding="utf-8"))
        preview = json.loads((path.parent / "preview.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise ActionError("the preview is gone from this computer: Preview this run again") from None
    if plan.get("pipeline") not in (None, ctx.pipeline):
        raise ActionError(f"the preview is for pipeline {plan.get('pipeline')}, not {ctx.pipeline}")
    try:
        created = dt.datetime.fromisoformat(str(preview.get("created_at") or plan.get("created_at")).replace("Z", "+00:00"))
        age_h = (dt.datetime.now(dt.timezone.utc) - created).total_seconds() / 3600
    except ValueError:
        age_h = PREVIEW_MAX_AGE_H + 1
    if age_h > PREVIEW_MAX_AGE_H:
        raise ActionError(f"the preview is older than {PREVIEW_MAX_AGE_H} hours: preview again")
    if preview.get("planned_sha256") and preview["planned_sha256"] != _sha256(path):
        raise ActionError("the planned files changed after the preview: preview again")
    return path, plan, preview


def _run_env(ctx: Ctx, s: Studio, wt: Path) -> dict:
    p = ctx.pipeline
    policy = json.loads((wt / "infra" / p / "iam-policy.json").read_text(encoding="utf-8"))
    if s.cfg.run_role_arn:
        try:
            env = infra.assume_run_role(s.cfg.run_role_arn, policy, s.cfg.aws_region, p)
        except infra.InfraError as exc:
            raise ActionError(str(exc)) from None
        ctx.log(f"  running as {s.cfg.run_role_arn}, capped by the generated worker policy")
        return {**env, "AWS_PROFILE": None, "AWS_DEFAULT_PROFILE": None}  # the run role's credentials only
    ctx.log("  running with your own AWS credentials (single-person dev team, confirmed)")
    return {}


def _need_confirmation(s: Studio, own_credentials: bool) -> None:
    if s.cfg.run_role_arn:
        return
    if s.strict or len(s.team.people) > 1:
        raise ActionError("a run role is required (project settings: run_role_arn) in this mode or with more than one person")
    if not own_credentials:
        raise ActionError("no run role: confirm running with your own AWS credentials")


FILES_CSV_HEADER = ["package", "index", "path", "source_key", "target_key", "status", "detail", "note", "size"]


def write_files_csv(path: Path, rows: list[dict]) -> dict[str, int]:
    """review/<p>/runs/<run>/files.csv (K7): one row per planned file; returns the count per status."""
    import csv

    counts: dict[str, int] = {}
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as fh:   # (no translation: csv writes LF itself)
        w = csv.DictWriter(fh, fieldnames=FILES_CSV_HEADER, extrasaction="ignore", lineterminator="\n")
        w.writeheader()
        for r in rows:
            if not isinstance(r, dict):
                continue
            w.writerow({k: ("" if r.get(k) is None else r.get(k)) for k in FILES_CSV_HEADER})
            st = str(r.get("status") or "")
            counts[st] = counts.get(st, 0) + 1
    return counts


def _publish(ctx: Ctx, s: Studio, p: str, branch: str, ok: bool | None) -> dict:
    """Push the record branch, open its PR and (dev) merge it. A failure is a problem, never an exception (D11)."""
    try:
        s.push(p)
        body = (f"## Run record · pipeline `{p}`\n\nThe G5 ledger row and the local run report "
                f"({'succeeded' if ok else 'failed'}). No reviewer: merged when the gate check passes.\n")
        s.invalidate()
        pr = s.prs(p, 0).get("RUN")
        if not (pr and pr.get("state") == "open" and (pr.get("head") or {}).get("ref") == branch):
            pr = s.gh.create_pull(f"[pkf] {p}: run record", branch, s.base, body)
    except Exception as exc:  # noqa: BLE001
        ctx.problem(f"the run finished but its record could not be published: {str(exc)[:400]} — Workflow → Publish run record")
        return {"pr": None, "merged": False, "published": False}
    s.invalidate()
    merged = None
    if not s.strict:
        try:
            merged = s.merge(p, "RUN", pr_number=pr["number"])
        except Exception as exc:  # noqa: BLE001
            ctx.log(f"  the record PR was not merged: {exc}")
    return {"pr": pr.get("html_url"), "merged": bool(merged), "published": True}


def run_local(ctx: Ctx, own_credentials: bool = False, params: dict | None = None, date: str | None = None,
              planned: str | None = None, max_packages: int | None = None) -> dict:
    """S6. A pipeline whose destination moves or deletes files (custom delivery) runs only from a preview (`planned`,
    D1): exactly the previewed files, with the preview's run variables, on the preview's branch. Others run in one click
    (optionally capped by `max_packages`). Order: credentials, apply the approved plan, G5 ledger row, started.json
    (committed before anything moves, R7), pkf run, files.csv + report.json, record PR."""
    import datetime as dt

    s = _open(ctx)
    p = ctx.pipeline
    _gates_done(s, p)
    custom = _custom_delivery(s.read_base, p)
    plan = preview = planned_path = None
    if planned:
        if not custom:
            raise ActionError("Preview and Move are for pipelines that move or delete files; use Run locally")
        if params or date:
            raise ActionError("a Move uses the preview's run variables (run id, date, params): change them in the preview")
        planned_path, plan, preview = _load_preview(ctx, planned)
    elif custom:
        raise ActionError(NEEDS_PREVIEW)
    _need_confirmation(s, own_credentials)
    ident = preflight(ctx, s)
    try:
        wt, branch = _open_run_branch(s, p, preview["branch"] if preview else None)
    except StudioError as exc:
        raise ActionError(str(exc)) from None
    ctx.log(f"  worktree {wt.name} on {branch}")
    run = _run_name(branch)
    rel_dir = f"review/{p}/runs/{run}"
    run_dir = wt / rel_dir
    if preview and (run_dir / "started.json").exists():
        raise ActionError("this preview was already moved: Preview this run again")
    run_dir.mkdir(parents=True, exist_ok=True)
    manifest = json.loads((wt / "infra" / p / "manifest.json").read_text(encoding="utf-8"))
    if manifest["needs_apply"]:
        applied = []
        try:
            applied.append(infra.apply_stack(ctx.root, wt / "infra" / p / "envs" / "dev", f"{p}/dev", ctx.log))
            for b in manifest["buckets"]:
                stack = wt / "infra" / "buckets" / b
                if (stack / "plan.sha256").exists() and not (stack / "handoff.json").exists():
                    applied.append(infra.apply_stack(ctx.root, stack, f"buckets/{b}", ctx.log))
        except infra.InfraError as exc:
            raise ActionError(str(exc)) from None
        _write_json(run_dir / "apply.json", applied)
    _ledger(ctx, s, "G5")                        # after a successful apply (D11)
    env = _run_env(ctx, s, wt)
    caller = (ident or {}).get("Arn")
    log_args = ["--log-file", f"{rel_dir}/run.log"]
    if planned_path is not None:
        started = {"planned_sha256": _sha256(planned_path), "planned_files": len(plan.get("files") or []),
                   "max_packages": plan.get("max_packages"), "run_id": plan.get("run_id"),
                   "started_at": dt.datetime.now(dt.timezone.utc).isoformat(), "caller_arn": caller}
        _write_json(run_dir / "started.json", started)
        _commit(ctx, s, f"S6 {p}: move started ({started['planned_files']} planned file(s)); G5 recorded")
        args = ["run", p, "--only", str(planned_path), *log_args]
    else:
        started = None
        n = int(max_packages) if max_packages else None
        args = ["run", p, *_run_args(params, date), *(["--max-packages", str(n)] if n else []), *log_args]
    run_doc = ctx.pkf(*args, cwd=wt, env=env, timeout=6 * 3600, quiet=True)
    ok = bool(run_doc.get("ok"))
    if not ok:
        ctx.problem(problem_text(args[:2], run_doc))
    data = dict(run_doc.get("data") or {})
    rows = data.pop("files", None) or []
    counts = write_files_csv(run_dir / "files.csv", rows)
    report = {"ok": ok, "issues": run_doc.get("issues"), "data": data, "files_csv": "files.csv", "rows": len(rows),
              "counts": counts, "caller_arn": caller, "planned_sha256": (started or {}).get("planned_sha256"),
              "planned_files": (started or {}).get("planned_files"),
              "log": "run.log" if (run_dir / "run.log").exists() else None}
    _write_json(run_dir / "report.json", report)
    _commit(ctx, s, f"S6 {p}: local run {'succeeded' if ok else 'FAILED'}; G5 recorded")
    pub = _publish(ctx, s, p, branch, ok)
    return {"run_ok": ok, **pub, "report": f"{rel_dir}/report.json", "files_csv": f"{rel_dir}/files.csv", "counts": counts,
            "rows": len(rows), "branch": branch, **({"severity": "error"} if not ok else {})}


def unpublished_runs(s: Studio, p: str) -> list[str]:
    """Local run branches not on GitHub with a report.json or a started.json (a crashed move): Publish run record."""
    return list(s.unpublished_runs(p))


def publish_run_record(ctx: Ctx) -> dict:
    """Workflow → Publish run record (D11): push, open and (dev) merge the record PR of every local run branch that is
    not on GitHub and has a report.json, or a started.json (a crashed move: its run.log and a "did not finish"
    report.json are committed first, so RUN shows `failed`)."""
    s = _open(ctx)
    p = ctx.pipeline
    todo = unpublished_runs(s, p)
    if not todo:
        raise ActionError("there is no unpublished run record")
    info = {b["branch"]: b for b in run_branches(s, p)}
    out = []
    for branch in sorted(todo):
        try:
            wt, _ = _open_run_branch(s, p, branch)
        except StudioError as exc:
            ctx.problem(f"{branch}: {exc}")
            continue
        b = info.get(branch) or {}
        crashed = not (wt / "review" / p / "runs" / _run_name(branch) / "report.json").exists()
        if crashed:
            if b.get("sha"):
                _archive(ctx, s, p, b)
            _finish_crashed(ctx, s, p, wt, branch)
        report = json.loads((wt / "review" / p / "runs" / _run_name(branch) / "report.json").read_text(encoding="utf-8"))
        pub = _publish(ctx, s, p, branch, report.get("ok"))
        out.append({"branch": branch, "crashed": crashed, "run_ok": report.get("ok"), **pub})
    failed = [x for x in out if x.get("run_ok") is False]
    return {"published": out, **({"severity": "error"} if failed else {})}


# ------------------------------------------------------------------------------------------------------------ 0.6.0
LANE_PHASES = ("ask", "diagnose", "brief")


def _lane_gates(gates) -> dict:
    """The gate states the UI passed to a lane job ({"G1": {"state": …}} or its JSON); {} when none or unreadable."""
    if isinstance(gates, str):
        try:
            gates = json.loads(gates)
        except ValueError:
            return {}
    return gates if isinstance(gates, dict) else {}


def assist(ctx: Ctx, phase: str = "diagnose", session_id: str | None = None, question: str | None = None, step_id: str | None = None,
           job_id: str | None = None, gates: dict | str | None = None) -> dict:
    """The assistant as a job (its transcript is tailed by the Workflow Developer tab): diagnose, fix (after the user
    approved the plan), ask, or (0.8.0) brief — the run brief's likely cause, one tool-less call, cached. An LLM failure
    is a problem of the job, never a crash.

    0.8.0, the assistant lane (jobs.start(..., lane="assistant")): ask, diagnose and brief run beside the pipeline's own
    job. The lane opens no Studio (no fetch, no GitHub, no git writes) and does not call derive() — the gates come in
    `gates` (as the UI derived them) or are unknown — and its session has no run_tests. fix changes files and runs the
    tests, so it stays on the pipeline's own lane."""
    import pkf

    from . import flowstore
    from .assistant import protocol
    from .assistant.tools import Context

    p = ctx.pipeline
    lane = ctx.cmd.get("lane") or None
    if lane:
        if phase not in LANE_PHASES:
            raise ActionError(f"the {phase} phase changes files and runs the tests: it runs on the pipeline's own lane (wait for "
                              "the running job to finish), never beside it")
        root = Path(ctx.root)
        flow = flowstore.load(root, p) or {}
        gates_now = _lane_gates(gates)
        worktree, mode = root / STUDIO_DIR / "worktrees" / p, str(flow.get("mode") or "dev")
    else:
        s = _open(ctx)
        root = s.root
        try:
            from .stages import derive

            gates_now = {g.gate: {"state": g.state, "note": g.note, "pr": (g.pr or {}).get("number")} for g in derive(s, p)["gates"]}
        except Exception as exc:  # noqa: BLE001 - offline: the agent gets what the run remembers
            gates_now = {"offline": True, "error": str(exc)[:200]}
        flow = flowstore.load(s.root, p) or {}
        worktree, mode = s.worktree_path(p), s.cfg.mode
    if not job_id and step_id:
        job_id = ((flow.get("steps") or {}).get(step_id) or {}).get("job_id")
    c = Context(root=root, pipeline=p, worktree=worktree, framework_dir=Path(pkf.__file__).resolve().parents[1], mode=mode,
                job_id=job_id, step_id=step_id or flow.get("cursor"), gates=gates_now, flow=flow, events=flowstore.events(root, p, n=60),
                lane=bool(lane))
    ctx.log(f"  assistant {phase}{' (assistant lane)' if lane else ''}: step {c.step_id or '?'}, job {c.job_id or '?'}"
            + (f", session {session_id}" if session_id else ""))
    if phase == "brief":
        out = protocol.likely_cause(c, log=ctx.log)
        if out.get("error"):
            ctx.problem(f"assistant brief: {out['error']}")
        return {"phase": phase, **{k: v for k, v in out.items() if k != "brief"}}
    if phase == "fix":
        if not session_id:
            raise ActionError("the fix phase needs the diagnosis session id")
        out = protocol.prepare_fix(c, session_id, log=ctx.log)
    elif phase == "ask":
        if not question:
            raise ActionError("ask needs a question")
        out = protocol.ask(c, question, session_id=session_id, log=ctx.log)
    else:
        out = protocol.diagnose(c, question=question, session_id=session_id, log=ctx.log)
    if out.get("error"):
        ctx.problem(f"assistant {phase}: {out['error']}")
    return {"phase": phase, "session_id": out.get("session_id"), **{k: v for k, v in out.items() if k not in ("diff",)}}


def build_check(ctx: Ctx) -> dict:
    """7.1: `pkf build --all --check` on the base branch content (the worktree when it is on a branch, else a fresh checkout
    of the base in the job folder): every pipeline's DAG builds and matches."""
    s = _open(ctx)
    wt = s.worktree_path(ctx.pipeline)
    if not (wt / ".git").exists():
        wt = ctx.dir / "base"
        s.git.run("worktree", "add", "--detach", str(wt), s.base_ref, check=True)
    doc = ctx.pkf("build", "--all", "--check", cwd=wt, quiet=True)
    if doc.get("ok"):
        return {"ok": True, "issues": doc.get("issues", [])}
    # 0.6.1: only this pipeline's DAG decides. Another pipeline that is still on its way through the gates (its spec is
    # on the base branch, its DAG is not built yet) is not part of this deploy: logged only. A deployed pipeline whose
    # DAG is out of date is reported as a problem (Continue anyway), because dags/ is copied as a whole.
    mine, others = split_issues(doc.get("issues") or [], wt, ctx.pipeline)
    deployed, in_flight = [], []
    for i in others:
        name = _pipeline_of(str(i.get("file") or ""))
        # a pipeline still on its way (no DAG on the base yet) is logged; a deployed one — or a shared component file,
        # which may belong to a deployed pipeline — is reported
        (in_flight if name and s.read_base(f"dags/pkf_{name}.py") is None else deployed).append(i)
    if in_flight:
        ctx.log("  not part of this deploy (pipelines still on their way through the gates): "
                + "; ".join(f"{i.get('file')}: {i.get('msg')}" for i in in_flight[:8])[:1500])
    if deployed:
        ctx.problem("other deployed pipelines' DAGs are out of date in dags/ (copied as a whole): "
                    + "; ".join(f"{i.get('file')}: {i.get('msg')}" for i in deployed[:5])[:800])
    if mine:
        ctx.problem(problem_text(("build", "--all", "--check"), {**doc, "issues": mine}))
    return {"ok": not mine, "issues": mine, "other_pipelines": [i.get("file") for i in others]}


def _pipeline_of(rel: str) -> str | None:
    """The pipeline a repository path belongs to, for pipelines/<p>.yaml, dags/pkf_<p>.py, dags/pkf/<p>/… (else None)."""
    for pat in (r"^pipelines/([a-z][a-z0-9_]*)\.(?:lock\.)?yaml$", r"^dags/pkf_([a-z][a-z0-9_]*)\.py$", r"^dags/pkf/([a-z][a-z0-9_]*)/"):
        m = re.match(pat, rel.replace("\\", "/"))
        if m:
            return m.group(1)
    return None


def stage_test_data(ctx: Ctx) -> dict:
    """S6, 0.8.2: place a test package (the objects of one of the pipeline's own examples) in its dev source, so a 6.3
    preview that found nothing has something to move. Dev mode only; your AWS credentials (the same identity check as the
    preview); never overwrites, never deletes (testdata). When it cannot place the package, the job ends with problems
    and `commands` / `upload_sh`: run them yourself, then Preview again."""
    from . import testdata

    s = _open(ctx)
    p = ctx.pipeline
    mode = s.cfg.mode
    if s.strict or mode != "dev":
        raise ActionError(f"a test package is placed only in dev (this repository is in {mode})")
    pl = testdata.plan(s.read_base, p, mode)
    if pl["ok"] and not pl.get("region"):
        pl["region"] = s.cfg.aws_region
    if not pl["ok"]:
        ctx.problem(pl["problem"])
        return {"ok": False, "problem": pl["problem"]}
    folder = testdata.write_local(ctx.root, p, pl)
    cmds = testdata.commands(pl, folder)
    out = {"example": pl["example"], "bucket": pl["bucket"], "objects": len(pl["objects"]), "bytes": pl["bytes"],
           "folder": str(folder), "upload_sh": str(folder / "upload.sh"), "commands": cmds, "min_age_s": pl["min_age_s"]}
    ctx.log(f"  test package: example {pl['example']} — {len(pl['objects'])} object(s), {pl['bytes']} bytes → s3://{pl['bucket']}/")
    try:
        if uses_aws(s.read_base, p):
            preflight(ctx, s)
        import boto3

        client = boto3.client("s3", region_name=pl.get("region") or None, endpoint_url=pl.get("endpoint_url") or None)
        try:
            foreign = infra.foreign_notifications(client, pl["bucket"], set())
        except Exception as exc:  # noqa: BLE001
            raise testdata.TestDataError(f"could not read the notification configuration of {pl['bucket']}: {exc}") from None
        wt = s.worktree_path(p)
        ours = {n.get("queue_arn") for n in infra.repo_notifications(wt) if n.get("pipeline") == p} if wt.is_dir() else set()
        others = [x for x in foreign if x not in ours]
        if others:      # an upload would send events to other pipelines' queues / functions
            raise testdata.TestDataError(f"{pl['bucket']} notifies {len(others)} target(s) this pipeline does not own "
                                         f"({', '.join(str(x) for x in others[:3])}): placing files there would start them")
        res = testdata.upload(pl, folder, client=client)
    except (ActionError, testdata.TestDataError, Exception) as exc:  # noqa: BLE001 - every failure ends with the commands
        msg = f"could not place the test package: {exc}"
        ctx.problem(msg + " — run the commands in upload.sh yourself (Git Bash, your AWS credentials), then Preview again")
        for c in cmds[:20]:
            ctx.log("  " + c)
        return {**out, "ok": False, "problem": msg}
    ctx.log(f"  placed {len(res['placed'])} object(s); {len(res['existing'])} already there (kept)")
    import datetime as dt

    (folder / "result.json").write_text(json.dumps({**res, "at": dt.datetime.now(dt.timezone.utc).isoformat()}, indent=2) + "\n",
                                        encoding="utf-8", newline="\n")
    return {**out, "ok": True, **res}


ACTIONS = {f.__name__: f for f in (analyze, resplit, start_g2, start_g3, plan_components, plan_component, start_g4, code_component,
                                   test_build, start_g5, prepare_env, preview_run, run_local, publish_run_record,
    assist, build_check, stage_test_data)}
