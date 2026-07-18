# Repository layout

This document is the complete tracked-file map for developers and coding agents.
Generated files, caches, local runtime artifacts, licensed manuals, and private
models are intentionally absent.

## Repository root and automation

- `.gitattributes` — This file defines repository text and line-ending attributes.
- `.gitignore` — This file excludes generated, local, and sensitive artifacts from Git.
- `.github/workflows/ci.yml` — This workflow runs the blocking Python build and test gates.
- `.github/workflows/dependency_report.yml` — This workflow produces the scheduled information-only dependency report.
- `LICENSE` — This file contains the repository MIT license.
- `README.md` — This file is the primary English project introduction and usage guide.
- `README_CN.md` — This file is the Chinese project introduction and usage guide.
- `DEPLOYMENT.md` — This file explains the supported English deployment procedure.
- `DEPLOYMENT_CN.md` — This file explains the supported Chinese deployment procedure.
- `pyproject.toml` — This file defines package metadata, dependencies, build settings, tests, and the console entry point.
- `settings.json` — This file stores the shared grouped startup settings and safe defaults.

## Client configuration and dependency constraints

- `config/claude-code-mcp.example.json` — This file provides an example Claude Code stdio MCP configuration.
- `config/codex-mcp.example.toml` — This file provides an example Codex stdio MCP configuration.
- `config/hermes-mcp.example.yaml` — This file provides an example Hermes Agent stdio MCP configuration.
- `config/opencode-mcp.example.json` — This file provides an example opencode stdio MCP configuration.
- `constraints/release_locked_py314.txt` — This file locks the complete Python 3.14 runtime dependency set with hashes.
- `constraints/minimum_supported_py314.txt` — This file pins the reviewed minimum binary-installable Python 3.14 direct dependencies.
- `constraints/tested_versions.json` — This file records the human-reviewed direct dependency versions and compatibility lane.
- `docs/profile_migration.md` — This file explains static profile selection and migration from broader tool surfaces.
- `docs/evidence_integrity/README.md` — This file is the complete English evidence-integrity and anti-hallucination user guide.
- `docs/evidence_integrity/README_CN.md` — This file is the complete Chinese evidence-integrity and anti-hallucination user guide.
- `docs/evidence_integrity/default_settings.json` — This file is the tested all-checks-enabled evidence-integrity settings example.
- `docs/evidence_integrity/exploration_settings.json` — This file is the tested single-check opt-out exploration example.
- `docs/interactive_shared_session/README.md` — This file is the complete English interactive Desktop/Server collaboration guide.
- `docs/interactive_shared_session/README_CN.md` — This file is the complete Chinese interactive Desktop/Server collaboration guide.

## Standalone recipes

- `recipes/_paths.py` — This module provides shared ASCII-safe paths for standalone recipes.
- `recipes/mim_drude_sweep.py` — This script demonstrates a durable Drude-material parameter sweep.
- `recipes/mim_lml_continuous.py` — This script demonstrates a continuous layered-metal workflow.
- `recipes/mim_patch_partition.py` — This script demonstrates partitioned patch-metasurface construction.

## Development kit entry points

- `development_kit/__init__.py` — This file marks the repository-only development assets as a Python package.
- `development_kit/README.md` — This file is the starting guide for tests, release gates, fixtures, and handoff work.
- `development_kit/benchmarks/__init__.py` — This file marks repository-only benchmark drivers as a Python package.
- `development_kit/benchmarks/semantic_benchmark.py` — This script runs the frozen lexical and semantic retrieval benchmark.
- `development_kit/docs/layout.md` — This file maps every tracked repository file to one concise purpose statement.
- `development_kit/docs/legacy_phase_compatibility.md` — This file records the frozen compatibility allowlist for historical aliases.
- `development_kit/docs/release_checklist.md` — This file gives the ordered dependency, package, licensed, install, and restart release checklist.

## Release contracts and fixtures

- `development_kit/release/support_matrix.json` — This file declares release identity sources, profile states, and licensed-gate requirements.
- `development_kit/release/release_facts.json` — This generated view records live tool, profile, schema, and compatibility identities.
- `development_kit/release/profile_migration.json` — This receipt records the exact recommended-profile tool diff and compatibility replacement.
- `development_kit/release/vulnerability_allowlist.json` — This file records exact reviewed vulnerabilities with mandatory expiry dates.
- `development_kit/release/dependency_license_review.json` — This file records accepted license metadata for every declared runtime dependency.
- `development_kit/release/coverage_policy.json` — This file records the non-decreasing global coverage floor and owned per-file safety targets.
- `development_kit/release/planning_code_allowlist.json` — This file freezes historical planning-code matches by path, count, and hash.
- `development_kit/release/integration_fixtures/manifest.json` — This file inventories sanitized integration contracts and their canonical hashes.
- `development_kit/release/integration_fixtures/capacitor_clientapi_regression.json` — This contract defines the analytic capacitor clientapi regression.
- `development_kit/release/integration_fixtures/job_recovery_cancellation.json` — This contract defines durable recovery and cancellation acceptance.
- `development_kit/release/integration_fixtures/lexical_manual_retrieval.json` — This contract defines bounded lexical manual retrieval acceptance.
- `development_kit/release/integration_fixtures/passive_port_closure.json` — This contract defines passive port power-closure acceptance.
- `development_kit/release/integration_fixtures/periodic_mesh_audit.json` — This contract defines periodic mesh evidence acceptance.
- `development_kit/release/integration_fixtures/reference_air_polarization.json` — This contract defines reference-air polarization acceptance.
- `development_kit/release/integration_fixtures/reference_power_evidence.json` — This contract defines reference-power evidence acceptance.
- `development_kit/release/integration_fixtures/source_immutability.json` — This contract defines immutable-source acceptance.
- `development_kit/docs/namespace_migration.md` — This document declares the canonical namespace and bounded compatibility interval.

