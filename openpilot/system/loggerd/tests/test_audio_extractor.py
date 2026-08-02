import os
import subprocess

import pytest
import zstandard as zstd

import openpilot.cereal.messaging as messaging
from openpilot.tools.lib.logreader import LogReader

from openpilot.system.loggerd.audio_extractor import (
  process_segment_audio, AUDIO_FILENAME, QCAMERA_FILENAME, AUDIO_PROCESSED_ATTR_NAME,
)
from openpilot.system.loggerd.xattr_cache import getxattr


def _system_ffmpeg() -> str:
  """The project's vendored `ffmpeg` (comma-deps-ffmpeg) is a stripped-down build with only the
  encoders/demuxers loggerd/audio_extractor actually need (h264, aac, mpegts, stream-copy) - it has
  no lavfi/synthetic-source support, so test fixture generation needs a full-featured ffmpeg."""
  for candidate in ("/usr/bin/ffmpeg", "/usr/local/bin/ffmpeg"):
    if os.path.exists(candidate):
      return candidate
  raise RuntimeError("no full-featured system ffmpeg found for test fixture generation")


def _make_ts_with_audio(path: str, with_audio: bool) -> None:
  cmd = [_system_ffmpeg(), "-y", "-f", "lavfi", "-i", "testsrc=duration=1:size=64x64:rate=5"]
  if with_audio:
    cmd += ["-f", "lavfi", "-i", "anullsrc=r=16000:cl=mono:duration=1", "-c:a", "aac"]
  cmd += ["-c:v", "libx264", "-f", "mpegts", path]
  subprocess.run(cmd, check=True, capture_output=True)


def _has_audio_stream(path: str) -> bool:
  result = subprocess.run(
    ["ffprobe", "-i", path, "-show_streams", "-select_streams", "a", "-loglevel", "error"],
    capture_output=True,
  )
  return result.stdout.strip() != b''


def _write_log(path: str, msgs: list) -> None:
  dat = b"".join(m.to_bytes() for m in msgs)
  with open(path, "wb") as f:
    f.write(zstd.compress(dat, 10))


class TestAudioExtractor:
  def test_no_qcamera_is_a_noop(self, tmp_path):
    segment = str(tmp_path)
    assert process_segment_audio(segment) is True
    assert not os.path.exists(os.path.join(segment, AUDIO_FILENAME))

  def test_video_only_qcamera_marks_processed_without_extraction(self, tmp_path):
    segment = str(tmp_path)
    qcam_path = os.path.join(segment, QCAMERA_FILENAME)
    _make_ts_with_audio(qcam_path, with_audio=False)

    assert process_segment_audio(segment) is True
    assert not os.path.exists(os.path.join(segment, AUDIO_FILENAME))
    assert getxattr(qcam_path, AUDIO_PROCESSED_ATTR_NAME) is not None

  def test_extracts_and_strips_audio(self, tmp_path):
    segment = str(tmp_path)
    qcam_path = os.path.join(segment, QCAMERA_FILENAME)
    _make_ts_with_audio(qcam_path, with_audio=True)
    assert _has_audio_stream(qcam_path), "test fixture should have audio to begin with"

    rlog_path = os.path.join(segment, "rlog.zst")
    audio_msg = messaging.new_message('rawAudioData')
    audio_msg.rawAudioData.data = bytes(800 * 2)
    audio_msg.rawAudioData.sampleRate = 16000
    control_msg = messaging.new_message('carState')
    _write_log(rlog_path, [audio_msg, control_msg])

    assert process_segment_audio(segment) is True

    audio_path = os.path.join(segment, AUDIO_FILENAME)
    assert os.path.exists(audio_path)
    assert _has_audio_stream(audio_path)
    assert not _has_audio_stream(qcam_path), "qcamera.ts should no longer carry audio"

    which_values = [m.which() for m in LogReader(rlog_path)]
    assert 'rawAudioData' not in which_values, "rawAudioData should be stripped from rlog"
    assert 'carState' in which_values, "non-audio messages should be preserved"

  def test_reprocessing_is_a_cheap_noop(self, tmp_path):
    segment = str(tmp_path)
    qcam_path = os.path.join(segment, QCAMERA_FILENAME)
    _make_ts_with_audio(qcam_path, with_audio=True)

    assert process_segment_audio(segment) is True
    audio_path = os.path.join(segment, AUDIO_FILENAME)
    first_mtime = os.path.getmtime(audio_path)

    assert process_segment_audio(segment) is True
    assert os.path.getmtime(audio_path) == first_mtime, "should not redo extraction on an already-processed segment"

  def test_failed_extraction_does_not_mark_processed(self, tmp_path, monkeypatch):
    segment = str(tmp_path)
    qcam_path = os.path.join(segment, QCAMERA_FILENAME)
    _make_ts_with_audio(qcam_path, with_audio=True)

    real_run = subprocess.run

    def fake_run(cmd, *args, **kwargs):
      if cmd[0] == "ffmpeg":
        return subprocess.CompletedProcess(cmd, 1, stdout=b'', stderr=b'boom')
      return real_run(cmd, *args, **kwargs)

    monkeypatch.setattr(subprocess, "run", fake_run)

    assert process_segment_audio(segment) is False
    assert not os.path.exists(os.path.join(segment, AUDIO_FILENAME))
    assert getxattr(qcam_path, AUDIO_PROCESSED_ATTR_NAME) is None, "must not mark processed on failure"


if __name__ == "__main__":
  pytest.main([__file__])
