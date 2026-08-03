# COMSOL MCP Settings Guide

Applies to COMSOL MCP `0.6.2` and settings schema `1.2.0`.

The Settings GUI is the primary way to configure COMSOL MCP. Direct JSON editing
remains supported for developers, agents, automation, recovery, and advanced
deployments. Ordinary users should not need to edit JSON by hand. Both methods
write the same shared `settings.json`; there is no per-agent configuration file.

Saved changes take effect only after restarting Codex or the MCP client that owns
the server process.

## Open the Settings GUI

From any MCP profile, call:

```text
settings.start
```

The installed command-line fallback is:

```powershell
comsol-mcp-settings
comsol-mcp-settings --settings-path "D:\settings\settings.json"
comsol-mcp-settings --settings-path "D:\settings\settings.json" --validate-only
```

This executable starts the GUI directly; it does not require or start an MCP
stdio host, COMSOL, or Java. Use `--settings-path` to bind it to the exact file
used by the MCP client. `--validate-only` verifies the package, settings target,
GUI runtime, and shortcut prerequisites without importing Tk or writing a file.

The About page provides explicit **Create desktop shortcut** and **Remove desktop
shortcut** actions. The equivalent commands are:

```powershell
comsol-mcp-settings --settings-path "D:\settings\settings.json" --create-desktop-shortcut
comsol-mcp-settings --settings-path "D:\settings\settings.json" --shortcut-status
comsol-mcp-settings --settings-path "D:\settings\settings.json" --remove-desktop-shortcut
```

The per-user link is named `COMSOL MCP Settings.lnk` and remains bound to that
exact settings file. Installation, deployment, MCP startup, `settings.start`,
first launch, Save, and Apply never create it. A stale or foreign same-name item
is preserved until the user confirms replacement in the GUI or repeats the
create command with `--replace-existing-shortcut`. Remove deletes only an owned
shortcut; it preserves foreign or damaged Desktop items.

From a repository checkout or unpacked source distribution, use the root manual
launcher:

```powershell
.\Open_Settings_GUI.ps1
.\Open_Settings_GUI.ps1 -PythonPath "D:\path\to\python.exe"
.\Open_Settings_GUI.ps1 -PythonPath "D:\path\to\python.exe" -SettingsPath "D:\settings\settings.json"
.\Open_Settings_GUI.ps1 -ValidateOnly
```

Without `-PythonPath`, the script checks the active virtual environment, the
environment containing `comsol-mcp-settings`, `python.exe` on `PATH`, and the
Windows Python launcher for CPython 3.14. An explicit `-SettingsPath` must be
absolute and its parent directory must already exist. `-ValidateOnly` verifies
the interpreter, package import, and optional settings locator, prints a
path-free JSON receipt, and does not create the settings file, open Tk, or start
COMSOL.

Opening Settings does not start COMSOL or begin a calculation. After an agent opens
the window, it must pause and let the user finish instead of editing the JSON in
the background.

The first time an installed copy is used, the window asks whether it may create the
settings. Nothing is written until the user confirms. Confirmation creates:

```text
%LOCALAPPDATA%\comsol_mcp\settings.json
%LOCALAPPDATA%\comsol_mcp\models

%PROGRAMDATA%\comsol_mcp\runtime
%PROGRAMDATA%\comsol_mcp\artifacts
```

The first two locations work when the Windows user name contains Chinese or other
non-ASCII characters. The last two store working records, locks, and formal
outputs; they must contain only ASCII characters, so they are placed under the
normally ASCII `%PROGRAMDATA%` folder. Folders for optional features are not
created.

If no COMSOL path has been saved, the window opens the `COMSOL/Java` page and looks
for COMSOL 6.4 and its included Java:

- If exactly one usable installation is found, its paths appear in the boxes but
  are not saved automatically.
- If none is found, the boxes stay empty. The window does not invent a path or stop
  the user with an error.
- If several installations are found, the user chooses one; the window does not
  guess.
- After installing or moving COMSOL, click `Auto-detect` to look again.
- Click `Browse` to choose a folder manually.

## Use the GUI

Every option shows the exact name used in JSON. Path options include examples and
can also be selected with `Browse`.

- `Apply` checks and saves the settings, then keeps the window open.
- `Save and Exit` checks and saves the settings, then closes the window.
- `Cancel` closes the window without saving the current changes.
- A value that cannot be used is shown in red, and saving stays disabled until it
  is corrected.
- After the user changes a setting, the window explains that Codex or the MCP
  client must be restarted.
- Paths found automatically show the lasting restart message without displaying a
  restart pop-up as soon as the window opens.
- Changing the interface language keeps unsaved values and the current page.
- Changing interface size previews the selected scale immediately. Choose
  `Follow Windows display settings` unless a fixed 100%, 125%, 150%, or 200% size
  is more comfortable.
- The explanation below `profile.name` changes with the selected profile and says
  what it is for and when it should be avoided.

Only one Settings window can edit a file at a time. If the user opens it again, the
second window explains that Settings is already open. If another program changes
the same file while the window is open, saving stops so that neither copy silently
overwrites the other.

