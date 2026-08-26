import contextlib
import os
import subprocess
import tempfile
import time

import zstandard as zstd

from openpilot.cereal import log as capnp_log
from openpilot.common.utils import atomic_write
from openpilot.common.swaglog import cloudlog
from openpilot.system.loggerd.xattr_cache import getxattr, setxattr

# marks a file as having already been checked/stripped for mic audio, so repeat
# uploader passes over an already-handled segment are a single cheap getxattr call
AUDIO_PROCESSED_ATTR_NAME = 'user.audio_processed'
AUDIO_PROCESSED_ATTR_VALUE = b'1'

# unix timestamp (as ascii) before which a failed segment won't be retried - written to disk
# (rather than kept in memory) so the cooldown is shared across the "uploader" and
# "sunnylink_uploader" processes, which both call process_segment_audio() independently
AUDIO_RETRY_AFTER_ATTR_NAME = 'user.audio_retry_after'
RETRY_COOLDOWN_SECONDS = 300

# both uploader processes race to process the same segments; this lock file (named so it's
# picked up by the callers' existing "any .lock file present -> skip this segment" check)
# ensures only one of them runs ffmpeg on a given segment at a time. A lock left behind by a
# crash is cleared by clear_locks() on the next uploader startup, or reclaimed here if stale.
AUDIO_LOCK_FILENAME = "audio_processing.lock"
LOCK_STALE_SECONDS = 300

QCAMERA_FILENAME = "qcamera.ts"
AUDIO_FILENAME = "audio.m4a"
STRIPPED_LOG_FILENAMES = ("rlog.zst", "qlog.zst")

# nothing in this set may be uploaded from a segment until process_segment_audio()
# has returned True for it, since any of these could still carry mic audio
AUDIO_GATED_FILENAMES = frozenset({QCAMERA_FILENAME, *STRIPPED_LOG_FILENAMES})


def _is_processed(path: str) -> bool:
  return getxattr(path, AUDIO_PROCESSED_ATTR_NAME) == AUDIO_PROCESSED_ATTR_VALUE


def _mark_processed(path: str) -> None:
  setxattr(path, AUDIO_PROCESSED_ATTR_NAME, AUDIO_PROCESSED_ATTR_VALUE)


def _retry_is_due(qcam_path: str) -> bool:
  raw = getxattr(qcam_path, AUDIO_RETRY_AFTER_ATTR_NAME)
  if raw is None:
    return True
  try:
    return time.time() >= float(raw)
  except ValueError:
    return True


def _set_retry_backoff(qcam_path: str) -> None:
  with contextlib.suppress(OSError):
    setxattr(qcam_path, AUDIO_RETRY_AFTER_ATTR_NAME, str(time.time() + RETRY_COOLDOWN_SECONDS).encode())


def _try_acquire_lock(lock_path: str) -> bool:
  try:
    os.close(os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY))
    return True
  except FileExistsError:
    pass
  except OSError:
    cloudlog.exception(f"failed to create audio processing lock {lock_path}")
    return False

  try:
    if time.time() - os.path.getmtime(lock_path) < LOCK_STALE_SECONDS:
      return False
    os.unlink(lock_path)
    os.close(os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY))
    return True
  except OSError:
    return False


def _stripped_logs_done(segment_path: str) -> bool:
  for log_filename in STRIPPED_LOG_FILENAMES:
    log_path = os.path.join(segment_path, log_filename)
    if os.path.exists(log_path) and not _is_processed(log_path):
      return False
  return True


def _has_audio_stream(path: str) -> bool:
  try:
    result = subprocess.run(
      ["ffprobe", "-i", path, "-show_streams", "-select_streams", "a", "-loglevel", "error"],
      capture_output=True, timeout=30,
    )
  except (OSError, subprocess.TimeoutExpired):
    cloudlog.exception(f"ffprobe failed on {path}")
    return False
  return result.returncode == 0 and result.stdout.strip() != b''


def _run_ffmpeg_to_temp(segment_path: str, suffix: str, args: list[str]) -> str | None:
  """Runs ffmpeg with output directed at a fresh temp file in segment_path. Returns the temp
  path on success, or None on failure (nothing is left behind on failure)."""
  fd, tmp_path = tempfile.mkstemp(suffix=suffix, dir=segment_path)
  os.close(fd)
  try:
    result = subprocess.run(["ffmpeg", "-y", *args, tmp_path], capture_output=True, timeout=60)
    if result.returncode != 0 or os.path.getsize(tmp_path) == 0:
      cloudlog.event("audio_extractor_ffmpeg_failed", segment=segment_path, args=args, stderr=result.stderr[-2000:])
      os.unlink(tmp_path)
      return None
    return tmp_path
  except (OSError, subprocess.TimeoutExpired):
    cloudlog.exception(f"ffmpeg failed for {segment_path} ({args})")
    if os.path.exists(tmp_path):
      os.unlink(tmp_path)
    return None


