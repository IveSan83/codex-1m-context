#!/usr/bin/env python3
"""Set local Codex client metadata to 1M; does not change service limits."""
import sys

if sys.version_info < (3, 11):
    print("Error: Python 3.11 or newer is required. Install it, then run this script again.", file=sys.stderr)
    raise SystemExit(1)

import argparse
from contextlib import contextmanager
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import tempfile
import tomllib
import uuid

TOOL = "codex-1m-context-v1"
STATE = "codex-1m-context"
LIMITS = {"context_window": 1_000_000, "max_context_window": 1_000_000,
          "effective_context_window_percent": 100, "auto_compact_token_limit": 900_000}
HEX = re.compile(r"[0-9a-f]{64}\Z")
ABSENT = object()


class Error(Exception):
    pass


def digest(data):
    return hashlib.sha256(data).hexdigest() if data is not None else None


def safe(path, home):
    """Reject links/junctions inside the selected, already resolved home."""
    if ".." in path.parts or not path.is_relative_to(home):
        raise Error("Path is outside the selected Codex home.")
    for item in (home, *[home.joinpath(*path.relative_to(home).parts[:i])
                         for i in range(1, len(path.relative_to(home).parts) + 1)]):
        try:
            info = item.lstat()
        except FileNotFoundError:
            continue
        if item.is_symlink() or getattr(info, "st_file_attributes", 0) & 0x400:
            raise Error("Refusing a symbolic link or junction inside Codex home.")
    return path


def read(path, home):
    safe(path, home)
    return path.read_bytes() if path.exists() else None


def atomic(path, data, expected, home):
    """Replace a single file after an immediate byte comparison guard."""
    safe(path, home)
    fd, temporary = tempfile.mkstemp(prefix=".1m-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        if read(path, home) != expected:
            raise Error("A file changed during preparation; it was not overwritten.")
        if expected is None:
            # Hard-link creation is atomic and cannot replace a racing file.
            os.link(temporary, path)
        else:
            os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


@contextmanager
def locked(home):
    state = safe(home / STATE, home)
    state.mkdir(mode=0o700, exist_ok=True)
    backups = safe(state / "backups", home)
    backups.mkdir(mode=0o700, exist_ok=True)
    lock = safe(state / ".lock", home)
    if lock.exists() and (not stat.S_ISREG(lock.stat().st_mode) or lock.stat().st_nlink != 1):
        raise Error("Refusing a nonregular or hard-linked lock file.")
    flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)
    with os.fdopen(os.open(lock, flags, 0o600), "r+b", buffering=0) as stream:
        def verify():
            safe(lock, home)
            opened, current = os.fstat(stream.fileno()), lock.stat()
            if (not stat.S_ISREG(opened.st_mode) or opened.st_nlink != 1
                    or (opened.st_dev, opened.st_ino) != (current.st_dev, current.st_ino)):
                raise Error("Lock file changed or is unsafe; no configuration changed.")
            return opened

        verify()
        stream.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise Error("Another run holds the Codex home lock; retry when it finishes.") from None
        try:
            if verify().st_size == 0:
                stream.write(b"\0")  # Initialize only while holding the OS lock.
                os.fsync(stream.fileno())
            yield state
        finally:
            stream.seek(0)
            if os.name == "nt":
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
        # Never unlink: all future runs must lock the same persistent file.


def parsed(data):
    try:
        return tomllib.loads(data.decode("utf-8-sig"))
    except (UnicodeError, tomllib.TOMLDecodeError):
        raise Error("Invalid config.toml; fix its UTF-8/TOML before retrying.") from None


def wanted(catalog):
    return {"model_catalog_json": catalog.as_posix(), "model_context_window": 1_000_000,
            "model_auto_compact_token_limit": 900_000}


def matches(config, values):
    return all((key not in config if value is ABSENT else
                key in config and type(config[key]) is type(value) and config[key] == value)
               for key, value in values.items())