## Development and release scripts

- `development_kit/scripts/__init__.py` — This file marks repository-only release utilities as a Python package.
- `development_kit/scripts/dependency_license_gate.py` — This script emits a path-free receipt and fails on expired, missing, stale, or unmatched runtime dependency license reviews.
- `development_kit/scripts/quality_gate.py` — This script runs the ratcheted lint, format, typing, property, coverage, license, cold-start, and response-budget gates.
- `development_kit/scripts/generate_release_lock.py` — This script generates the complete hashed Windows Python release lock.
- `development_kit/scripts/installed_package_probe.py` — This script verifies installed discovery, schemas, profiles, and deployment identity without COMSOL startup.
- `development_kit/scripts/installed_stdio_probe.py` — This script verifies the installed console entry point over real MCP stdio transport.
- `development_kit/scripts/planning_code_gate.py` — This script verifies the exact frozen planning-code compatibility surface.
- `development_kit/scripts/python_compatibility_licensed_gate.py` — This script runs the pinned Python and COMSOL compatibility regression on a licensed host.
- `development_kit/scripts/reference_power_gate_preflight.py` — This script validates reference-power gate inputs without starting COMSOL.
- `development_kit/scripts/release_gate.py` — This script runs compile, test, package, clean-install, and installed-discovery gates.
- `development_kit/scripts/release_facts.py` — This script generates and checks the durable release-facts view from live implementation data.
- `development_kit/scripts/run_real_release_gate.py` — This script orchestrates the explicit serial licensed COMSOL release gate.
- `development_kit/scripts/sbom_probe.py` — This script generates a deterministic CycloneDX SBOM from the locked installed runtime.
- `development_kit/scripts/security_gate.py` — This script evaluates pip-audit findings against the exact expiring review policy.
- `development_kit/scripts/shared_interactive_licensed_gate.py` — This script runs bounded non-owning shared Desktop/Server prepare and readback acceptance phases.

## Test fixtures and frozen snapshots

- `development_kit/tests/__init__.py` — This file marks the dependency and process test suite as a Python package.
- `development_kit/tests/fixtures/semantic_retrieval_evaluation.json` — This fixture contains the frozen judged semantic retrieval queries.
- `development_kit/tests/snapshots/baseline_tool_schemas.json` — This snapshot freezes the baseline public tool schemas.
- `development_kit/tests/snapshots/full_tool_schemas.json` — This snapshot freezes every registered public tool schema.
- `development_kit/tests/snapshots/profile_tool_names.json` — This snapshot freezes tool membership for every static profile.

## Licensed and subprocess integration tests

- `development_kit/tests/integration/__init__.py` — This file marks explicit subprocess-isolated integration tests as a Python package.
- `development_kit/tests/integration/clientapi_property_acceptance.py` — This gate checks constrained clientapi property round trips without solving.
- `development_kit/tests/integration/coordinator_claim_kill.py` — This helper stops only the exact coordinator process after a durable claim.
- `development_kit/tests/integration/convergence_campaign_acceptance.py` — This runner executes one explicit licensed durable convergence campaign.
- `development_kit/tests/integration/branch_continuation_campaign_acceptance.py` — This runner executes one explicit licensed durable branch-continuation campaign.
- `development_kit/tests/integration/derived_geometry_acceptance.py` — This gate checks typed derived-geometry edits on controlled COMSOL input.
- `development_kit/tests/integration/durable_cancel_acceptance.py` — This gate checks real durable cancellation with an explicit local fixture.
- `development_kit/tests/integration/incidence_configuration_acceptance.py` — This gate checks typed periodic incidence mutation and readback.
- `development_kit/tests/integration/live_profile_acceptance.py` — This gate checks fresh-host profile discovery and bounded live calls.
- `development_kit/tests/integration/native_cancel_signature_probe.py` — This probe inspects native cancellation signatures without invoking cancellation.
- `development_kit/tests/integration/periodic_mesh_acceptance.py` — This gate checks periodic mesh audit and clone-only mesh smoke behavior.
- `development_kit/tests/integration/reference_power_acceptance.py` — This coordinator runs the licensed reference-power acceptance worker.
- `development_kit/tests/integration/spectral_characterization_acceptance.py` — This runner executes one explicit licensed durable spectral acceptance job.
- `development_kit/tests/integration/resource_admission_acceptance.py` — This gate checks detached resource admission on controlled COMSOL input.
- `development_kit/tests/integration/semantic_benchmark_soak.py` — This gate runs the frozen semantic retrieval soak and concurrent burst.
- `development_kit/tests/integration/semantic_profile_acceptance.py` — This gate checks fresh-stdio semantic profile discovery and tools.
- `development_kit/tests/integration/semantic_retrieval_acceptance.py` — This gate checks isolated semantic retrieval against a pinned local index.
- `development_kit/tests/integration/semantic_worker_containment.py` — This gate checks semantic worker hang and crash containment.
- `development_kit/tests/integration/test_native_cancel_candidate.py` — This gate checks the native cancellation candidate across fresh processes.
- `development_kit/tests/integration/test_real_comsol.py` — This module collects opt-in fresh-process COMSOL probes.
- `development_kit/tests/integration/wave_optics_point_audit_acceptance.py` — This gate checks the controlled one-point Wave Optics evidence matrix.
- `development_kit/tests/integration/wave_optics_preflight_acceptance.py` — This gate checks read-only Wave Optics preflight evidence.
- `development_kit/tests/integration/probes/capacitor.py` — This probe solves the standalone analytic capacitor fixture.
- `development_kit/tests/integration/probes/study_mesh.py` — This probe exercises standalone clientapi study and mesh behavior.
- `development_kit/tests/integration/probes/unicode_save.py` — This probe checks standalone Unicode-path model saving.

