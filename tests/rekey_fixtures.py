"""Re-key replay fixtures after a listed prompt change (K16); never re-record.

    python tests/rekey_fixtures.py [pytest args…]      (default: the replay tests)

Runs the replay tests with PKF_LLM_REKEY=1. On a replay miss, `pkf.llm.base.run_structured` computes the key the same call
had before the listed prompt changes (every entry of base.listed_changes(task) removed: the 0.5.0 sentences of
PROMPT_CHANGES_0_5 and the catalogue operations and parameters added since, CATALOGUE_ADDITIONS / PARAM_ADDITIONS;
LEGACY_SKILL_SHA for a changed SKILL.md). When that fixture exists it is copied under the new key, output unchanged, with `provenance.rekeyed_from`.
The tool prints the old → new pairs; commit the new files. Old files stay until the orphan sweep
(PKF_LLM_FIXTURE_TRACE). CI never sets PKF_LLM_REKEY, so a missing fixture there is still an error.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REPLAY_TESTS = ["tests/test_flow.py", "tests/test_components.py", "tests/test_studio_e2e.py"]


def main(argv: list[str]) -> int:
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
        log = Path(d) / "rekey.jsonl"
        env = {k: v for k, v in os.environ.items() if k != "CLAUDECODE"}
        env.update(PKF_LLM_REKEY="1", PKF_LLM_REKEY_LOG=str(log), PKF_LLM_FIXTURES=str(ROOT / "tests" / "llm-fixtures"))
        r = subprocess.run([sys.executable, "-m", "pytest", "-q", *(argv or REPLAY_TESTS)], cwd=ROOT, env=env)
        pairs = [json.loads(ln) for ln in log.read_text(encoding="utf-8").splitlines()] if log.exists() else []
    for p in pairs:
        print(f"{p['task']}/{p['old']} -> {p['task']}/{p['new']}")
    print(f"{len(pairs)} fixture(s) re-keyed; tests exit code {r.returncode}")
    return r.returncode


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
