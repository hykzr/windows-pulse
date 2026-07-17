# WindowPulse

Fast, change-aware capture of one window for Python.

WindowPulse records a selected application window, not a display and not a
rectangle cut from the composited desktop. The target does not need to be the
focused or topmost window. A native worker captures frames while Python
consumers process timestamped
[`PIL.Image.Image`](https://pillow.readthedocs.io/en/stable/reference/Image.html)
objects from a queue.

The native backend is built with Rust and PyO3. It uses direct Windows
Graphics Capture and macOS ScreenCaptureKit adapters, plus an `xcap`
X11/XWayland window backend on Linux. Audio is never captured.

## Why WindowPulse?

The name reflects the library's purpose: watch the visual “pulse” of one
window and deliver useful changes instead of every duplicate frame. It also
avoids the already-occupied `window-capture` package name.

## Install

WindowPulse requires Python 3.10 or newer.

```console
python -m pip install windowpulse
```

Install the optional video consumer and its bundled FFmpeg executable with:

```console
python -m pip install "windowpulse[video]"
```

Release wheels contain the Rust extension, so supported clients do not need a
Rust toolchain. The release workflow builds wheels for 64-bit Windows,
64-bit Linux (glibc/manylinux), Intel macOS 13+, and Apple silicon macOS 13+.
Releases are wheel-only, so installation never falls back to compiling Rust on
a client machine. Building a development checkout requires Rust and that
platform's native development libraries.

## Quick start

Select a window, configure change detection and queue behavior, then consume
`CapturedFrame(timestamp, image)` packets. `timestamp` is Unix time in seconds
and `image` is a Pillow image.

```python
from windowpulse import (
    CaptureOptions,
    ChangeDetectionOptions,
    QueueFullPolicy,
    QueueOptions,
    WindowRecorder,
    find_window,
)

window = find_window(title="Quarterly Review", exact=True)
recorder = WindowRecorder(
    window,
    capture=CaptureOptions(fps=30),
    change_detection=ChangeDetectionOptions(
        threshold=0.01,
        comparison_size=(64, 64),
        debounce_seconds=0.30,
    ),
    queue_options=QueueOptions(
        max_size=32,
        full_policy=QueueFullPolicy.DROP_OLDEST,
        clear_on_window_close=False,
    ),
)

with recorder:
    for packet in recorder:
        print(packet.timestamp, packet.image.size, packet.image.mode)
        packet.image.save("latest.png")
```

Stop the loop with Ctrl+C or call `recorder.stop()` from another thread. Closing
the target window also stops the recorder normally; `recorder.is_window_closed()`
distinguishes that case from an explicit stop. An iterator finishes after the
recorder has stopped and its queue has drained. Set
`QueueOptions(clear_on_window_close=True)` to discard pending frames instead.

Iteration acknowledges each packet automatically after the loop body finishes.
For direct queue-style consumption, use `recorder.get(timeout=...)`, followed
by `recorder.task_done()` in a `finally` block. The underlying `recorder.queue`
also provides `get`, `task_done`, `qsize`, `empty`, and `join`.

## Select one window

```python
from windowpulse import find_window, find_windows, list_windows

for window in list_windows():
    print(window.id, window.app_name, window.title, window.bounds)

# Matching is case-insensitive; exact=True requires the whole field.
editors = find_windows(app_name="Code")
window = find_window(title="notes.md", app_name="Code")
```

`list_windows(include_untitled=False)` omits untitled windows by default.
`find_windows` accepts keyword filters for `title`, `app_name`, and `pid`, plus
`exact`. `find_window` accepts the same filters but requires exactly one match;
it raises `WindowNotFoundError` or `AmbiguousWindowError` otherwise. Passing a
numeric window ID directly to `WindowRecorder` is also supported.

Window metadata may change while an application moves or resizes. Call
`window.refresh()` for an updated `WindowInfo`. `window.bounds` is a `Region`
in screen coordinates (or `None` when the backend cannot report it), and
`window.size` is the current `(width, height)` pair when known.

## Crop within the window

`CaptureOptions.crop` is a window-local region. It does not turn capture into
display capture; pixels outside the target window never become part of the
frame.

```python
from windowpulse import CaptureOptions, Region

screen_x, screen_y = 1200, 300
local_x, local_y = window.screen_to_window(screen_x, screen_y)

capture = CaptureOptions(
    fps=30,
    crop=Region(x=local_x, y=local_y, width=800, height=450),
)

# Convert a window-local point back to the desktop coordinate space.
assert window.window_to_screen(local_x, local_y) == (screen_x, screen_y)
```

Coordinates use the values reported by the native backend. Mixed-DPI desktop
layouts can have platform-specific coordinate behavior, so refresh the window
metadata after moving it between displays and keep the crop within its current
size.

The complete capture options are:

- `fps=30`
- `show_cursor=False`
- `show_highlight=False`
- `crop: Region | None = None`
- `output_resolution=OutputResolution.CAPTURED`

Output resizing, cursor inclusion, and a capture highlight depend on native
backend support. The Linux X11 backend does not include the cursor or expose a
system capture highlight.

## Change detection

`ChangeDetectionOptions.threshold` is a normalized mean absolute visual
difference from `0.0` to `1.0`. For a nonzero threshold, frames are compared
at `comparison_size` (default `(64, 64)`) to keep the hot path inexpensive.
The exact-change `threshold=0.0` path hashes the full frame instead.

- `threshold=0.0` emits any non-identical comparison frame.
- Raising the threshold ignores more small visual differences such as noise or
  cursor shimmer.
- A frame whose difference does not exceed the threshold is treated as
  unchanged and is not queued.

`emit_initial=True` controls whether the first captured frame is emitted.

Set `debounce_seconds` to enable stable, trailing-edge debounce. Once a
meaningful change starts, WindowPulse holds the candidate frame. Each further
meaningful change restarts the timer. One frame is queued only after the image
has remained visually stable for the configured interval. This is useful for
slides and other interfaces with animations: consumers receive the settled
result instead of every transition frame. Leave `debounce_seconds=None` to
emit every frame that passes the change threshold.

## Queue backpressure

Queues are unbounded by default (`QueueOptions(max_size=None)`). For a bounded
queue, capacity includes both pending packets and packets already handed to a
consumer but not yet acknowledged with `task_done()`.

`QueueFullPolicy.BLOCK` pauses the producer at enqueue time until a consumer
frees capacity. `QueueFullPolicy.DROP_OLDEST` evicts the oldest pending packet
so the queue favors recent state. It never discards a packet that a handler has
already started processing. If every slot is in flight and nothing is pending,
the producer must wait for `task_done()` even under `DROP_OLDEST`.

For handler-style consumers, `start_handler(handler, workers=N)` runs the
Python callable with each `CapturedFrame` on `N` consumer threads while the
native capture worker continues recording:

```python
def analyze(packet):
    # CPU-heavy work can be sent onward to a process pool if appropriate.
    print(packet.timestamp, packet.image.getbbox())

with recorder:
    handlers = recorder.start_handler(analyze, workers=4)
    # Keep the owning application alive while capture and handlers run.
    ...
```

The returned `HandlerPool` coordinates the consumer workers. It provides
`start()`, `stop(drain=True, timeout=None)`, `raise_if_failed()`, `is_alive`,
and context-manager support. Recorder shutdown also stops pools created by
that recorder. See [`examples/README.md`](examples/README.md) for complete
consumer patterns.

## Permissions and backend checks

Use `is_supported()`, `backend_name()`, `has_permission()`, and
`request_permission()` before starting interactive capture when your
application needs to control the permission flow.

```python
import windowpulse

if not windowpulse.is_supported():
    raise RuntimeError("Window capture is unavailable on this platform")
if not windowpulse.has_permission() and not windowpulse.request_permission():
    raise PermissionError("Window capture permission was not granted")
```

Permission prompts are controlled by the operating system. They may require
the user to leave the terminal or application and grant access in system
settings.

## Platform behavior and limitations

WindowPulse asks the operating system for the selected window's content. It
does not capture the full screen and crop it afterward. On supported native
backends, changing focus or covering the target with another window does not
change the capture target. However, applications are allowed to stop rendering
while backgrounded, and native capture behavior is ultimately controlled by
the OS.

- **Minimized windows:** do not rely on capturing a minimized window. An OS or
  application may suspend its surfaces, stop producing frames, return stale
  content, or end the stream. Keep the target restored; it may remain behind
  other windows and unfocused.
- **macOS:** Screen Recording permission is required. Window capture uses a
  direct native ScreenCaptureKit adapter.
- **Windows:** capture uses a direct native Windows Graphics Capture adapter.
  Protected or restricted content may not be available.
- **Linux/X11 and XWayland:** direct window discovery and window-only capture
  are available through `xcap`, subject to the window manager and application.
- **Linux/native Wayland:** Wayland intentionally prevents arbitrary access to
  other clients' surfaces and does not offer deterministic selection by the
  title/ID API used here. WindowPulse rejects native Wayland capture. It does
  not open a display picker and never falls back to capturing a screen. An
  XWayland target may work when the session exposes it through X11.

No backend can capture DRM-protected content or force an application to render
frames it has stopped producing.

## Included consumers

### Record video

The optional Python `WindowVideoRecorder` class and `windowpulse-video` command
encode one selected window to a video file:

```python
from windowpulse import CaptureOptions, WindowVideoRecorder, find_window

window = find_window(title="Quarterly Review", exact=True)
video = WindowVideoRecorder(
    window,
    "presentation.mp4",
    capture=CaptureOptions(fps=30),
    video_fps=30,
    codec="libx264",
)
path = video.record(duration=60)
print(path)
```

The class also has `start()`, `stop(timeout=None)`, and context-manager support.
Its `change_detection` and `queue_options` keyword arguments accept the same
configuration objects as `WindowRecorder`; `ffmpeg_output_params` passes an
argument sequence to the encoder.

```console
windowpulse-video presentation.mp4 \
  --title "Quarterly Review" \
  --exact-title \
  --duration 60 \
  --fps 30 \
  --video-fps 30 \
  --codec libx264
```

### Stream changed frames to stdout

`windowpulse-watch` emits only frames accepted by change detection and stable
debounce:

```console
windowpulse-watch \
  --title "Quarterly Review" \
  --threshold 0.01 \
  --debounce-ms 300 \
  --format jsonl
```

The default `jsonl` format writes one JSON object per frame with `timestamp`,
`width`, `height`, `mode`, `encoding="png"`, and `png_base64`. For a compact
binary stream, `--format png-stream` writes an unsigned 8-byte big-endian PNG
length followed by that many PNG bytes, repeated until capture stops.

Both commands share `--window-id`, `--title`, `--exact-title`, and
`--list-windows`, plus capture controls including `--fps`,
`--crop X,Y,W,H`, `--threshold`, `--compare-size WxH`, `--debounce-ms`,
`--cursor`, and `--highlight`. Run either command with `--help` for the full
interface.

## Development

A Rust toolchain is needed only to develop WindowPulse or build from source.
With [`just`](https://github.com/casey/just) and `uv` installed, the common
development tasks are:

```console
just setup                   # install locked development dependencies
just compile                 # build and install the native extension locally
just test                    # compile, then run the complete test suite
just typecheck               # run Pyright against the Python package
just check                   # formatting, lint, types, Rust, and tests
just build                   # create a release wheel in dist/
```

Recipes forward additional arguments where useful:

```console
just test tests/test_queue.py -q
just windows
just watch --title "Quarterly Review" --debounce-ms 300
just video presentation.mp4 --title "Quarterly Review" --duration 60
```

Linux source builds also require the PipeWire, Wayland, XCB/XRandR, D-Bus,
GBM/EGL, pkg-config, and libclang development packages used by the native
backend.

## Releases

The tag workflow builds native wheels on each supported runner, uploads them as
workflow artifacts, installs those downloaded artifacts in clean jobs without
Cargo, and publishes only the wheels after every installation smoke test
passes. No source distribution is uploaded, so supported clients never need a
Rust toolchain during installation.

Before the first release, configure a PyPI Trusted Publisher for this GitHub
repository with workflow filename `release.yml` and environment name `pypi`.
Then update the project version consistently and push a `v*` tag. Publishing
uses GitHub's OpenID Connect identity; no long-lived PyPI API token is stored in
the repository.

## License

WindowPulse is available under the [MIT License](LICENSE).