## Dependency and process tests

- `development_kit/tests/test_artifact_chain.py` — This module tests bounded solver-free artifact hash-chain verification.
- `development_kit/tests/test_attached_job_backend.py` — This module tests immutable attached-job targets, handoff, worker execution, resume, cancellation, and preservation.
- `development_kit/tests/test_async_solver.py` — This module tests asynchronous solver thread state with fake studies.
- `development_kit/tests/test_basic.py` — This module tests basic server helpers and registration assumptions.
- `development_kit/tests/test_branch_continuation.py` — This module tests ordered solver-free branch-continuation state binding and planning.
- `development_kit/tests/test_branch_continuation_acceptance_runner.py` — This module tests the explicit licensed continuation runner without starting COMSOL.
- `development_kit/tests/test_branch_continuation_campaign_job.py` — This module tests immutable bounded durable branch-continuation campaign specifications.
- `development_kit/tests/test_branch_continuation_campaign_rows.py` — This module tests hash-chained continuation state evidence and artifact replay.
- `development_kit/tests/test_branch_continuation_campaign_runner.py` — This module tests continuation composition, stopping, ambiguity, and exact resume.
- `development_kit/tests/test_branch_continuation_campaign_worker.py` — This module tests continuation worker ownership, later-state recovery, and cleanup failure.
- `development_kit/tests/test_cancel_state_machine.py` — This module tests deterministic cancellation state transitions without wall-clock sleeps.
- `development_kit/tests/test_clientapi_properties.py` — This module tests constrained clientapi property access with mocks.
- `development_kit/tests/test_control_plane_metrics.py` — This module tests bounded control-plane latency, overload, and fairness evidence.
- `development_kit/tests/test_control_plane_startup.py` — This module tests solver-free cold discovery and startup budgets.
- `development_kit/tests/conftest.py` — This module prepares the shared ASCII runtime parent for dependency-only tests.
- `development_kit/tests/test_convergence_campaign_job.py` — This module tests immutable bounded durable convergence campaign specifications.
- `development_kit/tests/test_convergence_campaign_rows.py` — This module tests hash-chained durable convergence level evidence and artifact replay.
- `development_kit/tests/test_convergence_campaign_runner.py` — This module tests composed spectral-level execution, convergence stopping, and exact resume.
- `development_kit/tests/test_convergence_campaign_worker.py` — This module tests convergence worker ownership, later-level recovery, and cleanup failure.
- `development_kit/tests/test_convergence_acceptance_runner.py` — This module tests the licensed convergence runner contract without starting COMSOL.
- `development_kit/tests/test_convergence_evaluation.py` — This module tests ordered solver-free convergence evidence and policy evaluation.
- `development_kit/tests/test_deployment_identity.py` — This module tests package version, build identity, and fresh-process deployment consistency.
- `development_kit/tests/test_derived_geometry.py` — This module tests typed derived-geometry edits without COMSOL.
- `development_kit/tests/test_durable_job_control_plane.py` — This module tests durable submission, reconciliation, status, cancellation, and resume behavior.
- `development_kit/tests/test_durable_primitives.py` — This module tests versioned canonical bytes, bounded hashing, atomic writes, and row recovery.
- `development_kit/tests/test_dependency_license_gate.py` — This module tests deterministic dependency-license receipts and fail-closed review behavior.
- `development_kit/tests/test_environment_identity.py` — This module tests redacted solver-free environment identity.
- `development_kit/tests/test_evidence_contracts.py` — This module tests physical evidence, policies, and immutable migration contracts.
- `development_kit/tests/test_evidence_integrity_controls.py` — This module tests default-on settings, explicit per-check opt-out, and fail-closed disclosure.
- `development_kit/tests/test_evidence_integrity_stdio.py` — This module discovers and invokes both evidence guard tools over real solver-free MCP stdio.
- `development_kit/tests/test_evidence_integrity_verifier.py` — This module tests settings-aware formal evidence verification and resume identity checks.
- `development_kit/tests/test_user_guides.py` — This module checks documented settings, tool names, warnings, and bilingual guide contracts.
- `development_kit/tests/test_field_artifacts.py` — This module tests bounded durable scalar field serialization.
- `development_kit/tests/test_field_bundle.py` — This module tests field-evidence request normalization and identity.
- `development_kit/tests/test_field_dataset.py` — This module tests read-only dataset adaptation to field evidence.
- `development_kit/tests/test_field_discovery.py` — This module tests locale-safe field dataset discovery.
- `development_kit/tests/test_field_interpolation.py` — This module tests bounded field interpolation onto declared grids.
- `development_kit/tests/test_field_manifest.py` — This module tests versioned field-evidence manifests and hashes.
- `development_kit/tests/test_field_matrix.py` — This module tests validation-matrix binding to field requests.
- `development_kit/tests/test_field_pipeline.py` — This module tests the raw-sample to durable field-evidence pipeline.
- `development_kit/tests/test_field_render.py` — This module tests isolated bounded field PNG rendering.
- `development_kit/tests/test_field_review.py` — This module tests paired field-review bundle assembly.
- `development_kit/tests/test_field_sampling.py` — This module tests bounded slice selection from raw field samples.
- `development_kit/tests/test_field_tools.py` — This module tests public field discovery and extraction adapters.
- `development_kit/tests/test_geometry.py` — This module tests geometry helpers without a COMSOL client.
- `development_kit/tests/test_incidence_config.py` — This module tests typed periodic incidence preview and mutation gates.
- `development_kit/tests/test_integration_boundaries.py` — This module tests isolation and safety boundaries for integration probes.
- `development_kit/tests/test_installed_stdio_probe.py` — This module tests installed stdio probe result decoding.
- `development_kit/tests/test_job_state_stress.py` — This module stress-tests durable state readers and writers without COMSOL.
- `development_kit/tests/test_lexical_manual.py` — This module tests bounded SQLite lexical manual search and page reading.
- `development_kit/tests/test_material_expressions.py` — This module tests solver-free dispersive material-expression previews.
- `development_kit/tests/test_mesh.py` — This module tests mesh helpers without a COMSOL client.
- `development_kit/tests/test_mim_patch.py` — This module tests patch-metasurface helper behavior without a COMSOL client.
- `development_kit/tests/test_model.py` — This module tests model management helpers without a COMSOL client.
- `development_kit/tests/test_outcome_contract.py` — This module tests orthogonal execution, evidence, and scientific outcome contracts.
- `development_kit/tests/test_operation_arbiter.py` — This module tests durable serialization and responsive control-plane operation classes.
- `development_kit/tests/test_native_cancel_probe.py` — This module tests native cancellation discovery and allowlisting without COMSOL.
- `development_kit/tests/test_namespace_compatibility.py` — This module tests canonical package identity and the bounded legacy import interval.
- `development_kit/tests/test_ownership.py` — This module tests solver ownership, leases, and collision detection.
- `development_kit/tests/test_parameters.py` — This module tests parameter tools without a COMSOL client.
- `development_kit/tests/test_path_policy.py` — This module tests configured model-read and owned-artifact path containment.
- `development_kit/tests/test_periodic_mesh_audit.py` — This module tests periodic mesh evidence and clone-only smoke logic.
- `development_kit/tests/test_physics.py` — This module tests physics helpers without a COMSOL client.
- `development_kit/tests/test_portfolio_verifier.py` — This module tests policy-free summary citations against exact hashed evidence chains.
- `development_kit/tests/test_power_audit.py` — This module tests solver-free declared physical-power evidence.
- `development_kit/tests/test_process_control.py` — This module tests exact-identity process inspection and termination policy.
- `development_kit/tests/test_process_inventory_stress.py` — This module stress-tests host inventory under PID churn without COMSOL.
- `development_kit/tests/test_property_transport.py` — This module tests bounded JSON transport for clientapi properties.
- `development_kit/tests/test_public_input_contracts.py` — This module tests bounded discovery schemas and matching pre-side-effect runtime limits.
- `development_kit/tests/test_quality_properties.py` — This module provides seeded property tests and exhaustive safety-decision branch cases for foundation contracts.
- `development_kit/tests/test_quality_gate.py` — This module tests exact coverage floors and fail-closed quality-policy evaluation.
- `development_kit/tests/test_real_fixture_contract.py` — This module tests portable contracts for controlled licensed fixtures.
- `development_kit/tests/test_recipe_paths.py` — This module tests standalone recipe output path policy.
- `development_kit/tests/test_reference_power_acceptance.py` — This module tests reference-power acceptance contracts and preflight.
- `development_kit/tests/test_reference_power_gate.py` — This module tests pure reference-power receipt evaluation.
- `development_kit/tests/test_reference_power_release_orchestrator.py` — This module tests mandatory serial release orchestration with fake processes.
- `development_kit/tests/test_reference_power_runner.py` — This module tests reference-power coordinator and worker process boundaries.
- `development_kit/tests/test_release_engineering.py` — This module tests repository, dependency, fixture, archive, and release policies.
- `development_kit/tests/test_release_facts.py` — This module tests the generated release-facts view against live implementation data.
- `development_kit/tests/test_release_receipts.py` — This module tests deterministic SBOM and release inventory receipts.
- `development_kit/tests/test_resource_admission.py` — This module tests resource policy normalization, telemetry, and admission decisions.
- `development_kit/tests/test_results.py` — This module tests result normalization without a COMSOL client.
- `development_kit/tests/test_runtime_paths.py` — This module tests shared ASCII-safe runtime and lease paths.
- `development_kit/tests/test_schema_registry.py` — This module tests named schema coverage and version support resolution.
- `development_kit/tests/test_security_gate.py` — This module tests vulnerability report parsing and expiring allowlist policy.
- `development_kit/tests/test_semantic_contracts.py` — This module tests semantic benchmark contracts, limits, and import safety.
- `development_kit/tests/test_semantic_index.py` — This module tests immutable semantic index construction and publication.
- `development_kit/tests/test_semantic_retrieval.py` — This module tests deterministic vector retrieval, filtering, fusion, and cache identity.
- `development_kit/tests/test_semantic_tools.py` — This module tests semantic profile schemas, configuration, and degradation behavior.
- `development_kit/tests/test_semantic_worker_protocol.py` — This module tests isolated semantic worker protocol and containment.
- `development_kit/tests/test_server.py` — This module tests server construction and capabilities without starting a transport.
- `development_kit/tests/test_settings.py` — This module tests grouped settings defaults, validation, and fallback errors.
- `development_kit/tests/test_shared_attach_request.py` — This module tests the complete pre-lease shared-server attach gate.
- `development_kit/tests/test_shared_cleanup_contracts.py` — This module tests non-owning detach and owned-cleanup outcome semantics.
- `development_kit/tests/test_shared_interactive_licensed_gate.py` — This module tests the licensed shared interactive gate's solver-free specification path.
- `development_kit/tests/test_shared_model_locking.py` — This module tests bounded shared-model revisions and exact enforcement locks.
- `development_kit/tests/test_shared_operation_dependencies.py` — This module tests shared operations against the reused arbiter and path-containment dependencies.
- `development_kit/tests/test_shared_process_probe.py` — This module tests redacted Windows process, listener, window, and version inventory.
- `development_kit/tests/test_shared_session_contracts.py` — This module tests default-off feature and loopback endpoint contracts.
- `development_kit/tests/test_shared_session_identity.py` — This module tests attached-server and exact model-selector identities.
- `development_kit/tests/test_shared_session_lifecycle.py` — This module tests fake-client attach, failure cleanup, and external-resource-preserving detach.
- `development_kit/tests/test_shared_session_tools.py` — This module tests the public default-off shared lifecycle tools and capability surface.
- `development_kit/tests/test_shared_server_preflight.py` — This module tests two-probe Desktop, listener, collision, and COMSOL release-line classification.
- `development_kit/tests/test_spectral_characterization.py` — This module tests provenance-bound offline spectral validation and measurements.
- `development_kit/tests/test_spectral_audit.py` — This module tests strict projection of point-audit artifacts into durable spectral rows.
- `development_kit/tests/test_spectral_acceptance_runner.py` — This module tests the licensed spectral runner contract without starting COMSOL.
- `development_kit/tests/test_spectral_characterization_job.py` — This module tests immutable bounded durable spectral job specifications.
- `development_kit/tests/test_spectral_progress.py` — This module tests adaptive spectral transitions and policy-separated scientific outcomes.
- `development_kit/tests/test_spectral_rows.py` — This module tests hash-chained durable spectral rows and exact artifact-bound resume.
- `development_kit/tests/spectral_job_fixtures.py` — This module creates sanitized fake point-audit artifacts for durable spectral tests.
- `development_kit/tests/test_spectral_runner.py` — This module tests the adaptive spectral point loop, summaries, and fault recovery.
- `development_kit/tests/test_spectral_level_execution.py` — This module tests loaded-model spectral execution with shared resource and collector machinery.
- `development_kit/tests/test_spectral_stages.py` — This module tests immutable spectral stage planning, freezing, and replay.
- `development_kit/tests/test_spectral_worker.py` — This module tests injected spectral worker ownership, resources, state, and cleanup.
- `development_kit/tests/test_study.py` — This module tests study helpers without a COMSOL client.
- `development_kit/tests/test_tool_catalog.py` — This module tests deterministic tool catalog metadata and discovery.
- `development_kit/tests/test_tool_profiles.py` — This module tests static profile selection, membership, and registration.
- `development_kit/tests/test_validation_collectors.py` — This module tests adapters from validation points to evidence collectors.
- `development_kit/tests/test_validation_matrix.py` — This module tests bounded validation-matrix specification normalization.
- `development_kit/tests/test_validation_rows.py` — This module tests append-only validation row identity and durability.
- `development_kit/tests/test_validation_runner.py` — This module tests the solver-independent validation point loop.
- `development_kit/tests/test_validation_worker.py` — This module tests detached validation worker boundaries and cleanup.
- `development_kit/tests/test_visual_review_contracts.py` — This module tests host-confirmed visual-review requests and receipts.
- `development_kit/tests/test_wave_optics_audit.py` — This module tests policy-separated one-point Wave Optics evidence.
- `development_kit/tests/test_wave_optics_preflight.py` — This module tests threshold-free read-only Wave Optics preflight evidence.
- `development_kit/tests/test_workflow.py` — This module tests durable staged workflow execution without COMSOL.

