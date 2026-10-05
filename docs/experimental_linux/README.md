# Experimental Ubuntu support

Version 0.7.7 adds an experimental solver-free lane for Ubuntu 26.04 x64.
Local acceptance uses WSL2. Independent hosted acceptance uses the Ubuntu GitHub runner.
These gates do not establish Linux COMSOL support or native agent interoperability.
Windows remains the accepted native solver platform.

## Supported scope

Use offline model inspection, evidence reads, keyword manual lookup, discovery, and the shared Tk Settings GUI.
The server retains the same profiles, tool names, and settings schema.
Read `platform_support` and `available_tools` from `capabilities` before selecting an action.
Catalog visibility does not mean that a native solver action is available.
Native execution, shared Server attachment, Model Manager actions, and production simulation jobs refuse before solver startup.
A refusal reports `unsupported_platform`, no solver start, and no filesystem modification.

Linux semantic search acceptance is deferred.
A cold `semantic_status` read is available. A warm request and semantic execution are unavailable.
Internal fake workers test Tasks and durable state. Public clients cannot submit fake simulations.
The 2026-07-28 core revision uses per-request metadata instead of an initialize handshake.
Older revisions retain initialize negotiation and ordinary tools.
Task TTL does not delete a running job or its evidence. This implementation retains completed mappings after TTL too.
SDK conformance does not establish native Claude or OpenCode acceptance.

## Installation

Use Python 3.14 and a short ASCII environment path.
Apply `constraints/release_locked_ubuntu_py314.txt` with `--require-hashes --only-binary=:all:`.
Install the exact reviewed project wheel with `--no-deps`.
Run `python -m pip check` outside the checkout.
Run the installed package and stdio probes with the same interpreter.
The Ubuntu lock retains the accepted runtime versions. It excludes the Windows-only `pywin32` package.
The `manuals` extra provides keyword PDF indexing. It does not include COMSOL manuals.
Use a licensed local corpus. Do not distribute its PDFs or generated private evidence.

## Paths and settings

| Purpose | Default |
| --- | --- |
| Settings | `$XDG_CONFIG_HOME/comsol-mcp/settings.json`, otherwise `~/.config/comsol-mcp/settings.json` |
| Runtime | `$XDG_STATE_HOME/comsol-mcp/runtime`, otherwise `~/.local/state/comsol-mcp/runtime` |
| Models | `$XDG_DATA_HOME/comsol-mcp/models`, otherwise `~/.local/share/comsol-mcp/models` |
| Artifacts | `$XDG_DATA_HOME/comsol-mcp/artifacts`, otherwise `~/.local/share/comsol-mcp/artifacts` |
| Optional launcher | `$XDG_DATA_HOME/applications/comsol-mcp-settings.desktop` |

XDG overrides must be absolute paths. Runtime and artifact roots must contain ASCII characters only.
Settings and permitted model paths retain Unicode support.
Explicit configuration and environment overrides keep their existing precedence.
The packaged Windows default tokens select native defaults on Linux.
Other configured Windows paths are not translated into guessed Linux paths.
Invalid roots produce configuration errors. They do not select a temporary fallback.

## GUI and ownership

Install Tk, CJK fonts, `desktop-file-utils`, and `gio` for graphical launcher checks.
CI uses Xvfb. Local visual acceptance uses WSLg.
The GUI uses one shared view, controller, form, and gettext catalog.
Its Linux notice states the solver-free and semantic acceptance limits.
The optional launcher binds the installed entry and exact settings token.
Creation and removal preserve foreign or changed launcher files.

POSIX `flock` serializes cooperating editors.
It cannot prevent a noncooperating process from replacing a pathname.
Baseline identity and content checks reject detected changes.
Read pins detect persistent file or ancestor changes and refuse success.
They do not provide Windows write-denying handles or universal protection against hostile replacement races.
Stable private lock files remain on disk. A retained file does not mean that a lock is held.
Cleanup removes only proven owned files. An unresolved cleanup conflict remains an error.
