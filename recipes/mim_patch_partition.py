"""
MIM patch (Au-Al2O3-Au metasurface) — partition approach.

Verified API (2026-07-07 probe):
  pf = g.feature().create('pf1','PartitionFaces')
  pf.set('partitionwith','workplane')        # enum: workplane | curvesegments
  pf.selection('face').set('<obj_tag>', [bnd_nums])   # PYTHON list, not JArray!
  # workplane property auto-set to 'wp1' when a WorkPlane feature exists.

Patch = 0.3x0.3µm center rectangle on the Al2O3/air interface (bnd6).
LayeredTransition + LML only on patch boundary; rest = plain continuity.
"""
import mph, jpype, sys, time
from _paths import parse_recipe_cores, recipe_output_dir
from _mim_safety import (
    bind_wavelength_step,
    require_named_domains,
    require_partition_result,
    require_port_pair,
    require_passive_reflection,
    require_required_properties,
    require_spectrum,
    save_required,
)
try:
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    sys.stderr.reconfigure(encoding='utf-8', errors='replace')
except Exception: pass

def jarr(v, d=jpype.JDouble): return jpype.JArray(d)(v)
def jarr_i(v): return jpype.JArray(jpype.JInt)(v)

# Geometry params (Chen et al. 2023)
Px=0.6e-6; Py=0.6e-6; t_al2o3=30e-9; H_air=0.83e-6; t_au=30e-9
ax=0.3e-6; px0=(Px-ax)/2  # 0.15µm, patch lower-left corner
# Drude Au via wl parameter (avoids ewfd.freq singularity in sweep)
au_drude = "1-(1.37e16)^2/((2*pi*c_const/wl)*((2*pi*c_const/wl)+i*4.1e13))"

client = mph.Client(cores=parse_recipe_cores(), version='6.4')
print('Connected', client.version, flush=True)
m = client.create('MIM_patch'); jm = m.java
jm.param().set('wl', '5e-6[m]')

# --- Global materials: Common mat_au (Drude) + LayeredMaterial lm_au ---
mat_au_g = jm.material().create('mat_au','Common')
mat_au_g.propertyGroup('def').set('relpermittivity', au_drude)
mat_au_g.propertyGroup('def').set('sigmabnd', '0')
mat_au_g.propertyGroup('def').set('murbnd', '1')
lm_au = jm.material().create('lm_au','LayeredMaterial')
lm_au.set('layername','Au'); lm_au.set('thickness', str(t_au)); lm_au.set('link','mat_au')
lm_au.propertyGroup('def').set('relpermittivity', au_drude)
lm_au.propertyGroup('def').set('sigmabnd', '0')
lm_au.propertyGroup('def').set('murbnd', '1')

# --- Component + geometry: Al2O3 block + air block ---
comp = jm.component().create('comp1', True)
g = comp.geom().create('geom1', 3)
b_al2 = g.feature().create('b_al2','Block'); b_al2.set('size',jarr([Px,Py,t_al2o3])); b_al2.set('selresult', True)
b_air = g.feature().create('b_air','Block'); b_air.set('size',jarr([Px,Py,H_air])); b_air.set('pos',jarr([0,0,t_al2o3])); b_air.set('selresult', True)
g.run()
print(f'Base: dom={g.getNDomains()} bnd={g.getNBoundaries()}', flush=True)

# --- WorkPlane at z=t_al2o3 (interface) with center rectangle (patch footprint) ---
wp = g.feature().create('wp1','WorkPlane')
wp.set('planetype','quick'); wp.set('quickplane','xy'); wp.set('quickz', str(t_al2o3))
wp.set('unite', True)
wpg = wp.geom()
r1 = wpg.feature().create('r1','Rectangle')
r1.set('pos', jarr([px0, px0])); r1.set('size', jarr([ax, ax]))
g.run()
print(f'After WP: dom={g.getNDomains()} bnd={g.getNBoundaries()}', flush=True)

