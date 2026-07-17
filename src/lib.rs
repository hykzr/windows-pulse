use std::time::{Duration, SystemTime, UNIX_EPOCH};

#[cfg(any(target_os = "windows", target_os = "linux"))]
use std::time::Instant;

#[cfg(any(target_os = "windows", target_os = "linux"))]
use image::{imageops::FilterType, RgbaImage};
use pyo3::exceptions::{PyRuntimeError, PyValueError};
use pyo3::prelude::*;
use pyo3::types::PyBytes;
#[cfg(any(target_os = "windows", target_os = "linux"))]
use xcap::Window;

#[cfg(target_os = "macos")]
use screencapturekit::prelude::*;
#[cfg(target_os = "macos")]
use screencapturekit::stream::delegate_trait::StreamCallbacks;
#[cfg(any(target_os = "windows", target_os = "macos"))]
use std::ffi::c_void;
#[cfg(any(target_os = "windows", target_os = "macos"))]
use std::sync::{Arc, Condvar, Mutex};
#[cfg(target_os = "windows")]
use windows_capture::capture::{CaptureControl, Context, GraphicsCaptureApiHandler};
#[cfg(target_os = "windows")]
use windows_capture::frame::Frame;
#[cfg(target_os = "windows")]
use windows_capture::graphics_capture_api::InternalCaptureControl;
#[cfg(target_os = "windows")]
use windows_capture::settings::{
    ColorFormat, CursorCaptureSettings, DirtyRegionSettings, DrawBorderSettings,
    MinimumUpdateIntervalSettings, SecondaryWindowSettings, Settings,
};
#[cfg(target_os = "windows")]
use windows_capture::window::Window as WindowsCaptureWindow;

#[pyclass(frozen, get_all, skip_from_py_object, module = "windowpulse._native")]
#[derive(Clone)]
struct NativeWindowInfo {
    id: u32,
    pid: Option<u32>,
    title: String,
    app_name: String,
    bundle_id: String,
    x: Option<i32>,
    y: Option<i32>,
    width: Option<u32>,
    height: Option<u32>,
    is_minimized: Option<bool>,
    is_maximized: Option<bool>,
    is_focused: Option<bool>,
}

fn runtime_error(context: &str, error: impl std::fmt::Display) -> PyErr {
    PyRuntimeError::new_err(format!("{context}: {error}"))
}

#[cfg(target_os = "macos")]
fn initialize_core_graphics() {
    // ScreenCaptureKit may call CoreGraphics APIs that assume the window server
    // connection has already been initialized. GUI applications do this as part
    // of AppKit startup, but Python command-line processes do not.
    // SAFETY: This function takes no arguments and only forces CoreGraphics to
    // initialize its process-wide connection by calling CGMainDisplayID().
    unsafe { screencapturekit::ffi::sc_initialize_core_graphics() }
}

#[cfg(target_os = "macos")]
unsafe extern "C" {
    fn proc_pidpath(pid: i32, buffer: *mut c_void, buffersize: u32) -> i32;
}

#[cfg(target_os = "macos")]
fn executable_is_window_server(path: &[u8]) -> bool {
    let path = path.split(|byte| *byte == 0).next().unwrap_or(path);
    path.rsplit(|byte| *byte == b'/').next() == Some(b"WindowServer")
}

#[cfg(target_os = "macos")]
fn is_window_server_process(pid: u32) -> bool {
    const MAX_PATH_SIZE: usize = 4096;

    let Ok(pid) = i32::try_from(pid) else {
        return false;
    };
    let mut path = [0_u8; MAX_PATH_SIZE];
    // SAFETY: `path` is writable for the supplied size and remains alive for the call.
    let length = unsafe {
        proc_pidpath(
            pid,
            path.as_mut_ptr().cast::<c_void>(),
            MAX_PATH_SIZE as u32,
        )
    };
    let Ok(length) = usize::try_from(length) else {
        return false;
    };
    length > 0 && length <= path.len() && executable_is_window_server(&path[..length])
}

