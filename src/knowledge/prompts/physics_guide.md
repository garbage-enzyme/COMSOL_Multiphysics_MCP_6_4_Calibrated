# Physics Interfaces Guide

This guide covers the most commonly used physics interfaces in COMSOL Multiphysics and how to set them up using the MCP tools.

## Overview

COMSOL physics interfaces define the governing equations and boundary conditions for simulations. A single model can have multiple physics interfaces, and they can be coupled together for multiphysics simulations.

## AC/DC Module

### Electrostatics (es)

For static electric fields and capacitance calculations.

**Key Features:**
- Electric potential distribution
- Capacitance calculation
- Electric field strength
- Energy storage

**Boundary Conditions:**
- `Ground`: Zero potential (V = 0)
- `ElectricPotential`: Specified voltage (V = V0)
- `SurfaceChargeDensity`: Surface charge (σ)
- `ZeroCharge`: Zero normal displacement (n·D = 0)
- `Terminal`: For terminal-based capacitance

**Example:**
```
physics_add_electrostatics()
physics_configure_boundary("Electrostatics", "Ground", [1])
physics_configure_boundary("Electrostatics", "ElectricPotential", [2], {"V0": "10[V]"})
```

**Useful Expressions:**
- `es.normE` - Electric field magnitude
- `es.normD` - Electric displacement magnitude
- `es.V` - Electric potential
- `es.intWe` - Electric energy (for integration)

### Electric Currents (ec)

For DC current conduction.

**Key Features:**
- Current density distribution
- Resistance calculation
- Power dissipation

**Boundary Conditions:**
- `Ground`: Zero potential
- `ElectricPotential`: Specified voltage
- `NormalCurrentDensity`: Specified current
- `Terminal`: For circuit connections

## Structural Mechanics Module

### Solid Mechanics (solid)

For stress, strain, and deformation analysis.

**Key Features:**
- Stress distribution
- Displacement fields
- Modal analysis
- Contact mechanics

**Boundary Conditions:**
- `Fixed`: Fixed constraint (u = 0)
- `Roller`: Roller constraint (normal displacement = 0)
- `Symmetry`: Symmetry plane
- `BoundaryLoad`: Applied force/pressure
- `Displacement`: Prescribed displacement

**Example:**
```
physics_add_solid_mechanics()
physics_configure_boundary("Solid Mechanics", "Fixed", [1])
physics_configure_boundary("Solid Mechanics", "BoundaryLoad", [2], {"F_total": "1000[N]"})
```

**Useful Expressions:**
- `solid.mises` - Von Mises stress
- `solid.disp` - Displacement magnitude
- `solid.u`, `solid.v`, `solid.w` - Displacement components
- `solid.epxx` - Strain components

## Heat Transfer Module

### Heat Transfer in Solids (ht)

For temperature distribution and thermal analysis.

**Key Features:**
- Temperature distribution
- Heat flux
- Thermal gradients
- Transient thermal analysis

**Boundary Conditions:**
- `Temperature`: Fixed temperature (T = T0)
- `HeatFlux`: Specified heat flux
- `ConvectiveHeatFlux`: Convection (q = h·(T - T∞))
- `Radiation`: Radiation heat transfer
- `Symmetry`: Symmetry (adiabatic)
- `ThermalInsulation`: No heat flux

**Example:**
```
physics_add_heat_transfer()
physics_configure_boundary("Heat Transfer", "Temperature", [1], {"T0": "300[K]"})
physics_configure_boundary("Heat Transfer", "ConvectiveHeatFlux", [2], {"h": "10[W/(m^2*K)]", "Text": "293[K]"})
```

**Useful Expressions:**
- `T` - Temperature
- `ht.qx`, `ht.qy`, `ht.qz` - Heat flux components
- `ht.gradTx` - Temperature gradient
- `ht.Qh` - Heat source

## Fluid Flow Module

### Laminar Flow (spf)

For incompressible fluid flow at low Reynolds numbers.

**Key Features:**
- Velocity field
- Pressure distribution
- Flow rate calculations
- Drag/lift forces

**Boundary Conditions:**
- `Wall`: No-slip wall
- `Inlet`: Velocity or mass flow inlet
- `Outlet`: Pressure outlet
- `Symmetry`: Symmetry plane
- `Slip`: Slip wall

