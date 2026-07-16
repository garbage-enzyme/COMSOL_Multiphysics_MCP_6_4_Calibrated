"""
MIM with LayeredMaterialLink approach:
- Global Common material mat_au (Drude/eps)
- Global LayeredMaterial lm_au (layer: Au, thickness, link=mat_au)
- Component LayeredMaterialLink lml_au (link=lm_au, boundary=bnd6)
- LayeredTransition on bnd6 (uses LML)
- LayeredImpedance on pport2 (substrate Au)
Test with eps=2.1 first, then Drude.
"""
import mph, jpype, sys, time
from _paths import recipe_output_dir
try:
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    sys.stderr.reconfigure(encoding='utf-8', errors='replace')
except Exception: pass

def jarr(v, d=jpype.JDouble): return jpype.JArray(d)(v)

Px=0.6e-6; Py=0.6e-6; t_al2o3=30e-9; H_air=0.83e-6; t_au=30e-9
wl0 = 5e-6; eps_test = "2.1"  # simple dielectric first

client = mph.Client(cores=4, version='6.4')
print('Connected', client.version, flush=True)
m = client.create('MIM_lml'); jm = m.java

# Global materials: Common mat_au + LayeredMaterial lm_au
mat_au_g = jm.material().create('mat_au','Common')
mat_au_g.propertyGroup('def').set('relpermittivity', eps_test)
mat_au_g.propertyGroup('def').set('sigmabnd', '0')
mat_au_g.propertyGroup('def').set('murbnd', '1')  # boundary permeability
print('mat_au props: relperm, sigmabnd, murbnd set', flush=True)
lm_au = jm.material().create('lm_au','LayeredMaterial')
lm_au.set('layername','Au'); lm_au.set('thickness', str(t_au)); lm_au.set('link','mat_au')
# Set properties directly on LayeredMaterial's def group
try: lm_au.propertyGroup('def').set('relpermittivity', eps_test); print('lm_au relperm set', flush=True)
except Exception as e: print('lm_au relperm err:', repr(e)[:80], flush=True)
try: lm_au.propertyGroup('def').set('sigmabnd', '0'); print('lm_au sigmabnd set', flush=True)
except Exception as e: print('lm_au sigmabnd err:', repr(e)[:80], flush=True)
try: lm_au.propertyGroup('def').set('murbnd', '1'); print('lm_au murbnd set', flush=True)
except Exception as e: print('lm_au murbnd err:', repr(e)[:80], flush=True)
print('lm_au pg tags:', list(lm_au.propertyGroup().tags()), flush=True)
print('lm_au def props:', list(lm_au.propertyGroup('def').properties()), flush=True)
print('Global LM: link=', lm_au.getString('link'), 'thick=', lm_au.getString('thickness'), flush=True)

# Component
comp = jm.component().create('comp1', True)
g = comp.geom().create('geom1', 3)
g.feature().create('b_al2','Block').set('size',jarr([Px,Py,t_al2o3]))
g.feature().create('b_air','Block').set('size',jarr([Px,Py,H_air])); g.feature('b_air').set('pos',jarr([0,0,t_al2o3]))
g.run()
print('dom', g.getNDomains(), 'bnd', g.getNBoundaries(), flush=True)

# Domain materials
mat_al2 = comp.material().create('mat_al2','Common')
mat_al2.propertyGroup('def').set('relpermittivity','3.1'); mat_al2.selection().set([1])
mat_air = comp.material().create('mat_air','Common')
mat_air.propertyGroup('def').set('relpermittivity','1'); mat_air.selection().set([2])

# Component LayeredMaterialLink on bnd6
lml_au = comp.material().create('lml_au','LayeredMaterialLink')
lml_au.set('link','lm_au')
lml_au.selection().all(); lml_au.selection().clear(); lml_au.selection().add([6])
# Set properties on LML too (BC might read from here)
try: lml_au.propertyGroup('def').set('relpermittivity', eps_test)
except Exception: pass
try: lml_au.propertyGroup('def').set('sigmabnd', '0')
except Exception: pass
try: lml_au.propertyGroup('def').set('murbnd', '1')
except Exception: pass
# Also try shell property group
try:
    sh_lml = lml_au.propertyGroup('shell')
    sh_lml.set('lth', str(t_au))
    sh_lml.set('relpermittivity', eps_test)
    sh_lml.set('sigmabnd', '0')
    sh_lml.set('murbnd', '1')
    print('LML shell group props set, lth=', sh_lml.getString('lth'), flush=True)
    print('LML shell props:', list(sh_lml.properties()), flush=True)
