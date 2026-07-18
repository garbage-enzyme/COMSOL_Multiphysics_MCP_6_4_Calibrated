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

## Client configuration and dependency constraints

- `config/claude-code-mcp.example.json` — This file provides an example Claude Code stdio MCP configuration.
- `config/codex-mcp.example.toml` — This file provides an example Codex stdio MCP configuration.
- `config/hermes-mcp.example.yaml` — This file provides an example Hermes Agent stdio MCP configuration.
- `config/opencode-mcp.example.json` — This file provides an example opencode stdio MCP configuration.
- `constraints/release_locked_py314.txt` — This file locks the complete Python 3.14 runtime dependency set with hashes.
- `constraints/minimum_supported_py314.txt` — This file pins the reviewed minimum binary-installable Python 3.14 direct dependencies.
- `constraints/tested_versions.json` — This file records the human-reviewed direct dependency versions and compatibility lane.
- `docs/profile_migration.md` — This file explains static profile selection and migration from broader tool surfaces.

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
- `development_kit/release/vulnerability_allowlist.json` — This file records exact reviewed vulnerabilities with mandatory expiry dates.
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

## Development and release scripts

- `development_kit/scripts/__init__.py` — This file marks repository-only release utilities as a Python package.
- `development_kit/scripts/generate_release_lock.py` — This script generates the complete hashed Windows Python release lock.
- `development_kit/scripts/installed_package_probe.py` — This script verifies installed discovery, schemas, profiles, and deployment identity without COMSOL startup.
- `development_kit/scripts/installed_stdio_probe.py` — This script verifies the installed console entry point over real MCP stdio transport.
- `development_kit/scripts/planning_code_gate.py` — This script verifies the exact frozen planning-code compatibility surface.
- `development_kit/scripts/python_compatibility_licensed_gate.py` — This script runs the pinned Python and COMSOL compatibility regression on a licensed host.
- `development_kit/scripts/reference_power_gate_preflight.py` — This script validates reference-power gate inputs without starting COMSOL.
- `development_kit/scripts/release_gate.py` — This script runs compile, test, package, clean-install, and installed-discovery gates.
- `development_kit/scripts/run_real_release_gate.py` — This script orchestrates the explicit serial licensed COMSOL release gate.
- `development_kit/scripts/sbom_probe.py` — This script generates a deterministic CycloneDX SBOM from the locked installed runtime.
- `development_kit/scripts/security_gate.py` — This script evaluates pip-audit findings against the exact expiring review policy.

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
- `development_kit/tests/test_convergence_campaign_job.py` — This module tests immutable bounded durable convergence campaign specifications.
- `development_kit/tests/test_convergence_campaign_rows.py` — This module tests hash-chained durable convergence level evidence and artifact replay.
- `development_kit/tests/test_convergence_campaign_runner.py` — This module tests composed spectral-level execution, convergence stopping, and exact resume.
- `development_kit/tests/test_convergence_campaign_worker.py` — This module tests convergence worker ownership, later-level recovery, and cleanup failure.
- `development_kit/tests/test_convergence_acceptance_runner.py` — This module tests the licensed convergence runner contract without starting COMSOL.
- `development_kit/tests/test_convergence_evaluation.py` — This module tests ordered solver-free convergence evidence and policy evaluation.
- `development_kit/tests/test_deployment_identity.py` — This module tests package version, build identity, and fresh-process deployment consistency.
- `development_kit/tests/test_derived_geometry.py` — This module tests typed derived-geometry edits without COMSOL.
- `development_kit/tests/test_durable_job_control_plane.py` — This module tests durable submission, reconciliation, status, cancellation, and resume behavior.
- `development_kit/tests/test_environment_identity.py` — This module tests redacted solver-free environment identity.
- `development_kit/tests/test_evidence_contracts.py` — This module tests physical evidence, policies, and immutable migration contracts.
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
- `development_kit/tests/test_real_fixture_contract.py` — This module tests portable contracts for controlled licensed fixtures.
- `development_kit/tests/test_recipe_paths.py` — This module tests standalone recipe output path policy.
- `development_kit/tests/test_reference_power_acceptance.py` — This module tests reference-power acceptance contracts and preflight.
- `development_kit/tests/test_reference_power_gate.py` — This module tests pure reference-power receipt evaluation.
- `development_kit/tests/test_reference_power_release_orchestrator.py` — This module tests mandatory serial release orchestration with fake processes.
- `development_kit/tests/test_reference_power_runner.py` — This module tests reference-power coordinator and worker process boundaries.
- `development_kit/tests/test_release_engineering.py` — This module tests repository, dependency, fixture, archive, and release policies.
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
- `development_kit/tests/test_shared_attach_request.py` — This module tests the complete pre-lease shared-server attach gate.
- `development_kit/tests/test_shared_cleanup_contracts.py` — This module tests non-owning detach and owned-cleanup outcome semantics.
- `development_kit/tests/test_shared_model_locking.py` — This module tests bounded shared-model revisions and exact enforcement locks.
- `development_kit/tests/test_shared_operation_dependencies.py` — This module tests shared operations against the reused arbiter and path-containment dependencies.
- `development_kit/tests/test_shared_session_contracts.py` — This module tests default-off feature and loopback endpoint contracts.
- `development_kit/tests/test_shared_session_identity.py` — This module tests attached-server and exact model-selector identities.
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