## Packaged runtime root

- `src/__init__.py` — This compatibility package aliases legacy imports to the canonical implementation modules.
- `comsol_mcp/__init__.py` — This module defines the single authored package version.
- `comsol_mcp/artifact_chain.py` — This module verifies bounded JSON artifact dependency chains without a solver.
- `comsol_mcp/build_identity.py` — This module derives package build identity from shipped paths and bytes.
- `comsol_mcp/compatibility.py` — This module loads and validates the packaged runtime compatibility declaration.
- `comsol_mcp/contracts/__init__.py` — This module exports lightweight public input contracts without solver imports.
- `comsol_mcp/contracts/job_submission.py` — This module defines bounded discriminated durable-job submission inputs.
- `comsol_mcp/contracts/structural.py` — This module applies shared public schema and runtime structural limits.
- `comsol_mcp/durable/__init__.py` — This module exports versioned canonicalization and durable filesystem primitives.
- `comsol_mcp/durable/canonical.py` — This module preserves legacy canonical bytes and adds domain-separated identities for new schemas.
- `comsol_mcp/durable/io.py` — This module implements bounded hashing, atomic replacement, and complete-row persistence.
- `comsol_mcp/compatibility_manifest.json` — This file declares exact licensed, dependency-only, and unknown runtime compatibility.
- `comsol_mcp/deployment_manifest.json` — This file binds deployment identity to frozen tool and profile snapshots.
- `comsol_mcp/environment_identity.py` — This module reports redacted Python, platform, dependency, and optional-feature identity.
- `comsol_mcp/operation_arbiter.py` — This module serializes COMSOL-bound calls with a durable exact-process lock.
- `comsol_mcp/path_policy.py` — This module enforces configured model-read and owned ASCII artifact roots.
- `comsol_mcp/schema_registry.py` — This module registers named artifact schema producers and readable and writable versions.
- `comsol_mcp/server.py` — This module creates the profiled MCP server and console entry point.
- `comsol_mcp/settings.py` — This module loads grouped project settings and reports bounded fallback errors.