#[cfg(any(target_os = "windows", target_os = "linux"))]
fn find_xcap_window(window_id: u32) -> PyResult<Window> {
    Window::all()
        .map_err(|error| runtime_error("failed to enumerate windows", error))?
        .into_iter()
        .find(|window| window.id().ok() == Some(window_id))
        .ok_or_else(|| PyValueError::new_err(format!("window {window_id} no longer exists")))
}

#[pyfunction]
fn list_windows_native() -> PyResult<Vec<NativeWindowInfo>> {
    #[cfg(target_os = "macos")]
    {
        let content = SCShareableContent::create()
            .with_exclude_desktop_windows(true)
            .with_on_screen_windows_only(false)
            .get()
            .map_err(|error| {
                runtime_error("failed to enumerate ScreenCaptureKit windows", error)
            })?;
        Ok(content
            .windows()
            .into_iter()
            .filter_map(|window| {
                let frame = window.frame();
                if frame.size.width <= 0.0 || frame.size.height <= 0.0 {
                    return None;
                }
                let application = window.owning_application();
                let pid = application
                    .as_ref()
                    .and_then(|app| u32::try_from(app.process_id()).ok());
                // Window titles and application display names may be localized, but the
                // WindowServer executable name is stable across system languages.
                if pid.is_some_and(is_window_server_process) {
                    return None;
                }
                Some(NativeWindowInfo {
                    id: window.window_id(),
                    pid,
                    title: window.title().unwrap_or_default(),
                    app_name: application
                        .as_ref()
                        .map_or_else(String::new, SCRunningApplication::application_name),
                    bundle_id: application
                        .as_ref()
                        .map_or_else(String::new, SCRunningApplication::bundle_identifier),
                    x: Some(frame.origin.x.floor() as i32),
                    y: Some(frame.origin.y.floor() as i32),
                    width: Some(frame.size.width.ceil() as u32),
                    height: Some(frame.size.height.ceil() as u32),
                    is_minimized: None,
                    is_maximized: None,
                    is_focused: None,
                })
            })
            .collect())
    }

    #[cfg(not(target_os = "macos"))]
    {
        let windows =
            Window::all().map_err(|error| runtime_error("failed to enumerate windows", error))?;
        Ok(windows
            .into_iter()
            .filter_map(|window| {
                let id = window.id().ok()?;
                Some(NativeWindowInfo {
                    id,
                    pid: window.pid().ok(),
                    title: window.title().unwrap_or_default(),
                    app_name: window.app_name().unwrap_or_default(),
                    bundle_id: String::new(),
                    x: window.x().ok(),
                    y: window.y().ok(),
                    width: window.width().ok(),
                    height: window.height().ok(),
                    is_minimized: window.is_minimized().ok(),
                    is_maximized: window.is_maximized().ok(),
                    is_focused: window.is_focused().ok(),
                })
            })
            .collect())
    }
}

#[pyfunction]
fn is_supported() -> bool {
    cfg!(any(
        target_os = "windows",
        target_os = "macos",
        target_os = "linux"
    ))
}

#[pyfunction]
fn has_permission() -> bool {
    #[cfg(target_os = "macos")]
    {
        objc2_core_graphics::CGPreflightScreenCaptureAccess()
    }
    #[cfg(not(target_os = "macos"))]
    {
        true
    }
}

#[pyfunction]
fn request_permission() -> bool {
    #[cfg(target_os = "macos")]
    {
        objc2_core_graphics::CGRequestScreenCaptureAccess()
    }
    #[cfg(not(target_os = "macos"))]
    {
        true
    }
}

#[pyfunction]
fn backend_name() -> &'static str {
    #[cfg(target_os = "windows")]
    {
        "windows-capture"
    }
    #[cfg(target_os = "macos")]
    {
        "screencapturekit"
    }
    #[cfg(target_os = "linux")]
    {
        "xcap-x11"
    }
    #[cfg(not(any(target_os = "windows", target_os = "macos", target_os = "linux")))]
    {
        "unsupported"
    }
}

fn unix_timestamp(time: SystemTime) -> f64 {
    time.duration_since(UNIX_EPOCH)
        .unwrap_or_default()
        .as_secs_f64()
}

#[derive(Clone, Copy)]
enum OutputResolution {
    Captured,
    MaxWidth(u32),
}

