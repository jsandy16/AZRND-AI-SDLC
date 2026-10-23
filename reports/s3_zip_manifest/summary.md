# s3_zip_manifest: pkf create (0.10)

Run `r-20261023-145609-96ad` · status **ok** · 2026-10-23T14:56:24+00:00

| stage | name | status | cost (USD) | note |
|---|---|---|---|---|
| S0 | pre-flight | ok | 0.0000 |  |
| S0.5 | parse the requirement | ok | 0.0000 |  |
| S1 | finalise requirements | ok | 0.0000 |  |
| S2 | code tasks | ok | 0.8790 |  |
| S3 | integrate components | ok | 0.0000 |  |
| S4 | create DAG | ok | 0.0000 |  |
| S5 | run DAG | skipped | 0.0000 | dev run failed; going on was approved |

## What was built

| lane | unit tasks | tests passed / failed | coverage | quality | deferred |
|---|---|---|---|---|---|
| SEP | SEP-1, SEP-2 | 5 / 0 | — | clean | 0 |
| TRN | TRN-1, TRN-2 | 7 / 0 | — | clean | 0 |
| DLP | DLP-1, DLP-2 | 7 / 0 | — | clean | 0 |

Requirement, contracts and pseudocode: `requirements/s3_zip_manifest/`. Code: `dags/pkf_pipelines/s3_zip_manifest/` (sep.py · trn.py · dlp.py · pipeline_raw.py). DAG: `dags/pkf_s3_zip_manifest.py` (dag_id `s3_zip_manifest`).

## Arbiter verdicts and integration fixes

None.

## Decisions and assumptions

See `requirements/s3_zip_manifest/assumptions.md`.

No further assumptions.

## Deferred tests

None: every test ran.

## Costs

This run: 0.8790 USD. Recorded for the pipeline: 0.0000 USD of the 10.00 USD run budget (all runs of the pipeline: 2.3931 USD).

## How to deploy

1. Merge this pull request.
2. Deploy `dags/pkf_s3_zip_manifest.py` and `dags/pkf_pipelines/s3_zip_manifest/` to the Airflow DAGs folder (the workers need `package-factory[runtime]`).
3. Fill `dags/pkf_pipelines/s3_zip_manifest/settings/<env>.yaml` for each higher environment and set `PKF_ENV` on the workers.
4. Apply `infra/s3_zip_manifest/envs/<env>/` through CI for test and prod.

Logs of every stage, agent, test and AWS call: pkf Studio → Logs, or `pkf logs s3_zip_manifest`.