- `src/__init__.py` — This module defines the single authored package version.
- `src/artifact_chain.py` — This module verifies bounded JSON artifact dependency chains without a solver.
- `src/build_identity.py` — This module derives package build identity from shipped paths and bytes.
- `src/compatibility.py` — This module loads and validates the packaged runtime compatibility declaration.
- `src/compatibility_manifest.json` — This file declares exact licensed, dependency-only, and unknown runtime compatibility.
- `src/deployment_manifest.json` — This file binds deployment identity to frozen tool and profile snapshots.
- `src/environment_identity.py` — This module reports redacted Python, platform, dependency, and optional-feature identity.
- `src/operation_arbiter.py` — This module serializes COMSOL-bound calls with a durable exact-process lock.
- `src/path_policy.py` — This module enforces configured model-read and owned ASCII artifact roots.
- `src/schema_registry.py` — This module registers named artifact schema producers and readable and writable versions.
- `src/server.py` — This module creates the profiled MCP server and console entry point.

## Asynchronous compatibility layer

- `src/async_handler/__init__.py` — This file exports asynchronous compatibility handlers.
- `src/async_handler/solver.py` — This module implements the experimental in-process asynchronous solver wrapper.

## Evidence modules

- `src/evidence/__init__.py` — This file exports versioned solver-free evidence contracts.
- `src/evidence/contracts.py` — This module implements strict physical evidence, policy, and migration contracts.
- `src/evidence/branch_continuation.py` — This module validates and plans ordered branch-continuation states without a solver.
- `src/evidence/convergence_evaluation.py` — This module validates ordered spectral convergence ladders and caller policies.
- `src/evidence/field_artifacts.py` — This module serializes bounded gridded scalar field artifacts.
- `src/evidence/field_bundle.py` — This module normalizes bounded field-evidence extraction requests.
- `src/evidence/field_dataset.py` — This module adapts existing MPh datasets to field-evidence samples.
- `src/evidence/field_discovery.py` — This module discovers exact dataset, solution, and component identities.
- `src/evidence/field_interpolation.py` — This module interpolates selected field samples onto declared grids.
- `src/evidence/field_manifest.py` — This module builds and validates field-evidence manifests.
- `src/evidence/field_matrix.py` — This module binds validation-matrix points to field requests.
- `src/evidence/field_pipeline.py` — This module coordinates raw field samples into durable artifacts.
- `src/evidence/field_plot_worker.py` — This module renders bounded scalar field PNGs in an isolated worker.
- `src/evidence/field_render.py` — This module coordinates isolated field PNG rendering.
- `src/evidence/field_sampling.py` — This module selects bounded raw samples for one declared slice.
- `src/evidence/material_expressions.py` — This module constructs and previews dispersive material expressions.
- `src/evidence/outcome_contract.py` — This module validates solver-free execution, evidence-completeness, and scientific-disposition outcomes.
- `src/evidence/portfolio_verifier.py` — This module verifies summary claims against exact values in hash-bound evidence chains.
- `src/evidence/power_audit.py` — This module normalizes declared reference-power evidence.
- `src/evidence/real_fixture.py` — This module validates portable controlled licensed-fixture contracts.
- `src/evidence/reference_power_acceptance.py` — This module validates reference-power acceptance and execution inputs.
- `src/evidence/reference_power_gate.py` — This module evaluates reference-power receipts and artifact accounting.
- `src/evidence/spectral_characterization.py` — This module validates and characterizes provenance-bound spectra without a solver.
- `src/evidence/visual_review.py` — This module defines visual-review capability, request, receipt, and dual-review contracts.

