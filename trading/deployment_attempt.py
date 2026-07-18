#!/usr/bin/python3 -I
"""Write-once, root-owned deployment-attempt phase journal."""

import argparse
import hashlib
import json
import os
import re
import stat
import sys


SHA40_RE = re.compile(r"^[0-9a-f]{40}$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
ATTEMPT_RE = re.compile(r"^[0-9]{4}$")
FACT_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
PHASES = (
    "PREPARED",
    "QUIESCED",
    "T0",
    "VALIDATED",
    "SEALED",
    "COMMIT_READY",
)
ROOT_UID = 0
ROOT_GID = 0


class JournalError(RuntimeError):
    pass


def _canonical(payload):
    return (json.dumps(
        payload, ensure_ascii=True, sort_keys=True,
        separators=(",", ":"), allow_nan=False) + "\n").encode("ascii")


def _digest(payload):
    return hashlib.sha256(_canonical(payload)).hexdigest()


def _validate_inputs(attempt_dir, release_sha, attempt_id, driver_sha256):
    if os.geteuid() != ROOT_UID:
        raise JournalError("phase journal must run as root")
    if not os.path.isabs(attempt_dir):
        raise JournalError("attempt-dir must be absolute")
    if not SHA40_RE.fullmatch(release_sha):
        raise JournalError("invalid release SHA")
    if not ATTEMPT_RE.fullmatch(attempt_id):
        raise JournalError("invalid attempt id")
    if not SHA256_RE.fullmatch(driver_sha256):
        raise JournalError("invalid driver SHA-256")


def _open_attempt_dir(path):
    before = os.lstat(path)
    if (not stat.S_ISDIR(before.st_mode) or stat.S_IMODE(before.st_mode) != 0o700 or
            before.st_uid != ROOT_UID or before.st_gid != ROOT_GID):
        raise JournalError("attempt directory must be root:root 0700")
    fd = os.open(
        path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) |
        getattr(os, "O_NOFOLLOW", 0))
    after = os.fstat(fd)
    if ((before.st_dev, before.st_ino) != (after.st_dev, after.st_ino) or
            not stat.S_ISDIR(after.st_mode)):
        os.close(fd)
        raise JournalError("attempt directory changed while opening")
    return fd


def _read_entry(dir_fd, name):
    before = os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
    if (not stat.S_ISREG(before.st_mode) or stat.S_IMODE(before.st_mode) != 0o600 or
            before.st_uid != ROOT_UID or before.st_gid != ROOT_GID or
            before.st_nlink != 1):
        raise JournalError(f"unsafe journal entry: {name}")
    fd = os.open(
        name, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0), dir_fd=dir_fd)
    try:
        after = os.fstat(fd)
        if (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino):
            raise JournalError(f"journal entry changed while opening: {name}")
        chunks = []
        while True:
            chunk = os.read(fd, 65536)
            if not chunk:
                break
            chunks.append(chunk)
            if sum(map(len, chunks)) > 131072:
                raise JournalError("journal entry is too large")
        try:
            return json.loads(b"".join(chunks))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise JournalError(f"invalid journal JSON: {name}") from exc
    finally:
        os.close(fd)


def _write_once(dir_fd, name, payload):
    data = _canonical(payload)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(name, flags, 0o600, dir_fd=dir_fd)
    try:
        os.fchmod(fd, 0o600)
        offset = 0
        while offset < len(data):
            written = os.write(fd, data[offset:])
            if written <= 0:
                raise JournalError("journal write made no progress")
            offset += written
        os.fsync(fd)
    finally:
        os.close(fd)
    os.fsync(dir_fd)


def _phase_name(sequence, phase):
    return f"phase-{sequence:02d}-{phase.lower()}.json"


def _parse_facts(values):
    result = {}
    for value in values:
        key, separator, item = value.partition("=")
        if not separator or not FACT_RE.fullmatch(key) or key in result:
            raise JournalError("facts must be unique lower-case key=value pairs")
        if not item or len(item) > 1024 or "\x00" in item:
            raise JournalError("invalid fact value")
        result[key] = item
    return result