## Asynchronous compatibility layer

- `comsol_mcp/async_handler/__init__.py` — This file exports asynchronous compatibility handlers.
- `comsol_mcp/async_handler/solver.py` — This module implements the experimental in-process asynchronous solver wrapper.

## Evidence modules

- `comsol_mcp/evidence/__init__.py` — This file exports versioned solver-free evidence contracts.
- `comsol_mcp/evidence/contracts.py` — This module implements strict physical evidence, policy, and migration contracts.
- `comsol_mcp/evidence/branch_continuation.py` — This module validates and plans ordered branch-continuation states without a solver.
- `comsol_mcp/evidence/convergence_evaluation.py` — This module validates ordered spectral convergence ladders and caller policies.
- `comsol_mcp/evidence/integrity_controls.py` — This module loads default-on evidence-integrity settings and defines warning propagation.
- `comsol_mcp/evidence/integrity_verifier.py` — This module composes settings-aware outcome, artifact, summary, and resume verification.
- `comsol_mcp/evidence/field_artifacts.py` — This module serializes bounded gridded scalar field artifacts.
- `comsol_mcp/evidence/field_bundle.py` — This module normalizes bounded field-evidence extraction requests.
- `comsol_mcp/evidence/field_dataset.py` — This module adapts existing MPh datasets to field-evidence samples.
- `comsol_mcp/evidence/field_discovery.py` — This module discovers exact dataset, solution, and component identities.
- `comsol_mcp/evidence/field_interpolation.py` — This module interpolates selected field samples onto declared grids.
- `comsol_mcp/evidence/field_manifest.py` — This module builds and validates field-evidence manifests.
- `comsol_mcp/evidence/field_matrix.py` — This module binds validation-matrix points to field requests.
- `comsol_mcp/evidence/field_pipeline.py` — This module coordinates raw field samples into durable artifacts.
- `comsol_mcp/evidence/field_plot_worker.py` — This module renders bounded scalar field PNGs in an isolated worker.
- `comsol_mcp/evidence/field_render.py` — This module coordinates isolated field PNG rendering.
- `comsol_mcp/evidence/field_sampling.py` — This module selects bounded raw samples for one declared slice.
- `comsol_mcp/evidence/material_expressions.py` — This module constructs and previews dispersive material expressions.
- `comsol_mcp/evidence/outcome_contract.py` — This module validates solver-free execution, evidence-completeness, and scientific-disposition outcomes.
- `comsol_mcp/evidence/portfolio_verifier.py` — This module verifies summary claims against exact values in hash-bound evidence chains.
- `comsol_mcp/evidence/power_audit.py` — This module normalizes declared reference-power evidence.
- `comsol_mcp/evidence/real_fixture.py` — This module validates portable controlled licensed-fixture contracts.
- `comsol_mcp/evidence/reference_power_acceptance.py` — This module validates reference-power acceptance and execution inputs.
- `comsol_mcp/evidence/reference_power_gate.py` — This module evaluates reference-power receipts and artifact accounting.
- `comsol_mcp/evidence/spectral_characterization.py` — This module validates and characterizes provenance-bound spectra without a solver.
- `comsol_mcp/evidence/visual_review.py` — This module defines visual-review capability, request, receipt, and dual-review contracts.