## Durable job modules

- `src/jobs/__init__.py` — This file exports durable background-job primitives.
- `src/jobs/cancel_worker.py` — This module coordinates detached durable cancellation and cleanup.
- `src/jobs/convergence_campaign.py` — This module normalizes immutable bounded durable convergence campaign specifications.
- `src/jobs/branch_continuation_campaign.py` — This module normalizes immutable bounded durable branch-continuation campaign specifications.
- `src/jobs/branch_continuation_campaign_rows.py` — This module persists hash-chained continuation state evidence bound to completed spectral artifacts.
- `src/jobs/branch_continuation_campaign_runner.py` — This module composes completed spectral states with offline continuation planning and durable summaries.
- `src/jobs/branch_continuation_campaign_worker.py` — This worker runs exact-model continuation states under one owned COMSOL attempt.
- `src/jobs/convergence_campaign_rows.py` — This module persists hash-chained convergence level evidence bound to completed spectral artifacts.
- `src/jobs/convergence_campaign_runner.py` — This module composes completed spectral levels with offline convergence evaluation and durable summaries.
- `src/jobs/convergence_campaign_worker.py` — This worker runs exact-model convergence ladders under one owned COMSOL attempt.
- `src/jobs/field_review.py` — This module assembles paired validation-matrix field-review artifacts.
- `src/jobs/manager.py` — This module handles durable job submission, status, cancellation, resume, and reconciliation.
- `src/jobs/native_cancel_probe.py` — This module inspects allowlisted native cancellation support.
- `src/jobs/native_cancel_profiles.json` — This file stores exact native cancellation compatibility profiles.
- `src/jobs/process_control.py` — This module performs exact-identity process inspection and containment.
- `src/jobs/resource_admission.py` — This module validates resource policy, telemetry, journals, and admission.
- `src/jobs/sequence_worker.py` — This module provides an injected process-only durability worker.
- `src/jobs/spectral_audit.py` — This module verifies point-audit artifacts before durable spectral row persistence.
- `src/jobs/spectral_characterization.py` — This module normalizes immutable bounded durable spectral job specifications.
- `src/jobs/spectral_progress.py` — This module derives bounded adaptive spectral transitions from frozen stages and durable rows.
- `src/jobs/spectral_runner.py` — This module runs the solver-independent adaptive spectral point loop and summary writes.
- `src/jobs/spectral_level_execution.py` — This module runs the accepted spectral pipeline against an already loaded owned model.
- `src/jobs/spectral_rows.py` — This module persists hash-chained raw spectral points with artifact verification.
- `src/jobs/spectral_stages.py` — This module builds and atomically freezes hash-chained adaptive spectral stage plans.
- `src/jobs/spectral_worker.py` — This module runs detached adaptive spectral jobs through the shared solver runtime.
- `src/jobs/store.py` — This module persists crash-durable job state and process-safe locks.
- `src/jobs/validation_collectors.py` — This module adapts validation points to physical evidence collectors.
- `src/jobs/validation_matrix.py` — This module normalizes bounded durable validation-matrix specifications.
- `src/jobs/validation_rows.py` — This module writes and validates append-only durable validation rows.
- `src/jobs/validation_runner.py` — This module runs the solver-independent validation point loop.
- `src/jobs/validation_worker.py` — This module runs one detached physical-validation matrix worker.
- `src/jobs/worker.py` — This module runs one detached staged COMSOL sweep worker.

## Knowledge modules and prompts

- `src/knowledge/__init__.py` — This file exports knowledge and documentation services.
- `src/knowledge/embedded.py` — This module registers embedded documentation tools.
- `src/knowledge/lexical_manual.py` — This module implements bounded SQLite full-text manual search.
- `src/knowledge/lexical_worker.py` — This module isolates lexical manual operations behind JSON transport.
- `src/knowledge/semantic_contracts.py` — This module defines dependency-free semantic service contracts.
- `src/knowledge/semantic_index.py` — This module builds and validates immutable semantic indexes.
- `src/knowledge/semantic_process.py` — This module manages the exact semantic worker child process.
- `src/knowledge/semantic_retrieval.py` — This module performs vector retrieval and deterministic BM25 fusion.
- `src/knowledge/semantic_runtime.py` — This module reports opt-in semantic runtime configuration.
- `src/knowledge/semantic_worker.py` — This module implements the isolated semantic worker protocol.
- `src/knowledge/prompts/mph_api.md` — This prompt summarizes calibrated MPh and clientapi usage.
- `src/knowledge/prompts/physics_guide.md` — This prompt summarizes physics construction and verification guidance.
- `src/knowledge/prompts/workflow.md` — This prompt summarizes safe model workflow sequencing.

