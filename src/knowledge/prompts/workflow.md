# COMSOL Modeling Workflow Guide

This guide describes the typical workflow for creating and running COMSOL simulations using the MCP tools.

## Basic Workflow

### 1. Session Management

```
┌─────────────────────────────────────────────────────┐
│  Start COMSOL Session                               │
│  comsol_start(cores=4)                              │
│  or                                                 │
│  comsol_connect(port=2036, host="server")           │
└─────────────────────────────────────────────────────┘
                        │
                        ▼
┌─────────────────────────────────────────────────────┐
│  Load or Create Model                               │
│  model_load("existing.mph")                         │
│  or                                                 │
│  model_create("new_model")                          │
└─────────────────────────────────────────────────────┘
```

### 2. Model Setup (for new models)

```
┌─────────────────────────────────────────────────────┐
│  Define Parameters                                  │
│  param_set("L", "10[mm]")                           │
│  param_set("W", "5[mm]")                            │
│  param_set("T", "1[mm]")                            │
└─────────────────────────────────────────────────────┘
                        │
                        ▼
┌─────────────────────────────────────────────────────┐
│  Create Geometry                                    │
│  geometry_add_block(size=["L", "W", "T"])           │
│  geometry_build()                                   │
└─────────────────────────────────────────────────────┘
                        │
                        ▼
┌─────────────────────────────────────────────────────┐
│  Add Physics                                        │
│  physics_add_electrostatics()                       │
│  physics_configure_boundary(...)                    │
└─────────────────────────────────────────────────────┘
                        │
                        ▼
┌─────────────────────────────────────────────────────┐
│  Create Mesh                                        │
│  mesh_create()                                      │
└─────────────────────────────────────────────────────┘
```

### 3. Stable Selections and Mesh

Boundary numbers can change after geometry operations. Prefer named selections
when boundary conditions must remain stable across geometry revisions.

For a rectangular 2D domain:

```
geometry_build()
geometry_create_side_selections(
    x_min="0[m]",
    x_max="domain_width",
    y_min="0[m]",
    y_max="domain_height",
    prefix="domain",
    entity_dimension=1
)
```

This creates `domain_left`, `domain_right`, `domain_bottom`, and
`domain_top`. Pass these tags as `selection_name` when configuring acoustic or
PDE boundaries.

`mesh_create` runs existing mesh sequences. If no mesh sequence exists, its
default `auto_create=True` behavior creates `mesh1` and binds it to the
selected or first geometry:

```
mesh_create(
    mesh_name="mesh1",
    geometry_name="geom1",
    component_name="comp1"
)
```

Use `mesh_create_sequence` first when the mesh tag and geometry association
must be created explicitly. Build the geometry before evaluating selections or
generating the mesh.

### 4. Solve and Analyze

```
┌─────────────────────────────────────────────────────┐
│  Solve                                              │
│  study_solve("stationary")                          │
│  or                                                 │
│  study_solve_async("time_dependent")                │
│  study_get_progress()                               │
└─────────────────────────────────────────────────────┘
                        │
                        ▼
┌─────────────────────────────────────────────────────┐
│  Evaluate Results                                   │
│  results_evaluate("T", "K")                         │
│  results_global_evaluate("ht.Tmax", "K")            │
└─────────────────────────────────────────────────────┘
                        │
                        ▼
┌─────────────────────────────────────────────────────┐
│  Export and Save                                    │
│  results_export_image("plot", "result.png")         │
│  model_save_version(description="final")            │
└─────────────────────────────────────────────────────┘
```

## Common Workflows by Application

### Electrostatics Simulation (Capacitor)

```
# 1. Start and create
comsol_start(cores=1)
model_load("capacitor_template.mph")

# 2. Modify parameters
param_set("electrode_spacing", "2[mm]")
param_set("applied_voltage", "10[V]")
param_set("dielectric_constant", "4.5")

# 3. Solve
study_solve("stationary")

# 4. Evaluate
C = results_global_evaluate("2*es.intWe/U^2", "pF")
E_max = results_evaluate("es.normE", "V/m")

# 5. Save version
model_save_version(description=f"C={C:.2f}pF")
```

### Thermal Analysis

