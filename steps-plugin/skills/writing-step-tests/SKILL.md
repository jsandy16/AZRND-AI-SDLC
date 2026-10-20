---
name: writing-step-tests
description: Writes test cases for one custom step of a pipeline component from its approved pseudocode (never from code), as structured output. Used headlessly by `pkf plan`; not for interactive use.
user-invocable: false
---

# Writing step tests

You are the independent tester. You see the pseudocode, the signature, the API and the case format — not the code,
which does not exist yet. Your cases are reviewed with the plan and the code must pass them.

## Cases

- 3-8 cases: the normal path; one case per rule or branch in the pseudocode; edge cases the pseudocode implies
  (empty input, nothing matching, duplicates, URL-encoded or nested paths, a refusal).
- Each case: minimal input that isolates its purpose; the exact expected output in the given format;
  `expect_error: true` (and `expected_json: "null"`) when the pseudocode says to refuse.
- Derive expectations only from the pseudocode and the requirement. Where they are silent, do not test that detail.
- `input_json`, `params_json` and `expected_json` must be valid JSON. Use only variables the step can see.
- `custom_delivery` steps: cases describe the S3 state before (`objects`), the `transfers`, optionally `fail_calls`
  (one S3 call made to fail), and the state after (`objects`: every object in every bucket; `tags` for the objects
  whose tags matter; `statuses` per path). Cover: the normal move/copy, each failure the pseudocode handles (copy
  fails → source still there and status "failed"; tagging fails → still delivered if the pseudocode says so),
  a target that already exists, and re-running after an earlier attempt (source already gone, target present).
  Bucket names must be valid S3 names (3+ lowercase characters).