def _extract_and_strip_qcamera_audio(segment_path: str, qcam_path: str) -> bool:
  audio_path = os.path.join(segment_path, AUDIO_FILENAME)

  # force the mp4 muxer explicitly rather than relying on extension auto-detection: the
  # project's vendored ffmpeg build only enables a handful of muxers and doesn't register the
  # "ipod"/m4a alias, so a bare ".m4a" output fails with "Unable to choose an output format"
  tmp_audio = _run_ffmpeg_to_temp(segment_path, ".m4a", ["-i", qcam_path, "-vn", "-c:a", "copy", "-f", "mp4"])
  if tmp_audio is None:
    return False
  os.replace(tmp_audio, audio_path)

  tmp_video = _run_ffmpeg_to_temp(segment_path, ".ts", ["-i", qcam_path, "-c:v", "copy", "-an"])
  if tmp_video is None:
    return False
  os.replace(tmp_video, qcam_path)

  return True


def _strip_audio_from_log(path: str) -> None:
  with open(path, "rb") as f:
    raw = f.read()
  if not raw:
    return

  dctx = zstd.ZstdDecompressor()
  with dctx.stream_reader(raw) as reader:
    dat = reader.read()

  events = capnp_log.Event.read_multiple_bytes(dat)
  kept = [e for e in events if e.which() != 'rawAudioData']

  out = b"".join(e.as_builder().to_bytes() for e in kept)
  compressed = zstd.compress(out, 10)

  with atomic_write(path, mode='wb', overwrite=True) as f:
    f.write(compressed)


def _process_segment_audio_locked(segment_path: str, qcam_path: str) -> bool:
  # deleter.py can remove files out from under us at any point; treat any resulting OSError
  # (e.g. the file disappearing between the exists() check above and here) as "not processed
  # yet, try again next time" rather than letting it propagate out of the uploader loop
  try:
    qcam_processed = _is_processed(qcam_path)
  except OSError:
    cloudlog.exception(f"failed to check audio-processed state for {qcam_path}")
    return False

  if not qcam_processed:
    # a segment that keeps failing (corrupt/truncated recording, etc.) would otherwise get
    # retried at full ffprobe+ffmpeg cost on every single uploader loop iteration forever;
    # back off instead so one bad segment can't stall the whole upload backlog
    if not _retry_is_due(qcam_path):
      return False

    try:
      if not _has_audio_stream(qcam_path):
        # confirmed no audio anywhere in this segment - nothing for rlog/qlog either
        _mark_processed(qcam_path)
        for log_filename in STRIPPED_LOG_FILENAMES:
          log_path = os.path.join(segment_path, log_filename)
          if os.path.exists(log_path):
            _mark_processed(log_path)
        return True

      if not _extract_and_strip_qcamera_audio(segment_path, qcam_path):
        _set_retry_backoff(qcam_path)
        return False
      _mark_processed(qcam_path)
    except OSError:
      cloudlog.exception(f"failed to process qcamera audio for {segment_path}")
      _set_retry_backoff(qcam_path)
      return False

  ok = True
  for log_filename in STRIPPED_LOG_FILENAMES:
    log_path = os.path.join(segment_path, log_filename)
    try:
      if os.path.exists(log_path) and not _is_processed(log_path):
        _strip_audio_from_log(log_path)
        _mark_processed(log_path)
    except Exception:
      # broad on purpose: corrupt/truncated log data can raise capnp or zstd errors here,
      # neither of which are OSError - any failure just means "retry this log next time"
      cloudlog.exception(f"failed to strip audio from {log_path}")
      ok = False
  return ok


def process_segment_audio(segment_path: str) -> bool:
  """Extracts mic audio out of qcamera.ts into its own file, strips it from qcamera.ts and
  rlog/qlog in place. Returns True once the segment is confirmed free of embedded audio (either
  because it never had any, or because stripping fully succeeded) - callers must not upload
  anything in AUDIO_GATED_FILENAMES from this segment until this returns True, since a partial
  failure leaves some of those files still carrying audio."""
  qcam_path = os.path.join(segment_path, QCAMERA_FILENAME)
  if not os.path.exists(qcam_path):
    return True

  try:
    if _is_processed(qcam_path) and _stripped_logs_done(segment_path):
      return True
  except OSError:
    pass

  # the "uploader" and "sunnylink_uploader" processes both call this function independently;
  # without this lock they can run ffmpeg on the same segment at once, and one process's
  # partially-stripped qcamera.ts can trick the other's _has_audio_stream() check into thinking
  # the segment never had audio, skipping the rlog/qlog strip below entirely
  lock_path = os.path.join(segment_path, AUDIO_LOCK_FILENAME)
  if not _try_acquire_lock(lock_path):
    return False

  try:
    return _process_segment_audio_locked(segment_path, qcam_path)
  finally:
    with contextlib.suppress(OSError):
      os.unlink(lock_path)
