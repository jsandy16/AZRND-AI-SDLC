import json
from typing import Any

from pkf_runtime.kit import DataError, PackageContext, Runtime, naming, s3, unit_task


def _index_key(rt: Runtime, stem: str, source_sha256: str, source_key_hash: str) -> str:
    """Return the delivery index key for a package.

    Args:
        rt: The runtime (for the index_prefix setting).
        stem: The package stem.
        source_sha256: SHA-256 of the source package.
        source_key_hash: Short hash of the source key.

    Returns:
        The checked index key.
    """
    prefix = rt.settings["index_prefix"]
    return naming.check_key(f"{prefix}{stem}/{source_sha256}_{source_key_hash}.json")


def _check_source_metadata(meta: dict[str, Any], ctx: PackageContext, out_key: str) -> None:
    """Check that the outgoing object carries the package's source metadata.

    Args:
        meta: The outgoing object's user metadata.
        ctx: The package context.
        out_key: The outgoing key (for the message).

    Raises:
        DataError: When pkf-source-key or pkf-source-sha256 differs from the vars.
    """
    if (meta.get("pkf-source-key") != ctx.vars.get("source_key")
            or meta.get("pkf-source-sha256") != ctx.vars.get("source_sha256")):
        raise DataError(f"outgoing object {out_key} lacks matching pkf-source-key/pkf-source-sha256 metadata")


@unit_task("DLP-1")
def dlp_1_deliver_to_outgoing(ctx: PackageContext, rt: Runtime) -> PackageContext:
    """DLP-1: deliver the staging zip to outgoing and write the delivery index (R-DLP-1-a, R-DLP-1-b, R-DLP-1-c).

    Args:
        ctx: The package context (artefacts.staging_key, vars.package_ts/stem/source_key/source_sha256/source_key_hash).
        rt: The runtime.

    Returns:
        The context with artefacts.outgoing_key and artefacts.index_key.

    Raises:
        DataError: When a different outgoing object exists or the metadata does not match.
    """
    client = rt.s3()
    tb = rt.settings["target_bucket"]
    stem = ctx.vars["stem"]
    package_ts = ctx.vars["package_ts"]
    out_key = naming.check_key(f"{rt.settings['outgoing_prefix']}{package_ts}_{stem}.zip")
    staging_key = ctx.artefacts["staging_key"]
    result = s3.verified_copy(client, tb, staging_key, tb, out_key, if_exists="skip_identical")
    rt.log.info("delivered %s → %s (skipped=%s)", staging_key, out_key, bool(result.get("skipped")))
    head = s3.head(client, tb, out_key)
    if head is None:
        raise DataError(f"outgoing object {out_key} not found after copy")
    _check_source_metadata(head.get("Metadata") or head.get("metadata") or {}, ctx, out_key)
    idx = {
        "source_key": ctx.vars["source_key"],
        "source_sha256": ctx.vars["source_sha256"],
        "outgoing_key": out_key,
        "outgoing_sha256": s3.sha256_of(client, tb, out_key),
        "package_ts": package_ts,
    }
    path = rt.tmpdir() / "idx.json"
    path.write_text(json.dumps(idx, sort_keys=True), encoding="utf-8")
    ik = _index_key(rt, stem, ctx.vars["source_sha256"], ctx.vars["source_key_hash"])
    s3.upload(client, path, tb, ik, if_exists="skip_identical")
    rt.log.info("wrote delivery index %s", ik)
    ctx.artefacts["outgoing_key"] = out_key
    ctx.artefacts["index_key"] = ik
    return ctx
