"""
MIM with Drude dispersion + wavelength sweep 1-10 µm.
Continuous Au film (no patch) first — verify Rtotal<1 at some wavelengths.
Then patch (spatial-varying lth) for resonance.
"""
import mph, jpype, sys, time
from _paths import recipe_output_dir
try:
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    sys.stderr.reconfigure(encoding='utf-8', errors='replace')
except Exception: pass

def jarr(v, d=jpype.JDouble): return jpype.JArray(d)(v)

Px=0.6e-6; Py=0.6e-6; t_al2o3=30e-9; H_air=0.83e-6; t_au=30e-9
au_drude = "1-(1.37e16)^2/((2*pi*ewfd.freq)*((2*pi*ewfd.freq)+i*4.1e13))"
# Parametric Drude using wl parameter (avoids ewfd.freq singularity in sweep)
au_drude_param = "1-(1.37e16)^2/((2*pi*c_const/wl)*((2*pi*c_const/wl)+i*4.1e13))"
# Fixed freq for testing (wl=5µm: f=6e13 Hz)
f_fix = 6e13
au_drude_fix = f"1-(1.37e16)^2/((2*pi*{f_fix})*((2*pi*{f_fix})+i*4.1e13))"

client = mph.Client(cores=4, version='6.4')
print('Connected', client.version, flush=True)
m = client.create('MIM_drude'); jm = m.java

# Global Au material (Drude)
mat_au_g = jm.material().create('mat_au','Common')
mat_au_g.propertyGroup('def').set('relpermittivity', au_drude_param)
mat_au_g.propertyGroup('def').set('sigmabnd', '0')
mat_au_g.propertyGroup('def').set('murbnd', '1')

# Global LayeredMaterial
lm_au = jm.material().create('lm_au','LayeredMaterial')
lm_au.set('layername','Au'); lm_au.set('thickness', str(t_au)); lm_au.set('link','mat_au')
lm_au.propertyGroup('def').set('relpermittivity', au_drude_param)
lm_au.propertyGroup('def').set('sigmabnd', '0')
lm_au.propertyGroup('def').set('murbnd', '1')

# Add wl parameter for Drude expression
jm.param().set('wl', '5e-6[m]')
print('param wl set', flush=True)

# Component
comp = jm.component().create('comp1', True)
g = comp.geom().create('geom1', 3)
g.feature().create('b_al2','Block').set('size',jarr([Px,Py,t_al2o3]))
g.feature().create('b_air','Block').set('size',jarr([Px,Py,H_air])); g.feature('b_air').set('pos',jarr([0,0,t_al2o3]))
g.run()

mat_al2 = comp.material().create('mat_al2','Common')
mat_al2.propertyGroup('def').set('relpermittivity','3.1'); mat_al2.selection().set([1])
mat_air = comp.material().create('mat_air','Common')
mat_air.propertyGroup('def').set('relpermittivity','1'); mat_air.selection().set([2])

# LayeredMaterialLink on bnd6
lml_au = comp.material().create('lml_au','LayeredMaterialLink')
lml_au.set('link','lm_au')
lml_au.selection().all(); lml_au.selection().clear(); lml_au.selection().add([6])
sh = lml_au.propertyGroup('shell')
sh.set('lth', str(t_au)); sh.set('relpermittivity', au_drude_param)
sh.set('sigmabnd', '0'); sh.set('murbnd', '1')

# ewfd + PeriodicStructure
p = comp.physics().create('ewfd','ElectromagneticWavesFrequencyDomain', str(g.getSDim()))
ps = p.feature().create('ps1','PeriodicStructure',3)
p1b = list(ps.feature('pport1').selection().entities()); p2b = list(ps.feature('pport2').selection().entities())
ps.selection('excitedPortSelection').set(p1b)

# LayeredImpedance on bottom (substrate Au, Drude)
lib = p.feature().create('lib1','LayeredImpedanceBoundaryCondition',2)
lib.selection().set(p2b)
lib.set('substrateMaterial','mat_au')
lib.set('DisplacementFieldModelSubstrate','DrudeLorentzDispersionModel')
# Drude substrate params
try:
    lib.set('epsilonrImp_mat','userdef')
    lib.set('epsilonrImp', au_drude_param)
    lib.set('allLayers', False)
    print('LIB Drude set (RelativePermittivity mode)', flush=True)
except Exception as e: print('LIB err:', repr(e)[:100], flush=True)

# LayeredTransition on bnd6
ltr = p.feature().create('ltr1','LayeredTransitionBoundaryCondition',2)
ltr.selection().set([6])
ltr.set('DisplacementFieldModel','RelativePermittivity')
for prop, val in [('sigmabnd_mat','userdef'),('sigmabnd','0'),('murbnd_mat','userdef'),('murbnd','1')]:
    try: ltr.set(prop, val)
    except Exception: pass
ax = 0.3e-6; px0 = (Px - ax)/2  # 0.15µm
patch_lth = f"if(x>{px0} && x<{px0+ax} && y>{px0} && y<{px0+ax}, {t_au}, 1e-15)"
# Set patch lth on LML shell + BC (global LM can't use coordinates)
sh.set('lth', patch_lth)
ltr.set('lth', patch_lth)
print('Patch lth set on LML+LTR:', patch_lth, flush=True)

# Mesh
mesh = comp.mesh().create('mesh1')
sz = mesh.feature().create('size1','Size')
sz.set('hmax', float(H_air/10)); sz.set('hmaxactive', True)
ftri = mesh.feature().create('ftri1','FreeTri'); ftri.selection().set(p2b)
sw = mesh.feature().create('sw1','Sweep'); sw.selection().set([1,2])
mesh.run(); print('Mesh:', mesh.getNumElem(), flush=True)

# Wavelength sweep via Parametric Sweep (wl parameter)
wls = [1e-6, 2e-6, 3e-6, 4e-6, 5e-6, 6e-6, 7e-6, 8e-6, 9e-6, 10e-6]
study = jm.study().create('std1'); study.create('step1','Wavelength')
step = study.feature('step1'); step.set('punit','m')
step.set('plist', str(5e-6))  # dummy wavelength (actual freq from wl param)
# Add parametric sweep
study.create('sweep1','Parametric')
sweep = study.feature('sweep1')
sweep.set('pname', 'wl')
sweep.set('plist', ' '.join(str(w) for w in wls))
print(f'Parametric sweep {len(wls)} wavelengths...', flush=True)
try:
    t0=time.time(); jm.study('std1').run(); t1=time.time()
    print(f'Solve OK {t1-t0:.2f}s', flush=True)
    R = m.evaluate('ewfd.Rtotal')
    print('Rtotal per wavelength:', flush=True)
    for i, wl in enumerate(wls):
        Ri = float(R[i])
        print(f'  wl={wl*1e6:.1f}µm  R={Ri:.6f}  eps=1-R={1-Ri:.6f}', flush=True)
except Exception as e:
    print('Solve FAIL:', repr(e)[:300], flush=True)
    import traceback; traceback.print_exc()

output_dir = recipe_output_dir()
try: m.java.save(str((output_dir / "MIM_drude.mph").resolve()))
except Exception as e: print('save err:', repr(e)[:150], flush=True)
try: client.disconnect()
except Exception: pass
print('Done.', flush=True)
