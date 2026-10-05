"""Atomic local evidence writes; existing immutable records cannot be replaced."""

import hashlib
import json
import os
import tempfile
from pathlib import Path


def json_bytes(value: dict) -> bytes:
    """Encode sorted, indented UTF-8 JSON with a trailing newline; reject nonfinite numbers."""
    # Canonical key order makes equal evidence produce equal hashes. Rejecting NaN
    # and infinity prevents nonstandard JSON values from masquerading as valid results.
    return json.dumps(value, sort_keys=True, indent=2, allow_nan=False).encode("utf-8") + b"\n"


def write_json(path: Path, value: dict, *, immutable: bool = False) -> None:
    """Atomically publish local evidence, optionally refusing changed existing content.

    Args:
        path: Destination; parent directories are created as needed.
        value: JSON-compatible evidence with finite numeric values.
        immutable: Use a same-directory hard link; identical existing content is allowed.

    Raises:
        ValueError: Existing immutable evidence differs, or JSON has nonfinite numbers.
        OSError: Filesystem publication fails; no partial final file is used as fallback.
    """
    # Write to a temporary file in the destination directory so publication stays
    # on one filesystem; readers see a complete old file or a complete new file.
    contents = json_bytes(value)
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(dir=path.parent, prefix=".evidence-")
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(contents)
            stream.flush()
            # Flush data before publishing the final name; atomic rename alone would not
            # force the written contents out of the process buffers.
            os.fsync(stream.fileno())
        if immutable:
            try:
                # A hard link publishes only if the final name is free. Unlike replacement, this
                # cannot overwrite a previously retained immutable snapshot.
                os.link(temporary, path)
            except FileExistsError:
                if path.read_bytes() != contents:
                    raise ValueError(f"Immutable evidence differs: {path}") from None
        else:
            # Mutable convenience pointers such as cloud.json may advance between runs;
            # the immutable source/run snapshots are stored under separate names.
            os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def save_snapshot(directory: Path, value: dict) -> Path:
    """Save a content-addressed, immutable JSON snapshot and return its path."""
    # Hash the serialized bytes, not an object identity or Git revision. The filename
    # therefore identifies the exact retained configuration/result payload.
    digest = hashlib.sha256(json_bytes(value)).hexdigest()
    path = directory / f"{digest}.json"
    write_json(path, value, immutable=True)
    return path


def read_snapshot(path: Path) -> dict:
    """Read a JSON snapshot after validating its SHA-256 filename against the contents.

    Raises:
        ValueError: The snapshot checksum does not match its filename stem.
    """
    # Verify bytes before decoding: valid JSON alone does not prove the retained
    # record is the one referenced by its content-addressed filename.
    contents = path.read_bytes()
    if hashlib.sha256(contents).hexdigest() != path.stem:
        raise ValueError(f"Snapshot checksum mismatch: {path}")
    return json.loads(contents)
