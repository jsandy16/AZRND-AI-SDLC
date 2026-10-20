"""0.6.1 pipeline isolation: one pipeline's build no longer breaks another's — shared bucket stacks, repository-wide
`pkf validate`, the 7.1 deploy check, and the G5 PR of two pipelines on the same bucket."""
from __future__ import annotations

import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from pkf_studio import actions as A
from pkf_studio import flow
from pkf_studio import ledger as L
from pkf_studio import infra
from pkf_studio.gitops import Git
from pkf_studio.runner import ActionError


def stack(*pipes: str, version: str = "0.6.0") -> str:
    blocks = "".join(infra.QUEUE_BLOCK.format(id=json.dumps(f"pkf-{p}"), arn=json.dumps(f"arn:aws:sqs:eu-west-1:1:pkf-dev-{p}"),
                                              prefix=json.dumps(f"in/{p}/"), suffix=json.dumps(".csv")) for p in sorted(pipes))
    return infra.BUCKET_MAIN.format(version=version, bucket="b1", bucket_json='"b1"', queues=blocks, backend="s3")


# ------------------------------------------------------------------------------------------------ bucket stack parsing
def test_queue_blocks_and_own_view():
    text = stack("alpha", "beta")
    assert set(L.queue_blocks(text)) == {"alpha", "beta"}
    assert set(L.foreign_blocks(text, "alpha")) == {"beta"}
    view = L.own_view("infra/buckets/b1/main.tf", text, "alpha")
    assert "pkf-alpha" in view and "pkf-beta" not in view and "Rendered by" not in view
    # another pipeline's block, or a newer Studio's header, is not part of alpha's approval
    assert view == L.own_view("infra/buckets/b1/main.tf", stack("alpha", "beta", "gamma", version="0.6.1"), "alpha")
    # alpha's own entry is
    assert view != L.own_view("infra/buckets/b1/main.tf", stack("alpha").replace("in/alpha/", "in/other/"), "alpha")
    tf = json.dumps({"region": "eu-west-1", "account_id": "1", "tags": {"owner": "x"}})
    assert L.own_view("infra/buckets/b1/terraform.tfvars.json", tf, "alpha") == \
        L.own_view("infra/buckets/b1/terraform.tfvars.json", tf.replace('"x"', '"y"'), "alpha")


def _reader(files: dict):
    return (lambda p: files.get(p)), (lambda pre: sorted(f for f in files if f.startswith(pre)))


def H(fn, gate, files):
    read, lister = _reader(files)
    return fn(gate, read, "alpha", lister)


def test_scoped_g5_hash_ignores_other_pipelines_but_not_own_changes():
    base = {"infra/alpha/manifest.json": json.dumps({"buckets": ["b1"]}), "infra/alpha/iam-policy.json": "{}",
            "dags/pkf_alpha.py": "dag", "infra/buckets/b1/main.tf": stack("alpha"), "infra/buckets/b1/backend.hcl": "x"}
    h0 = H(L.input_hash_scoped, "G5", base)
    other = {**base, "infra/buckets/b1/main.tf": stack("alpha", "beta")}
    assert H(L.input_hash_scoped, "G5", other) == h0               # beta's G5 on the same bucket
    assert H(L.input_hash, "G5", other) != H(L.input_hash, "G5", base)   # ledger hash unchanged
    mine = {**base, "infra/buckets/b1/main.tf": stack("alpha").replace(".csv", ".zip")}
    assert H(L.input_hash_scoped, "G5", mine) != h0                # alpha's own entry changed
    dag = {**base, "dags/pkf_alpha.py": "dag2"}
    assert H(L.input_hash_scoped, "G5", dag) != h0
    for g in ("G1", "G2", "G3", "G4"):                                              # the other gates: exactly input_hash
        assert H(L.input_hash_scoped, g, base) == H(L.input_hash, g, base)


