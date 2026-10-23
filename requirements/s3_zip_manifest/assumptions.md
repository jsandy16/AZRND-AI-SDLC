# s3_zip_manifest: assumptions and decisions

## Answers

- SUG-5 (suggestion): The bucket is versioned. If the same key is uploaded again between SEP-1 and SEP-2, a plain DeleteObject puts a delete marker on top of the NEW version, so a fresh upload disappears until someone notices. The second event then finds the source gone and is skipped. → accepted: SEP-1 stores the source VersionId from HeadObject in vars.source_version_id. SEP-2 becomes custom code: head the source and delete only when the current VersionId equals vars.source_version_id (DeleteObject with that VersionId). Otherwise it leaves the object alone and logs a WARNING (the new upload has its own event). The uses: s3.delete entry would be dropped.
- SUG-6 (suggestion): staging_prefix stays 'staging_test/' in every environment (Q-ENV-1 got no answer, so the owner's value is kept). → accepted: Set staging_prefix to 'staging/' in dev, test and prod, and update the lifecycle rule to match.
- SUG-7 (suggestion): The S3 notification is shared with an existing QueueConfiguration whose filter is unknown. A wrong overlap makes PutBucketNotificationConfiguration fail. → accepted: The IaC deployment fails with a clear message when an existing rule overlaps prefix {incoming_prefix} / suffix .zip, so the owner can apply the Q-TRG-1 decision before anything is changed.

## Assumptions

- Q-TRG-1 accepted: IaC adds one QueueConfiguration (ObjectCreated:*, prefix {incoming_prefix}, suffix .zip) to the new queue and keeps the existing rules; on overlap the owner decides.
- Q-SEP-1-1 accepted, combined with SUG-2: dedupe uses the index object {index_prefix}{stem}/{source_sha256}_{source_key_hash}.json written by DLP-1 instead of listing outgoing/. SEP-1 also verifies that the referenced outgoing object still has a matching pkf-source-sha256 before it deletes the original.
- SUG-1 accepted: lifecycle rule for noncurrent versions (30 days) under incoming, processing and staging, plus expired delete marker removal.
- SUG-3 accepted: report written to s3://{target_bucket}/{report_prefix}{run_id}.json; report_prefix dev value reports/s3_zip_manifest/.
- SUG-4 accepted: max_packages = 100.
- index_prefix dev value 'index/s3_zip_manifest/' is derived (owner named the setting only).
- Unanswered non-blocking proposals adopted: Q-SEP-2-1 (s3.delete with defaults, missing_ok), Q-TRN-1-1 (group TRN-1+TRN-2), Q-TRN-1-2 (manifest format; root manifest.csv → DataError), Q-SRC-1 (sub-folders accepted, names by stem; collisions → DataError), Q-SRC-2 (current version, WARNING on size mismatch), Q-SEP-1-2 (replay skip/resume), Q-TRG-2 (DLQ, maxReceiveCount 3, visibility 21600 s, SSE-SQS, queue policy), Q-NF-1 (≥ 8 GiB scratch, 3×size+10% check → TransientError), Q-DLP-1-1 (skip_identical; different sha → DataError).
- Q-ENV-1 got no answer: the owner's staging_prefix 'staging_test/' is kept (see SUG-6).
- Encryption: no sse param; objects inherit the bucket default SSE-KMS key; IAM grants kms:Decrypt/GenerateDataKey on it.
- No loop: the pipeline writes only to processing, staging, outgoing, index and reports, never under incoming/.
- Examples 2–4 are derived; example 1 is quoted from SEP-1/TRN-1 (zip contents derived).

## Derived examples

- replayed_message_source_gone
- zip_with_root_manifest_fails
- outgoing_collision_not_overwritten