## Durable job modules

- `comsol_mcp/jobs/__init__.py` — This file exports durable background-job primitives.
- `comsol_mcp/jobs/attached_backend.py` — This module normalizes immutable automation-exclusive attached-server execution specifications.
- `comsol_mcp/jobs/attached_runtime.py` — This module verifies attached server, model, revision, and preservation identities for durable workers.
- `comsol_mcp/jobs/cancel_worker.py` — This module coordinates detached durable cancellation and cleanup.
- `comsol_mcp/jobs/convergence_campaign.py` — This module normalizes immutable bounded durable convergence campaign specifications.
- `comsol_mcp/jobs/branch_continuation_campaign.py` — This module normalizes immutable bounded durable branch-continuation campaign specifications.
- `comsol_mcp/jobs/branch_continuation_campaign_rows.py` — This module persists hash-chained continuation state evidence bound to completed spectral artifacts.
- `comsol_mcp/jobs/branch_continuation_campaign_runner.py` — This module composes completed spectral states with offline continuation planning and durable summaries.
- `comsol_mcp/jobs/branch_continuation_campaign_worker.py` — This worker runs exact-model continuation states under one owned COMSOL attempt.
- `comsol_mcp/jobs/convergence_campaign_rows.py` — This module persists hash-chained convergence level evidence bound to completed spectral artifacts.
- `comsol_mcp/jobs/convergence_campaign_runner.py` — This module composes completed spectral levels with offline convergence evaluation and durable summaries.
- `comsol_mcp/jobs/convergence_campaign_worker.py` — This worker runs exact-model convergence ladders under one owned COMSOL attempt.
- `comsol_mcp/jobs/field_review.py` — This module assembles paired validation-matrix field-review artifacts.
- `comsol_mcp/jobs/manager.py` — This module handles durable job submission, status, cancellation, resume, and reconciliation.
- `comsol_mcp/jobs/native_cancel_probe.py` — This module inspects allowlisted native cancellation support.
- `comsol_mcp/jobs/native_cancel_profiles.json` — This file stores exact native cancellation compatibility profiles.
- `comsol_mcp/jobs/process_control.py` — This module performs exact-identity process inspection and containment.
- `comsol_mcp/jobs/resource_admission.py` — This module validates resource policy, telemetry, journals, and admission.
- `comsol_mcp/jobs/sequence_worker.py` — This module provides an injected process-only durability worker.
- `comsol_mcp/jobs/spectral_audit.py` — This module verifies point-audit artifacts before durable spectral row persistence.
- `comsol_mcp/jobs/spectral_characterization.py` — This module normalizes immutable bounded durable spectral job specifications.
- `comsol_mcp/jobs/spectral_progress.py` — This module derives bounded adaptive spectral transitions from frozen stages and durable rows.
- `comsol_mcp/jobs/spectral_runner.py` — This module runs the solver-independent adaptive spectral point loop and summary writes.
- `comsol_mcp/jobs/spectral_level_execution.py` — This module runs the accepted spectral pipeline against an already loaded owned model.
- `comsol_mcp/jobs/spectral_rows.py` — This module persists hash-chained raw spectral points with artifact verification.
- `comsol_mcp/jobs/spectral_stages.py` — This module builds and atomically freezes hash-chained adaptive spectral stage plans.
- `comsol_mcp/jobs/spectral_worker.py` — This module runs detached adaptive spectral jobs through the shared solver runtime.
- `comsol_mcp/jobs/store.py` — This module persists crash-durable job state and process-safe locks.
- `comsol_mcp/jobs/validation_collectors.py` — This module adapts validation points to physical evidence collectors.
- `comsol_mcp/jobs/validation_matrix.py` — This module normalizes bounded durable validation-matrix specifications.
- `comsol_mcp/jobs/validation_rows.py` — This module writes and validates append-only durable validation rows.
- `comsol_mcp/jobs/validation_runner.py` — This module runs the solver-independent validation point loop.
- `comsol_mcp/jobs/validation_worker.py` — This module runs one detached physical-validation matrix worker.
- `comsol_mcp/jobs/worker.py` — This module runs one detached staged COMSOL sweep worker.

