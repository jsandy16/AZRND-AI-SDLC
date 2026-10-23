# s3_zip_manifest: pseudocode

## Source extraction pattern

### SEP-1: Copy and rename the package

Check whether the package's source object (ctx.source.key) exists. If it does: compute its sha256 and look for the delivery index object {index_prefix}{stem}/{source_sha256}_{source_key_hash}.json. When the index exists, its source_key equals the source key, and the referenced outgoing object still exists with metadata pkf-source-sha256 equal to the sha: delete the incoming original and mark the package skipped. Otherwise server-side copy the source to {processing_prefix}{stem}_{package_ts}.zip in {source_bucket}, verified by size and sha256 (skip_identical). If the source is gone: resume from an existing processing copy, else skip with a WARNING (replay).

*Level:* package · *on failure:* fail_package · *idempotency:* verified_copy with if_exists=skip_identical: a target with the same sha counts as done, a different sha raises DataError. When the source is already gone, the existing processing copy is resumed; with no processing copy either, the package is skipped.

```text
1. s3 = rt.s3(); src_b = ctx.source.bucket; src_k = ctx.source.key; stem = ctx.vars['stem']; ts = ctx.vars['package_ts'].
2. proc_key = check_key(f"{rt.settings['processing_prefix']}{stem}_{ts}.zip"); kh = source_key_hash(src_k).
3. h = head(s3, src_b, src_k).
4. If h is None:
   a. ph = head(s3, rt.settings['source_bucket'], proc_key). If ph: sha = sha256_of(s3, source_bucket, proc_key); set vars source_sha256=sha, source_key_hash=kh, source_key=src_k; artefacts.renamed_key=proc_key; log INFO 'resume'; return ctx.
   b. Else log WARNING 'source gone, nothing to resume'; ctx.status='skipped'; return ctx.
5. If ctx.source.size is not None and h['size'] != ctx.source.size: log WARNING.
6. sha = sha256_of(s3, src_b, src_k).
7. ik = index_key(rt, stem, sha, kh); obj = head(s3, target_bucket, ik).
8. If obj: download it to rt.tmpdir() and json-load it → idx. If idx['source_key'] == src_k: oh = head(s3, target_bucket, idx['outgoing_key']). If oh and oh.metadata['pkf-source-sha256'] == sha: delete(s3, src_b, src_k, missing_ok=True); ctx.status='skipped'; vars.already_delivered=idx['outgoing_key']; return ctx.
9. verified_copy(s3, src_b, src_k, rt.settings['source_bucket'], proc_key, if_exists='skip_identical') (a different existing sha → DataError).
10. Set vars source_sha256=sha, source_key_hash=kh, source_key=src_k; artefacts.renamed_key=proc_key; return ctx.
```

- helper `def source_key_hash(key: str) -> str`: stable short hash of the source key for the index name

  ```text
  return hashlib.sha1(key.encode('utf-8')).hexdigest()[:12]
  ```
- helper `def index_key(rt: Runtime, stem: str, sha: str, key_hash: str) -> str`: render the delivery index key

  ```text
  return check_key(render('{index_prefix}{stem}/{sha}_{h}.json', index_prefix=rt.settings['index_prefix'], stem=stem, sha=sha, h=key_hash))
  ```
- clauses: R-SEP-1-a The processing key is {processing_prefix}{stem}_{package_ts}.zip, where package_ts is the S3 event time in YYYYMMDDTHHMMSSZ UTC., R-SEP-1-b The copy is verified by size and sha256, never by ETag. A different existing target raises DataError., R-SEP-1-c A package counts as already delivered only when the index object for (stem, source sha256, source key hash) exists, its source_key equals ctx.source.key, and its outgoing object exists with pkf-source-sha256 == source sha256. Then the original is deleted and status=skipped., R-SEP-1-d Source gone + processing copy present → resume. Source gone + no processing copy → status skipped with a WARNING (message is deleted by ack)., R-SEP-1-e When the current source size differs from ctx.source.size, log a WARNING and process the current version.
- edge cases: —

### SEP-2: Remove the original

Delete the package's own source object (ctx.source.bucket/ctx.source.key, the exact key from the event). An object that is already gone is fine. It runs only when SEP-1 left status ok, which means the processing copy is verified (skipped packages pass through).

*Level:* package · *on failure:* fail_package · *idempotency:* missing_ok=true: a second run finds the object gone and does nothing.

Kit operation `s3.delete` with `{"missing_ok": true}`.

