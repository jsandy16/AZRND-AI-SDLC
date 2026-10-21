# entimice_api_pipeline — pseudocode (display only)

1. Cron 0 6,18 * * * starts one run (max 1 active run, no catch-up); read force_refresh.
2. Validate the Self-Service API key (dpls_ prefix, >=20 chars, no spaces); fail run if invalid.
3. GET the submissions list; fail the run if the call fails or is not a JSON list.
4. For each record apply the due rules (skip FAILED_INGESTION_STOPPED, unknown schedule; check completed state and lastTransferAt age in UTC days; force_refresh overrides completion). Count not-due reasons.
5. If none due: report and finish successfully.
6. Determine the Entimice account per due study from destination map; log in to every needed account; if any login fails, delete opened sessions and fail the run.
7. For each due study (one package each):
   a. If location missing: result FAILED (Other issue).
   b. Recursively list Entimice from location (max 20 levels); any listing failure fails the study; no files fails the study.
   c. Resolve bucket from destination (exact), check studyId; configuration failures fail the study without copying.
   d. For each file: stream download, stream multipart upload to the key (SAS: incoming/<studyId>/<path>; clinicalstudies: <studyId>/ongoing/<path>; other: <studyId>/<path>), HEAD and compare size; record per-file failures and continue.
   e. Decide result by the ordered rules; derive reason; apply consecutive-failure counter and possible FAILED_INGESTION_STOPPED.
8. Delete all Entimice sessions (warning on failure).
9. For each due study with an id, PUT the updated submission_data (modifiedAt, lastTransferAt if DONE, status, error, modifier fields); log failures only.
10. Report per-study lines and totals; the run succeeds unless the control list, a login or an unexpected outer error failed.