```
# 1. Create new model
comsol_start()
model_create("heat_sink")

# 2. Parameters
param_set("base_temp", "300[K]")
param_set("heat_flux", "1000[W/m^2]")
param_set("convection_coeff", "15[W/(m^2*K)]")

# 3. Geometry
geometry_add_block(size=[0.1, 0.1, 0.01])  # Base
geometry_add_block(position=[0.01, 0.01, 0.01], size=[0.02, 0.02, 0.05])  # Fin
# Add more fins...
geometry_build()

# 4. Physics
physics_add_heat_transfer()
physics_configure_boundary("Heat Transfer", "HeatFlux", [1], {"q0": "heat_flux"})
physics_configure_boundary("Heat Transfer", "ConvectiveHeatFlux", [3,4,5], {"h": "convection_coeff"})

# 5. Mesh and solve
mesh_create()
study_solve()

# 6. Results
T_max = results_global_evaluate("ht.Tmax", "K")
results_export_image("temperature", "temp_distribution.png")
```

### Structural Analysis

```
# 1. Load model
model_load("bracket.mph")

# 2. Parameters
param_set("load_force", "1000[N]")
param_set("youngs_modulus", "200[GPa]")
param_set("poissons_ratio", "0.3")

# 3. Physics
physics_add_solid_mechanics()
physics_configure_boundary("Solid Mechanics", "Fixed", [1])
physics_configure_boundary("Solid Mechanics", "BoundaryLoad", [5], {"F_total": "load_force"})

# 4. Solve
mesh_create()
study_solve()

# 5. Evaluate
stress = results_evaluate("solid.mises", "MPa")
displacement = results_global_evaluate("solid.maxDisp", "mm")
```

### Pressure Acoustics in a 2D Duct

```
# 1. Create a 2D model and geometry
model_create("acoustic_duct")
model_create_component(component_name="comp1", space_dimension=2)
geometry_create(
    geometry_name="geom1",
    space_dimension=2,
    component_name="comp1"
)

param_set("duct_length", "1[m]")
param_set("duct_height", "0.1[m]")
param_set("inlet_velocity", "0.01[m/s]")

geometry_add_rectangle(
    position=[0, 0],
    size=["duct_length", "duct_height"],
    geometry_name="geom1",
    component_name="comp1"
)
geometry_build(geometry_name="geom1")

# 2. Create stable side selections
geometry_create_side_selections(
    x_min="0[m]",
    x_max="duct_length",
    y_min="0[m]",
    y_max="duct_height",
    prefix="duct",
    entity_dimension=1,
    geometry_name="geom1",
    component_name="comp1"
)

# 3. Add physics and configure boundaries
physics_add_pressure_acoustics(
    component_name="comp1",
    geometry_name="geom1",
    physics_tag="acpr"
)

boundary_result = physics_setup_acoustic_boundaries(
    physics_name="acpr",
    boundary_conditions=[
        {
            "type": "NormalVelocity",
            "selection_name": "duct_left",
            "properties": {"nvel": "inlet_velocity"}
        },
        {
            "type": "PlaneWaveRadiation",
            "selection_name": "duct_right"
        },
        {
            "type": "SoundHard",
            "selection_name": "duct_bottom"
        },
        {
            "type": "SoundHard",
            "selection_name": "duct_top"
        }
    ]
)

# Check failed_count and failed_boundaries before solving

# 4. Mesh and frequency-domain study
mesh_create(
    mesh_name="mesh1",
    geometry_name="geom1",
    component_name="comp1"
)
study_create(study_type="Frequency", study_name="std1")
study_solve(study_name="std1")
```

The actual frequency list and material properties must be configured for the
specific model. Acoustic feature types and result expressions can vary by
COMSOL version.

### Coefficient Form PDE with Named Boundaries

