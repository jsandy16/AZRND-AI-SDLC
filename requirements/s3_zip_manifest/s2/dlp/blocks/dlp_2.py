from pkf_runtime.kit import PackageContext, Runtime, s3, unit_task


@unit_task("DLP-2")
def dlp_2_clean_up(ctx: PackageContext, rt: Runtime) -> PackageContext:
    """DLP-2: delete the processing and staging copies once the outgoing object exists (R-DLP-2-a).

    Args:
        ctx: The package context (artefacts.outgoing_key, renamed_key, staging_key).
        rt: The runtime.

    Returns:
        The context, unchanged.
    """
    client = rt.s3()
    sb = rt.settings["source_bucket"]
    tb = rt.settings["target_bucket"]
    out_key = ctx.artefacts["outgoing_key"]
    if s3.head(client, tb, out_key) is None:
        rt.log.warning("outgoing object %s not found in %s; keeping processing and staging copies", out_key, tb)
        return ctx
    renamed_key = ctx.artefacts["renamed_key"]
    s3.delete(client, sb, renamed_key, missing_ok=True)
    rt.log.info("deleted processing copy s3://%s/%s", sb, renamed_key)
    staging_key = ctx.artefacts["staging_key"]
    s3.delete(client, tb, staging_key, missing_ok=True)
    rt.log.info("deleted staging copy s3://%s/%s", tb, staging_key)
    return ctx
