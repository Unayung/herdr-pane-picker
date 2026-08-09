# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project follows [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- Linux support: the manifest now declares the `linux` platform, and badge
  rebuilding falls back to common Linux system fonts (DejaVu, Liberation).

### Fixed

- Hint badges are resized to the client's reported cell geometry before being
  sent, so terminals whose 8×4-cell hint box is smaller than the native 128 px
  asset no longer show a badge cropped to its top-left corner. Resized badges
  are cached per geometry under `~/.local/state/herdr-pane-picker/badges/`.

## [0.1.1] - 2026-07-18

### Changed

- Hint badges are twice as large: an 8×4-cell footprint rendered from 128 px
  assets, so large panes get a clearly visible hint without upscaling blur.

### Fixed

- Demo panes no longer lose a keypress that arrives while the pane is still
  initializing, and no longer hang or exit nonzero when the pane's PTY is
  closed while unread output remains (terminal restore uses `TCSANOW`; the
  standard streams are detached before interpreter shutdown).

### Removed

- The internal demo-layout troubleshooting document; the changelog and README
  carry the relevant guidance.

## [0.1.0] - 2026-07-17

### Added

- Home-row-first character hints drawn directly over panes.
- Portable Herdr popup action for terminal-independent key capture.
- Optional popup-free WezTerm integration with native `PaneSelect` fallback.
- Pre-rendered rounded PNG badges with a dependency-free RGBA fallback.
- Automatic overlay cleanup and structured local error logging.
- Fictional demo layout for privacy-safe documentation screenshots.

### Fixed

- An inherited `HERDR_SOCKET_PATH` no longer redirects the demo to the live
  session when `HERDR_SESSION` is set explicitly; the isolation guard now also
  refuses any session that already holds tabs, regardless of its name.
- The demo works in fresh headless sessions (creates the first workspace
  itself) and `--close` removes the workspace when the demo tab is its last
  tab.

- The demo layout now creates its own `pane-picker-demo` tab instead of
  replacing the active tab, refuses to run against the default session, and
  gains a `--close` teardown flag. Previously it could destroy a real layout,
  and closing the last demo pane closed the user's tab with it.
- Demo panes now end on any keypress and on stdin EOF, in addition to
  SIGINT/SIGTERM/SIGHUP. A signal wakeup pipe closes a race where a
  termination signal could arrive before the pane blocked on input and be
  lost until the next keypress.

### Changed

- Send graphics updates sequentially to avoid local-socket contention and show
  four badges in approximately 5–7 ms on the reference system.

[Unreleased]: https://github.com/ugurtarlig/herdr-pane-picker/compare/v0.1.1...HEAD
[0.1.1]: https://github.com/ugurtarlig/herdr-pane-picker/compare/v0.1.0...v0.1.1
[0.1.0]: https://github.com/ugurtarlig/herdr-pane-picker/releases/tag/v0.1.0