**Example:**
```
physics_add_laminar_flow()
physics_configure_boundary("Laminar Flow", "Inlet", [1], {"U0": "1[m/s]"})
physics_configure_boundary("Laminar Flow", "Outlet", [2], {"p0": "0[Pa]"})
```

**Useful Expressions:**
- `u`, `v`, `w` - Velocity components
- `p` - Pressure
- `spf.U` - Velocity magnitude
- `spf.rho` - Density

## Acoustics Module

### Pressure Acoustics (acpr)

For frequency-domain propagation of pressure waves in fluids.

**Key Features:**
- Acoustic pressure and sound pressure level
- Radiation and impedance boundaries
- Prescribed pressure, velocity, and acceleration sources
- Domain restriction through explicit domain selections

**Add the Interface:**
```
physics_add_pressure_acoustics(
    physics_tag="acpr",
    domain_selection=[1]
)
```

Use `physics_add_acoustics` when an installed Acoustics Module interface does
not have a dedicated MCP tool. Pass the exact COMSOL Java API physics type:

```
physics_add_acoustics(
    physics_type="ThermoacousticsSinglePhysics",
    physics_tag="ta"
)
```

Availability depends on the installed COMSOL products, licenses, and version.

**Common Boundary Features and Properties:**

| Feature Type | Purpose | Common Properties |
|--------------|---------|-------------------|
| `SoundHard` | Rigid wall | None |
| `SoundSoft` | Zero acoustic pressure | None |
| `Pressure` | Prescribed acoustic pressure | `p0` |
| `Impedance` | Specific acoustic impedance | `Zn` |
| `NormalAcceleration` | Prescribed normal acceleration | `nacc` |
| `NormalVelocity` | Prescribed normal velocity | `nvel` |
| `PlaneWaveRadiation` | Plane-wave radiation boundary | None |
| `SphericalWaveRadiation` | Spherical-wave radiation boundary | None |

Use `physics_get_acoustic_boundary_conditions` to retrieve this reference
programmatically. Version-specific feature types can still be passed through
the specialized configuration tools.

**Configure One Boundary:**
```
physics_configure_acoustic_boundary(
    physics_name="acpr",
    boundary_condition="Impedance",
    selection_name="duct_outlet",
    properties={"Zn": "rho0*c0"}
)
```

`selection_name` binds the feature to a stable named COMSOL selection.
Alternatively, provide `boundary_selection=[3]` with explicit entity numbers.

**Configure Multiple Boundaries:**
```
physics_setup_acoustic_boundaries(
    physics_name="acpr",
    boundary_conditions=[
        {
            "type": "NormalVelocity",
            "selection_name": "duct_inlet",
            "properties": {"nvel": "inlet_velocity"}
        },
        {
            "type": "PlaneWaveRadiation",
            "selection_name": "duct_outlet"
        },
        {
            "type": "SoundHard",
            "selection_name": "duct_walls"
        }
    ]
)
```

**Useful Expressions:**
- `acpr.p_t` - Total acoustic pressure
- `acpr.Lp_t` - Total sound pressure level
- `acpr.Ix` - Acoustic intensity component in the x direction

Expression names can vary with the acoustic interface and COMSOL version.

## Mathematics Interfaces

The PDE tools create geometry-based Coefficient Form, General Form, and Weak
Form PDE interfaces. Dependent-variable names are user-defined and default to
`u`. Scalar, vector, or matrix equation properties may be required when more
than one dependent variable is used.

### Coefficient Form PDE (c)

For equations expressed using mass, damping, diffusion, convection, absorption,
and source coefficients.

```
physics_add_coefficient_form_pde(
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
```

Common equation properties are `c`, `a`, `f`, `da`, `ea`, `al`, `be`, and
`ga`.

### General Form PDE (g)

For equations defined by a conservative flux and source terms.

```
physics_add_general_form_pde(
    dependent_variables=["u"],
    equation_properties={
        "Ga": "-D*grad(u)",
        "f": "source",
        "da": "0",
        "ea": "0"
    },
    physics_tag="g"
)
```

Common equation properties are `Ga`, `f`, `da`, and `ea`.

### Weak Form PDE (w)

For equations specified directly as weak expressions.

```
physics_add_weak_form_pde(
    dependent_variables=["u"],
    equation_properties={"weak": "test(u)*(source-u)"},
    physics_tag="w"
)
```

