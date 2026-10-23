---
pkf-requirement: 1
pipeline: s3_zip_manifest
owner: data-engineering
environments: [dev, test, prod]
---

## Environment details
| setting | dev | test | prod | description |
| --- | --- | --- | --- | --- |
| aws_account | 992382551516 |  |  |  |
| region | eu-north-1 | eu-north-1 | eu-north-1 |  |
| source_bucket | dev-dpl-dstore-sas-eu-north-1-992382551516 |  |  | packages arrive here |
| target_bucket | dev-dpl-dstore-sas-eu-north-1-992382551516 |  |  | the same bucket as the source |
| incoming_prefix | incoming/ | incoming/ | incoming/ |  |
| processing_prefix | processing/ | processing/ | processing/ |  |
| staging_prefix | staging_test/ | staging_test/ | staging_test/ |  |
| outgoing_prefix | outgoing/ | outgoing/ | outgoing/ |  |
| queue_url | create | create | create | S3 ObjectCreated on incoming/*.zip goes to this queue |
| aws_conn_id | aws_default | aws_default | aws_default | Airflow connection (no secrets here) |

## Pipeline overview
A user or a source process uploads a zip package of PDFs under incoming/. S3 sends an event notification to an SQS
queue; an hourly DAG reads the messages, adds a manifest.csv to every package and delivers it under outgoing/.

## Source
S3 bucket {source_bucket}, prefix {incoming_prefix}. Packages are zip files of PDFs, up to 2 GB.

## Destination
S3 bucket {target_bucket}, prefix {outgoing_prefix}. An existing object is never overwritten.

## Trigger
sqs: { queue: queue_url, poll: "0 * * * *" }
S3 ObjectCreated events for incoming/*.zip go to the queue; the DAG polls it every hour (UTC), one active run.

## Source extraction pattern
### SEP-1: Copy and rename the package
does: |
  Skip a package that was already delivered (same source key and checksum).
  Server-side copy {incoming_prefix}<name>.zip to {processing_prefix}<name>_<package_ts>.zip in {source_bucket}
  and verify the copy by size and checksum.
example:
  given:
  - incoming/study_a.zip
  then:
  - processing/study_a_20261022T100000Z.zip

### SEP-2: Remove the original
does: |
  Delete {incoming_prefix}<name>.zip once SEP-1's copy is verified; when it is already gone, do nothing.
uses: s3.delete
params:
  bucket: '{source_bucket}'
  key: '{incoming_prefix}{name}.zip'

## Transformation
### TRN-1: Build the manifest
does: |
  Download {processing_prefix}<name>_<package_ts>.zip, unzip it safely and write manifest.csv with the columns
  file_name,size_bytes (header row; path inside the zip, uncompressed size, no folder entries).
  Make a new zip with the original files plus manifest.csv.
example:
  given:
    zip:
    - a.pdf (10 bytes)
    - b/c.pdf (20 bytes)
  then:
    manifest.csv:
    - file_name,size_bytes
    - a.pdf,10
    - b/c.pdf,20
  origin: derived

### TRN-2: Upload to staging
does: |
  Upload the new zip to {staging_prefix}<name>_<package_ts>.zip and hand over its key as staging_key.

## Destination load pattern
### DLP-1: Deliver to outgoing
does: |
  Copy {staging_prefix}<name>_<package_ts>.zip to {outgoing_prefix}<package_ts>_<name>.zip in {target_bucket}.
  Never overwrite an existing object.

### DLP-2: Clean up
does: |
  Delete the processing and staging copies once DLP-1 is verified.
on_failure: skip

## DAG
start → SEP-1 → SEP-2 → TRN-1 → TRN-2 → DLP-1 → DLP-2 → end

## Non-functional
Up to 50 packages an hour, largest zip 2 GB. Retries: 2. Alerts by e-mail to the data engineering team.
Data classification: internal; objects use the bucket's default encryption.
