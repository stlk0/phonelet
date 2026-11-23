#!/usr/bin/env python3
"""Publish MP3 and <mp3>.status.json atomically; preserve WAV on any failure."""
import argparse
import fcntl
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile

# Installed standalone, so it cannot import phonelet. The <mp3>.status.json "status" values
# ("ready", "failed") are read by phonelet.recording as phonelet.constants.RecordingState.

DEFAULT_MIN_FREE_BYTES = 100 * 1024 * 1024  # space kept free beyond the WAV size


class ConversionFailed(Exception):
    """A recording that cannot be published; args[0] is the stored status reason."""


def sync_dir(path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def publish_json(path, payload):
    fd, name = tempfile.mkstemp(prefix=".status-", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump(payload, stream)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
        sync_dir(path.parent)
    finally:
        Path(name).unlink(missing_ok=True)


def is_published(mp3, status):
    """True when a non-empty MP3 and a 'ready' marker are both in place."""
    if not (mp3.is_file() and mp3.stat().st_size and status.is_file()):
        return False
    try:
        return json.loads(status.read_text()).get("status") == "ready"
    except (OSError, ValueError):
        return False


def convert(wav, root, min_free_bytes=DEFAULT_MIN_FREE_BYTES, encoder="/usr/bin/lame"):
    root = Path(root).resolve(strict=True)
    wav = Path(wav)
    # No attacker-controlled shell interpolation; refuse symlinks/escaped paths.
    if wav.is_symlink() or not wav.resolve().is_relative_to(root) or wav.suffix != ".wav":
        raise ValueError("Recording must be a WAV beneath the recording root")
    wav = wav.resolve()
    mp3 = wav.with_suffix(".mp3")
    status = Path(str(mp3) + ".status.json")
    if mp3.is_symlink() or status.is_symlink():
        raise ValueError("Recording outputs must not be symlinks")
    lock_fd = os.open(str(mp3) + ".lock", os.O_CREAT | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
    with os.fdopen(lock_fd, "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if is_published(mp3, status):
            return True
        temporary = None
        try:
            if not wav.is_file() or wav.stat().st_size == 0:
                raise ConversionFailed("missing_input")
            if shutil.disk_usage(root).free < min_free_bytes + wav.stat().st_size:
                raise ConversionFailed("low_disk_space")
            fd, temporary = tempfile.mkstemp(prefix=".encoding-", suffix=".mp3", dir=mp3.parent)
            os.close(fd)
            subprocess.run([encoder, "-b", "32", "--silent", str(wav), temporary],
                           check=True, timeout=3600, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            if Path(temporary).stat().st_size == 0:
                raise ConversionFailed("empty_output")
            with open(temporary, "rb") as stream:
                os.fsync(stream.fileno())
            os.replace(temporary, mp3)
            sync_dir(mp3.parent)
            publish_json(status, {"status": "ready"})
            wav.unlink()
            sync_dir(wav.parent)
            return True
        except ConversionFailed as exc:
            publish_json(status, {"status": "failed", "error": exc.args[0]})
            return False
        except (OSError, ValueError, subprocess.SubprocessError):
            publish_json(status, {"status": "failed", "error": "conversion_failed"})
            return False
        finally:
            if temporary:
                Path(temporary).unlink(missing_ok=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("wav", nargs="?")
    parser.add_argument("--check-space", action="store_true")
    parser.add_argument("--root", required=True)
    parser.add_argument("--min-free-bytes", type=int, default=DEFAULT_MIN_FREE_BYTES)
    args = parser.parse_args()
    if args.min_free_bytes <= 0:
        parser.error("min-free-bytes must be positive")
    if args.check_space:
        raise SystemExit(0 if shutil.disk_usage(args.root).free >= args.min_free_bytes else 1)
    if not args.wav:
        parser.error("wav is required for conversion")
    raise SystemExit(0 if convert(args.wav, args.root, args.min_free_bytes) else 1)