impl OutputResolution {
    fn parse(value: &str) -> PyResult<Self> {
        match value {
            "captured" => Ok(Self::Captured),
            "480p" => Ok(Self::MaxWidth(640)),
            "720p" => Ok(Self::MaxWidth(1280)),
            "1080p" => Ok(Self::MaxWidth(1920)),
            "1440p" => Ok(Self::MaxWidth(2560)),
            "2160p" => Ok(Self::MaxWidth(3840)),
            "4320p" => Ok(Self::MaxWidth(7680)),
            _ => Err(PyValueError::new_err(format!(
                "unknown output resolution {value:?}"
            ))),
        }
    }

    fn fit(self, width: u32, height: u32) -> (u32, u32) {
        let Self::MaxWidth(max_width) = self else {
            return (width, height);
        };
        if width <= max_width || width == 0 {
            return (width, height);
        }
        let output_height =
            ((u64::from(height) * u64::from(max_width)) / u64::from(width)).max(1) as u32;
        (max_width, output_height)
    }
}

struct RawFrame {
    timestamp: f64,
    width: u32,
    height: u32,
    data: Vec<u8>,
}

type PythonFrame = (f64, u32, u32, String, Py<PyBytes>);

#[cfg(any(target_os = "windows", target_os = "linux"))]
fn resize_frame(mut frame: RawFrame, resolution: OutputResolution) -> PyResult<RawFrame> {
    let (output_width, output_height) = resolution.fit(frame.width, frame.height);
    if (output_width, output_height) == (frame.width, frame.height) {
        return Ok(frame);
    }
    let image = RgbaImage::from_raw(frame.width, frame.height, std::mem::take(&mut frame.data))
        .ok_or_else(|| PyRuntimeError::new_err("native backend returned an invalid RGBA buffer"))?;
    let resized =
        image::imageops::resize(&image, output_width, output_height, FilterType::Lanczos3);
    frame.width = resized.width();
    frame.height = resized.height();
    frame.data = resized.into_raw();
    Ok(frame)
}

#[cfg(any(target_os = "windows", target_os = "macos"))]
enum CaptureMessage {
    Frame(RawFrame),
    #[cfg(target_os = "windows")]
    Closed,
    Error(String),
}

#[cfg(target_os = "macos")]
fn mac_window_exists(window_id: u32) -> PyResult<bool> {
    let content = SCShareableContent::create()
        .with_exclude_desktop_windows(true)
        .with_on_screen_windows_only(false)
        .get()
        .map_err(|error| runtime_error("failed to query ScreenCaptureKit windows", error))?;
    Ok(content
        .windows()
        .into_iter()
        .any(|window| window.window_id() == window_id))
}

#[cfg(any(target_os = "windows", target_os = "macos"))]
enum CapturePoll {
    Frame(RawFrame),
    #[cfg(target_os = "windows")]
    Closed,
    Error(String),
    Empty,
}

#[cfg(any(target_os = "windows", target_os = "macos"))]
#[derive(Default)]
struct MailboxState {
    message: Option<CaptureMessage>,
    stopped: bool,
}

/// A one-slot latest-frame mailbox. Native callbacks never wait for Python consumers and
/// overwrite an unconsumed frame, keeping memory bounded even when the public queue blocks.
#[cfg(any(target_os = "windows", target_os = "macos"))]
#[derive(Default)]
struct LatestFrameMailbox {
    state: Mutex<MailboxState>,
    ready: Condvar,
}

#[cfg(any(target_os = "windows", target_os = "macos"))]
impl LatestFrameMailbox {
    fn push(&self, message: CaptureMessage) {
        let mut state = self
            .state
            .lock()
            .unwrap_or_else(|poisoned| poisoned.into_inner());
        if state.stopped {
            return;
        }
        state.message = Some(message);
        self.ready.notify_one();
    }

    fn stop(&self) {
        let mut state = self
            .state
            .lock()
            .unwrap_or_else(|poisoned| poisoned.into_inner());
        state.stopped = true;
        state.message = None;
        self.ready.notify_all();
    }

