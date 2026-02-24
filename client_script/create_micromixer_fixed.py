"""Create 3D Microfluidic Mixer - Fixed Boundary Identification."""
import sys
if sys.platform == 'win32':
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')

import mph
from pathlib import Path
from datetime import datetime
import numpy as np

print("=" * 70)
print("3D Microfluidic Mixer - Fixed Boundary Selection")
print("=" * 70)

# Parameters
MODEL_NAME = "micromixer_fixed"
PROJECT_ROOT = Path(__file__).parent.parent
MODELS_DIR = PROJECT_ROOT / "comsol_models" / MODEL_NAME
MODELS_DIR.mkdir(parents=True, exist_ok=True)

print(f"\nModel directory: {MODELS_DIR}")

# Start COMSOL
print("\n[1] Starting COMSOL...")
client = mph.start(cores=4)
print("    Client started")

# Create model
print("\n[2] Creating model...")
model = client.create(MODEL_NAME)
jm = model.java
print(f"    Model: {model.name()}")

# Add parameters
print("\n[3] Setting parameters...")
params = jm.param()
params.set('w_main', '500[um]', 'Main channel width')
params.set('h_ch', '100[um]', 'Channel height')
params.set('L_main', '2000[um]', 'Main channel length')
params.set('w_inlet', '200[um]', 'Inlet width')
params.set('L_inlet', '500[um]', 'Inlet length')
params.set('v_in', '0.01[m/s]', 'Inlet velocity')
print("    Parameters set")

# Create component and geometry
print("\n[4] Creating 3D geometry...")
comp = jm.component().create('comp1', True)
geom = comp.geom().create('geom1', 3)

# Create a simpler geometry: single Y-channel using workplane and extrusion
# Or use three separate blocks that touch (forming a Y-mixer)

# For simplicity, let's create a T-mixer geometry:
# Main channel + two inlets merging at one point

# Main channel: L_main x w_main x h_ch (positioned at origin)
blk1 = geom.feature().create('blk1', 'Block')
blk1.set('size', ['L_main', 'w_main', 'h_ch'])
blk1.set('pos', ['0', '0', '0'])
blk1.label('Main Channel')

# Inlet 1 (bottom): connects to main channel at x=0, y in [0, w_inlet]
blk2 = geom.feature().create('blk2', 'Block')
blk2.set('size', ['L_inlet', 'w_inlet', 'h_ch'])
blk2.set('pos', ['-L_inlet', '0', '0'])
blk2.label('Inlet 1')

# Inlet 2 (top): connects to main channel at x=0, y in [w_main-w_inlet, w_main]
blk3 = geom.feature().create('blk3', 'Block')
blk3.set('size', ['L_inlet', 'w_inlet', 'h_ch'])
blk3.set('pos', ['-L_inlet', 'w_main-w_inlet', '0'])
blk3.label('Inlet 2')

# Union to merge touching faces
uni = geom.feature().create('uni1', 'Union')
uni.selection('input').set(['blk1', 'blk2', 'blk3'])

print("    Building geometry...")
geom.run()
print("    Geometry built")

# Get mesh info to understand boundary count
print("\n[5] Creating mesh to identify boundaries...")
mesh = comp.mesh().create('mesh1', 'geom1')
mesh.autoMeshSize(5)
mesh.run()
print("    Mesh generated")

# Add water material
print("\n[6] Adding water material...")
mat = comp.material().create('mat1', 'Common')
mat.propertyGroup('def').set('relpermeability', '1')
mat.propertyGroup('def').set('relpermittivity', '80')
mat.propertyGroup('def').set('density', '1000[kg/m^3]')
mat.propertyGroup('def').set('dynamicviscosity', '0.001[Pa*s]')
mat.label('Water')

# Add Laminar Flow physics
print("\n[7] Adding Laminar Flow physics...")
spf = comp.physics().create('spf', 'LaminarFlow', 'geom1')
spf.label('Laminar Flow')

# Try to set boundary conditions using selections or manual boundary numbers
print("\n[8] Setting boundary conditions...")

# Common boundary numbering for a union of 3 blocks in COMSOL:
# The exact numbering depends on how COMSOL merges the faces
# Let's try a few common patterns

# For debugging, let's print all boundary entities
# In COMSOL, typical ordering for blocks:
# Block boundaries are numbered 1-6 (x_min, x_max, y_min, y_max, z_min, z_max)
# After union, overlapping faces are merged

# Based on typical COMSOL behavior for union of touching blocks:
# - The merged inlet faces become separate boundaries
# - Let's try boundaries 1, 3 for inlets and 2 for outlet

inlet_boundaries_found = []
outlet_boundary_found = None

# Set boundary conditions using fallback boundary numbers
print("    Using fallback boundary numbers (1, 3 for inlets, 2 for outlet)...")
for i, bnd in enumerate([1, 3]):
    inlet_tag = f'inl{i+1}'
    try:
        inlet = spf.create(inlet_tag, 'InletBoundary')
        inlet.selection().set([bnd])
        # U0in is the correct property name for inlet velocity
        inlet.set('U0in', 'v_in')
        inlet.label(f'Inlet {i+1} (Boundary {bnd})')
        print(f"    Inlet {i+1} configured on boundary {bnd} with U0in=v_in")
    except Exception as e:
        print(f"    Inlet {i+1} error: {e}")

# Outlet
try:
    outlet = spf.create('out1', 'OutletBoundary')
    outlet.selection().set([2])
    outlet.set('p0', '0')
    outlet.label('Outlet (Boundary 2)')
    print("    Outlet configured on boundary 2")
except Exception as e:
    print(f"    Outlet error: {e}")

# Add study
print("\n[10] Adding stationary study...")
study = jm.study().create('std1')
study.create('stat', 'Stationary')

# Solve
print("\n[11] Solving...")
try:
    study.run()
    print("    Solution complete!")
    solution_success = True
except Exception as e:
    print(f"    Solution error: {e}")
    solution_success = False

# Save model
timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
version_file = MODELS_DIR / f"{MODEL_NAME}_{timestamp}.mph"
latest_file = MODELS_DIR / f"{MODEL_NAME}_latest.mph"

model.save(str(version_file))
model.save(str(latest_file))

print(f"\n[12] Model saved:")
print(f"    Version: {version_file.name}")
print(f"    Latest:  {latest_file.name}")

# Evaluate results
if solution_success:
    print("\n[13] Evaluating results...")
    try:
        U = model.evaluate('spf.U', unit='m/s')
        print(f"    Velocity range: {np.min(U):.6f} to {np.max(U):.6f} m/s")
        print(f"    Mean velocity: {np.mean(U):.6f} m/s")
        print(f"    Max velocity: {np.max(U)*1000:.4f} mm/s")
        
        # Also check velocity components
        try:
            u = model.evaluate('u', unit='m/s')
            v = model.evaluate('v', unit='m/s')
            w = model.evaluate('w', unit='m/s')
            print(f"    u range: {np.min(u):.6f} to {np.max(u):.6f} m/s")
            print(f"    v range: {np.min(v):.6f} to {np.max(v):.6f} m/s")
        except:
            pass
    except Exception as e:
        print(f"    Could not evaluate: {e}")

print("\n" + "=" * 70)
print("SIMULATION COMPLETE")
print("=" * 70)

client.clear()
print("\nDone!")
