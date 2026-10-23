from pathlib import Path

from pkf_runtime.kit import PackageContext, Runtime, naming, s3, unit_task


@unit_task("TRN-2")
def trn_2_upload_to_staging(ctx: PackageContext, rt: Runtime) -> PackageContext:
    """TRN-2: upload the rebuilt zip to staging with source metadata (R-TRN-2-a, R-TRN-2-b).

    Args:
        ctx: the package context.
        rt: the runtime.

    Returns:
        The context with artefacts.staging_key set.
    """
    if ctx.vars.get("staged"):
        rt.log.info("staging zip %s already present; upload skipped", ctx.artefacts.get("staging_key"))
        return ctx
    key = naming.check_key(
        f"{rt.settings['staging_prefix']}{ctx.vars['stem']}_{ctx.vars['package_ts']}.zip"
    )
    bucket = rt.settings["target_bucket"]
    s3.upload(
        rt.s3(),
        Path(ctx.artefacts["new_zip_path"]),
        bucket,
        key,
        if_exists="fail",
        metadata={
            "pkf-source-key": ctx.vars["source_key"],
            "pkf-source-sha256": ctx.vars["source_sha256"],
        },
    )
    rt.log.info("uploaded %s → s3://%s/%s", ctx.artefacts["new_zip_path"], bucket, key)
    ctx.artefacts["staging_key"] = key
    return ctx
