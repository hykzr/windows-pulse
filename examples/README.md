# WindowPulse consumers

These patterns keep the native capture worker independent from Python frame
processing. Every emitted packet is a `CapturedFrame` containing a Unix
timestamp and a Pillow image.

Start by finding the exact window you intend to record:

```python
from windowpulse import find_window, list_windows

for candidate in list_windows():
    print(candidate.id, candidate.app_name, repr(candidate.title))

window = find_window(title="Quarterly Review", exact=True)
```

Use `find_windows(...)` first if a title can match multiple windows.
`find_window(...)` deliberately raises `AmbiguousWindowError` instead of
silently choosing the wrong window.

## Manual queue consumer

This is the lowest-level Python pattern. Always pair `get()` with
`task_done()`, including when the handler raises.

```python
from pathlib import Path

from windowpulse import (
    ChangeDetectionOptions,
    QueueFullPolicy,
    QueueOptions,
    WindowRecorder,
)

output = Path("shots")
output.mkdir(exist_ok=True)

recorder = WindowRecorder(
    window,
    change_detection=ChangeDetectionOptions(
        threshold=0.01,
        debounce_seconds=0.25,
    ),
    queue_options=QueueOptions(
        max_size=16,
        full_policy=QueueFullPolicy.DROP_OLDEST,
    ),
)

try:
    with recorder:
        sequence = 0
        while True:
            packet = recorder.get()
            try:
                packet.image.save(output / f"{sequence:06d}-{packet.timestamp:.3f}.png")
                sequence += 1
            finally:
                recorder.task_done()
except KeyboardInterrupt:
    pass
```

The context manager stops capture on Ctrl+C. Alternatively, iteration with
`for packet in recorder` acknowledges each packet automatically and exits after
another thread calls `stop()` and the queue has drained.

## Parallel handler consumers

Use `start_handler` when you want WindowPulse to own a pool of queue-consuming
threads. The same handler is invoked once for each packet.

```python
from threading import Event

from windowpulse import ChangeDetectionOptions, WindowRecorder


def inspect_slide(packet):
    # packet.image is a PIL.Image.Image.
    print(f"stable frame at {packet.timestamp:.3f}: {packet.image.size}")


recorder = WindowRecorder(
    window,
    change_detection=ChangeDetectionOptions(
        threshold=0.015,
        debounce_seconds=0.40,
    ),
)

with recorder:
    handlers = recorder.start_handler(inspect_slide, workers=4)
    try:
        Event().wait()  # The surrounding application normally does other work.
    except KeyboardInterrupt:
        pass
```

Thread workers are a good fit for handlers that perform I/O or call native
libraries that release the GIL. For sustained pure-Python CPU work, have the
handler submit compact work to a process pool. Choose a bounded queue when you
need an explicit memory/backpressure policy. `HandlerPool` also exposes
`stop(drain=True, timeout=None)`, `raise_if_failed()`, `is_alive`, and a context
manager for applications that need to control it separately.

## Crop using screen coordinates

WindowPulse crops in window-local coordinates. `WindowInfo` provides helpers
for applications whose region starts in desktop coordinates:

```python
from windowpulse import CaptureOptions, Region, WindowRecorder

window = window.refresh()
left, top = window.screen_to_window(1400, 250)
region = Region(x=left, y=top, width=640, height=360)

recorder = WindowRecorder(
    window,
    capture=CaptureOptions(fps=20, crop=region),
)
```

Refresh after moving or resizing the window. Validate that the region is still
inside `window.size` before starting a new capture.

## Video consumer

Install the optional encoder dependency first:

```console
python -m pip install "windowpulse[video]"
```

The packaged command records one selected window:

```console
windowpulse-video recording.mp4 \
  --title "Quarterly Review" \
  --exact-title \
  --duration 30 \
  --fps 30 \
  --video-fps 30 \
  --codec libx264
```

Selection can instead use `--window-id`. Add `--crop X,Y,W,H` for a
window-local area. `--fps` controls native sampling; `--video-fps` controls the
encoded stream's timeline. The corresponding Python SDK class is
included with the package:

```python
from windowpulse import (
    CaptureOptions,
    ChangeDetectionOptions,
    WindowVideoRecorder,
    find_window,
)

window = find_window(title="Quarterly Review", exact=True)
output = WindowVideoRecorder(
    window,
    "recording.mp4",
    capture=CaptureOptions(fps=30),
    change_detection=ChangeDetectionOptions(threshold=0.0),
    video_fps=30,
    codec="libx264",
).record(duration=30)

print(output)
```

`WindowVideoRecorder` also accepts `queue_options` and
`ffmpeg_output_params=()`. Use `start()` / `stop(timeout=None)` or its context
manager when the surrounding application controls the recording lifetime.

Run `windowpulse-video --help` for all options and available defaults. Codec
support depends on the FFmpeg executable provided by `imageio-ffmpeg`.

## Changed-frame stdout consumer

JSON Lines is convenient for scripts and message-oriented pipelines:

```console
windowpulse-watch \
  --title "Quarterly Review" \
  --exact-title \
  --threshold 0.01 \
  --compare-size 64x64 \
  --debounce-ms 300 \
  --format jsonl
```

Each line contains:

```json
{"timestamp": 1750000000.25, "width": 1280, "height": 720, "mode": "RGBA", "encoding": "png", "png_base64": "..."}
```

Decode a line in Python:

```python
import base64
import io
import json
import sys

from PIL import Image

for line in sys.stdin:
    event = json.loads(line)
    image = Image.open(io.BytesIO(base64.b64decode(event["png_base64"])))
    image.load()
    print(event["timestamp"], image.size, file=sys.stderr)
```

For less framing overhead, request the binary PNG stream:

```console
windowpulse-watch --window-id 1234 --format png-stream > changed-frames.bin
```

Every binary record is an unsigned 8-byte big-endian payload length followed
by exactly that many PNG bytes. A reader can use `int.from_bytes(header,
"big")`, then read the announced payload length. Do not treat the stream as a
concatenation of self-delimiting PNG files without consuming the length
prefix.

Only frames that exceed `--threshold` are sent. With `--debounce-ms`, a changed
frame is sent after it remains within that visual threshold for the debounce
interval, which filters intermediate animation frames.

Use `windowpulse-watch --list-windows` to inspect targets and
`windowpulse-watch --help` for all capture and selection flags.