    fn receive(&self, timeout: Duration) -> CapturePoll {
        let mut state = self
            .state
            .lock()
            .unwrap_or_else(|poisoned| poisoned.into_inner());
        if state.message.is_none() && !state.stopped {
            let result = self
                .ready
                .wait_timeout(state, timeout)
                .unwrap_or_else(|poisoned| poisoned.into_inner());
            state = result.0;
        }
        match state.message.take() {
            Some(CaptureMessage::Frame(frame)) => CapturePoll::Frame(frame),
            #[cfg(target_os = "windows")]
            Some(CaptureMessage::Closed) => CapturePoll::Closed,
            Some(CaptureMessage::Error(error)) => CapturePoll::Error(error),
            None => CapturePoll::Empty,
        }
    }
}

#[cfg(target_os = "windows")]
#[derive(Clone)]
struct WinHandlerFlags {
    mailbox: Arc<LatestFrameMailbox>,
    crop: Option<(u32, u32, u32, u32)>,
    frame_interval: Duration,
}

#[cfg(target_os = "windows")]
struct WinHandler {
    flags: WinHandlerFlags,
    last_frame: Option<Instant>,
}

#[cfg(target_os = "windows")]
impl GraphicsCaptureApiHandler for WinHandler {
    type Flags = WinHandlerFlags;
    type Error = String;

    fn new(context: Context<Self::Flags>) -> Result<Self, Self::Error> {
        Ok(Self {
            flags: context.flags,
            last_frame: None,
        })
    }

    fn on_frame_arrived(
        &mut self,
        frame: &mut Frame,
        capture_control: InternalCaptureControl,
    ) -> Result<(), Self::Error> {
        let now = Instant::now();
        if self
            .last_frame
            .is_some_and(|last| now.duration_since(last) < self.flags.frame_interval)
        {
            return Ok(());
        }
        self.last_frame = Some(now);

        let mut padding = Vec::new();
        let (width, height, data) = if let Some((x, y, width, height)) = self.flags.crop {
            let Some(right) = x.checked_add(width) else {
                self.flags.mailbox.push(CaptureMessage::Error(
                    "crop coordinates overflow the frame bounds".to_owned(),
                ));
                capture_control.stop();
                return Ok(());
            };
            let Some(bottom) = y.checked_add(height) else {
                self.flags.mailbox.push(CaptureMessage::Error(
                    "crop coordinates overflow the frame bounds".to_owned(),
                ));
                capture_control.stop();
                return Ok(());
            };
            if right > frame.width() || bottom > frame.height() {
                self.flags.mailbox.push(CaptureMessage::Error(format!(
                    "crop ({x}, {y}, {width}, {height}) exceeds frame size {}x{}",
                    frame.width(),
                    frame.height()
                )));
                capture_control.stop();
                return Ok(());
            }
            let buffer = match frame.buffer_crop(x, y, right, bottom) {
                Ok(buffer) => buffer,
                Err(error) => {
                    self.flags.mailbox.push(CaptureMessage::Error(format!(
                        "failed to read cropped Windows capture frame: {error}"
                    )));
                    capture_control.stop();
                    return Ok(());
                }
            };
            let data = buffer.as_nopadding_buffer(&mut padding).to_vec();
            (width, height, data)
        } else {
            let buffer = match frame.buffer() {
                Ok(buffer) => buffer,
                Err(error) => {
                    self.flags.mailbox.push(CaptureMessage::Error(format!(
                        "failed to read Windows capture frame: {error}"
                    )));
                    capture_control.stop();
                    return Ok(());
                }
            };
            let width = buffer.width();
            let height = buffer.height();
            let data = buffer.as_nopadding_buffer(&mut padding).to_vec();
            (width, height, data)
        };

        self.flags.mailbox.push(CaptureMessage::Frame(RawFrame {
            timestamp: unix_timestamp(SystemTime::now()),
            width,
            height,
            data,
        }));
        Ok(())
    }

    fn on_closed(&mut self) -> Result<(), Self::Error> {
        self.flags.mailbox.push(CaptureMessage::Closed);
        Ok(())
    }
}

#[cfg(target_os = "windows")]
struct WinBackend {
    control: Option<CaptureControl<WinHandler, String>>,
    mailbox: Arc<LatestFrameMailbox>,
    resolution: OutputResolution,
}

#[cfg(target_os = "macos")]
struct MacFrameHandler {
    mailbox: Arc<LatestFrameMailbox>,
}