The common equation property is `weak`.

### PDE Boundary Conditions

| Feature Type | Purpose | Common Properties |
|--------------|---------|-------------------|
| `DirichletBoundary` | Prescribed dependent-variable value | `r` |
| `FluxBoundary` | Generalized inward flux or source | `g`, `q` |
| `ZeroFluxBoundary` | Zero inward flux | None |
| `WeakContribution` | Additional weak boundary contribution | `weak` |
| `PeriodicCondition` | Periodic boundary condition | None |

The aliases `dirichlet`, `flux`, `neumann`, `zero_flux`, `no_flux`, `wall`,
`weak`, and `periodic` are accepted by the PDE boundary tools. Use
`physics_get_pde_boundary_conditions` to retrieve the current feature,
property, and alias reference.

**Configure PDE Boundaries:**
```
physics_setup_pde_boundaries(
    physics_name="c",
    boundary_conditions=[
        {
            "type": "dirichlet",
            "selection_name": "domain_left",
            "properties": {"r": "0"}
        },
        {
            "type": "flux",
            "selection_name": "domain_right",
            "properties": {"g": "boundary_source", "q": "0"}
        },
        {
            "type": "zero_flux",
            "selection_name": "domain_top"
        }
    ]
)
```

For both acoustics and PDE tools, inspect `property_errors`,
`failed_boundaries`, and `failed_count` in the returned result. A feature can
be created successfully while one or more version-specific properties are
rejected by COMSOL.

## Multiphysics Couplings

### Thermal Stress (ts)

Couples Heat Transfer and Solid Mechanics for thermal expansion.

**Required Physics:**
1. Heat Transfer in Solids
2. Solid Mechanics

**Example:**
```
physics_add_heat_transfer()
physics_add_solid_mechanics()
multiphysics_add("ThermalStress", ["Heat Transfer", "Solid Mechanics"])
```

### Joule Heating (jh)

Couples Electric Currents and Heat Transfer for resistive heating.

**Required Physics:**
1. Electric Currents
2. Heat Transfer

**Example:**
```
physics_add("ElectricCurrents")
physics_add_heat_transfer()
multiphysics_add("JouleHeating", ["Electric Currents", "Heat Transfer"])
```

### Fluid-Structure Interaction (fsi)

Couples Fluid Flow and Solid Mechanics.

**Required Physics:**
1. Laminar Flow (or turbulent)
2. Solid Mechanics

**Example:**
```
physics_add_laminar_flow()
physics_add_solid_mechanics()
multiphysics_add("FluidStructureInteraction", ["Laminar Flow", "Solid Mechanics"])
```

## Materials

Common material properties needed for different physics:

| Physics | Required Properties |
|---------|---------------------|
| Electrostatics | Relative permittivity (εr) |
| Electric Currents | Electrical conductivity (σ) |
| Solid Mechanics | Young's modulus (E), Poisson's ratio (ν), Density (ρ) |
| Heat Transfer | Thermal conductivity (k), Specific heat (Cp), Density (ρ) |
| Laminar Flow | Density (ρ), Dynamic viscosity (μ) |

Additional requirements for the newly supported interfaces:

| Physics | Required Properties |
|---------|---------------------|
| Pressure Acoustics | Density, Speed of sound |
| PDE Interfaces | Coefficients defined by the governing equation |

## Selection of Physics Interface

When choosing physics interfaces, consider:

1. **Dimensionality**: 2D, 2D axisymmetric, or 3D
2. **Time dependence**: Stationary or time-dependent
3. **Coupling**: Single physics or multiphysics
4. **Nonlinearity**: Linear or nonlinear material behavior
5. **Geometry complexity**: Simple shapes or imported CAD

## Study Types

Different physics require appropriate study types:

| Physics | Recommended Study |
|---------|------------------|
| Electrostatics | Stationary |
| Solid Mechanics | Stationary, Eigenfrequency |
| Heat Transfer | Stationary, Time Dependent |
| Fluid Flow | Stationary, Time Dependent |
| Pressure Acoustics | Frequency |
| Coefficient Form PDE | Stationary, Time Dependent, Eigenfrequency |
| General Form PDE | Stationary, Time Dependent |
| Weak Form PDE | Depends on the weak equation |
| Multiphysics | Depends on coupling |