## Knowledge modules and prompts

- `comsol_mcp/knowledge/__init__.py` — This file exports knowledge and documentation services.
- `comsol_mcp/knowledge/embedded.py` — This module registers embedded documentation tools.
- `comsol_mcp/knowledge/lexical_manual.py` — This module implements bounded SQLite full-text manual search.
- `comsol_mcp/knowledge/lexical_worker.py` — This module isolates lexical manual operations behind JSON transport.
- `comsol_mcp/knowledge/semantic_contracts.py` — This module defines dependency-free semantic service contracts.
- `comsol_mcp/knowledge/semantic_index.py` — This module builds and validates immutable semantic indexes.
- `comsol_mcp/knowledge/semantic_process.py` — This module manages the exact semantic worker child process.
- `comsol_mcp/knowledge/semantic_retrieval.py` — This module performs vector retrieval and deterministic BM25 fusion.
- `comsol_mcp/knowledge/semantic_runtime.py` — This module reports opt-in semantic runtime configuration.
- `comsol_mcp/knowledge/semantic_worker.py` — This module implements the isolated semantic worker protocol.
- `comsol_mcp/knowledge/prompts/mph_api.md` — This prompt summarizes calibrated MPh and clientapi usage.
- `comsol_mcp/knowledge/prompts/physics_guide.md` — This prompt summarizes physics construction and verification guidance.
- `comsol_mcp/knowledge/prompts/workflow.md` — This prompt summarizes safe model workflow sequencing.

