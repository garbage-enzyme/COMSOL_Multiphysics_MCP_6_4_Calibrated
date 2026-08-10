"""
MIM with Drude dispersion + wavelength sweep 1-10 µm.
Continuous Au film (no patch) first — verify Rtotal<1 at some wavelengths.
Then patch (spatial-varying lth) for resonance.
"""
import mph, jpype, sys, time
from _paths import parse_recipe_cores, recipe_output_dir
from _mim_safety import (
    bind_wavelength_step,
    require_interface_boundaries,
    require_named_domains,
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

Px=0.6e-6; Py=0.6e-6; t_al2o3=30e-9; H_air=0.83e-6; t_au=30e-9
# Parametric Drude using wl parameter (avoids ewfd.freq singularity in sweep)
au_drude_param = "1-(1.37e16)^2/((2*pi*c_const/wl)*((2*pi*c_const/wl)+i*4.1e13))"

client = mph.Client(cores=parse_recipe_cores(), version='6.4')
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
b_al2 = g.feature().create('b_al2','Block'); b_al2.set('size',jarr([Px,Py,t_al2o3])); b_al2.set('selresult', True)
b_air = g.feature().create('b_air','Block'); b_air.set('size',jarr([Px,Py,H_air])); b_air.set('pos',jarr([0,0,t_al2o3])); b_air.set('selresult', True)
g.run()
al2_domains = require_named_domains(comp, 'geom1_b_al2_dom')
air_domains = require_named_domains(comp, 'geom1_b_air_dom')
interface = require_interface_boundaries(g, al2_domains, air_domains)

mat_al2 = comp.material().create('mat_al2','Common')
mat_al2.propertyGroup('def').set('relpermittivity','3.1'); mat_al2.selection().set(al2_domains)
mat_air = comp.material().create('mat_air','Common')
mat_air.propertyGroup('def').set('relpermittivity','1'); mat_air.selection().set(air_domains)

# LayeredMaterialLink on bnd6
lml_au = comp.material().create('lml_au','LayeredMaterialLink')
lml_au.set('link','lm_au')
lml_au.selection().all(); lml_au.selection().clear(); lml_au.selection().add(interface)
sh = lml_au.propertyGroup('shell')
sh.set('lth', str(t_au)); sh.set('relpermittivity', au_drude_param)
sh.set('sigmabnd', '0'); sh.set('murbnd', '1')

# ewfd + PeriodicStructure
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

# LayeredImpedance on bottom (substrate Au, Drude)
lib = p.feature().create('lib1','LayeredImpedanceBoundaryCondition',2)
lib.selection().set(p2b)
require_required_properties(lib, {
    'substrateMaterial': 'mat_au',
    'DisplacementFieldModelSubstrate': 'DrudeLorentzDispersionModel',
    'epsilonrImp_mat': 'userdef',
    'epsilonrImp': au_drude_param,
    'allLayers': False,
})

# LayeredTransition on bnd6
ltr = p.feature().create('ltr1','LayeredTransitionBoundaryCondition',2)
ltr.selection().set(interface)
require_required_properties(ltr, {
    'DisplacementFieldModel': 'RelativePermittivity',
    'sigmabnd_mat': 'userdef',
    'sigmabnd': '0',
    'murbnd_mat': 'userdef',
    'murbnd': '1',
    'lth': str(t_au),
})
ax = 0.3e-6; px0 = (Px - ax)/2  # 0.15µm
patch_lth = f"if(x>{px0} && x<{px0+ax} && y>{px0} && y<{px0+ax}, {t_au}, 1e-15)"
# Keep the continuous-film baseline until it has been independently solved.

# Mesh
mesh = comp.mesh().create('mesh1')
sz = mesh.feature().create('size1','Size')
sz.set('hmax', float(H_air/10)); sz.set('hmaxactive', True)
ftri = mesh.feature().create('ftri1','FreeTri'); ftri.selection().set(p2b)
sw = mesh.feature().create('sw1','Sweep'); sw.selection().set(al2_domains + air_domains)
mesh.run(); print('Mesh:', mesh.getNumElem(), flush=True)

# Wavelength sweep via Parametric Sweep (wl parameter)
wls = [1e-6, 2e-6, 3e-6, 4e-6, 5e-6, 6e-6, 7e-6, 8e-6, 9e-6, 10e-6]
study = jm.study().create('std1'); study.create('step1','Wavelength')
step = study.feature('step1'); bind_wavelength_step(step, 'wl')
# Add parametric sweep
study.create('sweep1','Parametric')
sweep = study.feature('sweep1')
sweep.set('pname', 'wl')
sweep.set('plist', ' '.join(str(w) for w in wls))
def solve_required(label):
    t0=time.time(); jm.study('std1').run(); elapsed=time.time()-t0
    reflection = require_passive_reflection(
        require_spectrum(m.evaluate('ewfd.Rtotal'), wls, f'{label} Rtotal'),
        f'{label} Rtotal',
    )
    print(f'{label} solve OK {elapsed:.2f}s', flush=True)
    return reflection

baseline_reflection = solve_required('continuous baseline')
sh.set('lth', patch_lth); ltr.set('lth', patch_lth)
patch_reflection = solve_required('patch')
for label, reflection in [('baseline', baseline_reflection), ('patch', patch_reflection)]:
    for wl, value in zip(wls, reflection):
        print(f'  {label} wl={wl*1e6:.1f}um R={value:.6f}', flush=True)

output_dir = recipe_output_dir()
save_required(m.java, output_dir / "MIM_drude.mph")
try: client.disconnect()
except Exception: pass
print('Done.', flush=True)