# --- PartitionFaces: split the Al2O3/air interface using the workplane rectangle ---
# Block face numbering: 1=-x,2=+x,3=-y,4=+y,5=-z(bottom),6=+z(top)
# b_air bottom face (z=t_al2o3, facing -z) = bnd5 of b_air object
pf = g.feature().create('pf1','PartitionFaces')
pf.set('partitionwith','workplane')
before_partition_boundaries = int(g.getNBoundaries())
pf.selection('face').set('b_air', [5])
g.run()
after_partition_boundaries = int(g.getNBoundaries())

print(f'After partition: dom={g.getNDomains()} bnd={g.getNBoundaries()}', flush=True)

# --- Identify patch boundary by faceX center (patch center ≈ (Px/2, Py/2, t_al2o3)) ---
# Patch xy-extent = ax×ax = 0.3×0.3µm centered, so center=(0.3µm, 0.3µm, t_al2o3)
# Rest of interface has center offset from (0.3,0.3). Use faceX at param mid.
patch_candidates = []
nb = g.getNBoundaries()
print('\nBoundary centers (z≈t_al2o3):', flush=True)
JD2 = jpype.JArray(jpype.JArray(jpype.JDouble))
for bn in range(1, nb+1):
    try:
        pr = list(g.faceParamRange(bn))
        u_mid = (float(pr[0])+float(pr[1]))/2.0
        v_mid = (float(pr[2])+float(pr[3]))/2.0
        pp = JD2(1); pp[0] = jpype.JArray(jpype.JDouble)([u_mid, v_mid])
        cx, cy, cz = [float(x) for x in list(g.faceX(bn, pp)[0])]
        # Patch boundary: z≈t_al2o3 AND center xy near (Px/2, Py/2)
        if abs(cz - t_al2o3) < 1e-9 and abs(cx - Px/2) < ax/4 and abs(cy - Py/2) < ax/4:
            print(f'  bnd{bn}: center=({cx*1e6:.3f},{cy*1e6:.3f},{cz*1e6:.3f})µm  <- PATCH', flush=True)
            patch_candidates.append(bn)
        elif abs(cz - t_al2o3) < 1e-9:
            print(f'  bnd{bn}: center=({cx*1e6:.3f},{cy*1e6:.3f},{cz*1e6:.3f})µm  (interface rest)', flush=True)
    except Exception as e:
        pass
patch_bnd = require_partition_result(
    before_partition_boundaries,
    after_partition_boundaries,
    patch_candidates,
)
print(f'Patch boundary = bnd{patch_bnd}', flush=True)

# --- Domain materials ---
al2_domains = require_named_domains(comp, 'geom1_b_al2_dom')
air_domains = require_named_domains(comp, 'geom1_b_air_dom')
mat_al2 = comp.material().create('mat_al2','Common')
mat_al2.propertyGroup('def').set('relpermittivity','3.1'); mat_al2.selection().set(al2_domains)
mat_air = comp.material().create('mat_air','Common')
mat_air.propertyGroup('def').set('relpermittivity','1'); mat_air.selection().set(air_domains)

# --- ewfd + PeriodicStructure ---
p = comp.physics().create('ewfd','ElectromagneticWavesFrequencyDomain', str(g.getSDim()))
ps = p.feature().create('ps1','PeriodicStructure',3)
p1b, p2b = require_port_pair(
    ps.feature('pport1').selection().entities(),
    ps.feature('pport2').selection().entities(),
    geometry=g,
    top_domains=air_domains,
    bottom_domains=al2_domains,
)
ps.selection('excitedPortSelection').set(p1b)
print(f'pport1(top)={p1b} pport2(bottom)={p2b}', flush=True)