## MCP resources

- `comsol_mcp/resources/__init__.py` — This file exports MCP model resources.
- `comsol_mcp/resources/model_resources.py` — This module exposes bounded model status and information resources.

## Shared Desktop and attached-server contracts

- `comsol_mcp/shared_session/__init__.py` — This file exports the default-off shared-session contracts.
- `comsol_mcp/shared_session/attach_request.py` — This module normalizes all static and per-call gates before attached lease acquisition.
- `comsol_mcp/shared_session/cleanup.py` — This module distinguishes external-resource-preserving detach from owned cleanup.
- `comsol_mcp/shared_session/contracts.py` — This module normalizes the shared feature gate and local loopback endpoint.
- `comsol_mcp/shared_session/identity.py` — This module defines exact non-owned server and model-selector identities.
- `comsol_mcp/shared_session/locking.py` — This module defines bounded model revisions and shared-model enforcement locks.
- `comsol_mcp/shared_session/lifecycle.py` — This module attaches and disconnects one non-owned server client without start or clear behavior.
- `comsol_mcp/shared_session/preflight.py` — This module classifies stable local Desktop and Server readiness without importing MPh.
- `comsol_mcp/shared_session/process_probe.py` — This module collects bounded Windows process, listener, window, and executable-version evidence.

## MCP tool adapters

- `comsol_mcp/tools/__init__.py` — This file exports and registers MCP tool modules.
- `comsol_mcp/tools/capabilities.py` — This module reports profiles, compatibility, identities, schemas, and feature maturity.
- `comsol_mcp/tools/branch_continuation.py` — This module exposes bounded solver-free branch-continuation planning.
- `comsol_mcp/tools/convergence_evaluation.py` — This module exposes bounded solver-free convergence evaluation.
- `comsol_mcp/tools/catalog.py` — This module classifies tools and snapshots deterministic public schemas.
- `comsol_mcp/tools/derived_geometry.py` — This module applies typed edits only to provenance-tracked derived models.
- `comsol_mcp/tools/evidence_integrity.py` — This module exposes solver-free evidence-integrity status and formal verification tools.
- `comsol_mcp/tools/field_evidence.py` — This module exposes read-only field discovery and extraction tools.
- `comsol_mcp/tools/geometry.py` — This module exposes COMSOL geometry tools.
- `comsol_mcp/tools/incidence_config.py` — This module exposes typed periodic incidence preview and mutation gates.
- `comsol_mcp/tools/jobs.py` — This module exposes durable job submission and control tools.
- `comsol_mcp/tools/material_expressions.py` — This module exposes solver-free material-expression preview tools.
- `comsol_mcp/tools/mesh.py` — This module exposes COMSOL mesh tools.
- `comsol_mcp/tools/mim_patch.py` — This module exposes patch-metasurface construction helpers.
- `comsol_mcp/tools/model.py` — This module exposes model creation, loading, cloning, saving, and listing tools.
- `comsol_mcp/tools/ownership.py` — This module enforces cross-process solver ownership and collision preflight.
- `comsol_mcp/tools/parameters.py` — This module exposes COMSOL parameter tools.
- `comsol_mcp/tools/periodic_mesh_audit.py` — This module exposes periodic geometry and mesh evidence tools.
- `comsol_mcp/tools/physics.py` — This module exposes COMSOL physics and multiphysics tools.
- `comsol_mcp/tools/profiles.py` — This module resolves static profiles and filters tool registration.
- `comsol_mcp/tools/session_status.py` — This module stores last-known session booleans without importing COMSOL or MPh.
- `comsol_mcp/tools/properties.py` — This module exposes constrained clientapi property access.
- `comsol_mcp/tools/property_transport.py` — This module normalizes bounded property values for JSON transport.
- `comsol_mcp/tools/results.py` — This module exposes result evaluation and export tools.
- `comsol_mcp/tools/semantic_docs.py` — This module exposes bounded opt-in semantic documentation tools.
- `comsol_mcp/tools/spectral_characterization.py` — This module exposes bounded solver-free spectral characterization.
- `comsol_mcp/tools/session.py` — This module manages COMSOL client startup, status, models, and shutdown.
- `comsol_mcp/tools/shared_session.py` — This module exposes default-off local attached-server lifecycle tools.
- `comsol_mcp/tools/study.py` — This module exposes COMSOL study and solving tools.
- `comsol_mcp/tools/visual_review.py` — This module exposes solver-free visual-review contract adapters.
- `comsol_mcp/tools/wave_optics_audit.py` — This module exposes one-point policy-separated Wave Optics evidence audits.
- `comsol_mcp/tools/wave_optics_preflight.py` — This module exposes threshold-free read-only Wave Optics preflight.
- `comsol_mcp/tools/workflow.py` — This module exposes reusable staged study workflows.

## Shared utilities

- `comsol_mcp/utils/__init__.py` — This file exports shared utility functions.
- `comsol_mcp/utils/control_plane.py` — This module attaches bounded latency and outcome evidence to control calls.
- `comsol_mcp/utils/runtime_paths.py` — This module defines shared ASCII-safe runtime artifact locations.
- `comsol_mcp/utils/versioning.py` — This module creates and parses versioned model filenames.