def edit_config(data, values):
    before = parsed(data)
    expected = dict(before)
    for key, value in values.items():
        if value is ABSENT:
            expected.pop(key, None)
        else:
            expected[key] = value
    if matches(before, values):
        return data
    text = data.decode("utf-8-sig")
    newline = "\r\n" if "\r\n" in text else "\n"
    lines, output, seen = text.splitlines(keepends=True), [], set()
    i = 0
    while i < len(lines):
        line = lines[i].lstrip()
        if line.startswith("["):
            break
        if not line.strip() or line.startswith("#"):
            output.append(lines[i])
            i += 1
            continue
        # Parse complete root statements, including multiline strings/arrays.
        for end in range(i + 1, len(lines) + 1):
            try:
                statement = tomllib.loads("".join(lines[i:end]))
                break
            except tomllib.TOMLDecodeError:
                continue
        else:
            raise Error("Cannot safely identify root TOML statements; no files changed.")
        keys = set(statement) & values.keys()
        if keys:
            if len(statement) != 1 or len(keys) != 1:
                raise Error("Unsupported root TOML keys; no files changed.")
            key = keys.pop()
            if type(statement[key]) not in (str, int, float, bool):
                raise Error("Unsupported target root value; no files changed.")
            # Reject values that could not be serialized back during restore.
            try:
                json.dumps(statement[key], allow_nan=False)
            except ValueError:
                raise Error("Unsupported target root value; no files changed.") from None
            seen.add(key)
            if values[key] is ABSENT:
                pass
            elif matches(statement, {key: values[key]}):
                output.extend(lines[i:end])
            else:
                last = lines[end - 1]
                ending = "\r\n" if last.endswith("\r\n") else "\n" if last.endswith("\n") else ""
                comment = ""
                for position, char in enumerate(last):
                    if char == "#":
                        try:
                            tomllib.loads("".join(lines[i:end - 1]) + last[:position])
                            comment = " " + last[position:].rstrip("\r\n")
                            break
                        except tomllib.TOMLDecodeError:
                            pass
                output.append(key + " = " + json.dumps(values[key], ensure_ascii=False) + comment + ending)
        else:
            output.extend(lines[i:end])
        i = end
    missing = [key for key in values if key not in seen and values[key] is not ABSENT]
    if missing:
        if output and not output[-1].endswith("\n"):
            output.append(newline)
        output.extend(key + " = " + json.dumps(values[key], ensure_ascii=False) + newline
                      for key in missing)
    output.extend(lines[i:])
    prefix = b"\xef\xbb\xbf" if data.startswith(b"\xef\xbb\xbf") else b""
    result = prefix + "".join(output).encode("utf-8")
    after = parsed(result)
    if after != expected or not matches(after, values):
        raise Error("TOML validation found an unexpected change; no files changed.")
    return result


def catalog_bytes(cache):
    def invalid_constant(_):
        raise ValueError()

    def unique_keys(pairs):
        result = {}
        for key, item in pairs:
            if key in result:
                raise ValueError()
            result[key] = item
        return result

    try:
        value = json.loads(cache.decode("utf-8-sig"), parse_constant=invalid_constant,
                           object_pairs_hook=unique_keys)
        models = value["models"]
        if not isinstance(models, list) or not models or any(
                not isinstance(row, dict) or not isinstance(row.get("slug"), str)
                or not row["slug"] for row in models):
            raise ValueError()
        for row in models:
            row.update(LIMITS)
        return (json.dumps({"models": models}, ensure_ascii=False, indent=2,
                           allow_nan=False) + "\n").encode("utf-8"), len(models)
    except (UnicodeError, ValueError, KeyError, TypeError):
        raise Error("Invalid models_cache.json. Open Codex once to refresh its local model cache, then retry.") from None


def manifest(path, home):
    state = home / STATE
    if path.name != "manifest.json" or path.parent.parent != state / "backups":
        raise Error("Select a manifest.json from this tool's local backups directory.")
    raw = read(path, home)
    try:
        info = json.loads(raw)
        valid = (info["tool"] == TOOL and info["home"] == str(home)
                 and info["status"] in {"prepared", "applied", "restored"}
                 and (info["before_sha256"] is None or HEX.fullmatch(info["before_sha256"]))
                 and HEX.fullmatch(info["after_sha256"]) and HEX.fullmatch(info["backup_sha256"])
                 and HEX.fullmatch(info["catalog_sha256"])
                 and info["catalog"] == "catalog-" + info["catalog_sha256"] + ".json"
                 and type(info["catalog_created"]) is bool)
        if not valid:
            raise ValueError()
    except (ValueError, KeyError, TypeError):
        raise Error("Invalid or foreign backup manifest; no files changed.") from None
    return info, raw


def manifests(home):
    paths = sorted((home / STATE / "backups").glob("*/manifest.json"), reverse=True)
    return [(path, *manifest(path, home)) for path in paths]


