# s3_zip_manifest: questions from the requirements architect

Answer in a YAML file (question id → answer; `accept` takes the proposal), then run `pkf rebuild s3_zip_manifest --answers <answers.yaml>` — or answer in pkf Studio. `--assume-all` accepts every proposal.

## Q-ENV-alert_email (blocking) — Environment details

What is the dev value of the setting `alert_email`?

*Why:* data-engineering team e-mail address for alerts; the dev run and the DAG need a value. Secrets only by reference (an Airflow connection id or a secret name).

*Proposal:* none (leave it empty in dev)

## Suggestions (accept with `SUG-n: accept`)

- **SUG-5** (SEP-2): The bucket is versioned. If the same key is uploaded again between SEP-1 and SEP-2, a plain DeleteObject puts a delete marker on top of the NEW version, so a fresh upload disappears until someone notices. The second event then finds the source gone and is skipped. → SEP-1 stores the source VersionId from HeadObject in vars.source_version_id. SEP-2 becomes custom code: head the source and delete only when the current VersionId equals vars.source_version_id (DeleteObject with that VersionId). Otherwise it leaves the object alone and logs a WARNING (the new upload has its own event). The uses: s3.delete entry would be dropped.
- **SUG-6** (ENV): staging_prefix stays 'staging_test/' in every environment (Q-ENV-1 got no answer, so the owner's value is kept). → Set staging_prefix to 'staging/' in dev, test and prod, and update the lifecycle rule to match.
- **SUG-7** (TRG): The S3 notification is shared with an existing QueueConfiguration whose filter is unknown. A wrong overlap makes PutBucketNotificationConfiguration fail. → The IaC deployment fails with a clear message when an existing rule overlaps prefix {incoming_prefix} / suffix .zip, so the owner can apply the Q-TRG-1 decision before anything is changed.

Answers file template:

```yaml
Q-ENV-alert_email: "accept"
```