#[cfg(target_os = "macos")]
impl SCStreamOutputTrait for MacFrameHandler {
    fn did_output_sample_buffer(
        &self,
        sample_buffer: CMSampleBuffer,
        _output_type: SCStreamOutputType,
    ) {
        if !sample_buffer.is_valid() || !sample_buffer.is_data_ready() {
            return;
        }
        if sample_buffer
            .frame_status()
            .is_some_and(|status| !status.has_content())
        {
            return;
        }
        let Some(pixel_buffer) = sample_buffer.image_buffer() else {
            return;
        };
        let guard = match pixel_buffer.lock_read_only() {
            Ok(guard) => guard,
            Err(status) => {
                self.mailbox.push(CaptureMessage::Error(format!(
                    "failed to lock ScreenCaptureKit pixel buffer: {status}"
                )));
                return;
            }
        };
        let Ok(width) = u32::try_from(guard.width()) else {
            self.mailbox.push(CaptureMessage::Error(
                "ScreenCaptureKit frame width exceeds u32".to_owned(),
            ));
            return;
        };
        let Ok(height) = u32::try_from(guard.height()) else {
            self.mailbox.push(CaptureMessage::Error(
                "ScreenCaptureKit frame height exceeds u32".to_owned(),
            ));
            return;
        };
        if width == 0 || height == 0 {
            return;
        }

        let bytes_per_row = guard.bytes_per_row();
        let Some(row_bytes) = (width as usize).checked_mul(4) else {
            self.mailbox.push(CaptureMessage::Error(
                "ScreenCaptureKit frame row size overflow".to_owned(),
            ));
            return;
        };
        let source = guard.as_slice();
        let Some(source_size) = bytes_per_row.checked_mul(height as usize) else {
            self.mailbox.push(CaptureMessage::Error(
                "ScreenCaptureKit frame size overflow".to_owned(),
            ));
            return;
        };
        if bytes_per_row < row_bytes || source.len() < source_size {
            self.mailbox.push(CaptureMessage::Error(
                "ScreenCaptureKit returned an invalid BGRA buffer".to_owned(),
            ));
            return;
        }
        let Some(packed_size) = row_bytes.checked_mul(height as usize) else {
            self.mailbox.push(CaptureMessage::Error(
                "ScreenCaptureKit packed frame size overflow".to_owned(),
            ));
            return;
        };
        let mut rgba = Vec::with_capacity(packed_size);
        for row in source.chunks_exact(bytes_per_row).take(height as usize) {
            for pixel in row[..row_bytes].chunks_exact(4) {
                rgba.extend_from_slice(&[pixel[2], pixel[1], pixel[0], pixel[3]]);
            }
        }
        self.mailbox.push(CaptureMessage::Frame(RawFrame {
            timestamp: unix_timestamp(SystemTime::now()),
            width,
            height,
            data: rgba,
        }));
    }
}

#[cfg(target_os = "macos")]
struct MacBackend {
    stream: SCStream,
    mailbox: Arc<LatestFrameMailbox>,
    window_id: u32,
}

#[cfg(target_os = "linux")]
struct SnapshotBackend {
    window: Window,
    window_id: u32,
    crop: Option<(u32, u32, u32, u32)>,
    resolution: OutputResolution,
    frame_interval: Duration,
    next_due: Instant,
}

#[pyclass(module = "windowpulse._native")]
struct NativeCapturer {
    #[cfg(target_os = "windows")]
    backend: Option<WinBackend>,
    #[cfg(target_os = "macos")]
    backend: Option<MacBackend>,
    #[cfg(target_os = "linux")]
    backend: Option<SnapshotBackend>,
    stopped: bool,
    window_closed: bool,
}