# --- LayeredImpedance on bottom port (substrate Au, Drude) ---
lib = p.feature().create('lib1','LayeredImpedanceBoundaryCondition',2)
lib.selection().set(p2b)
lib.set('substrateMaterial','mat_au')
lib.set('DisplacementFieldModelSubstrate','RelativePermittivity')
lib.set('epsilonrImp_mat','userdef'); lib.set('epsilonrImp', au_drude); lib.set('allLayers', False)

# --- LML on patch boundary (Au thin film via LayeredMaterial) ---
lml_au = comp.material().create('lml_au','LayeredMaterialLink')
lml_au.set('link','lm_au')
lml_au.selection().all(); lml_au.selection().clear(); lml_au.selection().add([patch_bnd])
sh = lml_au.propertyGroup('shell')
sh.set('lth', str(t_au)); sh.set('relpermittivity', au_drude)
sh.set('sigmabnd', '0'); sh.set('murbnd', '1')

# --- LayeredTransition on patch boundary (uses LML shell) ---
ltr = p.feature().create('ltr1','LayeredTransitionBoundaryCondition',2)
ltr.selection().set([patch_bnd])
ltr.set('DisplacementFieldModel','RelativePermittivity')
require_required_properties(ltr, {
    'DisplacementFieldModel': 'RelativePermittivity',
    'epsilonr_mat': 'userdef',
    'epsilonr': au_drude,
    'sigmabnd_mat': 'userdef',
    'sigmabnd': '0',
    'murbnd_mat': 'userdef',
    'murbnd': '1',
    'lth': str(t_au),
})
print(f'LTR on bnd{patch_bnd}: lth={ltr.getString("lth")} shelllist={ltr.getString("shelllist")}', flush=True)

# --- Mesh: Sweep along z (FreeTri on bottom + Sweep) ---
mesh = comp.mesh().create('mesh1')
sz = mesh.feature().create('size1','Size')
sz.set('hmax', float(H_air/10)); sz.set('hmaxactive', True)
sz.set('hmin', float(t_al2o3/2)); sz.set('hminactive', True)
ftri = mesh.feature().create('ftri1','FreeTri'); ftri.selection().set(p2b)
sw = mesh.feature().create('sw1','Sweep'); sw.selection().set(al2_domains + air_domains)
try:
    mesh.run(); print(f'Mesh: {mesh.getNumElem()} elements', flush=True)
except Exception as e:
    print(f'Mesh FAIL: {repr(e)[:200]}', flush=True)
    # Fallback: plain FreeTet
    try:
        mesh.feature().remove('ftri1'); mesh.feature().remove('sw1')
        mesh.feature().create('ftet1','FreeTet')
        mesh.run(); print(f'Fallback FreeTet mesh: {mesh.getNumElem()} elements', flush=True)
    except Exception as e2:
        raise RuntimeError('both MIM mesh strategies failed') from e2

# --- Study: Wavelength step (dummy) + Parametric sweep on wl ---
wls = [3e-6, 4e-6, 5e-6, 6e-6, 7e-6, 8e-6]
study = jm.study().create('std1')
study.create('step1','Wavelength')
step = study.feature('step1'); bind_wavelength_step(step, 'wl')
study.create('sweep1','Parametric')
sweep = study.feature('sweep1'); sweep.set('pname','wl')
sweep.set('plist', ' '.join(str(w) for w in wls))
print(f'Sweeping {len(wls)} wavelengths: {wls}', flush=True)
t0=time.time(); jm.study('std1').run(); t1=time.time()
R = require_passive_reflection(require_spectrum(m.evaluate('ewfd.Rtotal'), wls, 'Rtotal'), 'Rtotal')
print(f'Solve OK in {t1-t0:.2f}s', flush=True)
for wl, value in zip(wls, R):
    print(f'  wl={wl*1e6:.1f}um R={value:.6f}', flush=True)

output_dir = recipe_output_dir()
save_required(m.java, output_dir / "MIM_patch.mph")
try: client.disconnect()
except Exception: pass
print('Done.', flush=True)