def _validate_entry(payload, release_sha, attempt_id, driver_sha256,
                    sequence, phase, previous_sha256):
    expected = {
        "schema_version", "release_sha", "attempt_id", "driver_sha256",
        "sequence", "phase", "previous_sha256", "facts",
    }
    if not isinstance(payload, dict) or set(payload) != expected:
        raise JournalError("journal schema is not exact")
    if payload != {
            **payload,
            "schema_version": 1,
            "release_sha": release_sha,
            "attempt_id": attempt_id,
            "driver_sha256": driver_sha256,
            "sequence": sequence,
            "phase": phase,
            "previous_sha256": previous_sha256}:
        raise JournalError("journal binding mismatch")
    facts = payload["facts"]
    if (not isinstance(facts, dict) or
            any(not isinstance(k, str) or not FACT_RE.fullmatch(k) or
                not isinstance(v, str) or not v or len(v) > 1024 or "\x00" in v
                for k, v in facts.items())):
        raise JournalError("invalid journal facts")


def _load_chain(dir_fd, release_sha, attempt_id, driver_sha256):
    names = os.listdir(dir_fd)
    unknown = [name for name in names if name.startswith("phase-") and
               name not in {_phase_name(i, phase)
                            for i, phase in enumerate(PHASES)}]
    if unknown:
        raise JournalError("unknown phase journal entry")
    chain = []
    previous = None
    gap = False
    for sequence, phase in enumerate(PHASES):
        name = _phase_name(sequence, phase)
        if name not in names:
            gap = True
            continue
        if gap:
            raise JournalError("phase journal has a gap")
        payload = _read_entry(dir_fd, name)
        _validate_entry(
            payload, release_sha, attempt_id, driver_sha256,
            sequence, phase, previous)
        previous = _digest(payload)
        chain.append(payload)
    if "abandoned.json" in names:
        abandoned = _read_entry(dir_fd, "abandoned.json")
        expected = {
            "schema_version", "release_sha", "attempt_id", "driver_sha256",
            "last_phase", "last_phase_sha256", "reason",
        }
        if (not isinstance(abandoned, dict) or set(abandoned) != expected or
                abandoned["schema_version"] != 1 or
                abandoned["release_sha"] != release_sha or
                abandoned["attempt_id"] != attempt_id or
                abandoned["driver_sha256"] != driver_sha256 or
                abandoned["last_phase"] != (chain[-1]["phase"] if chain else None) or
                abandoned["last_phase_sha256"] != previous or
                not isinstance(abandoned["reason"], str) or
                not abandoned["reason"] or len(abandoned["reason"]) > 1024):
            raise JournalError("invalid abandoned journal entry")
    return chain, previous, "abandoned.json" in names


def init_journal(attempt_dir, release_sha, attempt_id, driver_sha256):
    _validate_inputs(attempt_dir, release_sha, attempt_id, driver_sha256)
    dir_fd = _open_attempt_dir(attempt_dir)
    try:
        chain, _, abandoned = _load_chain(
            dir_fd, release_sha, attempt_id, driver_sha256)
        if abandoned:
            raise JournalError("attempt is abandoned")
        payload = {
            "schema_version": 1,
            "release_sha": release_sha,
            "attempt_id": attempt_id,
            "driver_sha256": driver_sha256,
            "sequence": 0,
            "phase": "PREPARED",
            "previous_sha256": None,
            "facts": {},
        }
        if chain:
            raise JournalError("attempt already started; use recover-deployment")
        _write_once(dir_fd, _phase_name(0, "PREPARED"), payload)
        return payload
    finally:
        os.close(dir_fd)


