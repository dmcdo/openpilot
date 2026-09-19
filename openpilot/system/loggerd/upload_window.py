"""Time-window gate for uploads.

A drive may only be uploaded when a window file exists in /dev/shm. The file holds a single
integer: a number of hours. Together with the file's own timestamp (T) that defines a cutoff
reaching back `hours`, and a drive is uploaded only if it ended (E) after that cutoff:

    ===== BLOCKED (too old) ===== | T - hours ..... uploaded ..... T ..... uploaded ..... >

So the window keeps recent driving flowing to the server and holds back everything older.
A drive that began after T is covered by the same test, since its end necessarily follows T.
Deleting the file blocks every upload, so the safe state is the default: a missing, empty or
unparseable file uploads nothing.

Living in /dev/shm makes this deliberately temporary - the window does not survive a reboot.
Note that rewriting the file to change `hours` also resets T, sliding the cutoff forward.
"""
import os

from openpilot.common.swaglog import cloudlog

UPLOAD_WINDOW_PATH = "/dev/shm/upload_window_hours"

# finalized segments never change, so their timestamps are worth keeping; a segment still being
# written (.lock present) is re-read every time instead
_segment_end_cache: dict[str, float] = {}

# route ends are rebuilt whenever a new segment directory appears under the log root
_route_end_cache: dict[str, tuple[float, dict[str, float]]] = {}


def _read_cutoff() -> float | None:
  """Returns T - hours as a unix timestamp: a drive ending after this may be uploaded. None
  means uploads are blocked outright because no usable window file exists."""
  try:
    st = os.stat(UPLOAD_WINDOW_PATH)
    with open(UPLOAD_WINDOW_PATH, "rb") as f:
      raw = f.read(64)
  except OSError:
    return None

  try:
    hours = int(raw.strip())
  except ValueError:
    cloudlog.event("upload_window_unparseable", path=UPLOAD_WINDOW_PATH, raw=raw[:64])
    return None

  if hours < 0:
    cloudlog.event("upload_window_negative", path=UPLOAD_WINDOW_PATH, hours=hours)
    return None

  return st.st_mtime - hours * 3600


def _split_segment(logdir: str) -> tuple[str, int] | None:
  """Splits "<route>--<n>" into (route, n), or None if this isn't a segment directory."""
  route, _, num = logdir.rpartition("--")
  if not route or not num.isdigit():
    return None
  return route, int(num)


def _segment_end(segment_path: str) -> float | None:
  """The latest mtime across a segment's files - when it finished being written. None if the
  segment is gone or empty.

  Deliberately mtime rather than ctime: uploader.py and audio_extractord both set xattrs on
  these files, and an xattr write bumps ctime, which would drag a segment's apparent time
  forward to whenever it was last marked."""
  cached = _segment_end_cache.get(segment_path)
  if cached is not None:
    return cached

  try:
    names = os.listdir(segment_path)
  except OSError:
    return None

  end = None
  final = True
  for name in names:
    if name.endswith(".lock"):
      final = False
    try:
      st = os.stat(os.path.join(segment_path, name))
    except OSError:
      continue
    end = st.st_mtime if end is None else max(end, st.st_mtime)

  if end is None:
    return None

  if final:
    _segment_end_cache[segment_path] = end
  return end


def _route_ends(root: str) -> dict[str, float]:
  """Maps each route on disk to when its last segment finished. A drive is judged as a whole,
  so every segment of it is gated identically."""
  try:
    root_mtime = os.stat(root).st_mtime
  except OSError:
    return {}

  cached = _route_end_cache.get(root)
  if cached is not None and cached[0] == root_mtime:
    return cached[1]

  ends: dict[str, float] = {}
  try:
    logdirs = os.listdir(root)
  except OSError:
    return {}

  for logdir in logdirs:
    split = _split_segment(logdir)
    if split is None:
      continue
    end = _segment_end(os.path.join(root, logdir))
    if end is None:
      continue
    route = split[0]
    prev = ends.get(route)
    ends[route] = end if prev is None else max(prev, end)

  _route_end_cache[root] = (root_mtime, ends)
  return ends


def is_upload_allowed(root: str, logdir: str, fn: str) -> bool:
  """Whether `fn`, living in `root`/`logdir`, may be uploaded under the current window.

  Segment directories are gated on the end of the whole drive they belong to, so a drive that
  straddles the cutoff uploads in full. Anything else under the log root (boot/, crash/) has no
  drive to belong to, so it's gated on its own timestamp against the same cutoff."""
  cutoff = _read_cutoff()
  if cutoff is None:
    return False

  split = _split_segment(logdir)
  if split is None:
    try:
      ended = os.stat(fn).st_mtime
    except OSError:
      return False
  else:
    route_end = _route_ends(root).get(split[0])
    if route_end is None:
      return False
    ended = route_end

  return ended > cutoff