# ------------------------------------------------------------------------------------------------ validate / 7.1
class FakeCtx:
    def __init__(self, pipeline: str, doc: dict):
        self.pipeline, self.doc, self.logs, self.problems = pipeline, doc, [], []

    def pkf(self, *args, cwd=None, check=False, quiet=False, **kw):
        return self.doc

    def log(self, m):
        self.logs.append(m)

    def problem(self, m):
        self.problems.append(m)


def _wt(tmp_path: Path) -> Path:
    wt = tmp_path / "wt"
    (wt / "pipelines").mkdir(parents=True)
    (wt / "connections").mkdir()
    (wt / "pipelines" / "alpha.yaml").write_text("name: alpha\nconnection: alpha_conn\ntrigger: alpha_t\n")
    (wt / "connections" / "alpha_conn.yaml").write_text("source: shared_src\ndestination: alpha_dst\n")
    return wt


def test_validate_own_ignores_other_pipelines(tmp_path):
    wt = _wt(tmp_path)
    doc = {"ok": False, "issues": [
        {"severity": "error", "file": "pipelines/beta.yaml", "msg": "connection 'beta_conn' not found"},
        {"severity": "warning", "file": "sources/x.yaml", "msg": "w"}]}
    ctx = FakeCtx("alpha", doc)
    out = A.validate_own(ctx, wt)
    assert out["ok"] and out["ignored_other_pipelines"] == 1 and any("beta" in x for x in ctx.logs)


@pytest.mark.parametrize("bad", ["pipelines/alpha.yaml", "sources/shared_src.yaml", "connections/alpha_conn.yaml", ""])
def test_validate_own_still_fails_on_own_files(tmp_path, bad):
    """Its own specs — and a component it reuses (shared_src) — still fail the job, as before."""
    wt = _wt(tmp_path)
    ctx = FakeCtx("alpha", {"ok": False, "issues": [{"severity": "error", "file": bad, "msg": "broken"}]})
    with pytest.raises(ActionError, match="pkf validate failed: broken"):
        A.validate_own(ctx, wt)


def test_build_check_only_this_pipeline_decides(tmp_path, monkeypatch):
    wt = _wt(tmp_path)
    (wt / ".git").mkdir()
    doc = {"ok": False, "issues": [
        {"severity": "error", "file": "pipelines/beta.yaml", "msg": "beta: transformation not approved"},     # in flight
        {"severity": "error", "file": "pipelines/gamma.yaml", "msg": "dags/pkf_gamma.py differs"}]}         # deployed
    ctx = FakeCtx("alpha", doc)
    ctx.dir = tmp_path
    s = SimpleNamespace(worktree_path=lambda p: wt, read_base=lambda rel: "dag" if rel == "dags/pkf_gamma.py" else None)
    monkeypatch.setattr(A, "_open", lambda c: s)
    out = A.build_check(ctx)
    assert out["ok"] and not out["issues"]
    assert len(ctx.problems) == 1 and "gamma" in ctx.problems[0] and "beta" not in ctx.problems[0]
    ctx = FakeCtx("alpha", {"ok": False, "issues": [{"severity": "error", "file": "pipelines/alpha.yaml", "msg": "stale"}]})
    ctx.dir = tmp_path
    out = A.build_check(ctx)
    assert not out["ok"] and ctx.problems and "stale" in ctx.problems[0]