def advance_journal(attempt_dir, release_sha, attempt_id, driver_sha256,
                    expected, next_phase, facts):
    _validate_inputs(attempt_dir, release_sha, attempt_id, driver_sha256)
    if expected not in PHASES or next_phase not in PHASES:
        raise JournalError("unknown phase")
    if PHASES.index(next_phase) != PHASES.index(expected) + 1:
        raise JournalError("phase transition is not consecutive")
    dir_fd = _open_attempt_dir(attempt_dir)
    try:
        chain, previous, abandoned = _load_chain(
            dir_fd, release_sha, attempt_id, driver_sha256)
        if abandoned or not chain or chain[-1]["phase"] != expected:
            raise JournalError("current phase does not match --expect")
        sequence = PHASES.index(next_phase)
        payload = {
            "schema_version": 1,
            "release_sha": release_sha,
            "attempt_id": attempt_id,
            "driver_sha256": driver_sha256,
            "sequence": sequence,
            "phase": next_phase,
            "previous_sha256": previous,
            "facts": _parse_facts(facts),
        }
        _write_once(dir_fd, _phase_name(sequence, next_phase), payload)
        return payload
    finally:
        os.close(dir_fd)


def abandon_journal(attempt_dir, release_sha, attempt_id, driver_sha256, reason):
    _validate_inputs(attempt_dir, release_sha, attempt_id, driver_sha256)
    if not reason or len(reason) > 1024 or "\x00" in reason:
        raise JournalError("invalid abandon reason")
    dir_fd = _open_attempt_dir(attempt_dir)
    try:
        chain, previous, abandoned = _load_chain(
            dir_fd, release_sha, attempt_id, driver_sha256)
        payload = {
            "schema_version": 1,
            "release_sha": release_sha,
            "attempt_id": attempt_id,
            "driver_sha256": driver_sha256,
            "last_phase": chain[-1]["phase"] if chain else None,
            "last_phase_sha256": previous,
            "reason": reason,
        }
        if abandoned:
            if _read_entry(dir_fd, "abandoned.json") != payload:
                raise JournalError("attempt was abandoned with different evidence")
            return payload
        _write_once(dir_fd, "abandoned.json", payload)
        return payload
    finally:
        os.close(dir_fd)


def _common(child):
    child.add_argument("--attempt-dir", required=True)
    child.add_argument("--release-sha", required=True)
    child.add_argument("--attempt-id", required=True)
    child.add_argument("--driver-sha256", required=True)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    init = commands.add_parser("init")
    _common(init)
    advance = commands.add_parser("advance")
    _common(advance)
    advance.add_argument("--expect", required=True, choices=PHASES)
    advance.add_argument("--next", required=True, choices=PHASES)
    advance.add_argument("--fact", action="append", default=[])
    abandon = commands.add_parser("abandon")
    _common(abandon)
    abandon.add_argument("--reason", required=True)
    status = commands.add_parser("status")
    _common(status)
    args = parser.parse_args(argv)
    try:
        if args.command == "init":
            payload = init_journal(
                args.attempt_dir, args.release_sha, args.attempt_id,
                args.driver_sha256)
        elif args.command == "advance":
            payload = advance_journal(
                args.attempt_dir, args.release_sha, args.attempt_id,
                args.driver_sha256, args.expect, args.next, args.fact)
        elif args.command == "abandon":
            payload = abandon_journal(
                args.attempt_dir, args.release_sha, args.attempt_id,
                args.driver_sha256, args.reason)
        else:
            _validate_inputs(
                args.attempt_dir, args.release_sha, args.attempt_id,
                args.driver_sha256)
            dir_fd = _open_attempt_dir(args.attempt_dir)
            try:
                chain, digest, abandoned_state = _load_chain(
                    dir_fd, args.release_sha, args.attempt_id,
                    args.driver_sha256)
            finally:
                os.close(dir_fd)
            payload = {
                "phase": chain[-1]["phase"] if chain else None,
                "phase_sha256": digest,
                "abandoned": abandoned_state,
            }
        print(json.dumps(payload, sort_keys=True, separators=(",", ":")))
        return 0
    except (JournalError, OSError) as exc:
        print(f"deployment phase journal blocked: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