Language choices are displayed by self-name and stored key:

```text
English (en)
简体中文 (zh-cn)
繁體中文 (zh-tw)
```

Legacy `zh_CN` and `zh_TW` values are read and normalized to `zh-cn` and `zh-tw`.

## Where Settings Are Read

The read and write target is resolved in this order:

1. The absolute file selected by `COMSOL_MCP_SETTINGS_PATH`.
2. The project-root `settings.json` in a source checkout.
3. `%LOCALAPPDATA%\comsol_mcp\settings.json` in an installed deployment.
4. The packaged read-only template, used only to offer first-run setup.

The GUI never writes into `site-packages`. A settings path may contain Unicode,
but it must be an ordinary file whose path contains no symbolic link or junction.

The document must be UTF-8 JSON, contain exactly one object, contain no duplicate
keys, and be no larger than 64 KiB. Unknown fields and invalid values are reported
through `capabilities.project_settings.settings_errors` without exposing local
paths. A missing field uses its safe default. An invalid leaf falls back only that
leaf. Malformed JSON falls back to the complete default document.

## Path Rules

The portable prefixes `%LOCALAPPDATA%` and `%PROGRAMDATA%` are expanded when a
settings document is loaded. Other environment-variable tokens are not expanded.
All configured paths must be absolute; empty strings, relative paths, and control
characters are invalid.

| Path type | Unicode support | Default |
| --- | --- | --- |
| Settings file | Yes | `%LOCALAPPDATA%\comsol_mcp\settings.json` |
| Model-read roots | Yes | `%LOCALAPPDATA%\comsol_mcp\models` |
| Runtime and solver lease | ASCII only | `%PROGRAMDATA%\comsol_mcp\runtime` |
| Durable jobs override | ASCII only | `null`, derived below runtime |
| Owned artifacts | ASCII only | `%PROGRAMDATA%\comsol_mcp\artifacts` |
| COMSOL and Java | Installation-dependent | `null`, discovered by the GUI |
| Semantic assets | Backend-dependent; use ASCII when possible | `null` |

Model-read roots may contain Chinese or other Unicode characters. Runtime,
durable-job, and artifact roots containing non-ASCII characters are rejected by
the GUI before save and by backend validation when JSON is edited directly.

## Settings Reference

### Document and Interface

| Key | Default | Meaning and accepted values |
| --- | --- | --- |
| `schema_name` | `"comsol_mcp.settings"` | Read-only schema identity; must match exactly. |
| `schema_version` | `"1.2.0"` | New writes use `1.2.0`; `1.0.0` and `1.1.0` are read and migrated in memory. |
| `gui.language` | `"zh-cn"` | `"en"`, `"zh-cn"`, or `"zh-tw"`. |
| `gui.scale` | `"system"` | `"system"`, `"100"`, `"125"`, `"150"`, or `"200"`. The GUI displays the numeric choices as percentages. |

### Tool Profile

| Key | Default | Meaning and accepted values |
| --- | --- | --- |
| `profile.name` | `"core"` | `core`, `basic_fem`, `wave_optics`, `experimental`, or `full`. The stored value is lower-case. Unsupported values fall back to `core` with reported provenance. |

New users can begin with the smaller `core` surface when safety is the priority.
Most users doing ordinary simulation should choose `basic_fem`. Profiles only
control COMSOL automation/simulation and future autonomous-exploration tool
visibility. Shared collaboration and semantic retrieval use independent Boolean
feature gates and may be enabled together for any profile.

| Profile | Best suited to |
| --- | --- |
| `core` | Safety-first default for new users: fewer operations, model inspection, jobs, careful single-point checks, and manual search. |
| `basic_fem` | Recommended for most users: ordinary FEM construction, result export, and Windows standalone packages. |
| `wave_optics` | Optical and metasurface work, field review, Wave Optics checks, point audits, and staged parameters. |
| `experimental` | Extra helpers that are broader or less mature and require careful review. |
| `full` | Legacy migration that needs nearly every non-feature tool and accepts weaker file containment; not recommended for new users. |

### Runtime and Containment

| Key | Default | Meaning and accepted values |
| --- | --- | --- |
| `runtime.directory` | `%PROGRAMDATA%\comsol_mcp\runtime` | Server-owned runtime and lease root. Use `null` only to delegate to the legacy platform fallback. A configured value must be absolute and ASCII-only. |
| `runtime.jobs_directory` | `null` | Optional durable-job override. `null` derives the job directory from the effective runtime root. A configured value must be absolute and ASCII-only. |
| `paths.model_read_roots` | `[%LOCALAPPDATA%\comsol_mcp\models]` | Approved immutable source-model roots. Values must be unique absolute paths; Unicode is supported. `[]` deliberately rejects every model input. |
| `paths.artifact_write_root` | `%PROGRAMDATA%\comsol_mcp\artifacts` | MCP-owned artifacts, manifests, and evidence. Use `null` only to derive the legacy owned-artifact root. A configured value must be absolute and ASCII-only. |

A path accepted by settings validation is not automatically authorized for every
operation. Tools still enforce existence, containment, extension, link/junction,
overwrite, and operation-specific rules.