except Exception as e: print('LML shell err:', repr(e)[:100], flush=True)
print('LML: link=', lml_au.getString('link'), 'sel=', list(lml_au.selection().entities()), flush=True)
print('LML pg tags:', list(lml_au.propertyGroup().tags()), 'def props:', list(lml_au.propertyGroup('def').properties()), flush=True)

# ewfd + PeriodicStructure
p = comp.physics().create('ewfd','ElectromagneticWavesFrequencyDomain', str(g.getSDim()))
ps = p.feature().create('ps1','PeriodicStructure',3)
p1b = list(ps.feature('pport1').selection().entities()); p2b = list(ps.feature('pport2').selection().entities())
ps.selection('excitedPortSelection').set(p1b)
print('pport1:', p1b, 'pport2:', p2b, flush=True)

# LayeredImpedance on bottom (substrate Au)
lib = p.feature().create('lib1','LayeredImpedanceBoundaryCondition',2)
lib.selection().set(p2b)
lib.set('substrateMaterial','mat_au')
lib.set('DisplacementFieldModelSubstrate','RelativePermittivity')
lib.set('epsilonrImp_mat','userdef'); lib.set('epsilonrImp', eps_test); lib.set('allLayers', False)

# LayeredTransition on interface bnd6
ltr = p.feature().create('ltr1','LayeredTransitionBoundaryCondition',2)
ltr.selection().set([6])
ltr.set('DisplacementFieldModel','RelativePermittivity')
# Set _mat to userdef for boundary props (from_mat can't find them in LayeredMaterial via API)
for prop, val in [('sigmabnd_mat','userdef'),('sigmabnd','0'),('murbnd_mat','userdef'),('murbnd','1')]:
    try: ltr.set(prop, val); print(f'{prop}={val} set', flush=True)
    except Exception as e: print(f'{prop} err:', repr(e)[:80], flush=True)
try: ltr.set('lth', str(t_au)); print('lth set ->', ltr.getString('lth'), flush=True)
except Exception as e: print('lth err:', repr(e)[:80], flush=True)
print('\n--- ltr props ---', flush=True)
for prop in ['DisplacementFieldModel','epsilonr_mat','epsilonr','lth_mat','lth','allLayers','shelllist','bndType']:
    try: print(f'  {prop}={ltr.getString(prop)}', flush=True)
    except Exception: pass

# Mesh
mesh = comp.mesh().create('mesh1')
sz = mesh.feature().create('size1','Size')
sz.set('hmax', float(H_air/10)); sz.set('hmaxactive', True)
ftri = mesh.feature().create('ftri1','FreeTri'); ftri.selection().set(p2b)
sw = mesh.feature().create('sw1','Sweep'); sw.selection().set([1,2])
mesh.run(); print('Mesh:', mesh.getNumElem(), flush=True)
try: print('shelllist after mesh=', ltr.getString('shelllist'), flush=True)
except Exception: pass

# Study
study = jm.study().create('std1'); study.create('step1','Wavelength')
step = study.feature('step1'); step.set('punit','m'); step.set('plist', str(wl0))
print('Solving wl=5um eps=2.1...', flush=True)
try:
    t0=time.time(); jm.study('std1').run(); t1=time.time()
    print(f'Solve OK {t1-t0:.2f}s', flush=True)
    R = float(m.evaluate('ewfd.Rtotal'))
    print(f'Rtotal={R:.6f}', flush=True)
except Exception as e:
    print('Solve FAIL:', repr(e)[:300], flush=True)
    import traceback; traceback.print_exc()

output_dir = recipe_output_dir()
try: m.java.save(str((output_dir / "MIM_lml.mph").resolve()))
except Exception as e: print('save err:', repr(e)[:150], flush=True)
try: client.disconnect()
except Exception: pass
print('Done.', flush=True)
