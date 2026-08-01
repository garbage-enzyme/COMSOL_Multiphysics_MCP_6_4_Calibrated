# Physics Interfaces Guide

This guide covers common COMSOL physics interfaces and the current typed MCP
helpers. The verified runtime is COMSOL 6.4.0.293 with MPh 1.3.1. The creation
helpers shown below require the `basic_fem` or `full` profile; call
`capabilities` and use live tool schemas as authority.

Before constructing physics, run `solver_status` and `solver_preflight`, build
the geometry, and probe exact domain and boundary IDs. Entity numbers shown in
examples are placeholders, not portable selections. After any topology change,
probe again before applying physics.

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

For a dielectric, call
`physics_add_electrostatics(relpermittivity=2.1, domain_numbers=[...])`. COMSOL
6.3/6.4 otherwise creates a default `FreeSpace` feature that uses vacuum
permittivity instead of the material's relative permittivity.

**Useful Expressions:**
- `es.normE` - Electric field magnitude
- `es.normD` - Electric displacement magnitude
- a bare potential variable may be available where `es.V` is undefined
- `es.intWe` - Electric energy (for integration)

Use grouped unit expressions such as `2*es.intWe/(1[V])^2`.

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
- `TemperatureBoundary` (alias `Temperature`): Fixed temperature (T = T0)
- `HeatFlux`: Specified heat flux
- `ConvectiveHeatFlux`: Convection (q = h·(T - T∞))
- `Radiation`: Radiation heat transfer
- `Symmetry`: Symmetry (adiabatic)
- `ThermalInsulation`: No heat flux

**Example:**
```
physics_add_heat_transfer()
physics_configure_boundary("Heat Transfer", "TemperatureBoundary", [1], {"T0": "300[K]"})
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

### Pressure Acoustics (`acpr`)

Use `physics_add_pressure_acoustics` for an exact Pressure Acoustics interface.
Call `geometry_create_box_selection` or `geometry_create_side_selections` after
building the geometry when stable named boundaries are preferable to numeric
entity IDs. Read `physics_get_acoustic_boundary_conditions` before configuring
one boundary or an atomic batch.

Supported boundary types are `SoundHard`, `SoundSoft`, `Pressure`, `Impedance`,
`NormalAcceleration`, `NormalVelocity`, and `PlaneWaveRadiation`. Their exposed
properties are exact allowlists rather than arbitrary clientapi pass-through.
For example:

```text
physics_add_pressure_acoustics(physics_tag="acpr")
physics_setup_acoustic_boundaries(
  physics_name="acpr",
  boundary_conditions=[
    {"type": "Pressure", "selection_name": "duct_left", "properties": {"p0": "1[Pa]"}},
    {"type": "SoundSoft", "selection_name": "duct_right"}
  ]
)
```

## Mathematical PDE Interfaces

The typed helpers create only Coefficient, General, or Weak Form PDE interfaces.
Dependent-variable tags are bounded and unique, equation properties are
allowlisted per form, and a failed setup removes the new interface. Use
`physics_get_pde_boundary_conditions` before applying Dirichlet, flux, zero-flux,
or periodic conditions. A boundary batch either creates every requested feature
or removes all features created by that request.

```text
physics_add_coefficient_form_pde(
  dependent_variables=["u"],
  equation_properties={"c": "1", "a": "0", "f": "source"},
  physics_tag="c"
)
physics_setup_pde_boundaries(
  physics_name="c",
  boundary_conditions=[
    {"type": "DirichletBoundary", "selection_name": "square_left", "properties": {"r": "0"}}
  ]
)
```

Creation and property readback prove the clientapi operation, not the physical
equation. Validate units, source terms, boundary coverage, mesh, and a numerical
or analytical reference before accepting a PDE result.

## Multiphysics Couplings

`multiphysics_add` is an experimental/full-profile compatibility helper. Verify
the exact coupling type, owning component, selections, and readback for the
target COMSOL build before treating the coupling as configured.

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

## Selection of Physics Interface

When choosing physics interfaces, consider:

1. **Dimensionality**: 2D, 2D axisymmetric, or 3D
2. **Time dependence**: Stationary or time-dependent
3. **Coupling**: Single physics or multiphysics
4. **Nonlinearity**: Linear or nonlinear material behavior
5. **Geometry complexity**: Simple shapes or imported CAD

Typed creation returning success means only that the native API call completed.
It does not prove correct selections, material use, polarization, power closure,
or scientific acceptance.

## Study Types

Different physics require appropriate study types:

| Physics | Recommended Study |
|---------|------------------|
| Electrostatics | Stationary |
| Solid Mechanics | Stationary, Eigenfrequency |
| Heat Transfer | Stationary, Time Dependent |
| Fluid Flow | Stationary, Time Dependent |
| Pressure Acoustics | Frequency Domain, Time Dependent |
| Coefficient/General/Weak Form PDE | Stationary, Time Dependent, Eigenvalue |
| Multiphysics | Depends on coupling |