### COMSOL and Java

| Key | Default | Meaning and accepted values |
| --- | --- | --- |
| `comsol.installation_root` | `null` | COMSOL Multiphysics 6.4 installation root. The GUI can discover it without starting COMSOL. An explicit standalone tool argument has precedence. |
| `java.java_home` | `null` | Optional Java runtime home. Auto-detection prefers the COMSOL-bundled runtime. |
| `java.jdk_home` | `null` | Optional JDK home. It commonly matches `java.java_home` for the validated COMSOL runtime. |

Java lookup order is the usable COMSOL-bundled runtime, `JAVA_HOME`, `JDK_HOME`,
then `PATH`. Auto-detect never replaces a non-null value without confirmation.

### Shared Server and Ownership

| Key | Default | Meaning and accepted values |
| --- | --- | --- |
| `shared_server.enabled` | `false` | Independent Boolean gate for the explicit local Desktop/attached-Server workflow. It composes with every profile and never starts or terminates the user's COMSOL Server. |
| `ownership.owner` | `null` | Optional non-empty owner label, at most 256 characters and without control characters. `null` derives a bounded label from the parent process. |

### Evidence Integrity

| Key | Default | Meaning |
| --- | --- | --- |
| `evidence_integrity.checks.outcome_contract_validation` | `true` | Validates declared execution and outcome contracts. |
| `evidence_integrity.checks.artifact_chain_verification` | `true` | Verifies artifact bytes, provenance, and hash chains. |
| `evidence_integrity.checks.summary_claim_verification` | `true` | Checks summary claims against cited artifact values. |
| `evidence_integrity.checks.producer_driver_compatibility` | `true` | Verifies producer and driver identity before resume. |

Disabling any check is an exploration opt-out. Affected formal results remain
explicitly unverified.

### Optional Semantic Retrieval

| Key | Default | Meaning and accepted values |
| --- | --- | --- |
| `semantic_docs.enabled` | `false` | Independent Boolean gate for the isolated semantic tools. It composes with every profile and with `shared_server.enabled`. |
| `semantic_docs.root` | `null` | Optional root for preprocessed semantic retrieval assets. This is not COMSOL's bundled manual directory and is not auto-detected. |
| `semantic_docs.lexical_index` | `null` | Optional immutable SQLite lexical index file. |
| `semantic_docs.model_path` | `null` | Optional local semantic-model revision directory. |

Leaving the asset values at `null` is valid. Enabling `semantic_docs.enabled`
becomes useful only after the required preprocessed assets exist.

## Developer and Agent JSON Editing (Advanced)

Use JSON when a developer or agent needs reproducible automation, an installer
manages settings, the GUI cannot be used, or recovery requires it. Agent editing
requires an explicit user request. Stop the MCP host and close the GUI before
editing. Modify only the resolved writable file, validate it, then restart the
exact owning client.

Canonical default template:

```json
{
  "schema_name": "comsol_mcp.settings",
  "schema_version": "1.2.0",
  "profile": {"name": "core"},
  "runtime": {
    "directory": "%PROGRAMDATA%/comsol_mcp/runtime",
    "jobs_directory": null
  },
  "paths": {
    "model_read_roots": ["%LOCALAPPDATA%/comsol_mcp/models"],
    "artifact_write_root": "%PROGRAMDATA%/comsol_mcp/artifacts"
  },
  "shared_server": {"enabled": false},
  "evidence_integrity": {
    "checks": {
      "outcome_contract_validation": true,
      "artifact_chain_verification": true,
      "summary_claim_verification": true,
      "producer_driver_compatibility": true
    }
  },
  "semantic_docs": {
    "enabled": false,
    "root": null,
    "lexical_index": null,
    "model_path": null
  },
  "ownership": {"owner": null},
  "java": {"java_home": null, "jdk_home": null},
  "comsol": {"installation_root": null},
  "gui": {"language": "zh-cn", "scale": "system"}
}
```

Existing `COMSOL_MCP_*`, `COMSOL_SEMANTIC_*`, `JAVA_HOME`, and `JDK_HOME`
variables remain compatibility overrides. A value already present in the process
environment has precedence over the corresponding JSON-derived environment value.
New user deployments should use the GUI. Developer and agent automation may use
the same JSON file, but must not create separate environment-only configurations.

## Update and Recovery

Before updating or reinstalling, back up the effective writable `settings.json`.
If `COMSOL_MCP_SETTINGS_PATH` is set, back up that exact file. After installation,
restore or review the settings, restart the MCP client, and inspect
`capabilities.project_settings` for:

```text
configuration_state: valid
settings_errors: []
setup_required: false
```

If the file is missing, malformed, duplicated-key, non-UTF-8, oversized, or uses
an unsupported future schema, the GUI offers only bounded recovery or exit. A
confirmed recovery preserves one damaged copy and writes canonical `1.2.0`
settings atomically.

For the evidence-check meanings, see
[`../evidence_integrity/README.md`](../evidence_integrity/README.md). For the
default-off shared Desktop/Server workflow, see
[`../interactive_shared_session/README.md`](../interactive_shared_session/README.md).