# ------------------------------------------------------------------------------------------------ shared stack in git
def _git(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


@pytest.fixture()
def repo(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q", "-b", "main")
    _git(root, "config", "user.email", "t@example.com")
    _git(root, "config", "user.name", "T")
    (root / "infra" / "buckets" / "b1").mkdir(parents=True)
    (root / "infra" / "buckets" / "b1" / "main.tf").write_text(stack("gamma"))
    (root / "README.md").write_text("x\n")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "base")
    return root


def _studio(root: Path):
    git = Git(root)
    return SimpleNamespace(git=git, base_ref="main", base="main", _git_env=lambda: {}, worktree_branch=lambda p: "pkf/alpha/g5",
                           commit=lambda p, m: None, left_uncommitted_message=lambda p: None,
                           worktree_path=lambda p: root, read_base=lambda rel: git.show("main", rel))


def test_sync_merges_the_base_when_another_pipeline_changed_the_stack(repo):
    _git(repo, "checkout", "-q", "-b", "pkf/alpha/g5")
    (repo / "infra" / "buckets" / "b1" / "main.tf").write_text(stack("alpha", "gamma"))   # alpha rendered at 5.1
    _git(repo, "commit", "-qam", "alpha 5.1")
    _git(repo, "checkout", "-q", "main")
    (repo / "infra" / "buckets" / "b1" / "main.tf").write_text(stack("beta", "gamma"))    # beta's G5 merged meanwhile
    _git(repo, "commit", "-qam", "beta G5")
    _git(repo, "checkout", "-q", "pkf/alpha/g5")
    s = _studio(repo)
    ctx = FakeCtx("alpha", {})
    changed = A._sync_shared_stacks(ctx, s, repo, ["b1"])
    assert changed == ["infra/buckets/b1/main.tf"]
    assert _git(repo, "merge-base", "--is-ancestor", "main", "HEAD") == ""           # the base is in the branch
    # the conflict was resolved with the base's version; rendering again then adds alpha's block next to beta's
    assert "pkf-beta" in (repo / "infra" / "buckets" / "b1" / "main.tf").read_text()
    assert not _git(repo, "status", "--porcelain")
    assert A._sync_shared_stacks(ctx, s, repo, ["b1"]) == []                         # nothing new: no second merge


def test_sync_refuses_conflicts_outside_the_stack(repo):
    _git(repo, "checkout", "-q", "-b", "pkf/alpha/g5")
    (repo / "README.md").write_text("alpha\n")
    (repo / "infra" / "buckets" / "b1" / "main.tf").write_text(stack("alpha", "gamma"))
    _git(repo, "commit", "-qam", "alpha")
    _git(repo, "checkout", "-q", "main")
    (repo / "README.md").write_text("main\n")
    (repo / "infra" / "buckets" / "b1" / "main.tf").write_text(stack("beta", "gamma"))
    _git(repo, "commit", "-qam", "main")
    _git(repo, "checkout", "-q", "pkf/alpha/g5")
    head = _git(repo, "rev-parse", "HEAD")
    with pytest.raises(ActionError, match="conflicts in README.md"):
        A._sync_shared_stacks(FakeCtx("alpha", {}), _studio(repo), repo, ["b1"])
    assert _git(repo, "rev-parse", "HEAD") == head and not _git(repo, "status", "--porcelain")   # merge aborted cleanly


def test_bucket_drift_names_the_other_pipeline(tmp_path):
    wt = tmp_path / "wt"
    (wt / "infra" / "alpha").mkdir(parents=True)
    (wt / "infra" / "buckets" / "b1").mkdir(parents=True)
    (wt / "infra" / "alpha" / "manifest.json").write_text(json.dumps({"buckets": ["b1"]}))
    (wt / "infra" / "buckets" / "b1" / "main.tf").write_text(stack("alpha", "gamma"))
    manifests = {"infra/beta/manifest.json", "infra/gamma/manifest.json"}
    s = SimpleNamespace(worktree_path=lambda p: wt, base="main",
                        read_base=lambda rel: stack("beta", "gamma") if rel.endswith("main.tf") else "{}" if rel in manifests else None)
    drift = flow.bucket_drift(s, "alpha")
    assert len(drift) == 1 and "bucket b1" in drift[0] and "beta" in drift[0]
    s.read_base = lambda rel: stack("gamma") if rel.endswith("main.tf") else "{}" if rel in manifests else None   # only alpha's own block differs
    assert flow.bucket_drift(s, "alpha") == []
    # a block left behind by a removed pipeline (no manifest on the base) is dropped by the next render: no drift
    s.read_base = lambda rel: stack("delta", "gamma") if rel.endswith("main.tf") else "{}" if rel in manifests else None
    assert flow.bucket_drift(s, "alpha") == []
    assert flow.fix_step_for("G5", drift) == "5.2"