#[pymethods]
impl NativeCapturer {
    #[new]
    #[pyo3(signature = (window_id, fps=30, show_cursor=false, show_highlight=false, crop=None, output_resolution="captured"))]
    fn new(
        window_id: u32,
        fps: u32,
        show_cursor: bool,
        show_highlight: bool,
        crop: Option<(u32, u32, u32, u32)>,
        output_resolution: &str,
    ) -> PyResult<Self> {
        if fps == 0 {
            return Err(PyValueError::new_err("fps must be greater than zero"));
        }
        if let Some((_, _, width, height)) = crop {
            if width == 0 || height == 0 {
                return Err(PyValueError::new_err(
                    "crop width and height must be greater than zero",
                ));
            }
        }
        let resolution = OutputResolution::parse(output_resolution)?;

        #[cfg(target_os = "windows")]
        {
            let _ = find_xcap_window(window_id)?;
            let window = WindowsCaptureWindow::from_raw_hwnd(window_id as usize as *mut c_void);
            if !window.is_valid() {
                return Err(PyValueError::new_err(format!(
                    "window {window_id} is not captureable"
                )));
            }

            let mailbox = Arc::new(LatestFrameMailbox::default());
            let flags = WinHandlerFlags {
                mailbox: Arc::clone(&mailbox),
                crop,
                frame_interval: Duration::from_secs_f64(1.0 / f64::from(fps)),
            };
            let settings = Settings::new(
                window,
                if show_cursor {
                    CursorCaptureSettings::WithCursor
                } else {
                    CursorCaptureSettings::WithoutCursor
                },
                if show_highlight {
                    DrawBorderSettings::WithBorder
                } else {
                    DrawBorderSettings::WithoutBorder
                },
                SecondaryWindowSettings::Exclude,
                MinimumUpdateIntervalSettings::Default,
                DirtyRegionSettings::Default,
                ColorFormat::Rgba8,
                flags,
            );
            let control = WinHandler::start_free_threaded(settings)
                .map_err(|error| runtime_error("failed to start Windows window capture", error))?;
            Ok(Self {
                backend: Some(WinBackend {
                    control: Some(control),
                    mailbox,
                    resolution,
                }),
                stopped: false,
                window_closed: false,
            })
        }

        #[cfg(target_os = "macos")]
        {
            if show_highlight {
                return Err(PyValueError::new_err(
                    "show_highlight is not supported by ScreenCaptureKit",
                ));
            }
            let content = SCShareableContent::create()
                .with_exclude_desktop_windows(true)
                .with_on_screen_windows_only(false)
                .get()
                .map_err(|error| {
                    runtime_error("failed to query ScreenCaptureKit windows", error)
                })?;
            let window = content
                .windows()
                .into_iter()
                .find(|window| window.window_id() == window_id)
                .ok_or_else(|| {
                    PyValueError::new_err(format!("window {window_id} is not captureable"))
                })?;
            let frame = window.frame();
            let window_width = frame.size.width.ceil().max(1.0) as u32;
            let window_height = frame.size.height.ceil().max(1.0) as u32;
            let (source_width, source_height) = if let Some((x, y, width, height)) = crop {
                let right = x.checked_add(width).ok_or_else(|| {
                    PyValueError::new_err("crop coordinates overflow the window bounds")
                })?;
                let bottom = y.checked_add(height).ok_or_else(|| {
                    PyValueError::new_err("crop coordinates overflow the window bounds")
                })?;
                if right > window_width || bottom > window_height {
                    return Err(PyValueError::new_err(format!(
                        "crop ({x}, {y}, {width}, {height}) exceeds window size \
                         {window_width}x{window_height}"
                    )));
                }
                (width, height)
            } else {
                (window_width, window_height)
            };
            let (output_width, output_height) = resolution.fit(source_width, source_height);

            let filter = SCContentFilter::create().with_window(&window).build();
            let mut configuration = SCStreamConfiguration::new()
                .with_width(output_width)
                .with_height(output_height)
                .with_pixel_format(PixelFormat::BGRA)
                .with_shows_cursor(show_cursor)
                .with_fps(fps)
                .with_queue_depth(2)
                .with_captures_audio(false)
                .with_excludes_current_process_audio(true);
            if let Some((x, y, width, height)) = crop {
                configuration = configuration.with_source_rect(CGRect::new(
                    f64::from(x),
                    f64::from(y),
                    f64::from(width),
                    f64::from(height),
                ));
            }

            let mailbox = Arc::new(LatestFrameMailbox::default());
            let error_mailbox = Arc::clone(&mailbox);
            let delegate = StreamCallbacks::new().on_error(move |error| {
                error_mailbox.push(CaptureMessage::Error(format!(
                    "ScreenCaptureKit stream ended: {error}"
                )));
            });
            let mut stream = SCStream::new_with_delegate(&filter, &configuration, delegate);
            if stream
                .add_output_handler(
                    MacFrameHandler {
                        mailbox: Arc::clone(&mailbox),
                    },
                    SCStreamOutputType::Screen,
                )
                .is_none()
            {
                return Err(PyRuntimeError::new_err(
                    "ScreenCaptureKit rejected the window frame output handler",
                ));
            }
            stream
                .start_capture()
                .map_err(|error| runtime_error("failed to start ScreenCaptureKit", error))?;
            Ok(Self {
                backend: Some(MacBackend {
                    stream,
                    mailbox,
                    window_id,
                }),
                stopped: false,
                window_closed: false,
            })
        }

        #[cfg(target_os = "linux")]
        {
            if show_cursor {
                return Err(PyValueError::new_err(
                    "show_cursor is not supported by this window backend",
                ));
            }
            if show_highlight {
                return Err(PyValueError::new_err(
                    "show_highlight is not supported by this window backend",
                ));
            }
            let window = find_xcap_window(window_id)?;
            Ok(Self {
                backend: Some(SnapshotBackend {
                    window,
                    window_id,
                    crop,
                    resolution,
                    frame_interval: Duration::from_secs_f64(1.0 / f64::from(fps)),
                    next_due: Instant::now(),
                }),
                stopped: false,
                window_closed: false,
            })
        }

        #[cfg(not(any(target_os = "windows", target_os = "macos", target_os = "linux")))]
        {
            let _ = (window_id, show_cursor, show_highlight, crop, resolution);
            Err(PyRuntimeError::new_err(
                "this operating system is unsupported",
            ))
        }
    }

