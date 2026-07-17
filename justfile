set windows-shell := ["pwsh.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-NoLogo", "-Command"]

macos-build-env := if os() == "macos" { "CARGO_TARGET_DIR=target/macos-13 MACOSX_DEPLOYMENT_TARGET=13.0" } else { "" }

# List the available commands.
default:
    @just --list

# Install all development dependencies without building WindowPulse itself.
setup:
    uv sync --all-extras --no-install-project --inexact

# Compile and install the native extension into .venv for local development.
compile *flags: setup
    {{ macos-build-env }} uv run --no-sync maturin develop --uv --extras video {{ flags }}

# Build a release wheel in dist/.
build *flags: setup
    {{ macos-build-env }} uv run --no-sync maturin build --release --out dist {{ flags }}

# Run the Python test suite (extra pytest arguments are accepted).
test *flags: compile
    uv run --no-sync pytest {{ flags }}

# Check Python lint rules and types without modifying files.
lint *flags: setup
    uv run --no-sync ruff check python tests {{ flags }}
    uv run --no-sync pyright python

# Apply Ruff's safe lint fixes.
fix *flags: setup
    uv run --no-sync ruff check --fix python tests {{ flags }}

# Type-check the public Python package.
typecheck *flags: setup
    uv run --no-sync pyright python {{ flags }}

# Format Python and Rust sources.
format *flags: setup
    uv run --no-sync ruff format python tests {{ flags }}
    cargo fmt --all

# Verify formatting without modifying files.
format-check: setup
    uv run --no-sync ruff format --check python tests
    cargo fmt --all -- --check

# Run Rust compiler and Clippy checks.
rust-check:
    cargo check --all-targets --all-features
    cargo clippy --all-targets --all-features -- -D warnings

# Run formatting, lint, typing, Rust, and test checks.
check: format-check lint rust-check test

# List windows that can be selected for capture.
windows: compile
    uv run --no-sync windowpulse-watch --list-windows

# Stream changed frames; pass CLI options after the recipe name.
watch *args: compile
    uv run --no-sync windowpulse-watch {{ args }}

# Record a video; for example: just video output.mp4 --title PowerPoint
video *args: compile
    uv run --no-sync windowpulse-video {{ args }}

# Show help for both installed command-line programs.
cli-help: compile
    uv run --no-sync windowpulse-watch --help
    uv run --no-sync windowpulse-video --help