def enable(home, dry_run=False):
    config, cache_path = home / "config.toml", home / "models_cache.json"
    original, cache = read(config, home), read(cache_path, home)
    if cache is None:
        raise Error("models_cache.json is missing. Open Codex once to create its local model cache, then retry.")
    data, count = catalog_bytes(cache)
    catalog = home / STATE / ("catalog-" + digest(data) + ".json")
    candidate = edit_config(original or b"", wanted(catalog))
    existing = read(catalog, home)
    if existing is not None and (existing != data or not any(
            info["catalog"] == catalog.name and info["catalog_created"]
            and (info["status"] != "restored" or info.get("catalog_retained") is True)
            for _, info, _ in manifests(home))):
        raise Error("Generated catalog path already exists but is changed or unowned; no files changed.")
    result = {"changed": candidate != original or existing is None, "models": count,
              "config": str(config), "catalog": str(catalog), "dry_run": dry_run}
    if dry_run or not result["changed"]:
        return result
    with locked(home) as state:
        if read(config, home) != original or read(cache_path, home) != cache:
            raise Error("Configuration or model cache changed during preparation; retry.")
        if read(catalog, home) != existing:
            raise Error("Generated catalog changed during preparation; retry.")
        stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        backup = state / "backups" / (stamp + "-" + uuid.uuid4().hex)
        backup.mkdir(mode=0o700)
        atomic(backup / "config.before.toml", original or b"", None, home)
        info = {"tool": TOOL, "home": str(home), "status": "prepared",
                "before_sha256": digest(original), "after_sha256": digest(candidate),
                "backup_sha256": digest(original or b""), "catalog": catalog.name,
                "catalog_sha256": digest(data), "catalog_created": False}
        record = backup / "manifest.json"
        raw = (json.dumps(info, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
        atomic(record, raw, None, home)
        if existing is None:
            atomic(catalog, data, None, home)
            info["catalog_created"] = True
            owned = (json.dumps(info, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
            atomic(record, owned, raw, home)
            raw = owned
        atomic(config, candidate, original, home)
        info["status"] = "applied"
        atomic(record, (json.dumps(info, ensure_ascii=False, indent=2) + "\n").encode("utf-8"), raw, home)
        result["backup"] = str(record)
    return result


def restore(home, selection="latest", dry_run=False):
    if selection == "latest":
        current_digest = digest(read(home / "config.toml", home))
        records = [(path, info, raw) for path, info, raw in manifests(home)
                   if info["status"] == "applied" or (info["status"] == "prepared"
                                                      and info["after_sha256"] == current_digest)]
        if not records:
            raise Error("No unrestored backup manifest exists for this Codex home.")
        record, info, raw = records[0]
    else:
        record = Path(selection).expanduser().absolute()
        info, raw = manifest(record, home)
    config, catalog = home / "config.toml", home / STATE / info["catalog"]
    before = read(record.parent / "config.before.toml", home)
    if digest(before) != info["backup_sha256"]:
        raise Error("Backup changed or is missing; restore refused.")
    original = before if info["before_sha256"] is not None else None
    if (original is None and before != b"") or digest(original) != info["before_sha256"] or digest(
            edit_config(before, wanted(catalog))) != info["after_sha256"]:
        raise Error("Backup manifest does not match the expected configuration change.")
    current = read(config, home)
    previous = {key: parsed(before).get(key, ABSENT) for key in wanted(catalog)}
    if info["status"] == "restored":
        if matches(parsed(current or b""), previous):
            return {"restored": False, "changed": False, "config": str(config), "dry_run": dry_run}
        raise Error("This backup was already restored; managed root keys have later edits.")
    if not matches(parsed(current or b""), wanted(catalog)):
        raise Error("Managed root keys have later edits; restore refused to overwrite them.")
    candidate = original if digest(current) == info["after_sha256"] else edit_config(current, previous)
    data = read(catalog, home)
    if digest(data) != info["catalog_sha256"]:
        raise Error("Generated catalog changed or is missing; restore refused.")
    def references(value):
        if isinstance(value, dict):
            return any(references(item) for item in value.values())
        if isinstance(value, list):
            return any(references(item) for item in value)
        if not isinstance(value, str):
            return False
        normalized = os.path.normcase(os.path.normpath(value.replace("\\", "/")))
        # Retain conservatively for relative/profile paths and Windows case aliases.
        return (normalized == os.path.normcase(os.path.normpath(str(catalog)))
                or os.path.basename(normalized) == os.path.normcase(catalog.name))

    delete = info["catalog_created"] and not references(parsed(candidate or b""))
    if not dry_run:
        with locked(home):
            if (read(record, home) != raw or read(catalog, home) != data
                    or read(record.parent / "config.before.toml", home) != before):
                raise Error("Restore files changed during preparation; retry.")
            if candidate is None:
                if read(config, home) != current:
                    raise Error("Configuration changed during preparation; it was not removed.")
                config.unlink()
            else:
                atomic(config, candidate, current, home)
            if delete and read(catalog, home) == data:
                catalog.unlink()
            info["status"] = "restored"
            info["catalog_retained"] = not delete
            atomic(record, (json.dumps(info, ensure_ascii=False, indent=2) + "\n").encode("utf-8"), raw, home)
    return {"restored": not dry_run, "changed": True, "config": str(config),
            "backup": str(record), "dry_run": dry_run}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--home", type=Path, help="Codex home (default: CODEX_HOME or ~/.codex)")
    parser.add_argument("--dry-run", action="store_true", help="validate and report without writing")
    parser.add_argument("--restore", nargs="?", const="latest", metavar="MANIFEST",
                        help="restore latest or a selected local manifest")
    args = parser.parse_args(argv)
    home = (args.home or Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex")).expanduser().resolve()
    try:
        result = restore(home, args.restore, args.dry_run) if args.restore else enable(home, args.dry_run)
        # ASCII JSON also prints safely on consoles with a legacy encoding.
        print(json.dumps(result, ensure_ascii=True))
        return 0
    except (Error, OSError) as error:
        print("Error: " + str(error).encode("ascii", "backslashreplace").decode("ascii"), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
