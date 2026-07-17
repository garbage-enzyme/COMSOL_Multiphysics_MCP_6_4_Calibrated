# COMSOL MCP workflow guide

This guide describes the current profile-aware workflow. Tool availability and
schemas come from live MCP discovery; examples are illustrative named-argument
calls rather than a substitute for the returned schema.

## 1. Discover before starting COMSOL

```text
capabilities
solver_status
solver_preflight
```

- `capabilities` is solver-free and reports the active profile, registered tools,
  supported versions, maturity, and deployment hashes.
- `solver_status` checks the shared ASCII runtime root, lease, and known process
  identity.
- `solver_preflight` performs the fresh checks required before constructing a
  client or submitting substantial work.
- Keep one solver owner and serialize COMSOL operations.

Do not use `comsol_connect` as a shared Desktop workflow. It is legacy
experimental compatibility and does not protect a user-owned Server or lock one
server-side model. The current release has no protected shared Desktop profile.

## 2. Start and inspect a session

The default `core` profile supports existing-model work:

```text
comsol_start(cores=4, version="6.4")
comsol_status
model_load(file_path="D:\\models\\source.mph")
model_inspect
model_list_components
physics_list
mesh_list
study_list
datasets_list
```

`comsol_start` is non-blocking. Poll `comsol_status` until `connected=true`; do
not call `comsol_start` again while `starting=true`.

Treat the source `.mph` as immutable. Use a derived clone or a separately named
output for mutations and checkpoints. Runtime, journal, index, and native-cache
roots should be ASCII-only.

## 3. Build a conventional FEM model

Select `basic_fem` before the MCP host starts, then restart the host. A minimal
new-model sequence is:

```text
model_create(name="capacitor")
model_create_component(component_name="comp1", space_dimension=3)
geometry_create(component_name="comp1", geometry_name="geom1", space_dimension=3)
geometry_add_block(
    component_name="comp1",
    geometry_name="geom1",
    size=[0.01, 0.01, 0.001],
    position=[0.0, 0.0, 0.0]
)
geometry_build(component_name="comp1", geometry_name="geom1")
geometry_probe_domains
geometry_get_boundaries
```

Geometry helper dimensions are numeric SI values. Probe domains and boundaries
after every topology-changing build; never assume entity IDs from feature order.

For a dielectric electrostatics model:

```text
physics_add_electrostatics(relpermittivity=2.1, domain_numbers=[1])
physics_configure_boundary(
    physics_name="Electrostatics",
    boundary_condition="Ground",
    boundary_selection=[<probed-bottom-boundary>]
)
physics_configure_boundary(
    physics_name="Electrostatics",
    boundary_condition="ElectricPotential",
    boundary_selection=[<probed-top-boundary>],
    properties={"V0": "1[V]"}
)
mesh_sequence_create(
    component_name="comp1",
    mesh_name="mesh1",
    element_type="FreeTet",
    build=true
)
study_create(study_name="std1", study_type="Stationary")
```

Passing `relpermittivity` creates the required material and
`ChargeConservation` feature. Without it, COMSOL 6.3/6.4 Electrostatics defaults
to `FreeSpace`, which uses vacuum permittivity.

## 4. Solve one point and collect evidence

```text
study_solve(study_name="std1", wait=true)
results_global_evaluate(expression="2*es.intWe/(1[V])^2", unit="pF")
model_save(file_path="D:\\derived_models\\capacitor_result.mph")
```

For transient or multi-solution models, pass an explicit dataset and inner
solution to `results_evaluate`. A successful solve call is not by itself
physical validation: preserve raw values, units, configuration identity, source
hash, and the caller's acceptance policy.

## 5. Durable sweeps and long work

Use the `core` durable job controls:

```text
job_submit(spec=<immutable validated specification>)
job_status(job_id="<job-id>")
job_tail(job_id="<job-id>", n=20)
job_cancel(job_id="<job-id>")
job_resume(job_id="<job-id>")
```

Read the exact schemas from discovery. Durable work runs in an owned worker and
stores an immutable specification, atomic state, append-only result journal,
checkpoint, and bounded log beneath the configured ASCII runtime root.

Each point follows this order:

```text
set point -> pre-solve resource admission -> solve -> evaluate raw evidence
-> validate -> append row -> flush + fsync -> checkpoint -> next point
```

Resume only exact matching point identities. Cancellation is terminal only
after the worker, owned descendants, port, and lease are verified clean. Do not
use `study_solve_async` or daemon-thread progress as a resumable or unattended
workflow; those tools are experimental compatibility only.

## 6. Wave Optics evidence workflow

Select `wave_optics` and restart the MCP host:

```text
solver_status
solver_preflight
wave_optics_preflight
wave_optics_reference_audit   # optional and experimental
wave_optics_point_audit       # exactly one declared wavelength
```

The preflight is read-only. A point audit binds the source hash, configuration,
requested/evaluated wavelength, raw caller-declared R/T/A expressions, closure,
mesh state, and artifact manifests. Without a versioned caller policy it returns
evidence only, not a scientific pass/fail decision.

Use staged one-point solves for parameter or wavelength scans. Fixed-wavelength
amplitudes do not establish angular convergence; track each configuration's own
peak when resonance motion matters.

## 7. Save and disconnect

- Save only to an approved derived/output path.
- On Unicode destinations, the server uses Java clientapi saving with an
  absolute path.
- `comsol_disconnect` clears models tracked by the MCP-owned session before
  releasing the client.
- `session_reset` is destructive and is intended for an MCP-owned session.
- Verify `solver_status` after cleanup; process activity alone does not prove
  progress or successful release.

## 8. Common diagnostics

### Geometry build fails

- Confirm all numeric parameters and units.
- Probe for tiny or overlapping features.
- Rebuild, then re-probe all entity selections.

### Mesh generation fails

- Confirm a mesh sequence exists.
- Inspect geometry scale and small features.
- Use a smaller diagnostic before broad refinement.

### Solver convergence fails

- Check physics selections, materials, units, and study type.
- Inspect scaling and boundary conditions.
- Preserve the failed point and logs instead of silently retrying with changed
  settings.

### Memory pressure

- Stop before starting another factorization when the caller's resource policy
  refuses it.
- Check durable rows and exact worker identity; CPU or disk activity alone is
  not proof of progress.