    /// Return `(timestamp, width, height, pixel_format, data)`, or `None` after a
    /// short timeout, while releasing the GIL.
    fn next_frame(&mut self, py: Python<'_>) -> PyResult<Option<PythonFrame>> {
        if self.stopped {
            return Err(PyRuntimeError::new_err("capturer has been stopped"));
        }
        if self.window_closed {
            return Ok(None);
        }
        let Some(frame) = py.detach(|| self.next_frame_inner())? else {
            return Ok(None);
        };
        let bytes = PyBytes::new(py, &frame.data).unbind();
        Ok(Some((
            frame.timestamp,
            frame.width,
            frame.height,
            "RGBA".to_owned(),
            bytes,
        )))
    }

    fn stop(&mut self) -> PyResult<()> {
        self.stop_inner()
    }

    #[getter]
    fn is_stopped(&self) -> bool {
        self.stopped
    }

    #[getter]
    fn is_window_closed(&self) -> bool {
        self.window_closed
    }
}

impl NativeCapturer {
    #[cfg(target_os = "windows")]
    fn next_frame_inner(&mut self) -> PyResult<Option<RawFrame>> {
        let backend = self
            .backend
            .as_ref()
            .ok_or_else(|| PyRuntimeError::new_err("capturer has been stopped"))?;
        match backend.mailbox.receive(Duration::from_millis(100)) {
            CapturePoll::Frame(frame) => resize_frame(frame, backend.resolution).map(Some),
            CapturePoll::Closed => {
                self.window_closed = true;
                Ok(None)
            }
            CapturePoll::Error(error) => Err(PyRuntimeError::new_err(error)),
            CapturePoll::Empty => Ok(None),
        }
    }

    #[cfg(target_os = "macos")]
    fn next_frame_inner(&mut self) -> PyResult<Option<RawFrame>> {
        let backend = self
            .backend
            .as_ref()
            .ok_or_else(|| PyRuntimeError::new_err("capturer has been stopped"))?;
        let window_id = backend.window_id;
        match backend.mailbox.receive(Duration::from_millis(100)) {
            CapturePoll::Frame(frame) => Ok(Some(frame)),
            CapturePoll::Error(error) => {
                if !mac_window_exists(window_id)? {
                    self.window_closed = true;
                    return Ok(None);
                }
                Err(PyRuntimeError::new_err(error))
            }
            CapturePoll::Empty => Ok(None),
        }
    }