## MCP resources

- `src/resources/__init__.py` — This file exports MCP model resources.
- `src/resources/model_resources.py` — This module exposes bounded model status and information resources.

## Shared Desktop and attached-server contracts

- `src/shared_session/__init__.py` — This file exports the default-off shared-session contracts.
- `src/shared_session/attach_request.py` — This module normalizes all static and per-call gates before attached lease acquisition.
- `src/shared_session/cleanup.py` — This module distinguishes external-resource-preserving detach from owned cleanup.
- `src/shared_session/contracts.py` — This module normalizes the shared feature gate and local loopback endpoint.
- `src/shared_session/identity.py` — This module defines exact non-owned server and model-selector identities.
- `src/shared_session/locking.py` — This module defines bounded model revisions and shared-model enforcement locks.
- `src/shared_session/preflight.py` — This module classifies stable local Desktop and Server readiness without importing MPh.

## MCP tool adapters

- `src/tools/__init__.py` — This file exports and registers MCP tool modules.
- `src/tools/capabilities.py` — This module reports profiles, compatibility, identities, schemas, and feature maturity.
- `src/tools/branch_continuation.py` — This module exposes bounded solver-free branch-continuation planning.
- `src/tools/convergence_evaluation.py` — This module exposes bounded solver-free convergence evaluation.
- `src/tools/catalog.py` — This module classifies tools and snapshots deterministic public schemas.
- `src/tools/derived_geometry.py` — This module applies typed edits only to provenance-tracked derived models.
- `src/tools/field_evidence.py` — This module exposes read-only field discovery and extraction tools.
- `src/tools/geometry.py` — This module exposes COMSOL geometry tools.
- `src/tools/incidence_config.py` — This module exposes typed periodic incidence preview and mutation gates.
- `src/tools/jobs.py` — This module exposes durable job submission and control tools.
- `src/tools/material_expressions.py` — This module exposes solver-free material-expression preview tools.
- `src/tools/mesh.py` — This module exposes COMSOL mesh tools.
- `src/tools/mim_patch.py` — This module exposes patch-metasurface construction helpers.
- `src/tools/model.py` — This module exposes model creation, loading, cloning, saving, and listing tools.
- `src/tools/ownership.py` — This module enforces cross-process solver ownership and collision preflight.
- `src/tools/parameters.py` — This module exposes COMSOL parameter tools.
- `src/tools/periodic_mesh_audit.py` — This module exposes periodic geometry and mesh evidence tools.
- `src/tools/physics.py` — This module exposes COMSOL physics and multiphysics tools.
- `src/tools/profiles.py` — This module resolves static profiles and filters tool registration.
- `src/tools/properties.py` — This module exposes constrained clientapi property access.
- `src/tools/property_transport.py` — This module normalizes bounded property values for JSON transport.
- `src/tools/results.py` — This module exposes result evaluation and export tools.
- `src/tools/semantic_docs.py` — This module exposes bounded opt-in semantic documentation tools.
- `src/tools/spectral_characterization.py` — This module exposes bounded solver-free spectral characterization.
- `src/tools/session.py` — This module manages COMSOL client startup, status, models, and shutdown.
- `src/tools/study.py` — This module exposes COMSOL study and solving tools.
- `src/tools/visual_review.py` — This module exposes solver-free visual-review contract adapters.
- `src/tools/wave_optics_audit.py` — This module exposes one-point policy-separated Wave Optics evidence audits.
- `src/tools/wave_optics_preflight.py` — This module exposes threshold-free read-only Wave Optics preflight.
- `src/tools/workflow.py` — This module exposes reusable staged study workflows.

## Shared utilities

- `src/utils/__init__.py` — This file exports shared utility functions.
- `src/utils/control_plane.py` — This module attaches bounded latency and outcome evidence to control calls.
- `src/utils/runtime_paths.py` — This module defines shared ASCII-safe runtime artifact locations.
- `src/utils/versioning.py` — This module creates and parses versioned model filenames.
