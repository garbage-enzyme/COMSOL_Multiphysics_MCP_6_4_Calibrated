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
from _paths import recipe_output_dir
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

client = mph.Client(cores=4, version='6.4')
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
g.feature().create('b_al2','Block').set('size',jarr([Px,Py,t_al2o3]))
g.feature().create('b_air','Block').set('size',jarr([Px,Py,H_air]))
g.feature('b_air').set('pos',jarr([0,0,t_al2o3]))
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
# Try b_air face 5 (bottom); fallback to b_al2 face 6 (top)
partitioned = False
for obj_tag, face_nums in [('b_air',[5]), ('b_al2',[6]), ('b_air',[1,2,3,4,5,6]), ('b_al2',[1,2,3,4,5,6])]:
    try:
        pf.selection('face').set(obj_tag, face_nums)
        g.run()
        print(f'  partition face.set({obj_tag},{face_nums}): OK -> bnd={g.getNBoundaries()}', flush=True)
        partitioned = True
        break
    except Exception as e:
        print(f'  partition face.set({obj_tag},{face_nums}): {repr(e)[:120]}', flush=True)
if not partitioned:
    print('FATAL: could not partition. Aborting.', flush=True)
    raise RuntimeError('partition failed')

print(f'After partition: dom={g.getNDomains()} bnd={g.getNBoundaries()}', flush=True)

# --- Identify patch boundary by faceX center (patch center ≈ (Px/2, Py/2, t_al2o3)) ---
# Patch xy-extent = ax×ax = 0.3×0.3µm centered, so center=(0.3µm, 0.3µm, t_al2o3)
# Rest of interface has center offset from (0.3,0.3). Use faceX at param mid.
patch_bnd = None
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
            patch_bnd = bn
        elif abs(cz - t_al2o3) < 1e-9:
            print(f'  bnd{bn}: center=({cx*1e6:.3f},{cy*1e6:.3f},{cz*1e6:.3f})µm  (interface rest)', flush=True)
    except Exception as e:
        pass
if patch_bnd is None:
    print('Could not auto-identify patch boundary; falling back to highest bnd number.', flush=True)
    patch_bnd = nb
print(f'Patch boundary = bnd{patch_bnd}', flush=True)

# --- Domain materials ---
mat_al2 = comp.material().create('mat_al2','Common')
mat_al2.propertyGroup('def').set('relpermittivity','3.1'); mat_al2.selection().set([1])
mat_air = comp.material().create('mat_air','Common')
mat_air.propertyGroup('def').set('relpermittivity','1'); mat_air.selection().set([2])

# --- ewfd + PeriodicStructure ---
p = comp.physics().create('ewfd','ElectromagneticWavesFrequencyDomain', str(g.getSDim()))
ps = p.feature().create('ps1','PeriodicStructure',3)
p1b = list(ps.feature('pport1').selection().entities())
p2b = list(ps.feature('pport2').selection().entities())
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
for prop, val in [('sigmabnd_mat','userdef'),('sigmabnd','0'),('murbnd_mat','userdef'),('murbnd','1')]:
    try: ltr.set(prop, val)
    except Exception: pass
ltr.set('lth', str(t_au))
print(f'LTR on bnd{patch_bnd}: lth={ltr.getString("lth")} shelllist={ltr.getString("shelllist")}', flush=True)

# --- Mesh: Sweep along z (FreeTri on bottom + Sweep) ---
mesh = comp.mesh().create('mesh1')
sz = mesh.feature().create('size1','Size')
sz.set('hmax', float(H_air/10)); sz.set('hmaxactive', True)
sz.set('hmin', float(t_al2o3/2)); sz.set('hminactive', True)
ftri = mesh.feature().create('ftri1','FreeTri'); ftri.selection().set(p2b)
sw = mesh.feature().create('sw1','Sweep'); sw.selection().set([1,2])
try:
    mesh.run(); print(f'Mesh: {mesh.getNumElem()} elements', flush=True)
except Exception as e:
    print(f'Mesh FAIL: {repr(e)[:200]}', flush=True)
    # Fallback: plain FreeTet
    try:
        mesh.feature().remove('ftri1'); mesh.feature().remove('sw1')
        ftet = mesh.feature().create('ftet1','FreeTet')
        mesh.run(); print(f'Fallback FreeTet mesh: {mesh.getNumElem()} elements', flush=True)
    except Exception as e2:
        print(f'FreeTet also FAIL: {repr(e2)[:200]}', flush=True)

# --- Study: Wavelength step (dummy) + Parametric sweep on wl ---
wls = [3e-6, 4e-6, 5e-6, 6e-6, 7e-6, 8e-6]
study = jm.study().create('std1')
study.create('step1','Wavelength')
step = study.feature('step1'); step.set('punit','m'); step.set('plist', str(5e-6))
study.create('sweep1','Parametric')
sweep = study.feature('sweep1'); sweep.set('pname','wl')
sweep.set('plist', ' '.join(str(w) for w in wls))
print(f'Sweeping {len(wls)} wavelengths: {wls}', flush=True)
try:
    t0=time.time(); jm.study('std1').run(); t1=time.time()
    print(f'Solve OK in {t1-t0:.2f}s', flush=True)
    R = m.evaluate('ewfd.Rtotal')
    print('\n=== Results ===', flush=True)
    for i, wl in enumerate(wls):
        Ri = float(R[i])
        print(f'  wl={wl*1e6:.1f}µm  R={Ri:.6f}  eps=1-R={1-Ri:.6f}', flush=True)
except Exception as e:
    print(f'Solve FAIL: {repr(e)[:300]}', flush=True)
    import traceback; traceback.print_exc()

output_dir = recipe_output_dir()
try: m.java.save(str((output_dir / "MIM_patch.mph").resolve()))
except Exception as e: print(f'save err: {repr(e)[:150]}', flush=True)
try: client.disconnect()
except Exception: pass
print('Done.', flush=True)