    #[cfg(target_os = "linux")]
    fn next_frame_inner(&mut self) -> PyResult<Option<RawFrame>> {
        let backend = self
            .backend
            .as_mut()
            .ok_or_else(|| PyRuntimeError::new_err("capturer has been stopped"))?;
        let now = Instant::now();
        if backend.next_due > now {
            std::thread::sleep(backend.next_due - now);
        }
        backend.next_due = Instant::now() + backend.frame_interval;

        let image = match backend.window.capture_image() {
            Ok(image) => image,
            Err(error) => {
                let window_exists = Window::all()
                    .map_err(|list_error| runtime_error("failed to enumerate windows", list_error))?
                    .into_iter()
                    .any(|window| window.id().ok() == Some(backend.window_id));
                if !window_exists {
                    self.window_closed = true;
                    return Ok(None);
                }
                return Err(runtime_error("failed to capture window", error));
            }
        };
        let image = if let Some((x, y, width, height)) = backend.crop {
            let right = x.checked_add(width).ok_or_else(|| {
                PyValueError::new_err("crop coordinates overflow the frame bounds")
            })?;
            let bottom = y.checked_add(height).ok_or_else(|| {
                PyValueError::new_err("crop coordinates overflow the frame bounds")
            })?;
            if right > image.width() || bottom > image.height() {
                return Err(PyValueError::new_err(format!(
                    "crop ({x}, {y}, {width}, {height}) exceeds frame size {}x{}",
                    image.width(),
                    image.height()
                )));
            }
            image::imageops::crop_imm(&image, x, y, width, height).to_image()
        } else {
            image
        };
        resize_frame(
            RawFrame {
                timestamp: unix_timestamp(SystemTime::now()),
                width: image.width(),
                height: image.height(),
                data: image.into_raw(),
            },
            backend.resolution,
        )
        .map(Some)
    }

    #[cfg(not(any(target_os = "windows", target_os = "macos", target_os = "linux")))]
    fn next_frame_inner(&mut self) -> PyResult<Option<RawFrame>> {
        Err(PyRuntimeError::new_err(
            "this operating system is unsupported",
        ))
    }

    fn stop_inner(&mut self) -> PyResult<()> {
        if self.stopped {
            return Ok(());
        }
        self.stopped = true;

        #[cfg(target_os = "windows")]
        if let Some(mut backend) = self.backend.take() {
            backend.mailbox.stop();
            if let Some(control) = backend.control.take() {
                control.stop().map_err(|error| {
                    runtime_error("failed to stop Windows window capture", error)
                })?;
            }
        }
        #[cfg(target_os = "macos")]
        if let Some(backend) = self.backend.take() {
            backend.mailbox.stop();
            backend
                .stream
                .stop_capture()
                .map_err(|error| runtime_error("failed to stop ScreenCaptureKit", error))?;
        }
        #[cfg(target_os = "linux")]
        {
            self.backend.take();
        }
        Ok(())
    }
}

impl Drop for NativeCapturer {
    fn drop(&mut self) {
        let _ = self.stop_inner();
    }
}

#[pymodule]
fn _native(module: &Bound<'_, PyModule>) -> PyResult<()> {
    #[cfg(target_os = "macos")]
    initialize_core_graphics();

    module.add_class::<NativeWindowInfo>()?;
    module.add_class::<NativeCapturer>()?;
    module.add_function(wrap_pyfunction!(list_windows_native, module)?)?;
    module.add_function(wrap_pyfunction!(is_supported, module)?)?;
    module.add_function(wrap_pyfunction!(has_permission, module)?)?;
    module.add_function(wrap_pyfunction!(request_permission, module)?)?;
    module.add_function(wrap_pyfunction!(backend_name, module)?)?;
    Ok(())
}

#[cfg(all(test, target_os = "macos"))]
mod tests {
    use super::executable_is_window_server;

    #[test]
    fn identifies_window_server_by_executable_basename() {
        assert!(executable_is_window_server(
            b"/System/Library/PrivateFrameworks/SkyLight.framework/Resources/WindowServer\0"
        ));
        assert!(!executable_is_window_server(
            b"/Applications/WindowServer Helper"
        ));
    }
}