```
# 1. Create a 2D PDE domain
model_create("coefficient_pde")
model_create_component(component_name="comp1", space_dimension=2)
geometry_create(
    geometry_name="geom1",
    space_dimension=2,
    component_name="comp1"
)

param_set("domain_width", "1[m]")
param_set("domain_height", "1[m]")
param_set("source", "1")

geometry_add_rectangle(
    position=[0, 0],
    size=["domain_width", "domain_height"],
    geometry_name="geom1",
    component_name="comp1"
)
geometry_build(geometry_name="geom1")

# 2. Create named selections
geometry_create_side_selections(
    x_min="0[m]",
    x_max="domain_width",
    y_min="0[m]",
    y_max="domain_height",
    prefix="domain",
    entity_dimension=1,
    geometry_name="geom1",
    component_name="comp1"
)

# 3. Add the PDE and its equation coefficients
pde_result = physics_add_coefficient_form_pde(
    dependent_variables=["u"],
    equation_properties={
        "c": "1",
        "a": "0",
        "f": "source",
        "da": "0",
        "ea": "0"
    },
    physics_tag="c"
)

# Check property_errors before continuing

# 4. Configure PDE boundaries
boundary_result = physics_setup_pde_boundaries(
    physics_name="c",
    boundary_conditions=[
        {
            "type": "dirichlet",
            "selection_name": "domain_left",
            "properties": {"r": "0"}
        },
        {
            "type": "dirichlet",
            "selection_name": "domain_right",
            "properties": {"r": "1"}
        },
        {
            "type": "zero_flux",
            "selection_name": "domain_bottom"
        },
        {
            "type": "zero_flux",
            "selection_name": "domain_top"
        }
    ]
)

# Check failed_count and failed_boundaries before solving

# 5. Mesh, solve, and evaluate
mesh_create(
    mesh_name="mesh1",
    geometry_name="geom1",
    component_name="comp1"
)
study_create(study_type="Stationary", study_name="std1")
study_solve(study_name="std1")
results_evaluate("u")
```

For General Form PDE, replace the add call with
`physics_add_general_form_pde` and configure `Ga`, `f`, `da`, and `ea`. For
Weak Form PDE, use `physics_add_weak_form_pde` and set the `weak` equation
property. Boundary selection and mesh steps remain the same.

### Parametric Sweep

```
# 1. Setup model
model_load("sensitivity_study.mph")

# 2. Configure sweep
param_sweep_setup("electrode_spacing", [1, 2, 3, 4, 5])
# Or continuous range: param_sweep_setup("voltage", ["1[V]", "5[V]", "10[V]", "20[V]"])

# 3. Solve
study_solve("parametric")

# 4. Analyze results
for i in range(1, 6):
    C = results_global_evaluate("2*es.intWe/U^2", "pF", outer=i)
    print(f"Spacing {i}mm: C = {C:.3f} pF")

# 5. Export
results_export_data("sweep_data", "parametric_results.txt")
```

### Time-Dependent Simulation

```
# 1. Setup
model_load("transient_heat.mph")

# 2. Solve asynchronously
study_solve_async("time_dependent")

# 3. Monitor progress
while True:
    progress = study_get_progress()
    print(f"Progress: {progress['progress']*100:.1f}%")
    if progress['status'] in ['completed', 'failed']:
        break
    time.sleep(10)

# 4. Analyze time history
indices, times = results_inner_values("time_dependent")
for t, idx in zip(times[-5:], indices[-5:]):  # Last 5 time steps
    T_max = results_global_evaluate("ht.Tmax", "K", inner=idx)
    print(f"t={t}s: T_max={T_max:.1f}K")
```

### Multiphysics (Thermal-Stress)

```
# 1. Create model
model_create("thermal_stress_analysis")

# 2. Geometry
geometry_add_cylinder(radius="r", height="h")
geometry_build()

# 3. Add multiple physics
physics_add_heat_transfer()
physics_configure_boundary("Heat Transfer", "Temperature", [1], {"T0": "T_hot"})
physics_configure_boundary("Heat Transfer", "Temperature", [2], {"T0": "T_cold"})

physics_add_solid_mechanics()
physics_configure_boundary("Solid Mechanics", "Fixed", [1])

# 4. Add coupling
multiphysics_add("ThermalStress", ["Heat Transfer", "Solid Mechanics"])

# 5. Solve
mesh_create()
study_solve()

# 6. Results
T = results_evaluate("T", "K")
stress = results_evaluate("solid.mises", "MPa")
displacement = results_evaluate("solid.disp", "mm")
```

## Version Control Workflow

```
# Save versions at key milestones
model_save_version(description="initial_geometry")
# ... modify geometry ...
model_save_version(description="geometry_final")

# ... add physics ...
model_save_version(description="physics_configured")

# ... after solving ...
model_save_version(description="solved_baseline")

# ... parameter variation ...
model_save_version(description="param_optimized_v1")
```

## Troubleshooting Common Issues

### Geometry Build Fails
- Check for overlapping features
- Ensure all parameters are defined
- Verify coordinate systems

### Mesh Generation Fails
- Refine geometry details
- Check for very small features
- Try different mesh size settings

### Solver Convergence Issues
- Refine mesh quality
- Check boundary conditions
- Use appropriate solver settings
- Consider scaling of variables

### Memory Issues
- Reduce mesh density
- Use symmetry to reduce domain size
- Solve on more powerful hardware