- clauses: R-SEP-2-a Delete the source only after SEP-1 verified the processing copy (guaranteed by the order and status ok)., R-SEP-2-b bucket/key default to ctx.source; the owner's '{incoming_prefix}{name}.zip' template is replaced because {name} already holds '.zip'.
- edge cases: —

## Transformation

### TRN-1: Build the manifest

Unless a staging zip for this package already exists with matching pkf-source-sha256: check free disk space, then download artefacts.renamed_key to rt.shared_dir() and safe-extract it. Build manifest.csv with columns file_name,size_bytes (header row; one row per non-folder entry in zip order; file_name is the path inside the zip, size_bytes the uncompressed size). Build a new zip64 with the original files plus manifest.csv at the root. Hand over its path in artefacts.new_zip_path.

*Level:* package · *on failure:* fail_package · *idempotency:* When the staging zip already exists with pkf-source-sha256 == vars.source_sha256, it sets artefacts.staging_key and builds nothing; a staging zip with a different source sha raises DataError. Otherwise it is a pure local rebuild in shared_dir.

```text
1. s3=rt.s3(); stem, ts = vars; stg_key = check_key(f"{rt.settings['staging_prefix']}{stem}_{ts}.zip").
2. sh = head(s3, target_bucket, stg_key). If sh: if sh.metadata.get('pkf-source-sha256') == vars.source_sha256: artefacts.staging_key=stg_key; vars.staged=True; return ctx. Else raise DataError('staging object exists for another source').
3. d = rt.shared_dir(); ph = head(s3, source_bucket, artefacts.renamed_key); ensure_disk(d, ph['size']).
4. zp = download(s3, source_bucket, artefacts.renamed_key, d/'in.zip').
5. ents = entries(zp); if any(e['name']=='manifest.csv' for e in ents): raise DataError.
6. files = safe_extract(zp, d/'x'); map each entry name to d/'x'/name.
7. m = build_manifest(ents, d/'manifest.csv').
8. out = make_zip([(d/'x'/e['name'], e['name']) for e in ents if not e['is_dir']] + [(m,'manifest.csv')], d/'out.zip').
9. Delete in.zip to free space; artefacts.new_zip_path=str(out); vars.manifest_rows=len(non-folder entries); return ctx.
```

- helper `def build_manifest(entries: list[dict], dest: Path) -> Path`: write manifest.csv from zip entries

  ```text
  rows=[(e['name'], e['size']) for e in entries if not e['is_dir']]; open dest 'w', encoding='utf-8', newline=''; csv.writer(f, lineterminator='\n', quoting=csv.QUOTE_MINIMAL); writerow(['file_name','size_bytes']); writerows(rows); return dest
  ```
- helper `def ensure_disk(path: Path, zip_size: int) -> None`: guard the worker disk

  ```text
  free=shutil.disk_usage(path).free; if free < int(zip_size*3*1.1): raise TransientError(f'need {..} bytes, free {free}')
  ```
- clauses: R-TRN-1-a manifest.csv: UTF-8 without BOM, comma separator, RFC 4180 quoting, LF line endings, header file_name,size_bytes, rows in zip entry order, every non-folder entry (not only .pdf)., R-TRN-1-b A zip that already contains a root manifest.csv fails the package with DataError., R-TRN-1-c Extraction uses archive.safe_extract (zip-slip, symlinks and bombs → DataError)., R-TRN-1-d Before downloading, free space in shared_dir must be ≥ 3 × zip size × 1.1, else TransientError., R-TRN-1-e TRN-1 and TRN-2 share one Airflow task (group); the local zip path is handed over in artefacts.new_zip_path.
- edge cases: —

### TRN-2: Upload to staging

Upload artefacts.new_zip_path to {staging_prefix}{stem}_{package_ts}.zip in {target_bucket}, with metadata pkf-source-key=vars.source_key and pkf-source-sha256=vars.source_sha256 (pkf-sha256 is added by the kit), if_exists=fail. Hand over the key as artefacts.staging_key. When TRN-1 found the staging zip already done (vars.staged), do nothing.

*Level:* package · *on failure:* fail_package · *idempotency:* TRN-1 detects an existing staging zip for the same source sha and sets vars.staged, so the upload is skipped. Written under the final name only.

```text
1. If vars.get('staged'): return ctx (staging_key already set).
2. key = check_key(f"{rt.settings['staging_prefix']}{vars.stem}_{vars.package_ts}.zip").
3. upload(rt.s3(), Path(artefacts.new_zip_path), target_bucket, key, if_exists='fail', metadata={'pkf-source-key': vars.source_key, 'pkf-source-sha256': vars.source_sha256}).
4. artefacts.staging_key = key; return ctx.
```

