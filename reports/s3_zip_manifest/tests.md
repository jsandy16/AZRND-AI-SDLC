# s3_zip_manifest: tests to review

Defer with `pkf defer s3_zip_manifest <node id> --reason …`; then `pkf review-tests s3_zip_manifest --done` and `pkf rebuild s3_zip_manifest`.

- `tests/s3_zip_manifest/sep/test_component.py::test_happy_path_study_a_lane`: functional: SEP-1 then SEP-2 leave only the verified processing copy and the expected vars. (covers SEP-1, SEP-2)
- `tests/s3_zip_manifest/sep/test_component.py::test_replayed_message_source_gone_lane`: functional: a replayed event with no source and no processing copy is skipped and creates nothing. (covers SEP-1, SEP-2)
- `tests/s3_zip_manifest/sep/test_sep_1.py::test_copy_to_processing_keeps_original_and_content`: critical: the package is copied to processing/ with identical content, original kept, vars set. (covers SEP-1, R-SEP-1-a, R-SEP-1-b)
- `tests/s3_zip_manifest/sep/test_sep_1.py::test_already_delivered_deletes_original_and_skips`: critical: a verified earlier delivery deletes the incoming original and marks skipped, no processing copy. (covers SEP-1, R-SEP-1-c)
- `tests/s3_zip_manifest/sep/test_sep_1.py::test_source_gone_resumes_or_skips`: important: source gone with a processing copy resumes; without one the package is skipped. (covers SEP-1, R-SEP-1-d)
- `tests/s3_zip_manifest/trn/test_component.py::test_happy_path_study_a`: functional: study_a goes from the processing copy to a staging zip with manifest.csv. (covers TRN-1, TRN-2)
- `tests/s3_zip_manifest/trn/test_component.py::test_zip_with_root_manifest_fails`: functional: study_c with a root manifest.csv fails and nothing is staged. (covers TRN-1, R-TRN-1-b)
- `tests/s3_zip_manifest/trn/test_trn_1.py::test_manifest_built_and_zip_rebuilt`: critical: the rebuilt zip holds the original files plus a correct manifest.csv. (covers TRN-1, R-TRN-1-a)
- `tests/s3_zip_manifest/trn/test_trn_1.py::test_root_manifest_fails`: important: a zip that already has a root manifest.csv raises DataError. (covers TRN-1, R-TRN-1-b)
- `tests/s3_zip_manifest/trn/test_trn_1.py::test_existing_staging_same_sha_is_reused`: important: an existing staging zip with the same source sha is reused, nothing rebuilt. (covers TRN-1)
- `tests/s3_zip_manifest/trn/test_trn_2.py::test_upload_to_staging_with_metadata`: critical: the rebuilt zip lands in staging with source metadata and staging_key is set. (covers TRN-2, R-TRN-2-a, R-TRN-2-b)
- `tests/s3_zip_manifest/trn/test_trn_2.py::test_staged_does_nothing`: important: when TRN-1 found the staging zip done, nothing is uploaded and the key is kept. (covers TRN-2)
- `tests/s3_zip_manifest/dlp/test_component.py::test_happy_path_study_a`: functional: happy_path_study_a delivers outgoing zip and index, and removes processing and staging. (covers DLP-1, DLP-2)
- `tests/s3_zip_manifest/dlp/test_component.py::test_outgoing_collision_not_overwritten`: functional: outgoing_collision_not_overwritten fails the package and keeps staging and processing. (covers DLP-1, R-DLP-1-a)
- `tests/s3_zip_manifest/dlp/test_dlp_1.py::test_delivers_copy_and_index`: critical: staging zip is copied to outgoing with same content and the index JSON is written. (covers DLP-1, R-DLP-1-a, R-DLP-1-c)
- `tests/s3_zip_manifest/dlp/test_dlp_1.py::test_existing_different_outgoing_not_overwritten`: important: a different existing outgoing object raises DataError and is left untouched, no index. (covers DLP-1, R-DLP-1-a)
- `tests/s3_zip_manifest/dlp/test_dlp_1.py::test_metadata_mismatch_raises`: important: outgoing without matching pkf-source metadata raises DataError and writes no index. (covers DLP-1, R-DLP-1-b)
- `tests/s3_zip_manifest/dlp/test_dlp_2.py::test_cleanup_deletes_processing_and_staging`: critical: with the outgoing object present, processing and staging copies are deleted, outgoing kept. (covers DLP-2)
- `tests/s3_zip_manifest/dlp/test_dlp_2.py::test_cleanup_keeps_copies_without_outgoing`: important: when the outgoing object is missing nothing is deleted. (covers DLP-2, R-DLP-2-a)