- clauses: R-TRN-2-a The staging zip carries user metadata pkf-source-key and pkf-source-sha256., R-TRN-2-b artefacts.staging_key is set in every ok path.
- edge cases: —

## Destination load pattern

### DLP-1: Deliver to outgoing

Verified server-side copy of artefacts.staging_key to {outgoing_prefix}{package_ts}_{stem}.zip in {target_bucket} with if_exists=skip_identical (never overwrite; a different existing object raises DataError). Then check that the outgoing object carries pkf-source-key and pkf-source-sha256 equal to the vars. Finally write the delivery index object {index_prefix}{stem}/{source_sha256}_{source_key_hash}.json.

*Level:* package · *on failure:* fail_package · *idempotency:* skip_identical on both writes. The index content is deterministic (sorted keys, no run clock), so a re-run finds identical objects.

```text
1. s3=rt.s3(); tb=rt.settings['target_bucket']; out_key=check_key(f"{outgoing_prefix}{package_ts}_{stem}.zip").
2. r = verified_copy(s3, tb, artefacts.staging_key, tb, out_key, if_exists='skip_identical').
3. oh = head(s3, tb, out_key); if oh.metadata.get('pkf-source-key')!=vars.source_key or oh.metadata.get('pkf-source-sha256')!=vars.source_sha256: raise DataError.
4. idx = {source_key, source_sha256, outgoing_key: out_key, outgoing_sha256: sha256_of(s3,tb,out_key), package_ts}; write json.dumps(idx, sort_keys=True) to rt.tmpdir()/'idx.json'.
5. ik = index_key(rt, stem, source_sha256, source_key_hash); upload(s3, path, tb, ik, if_exists='skip_identical').
6. artefacts.outgoing_key=out_key; artefacts.index_key=ik; return ctx.
```

- clauses: R-DLP-1-a Outgoing name {outgoing_prefix}{package_ts}_{stem}.zip; an existing object is never overwritten., R-DLP-1-b After the copy, the outgoing metadata pkf-source-key/pkf-source-sha256 must equal vars.source_key/vars.source_sha256, else DataError., R-DLP-1-c Index JSON = {"source_key", "source_sha256", "outgoing_key", "outgoing_sha256", "package_ts"}, sort_keys, written only after the outgoing object is verified.
- edge cases: —

### DLP-2: Clean up

After confirming that artefacts.outgoing_key exists in {target_bucket}, delete artefacts.renamed_key (processing copy, source_bucket) and artefacts.staging_key (target_bucket), missing_ok. A failure here does not fail the package.

*Level:* package · *on failure:* skip · *idempotency:* Deletes with missing_ok=True.

```text
1. If head(s3, target_bucket, artefacts.outgoing_key) is None: log WARNING; return ctx.
2. delete(s3, source_bucket, artefacts.renamed_key, missing_ok=True).
3. delete(s3, target_bucket, artefacts.staging_key, missing_ok=True).
4. Return ctx.
```

- clauses: R-DLP-2-a Delete only when head(outgoing_key) succeeds; otherwise log a WARNING and keep both copies.
- edge cases: —

## DAG

DAG s3_zip_manifest: schedule '0 * * * *' UTC, catchup=False, max_active_runs=1, dagrun_timeout 5 h (below the 6 h SQS visibility timeout).
start → SEP-0 discover (sqs, queue_url, max_packages 100; drops TestEvents and duplicates) → per package (dynamic task mapping, max 8 parallel): SEP-1 (timeout 60 min) → SEP-2 (10 min) → [TRN-1 + TRN-2 in one Airflow task, shared_dir, timeout 120 min, worker scratch ≥ 8 GiB] → DLP-1 (60 min) → DLP-2 (10 min, on_failure skip) → outcome → DLP-Z ack (delete delivered/skipped handles, release failed) → report (write_uri s3://{target_bucket}/{report_prefix}{run_id}.json; RunFailed when any failed) → end.
Retries: 2 per task, retry_delay 5 min, exponential backoff, only for TransientError and unexpected errors. DataError fails the package without a retry; ConfigError fails the run. E-mail on task failure and on RunFailed to alert_email.

Order: start → SEP-0 discover → package[ SEP-1 → SEP-2 → TRN-1 → TRN-2 → DLP-1 → DLP-2 → outcome ] → DLP-Z ack → report → end
