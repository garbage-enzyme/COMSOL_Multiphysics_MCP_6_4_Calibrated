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
wl0 = 5e-6; eps_test = "2.1"  # simple dielectric first

client = mph.Client(cores=parse_recipe_cores(), version='6.4')
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
require_required_properties(lm_au.propertyGroup('def'), {
    'relpermittivity': eps_test,
    'sigmabnd': '0',
    'murbnd': '1',
})
print('lm_au pg tags:', list(lm_au.propertyGroup().tags()), flush=True)
print('lm_au def props:', list(lm_au.propertyGroup('def').properties()), flush=True)
print('Global LM: link=', lm_au.getString('link'), 'thick=', lm_au.getString('thickness'), flush=True)

# Component
comp = jm.component().create('comp1', True)
g = comp.geom().create('geom1', 3)
b_al2 = g.feature().create('b_al2','Block'); b_al2.set('size',jarr([Px,Py,t_al2o3])); b_al2.set('selresult', True)
b_air = g.feature().create('b_air','Block'); b_air.set('size',jarr([Px,Py,H_air])); b_air.set('pos',jarr([0,0,t_al2o3])); b_air.set('selresult', True)
g.run()
al2_domains = require_named_domains(comp, 'geom1_b_al2_dom')
air_domains = require_named_domains(comp, 'geom1_b_air_dom')
interface = require_interface_boundaries(g, al2_domains, air_domains)
print('dom', g.getNDomains(), 'bnd', g.getNBoundaries(), flush=True)

# Domain materials
mat_al2 = comp.material().create('mat_al2','Common')
mat_al2.propertyGroup('def').set('relpermittivity','3.1'); mat_al2.selection().set(al2_domains)
mat_air = comp.material().create('mat_air','Common')
mat_air.propertyGroup('def').set('relpermittivity','1'); mat_air.selection().set(air_domains)

# Component LayeredMaterialLink on bnd6
lml_au = comp.material().create('lml_au','LayeredMaterialLink')
lml_au.set('link','lm_au')
lml_au.selection().all(); lml_au.selection().clear(); lml_au.selection().add(interface)
# Set properties on LML too (BC might read from here)
require_required_properties(lml_au.propertyGroup('def'), {
    'relpermittivity': eps_test,
    'sigmabnd': '0',
    'murbnd': '1',
})
# Also try shell property group
sh_lml = lml_au.propertyGroup('shell')
require_required_properties(sh_lml, {
    'lth': str(t_au),
    'relpermittivity': eps_test,
    'sigmabnd': '0',
    'murbnd': '1',
})
print('LML: link=', lml_au.getString('link'), 'sel=', list(lml_au.selection().entities()), flush=True)
print('LML pg tags:', list(lml_au.propertyGroup().tags()), 'def props:', list(lml_au.propertyGroup('def').properties()), flush=True)

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
print('pport1:', p1b, 'pport2:', p2b, flush=True)

# LayeredImpedance on bottom (substrate Au)
lib = p.feature().create('lib1','LayeredImpedanceBoundaryCondition',2)
lib.selection().set(p2b)
require_required_properties(lib, {
    'substrateMaterial': 'mat_au',
    'DisplacementFieldModelSubstrate': 'RelativePermittivity',
    'epsilonrImp_mat': 'userdef',
    'epsilonrImp': eps_test,
    'allLayers': False,
})

# LayeredTransition on interface bnd6
ltr = p.feature().create('ltr1','LayeredTransitionBoundaryCondition',2)
ltr.selection().set(interface)
# Set _mat to userdef for boundary props (from_mat can't find them in LayeredMaterial via API)
require_required_properties(ltr, {
    'DisplacementFieldModel': 'RelativePermittivity',
    'epsilonr_mat': 'userdef',
    'epsilonr': eps_test,
    'sigmabnd_mat': 'userdef',
    'sigmabnd': '0',
    'murbnd_mat': 'userdef',
    'murbnd': '1',
    'lth': str(t_au),
})
print('\n--- ltr props ---', flush=True)
for prop in ['DisplacementFieldModel','epsilonr_mat','epsilonr','lth_mat','lth','allLayers','shelllist','bndType']:
    try: print(f'  {prop}={ltr.getString(prop)}', flush=True)
    except Exception: pass

# Mesh
mesh = comp.mesh().create('mesh1')
sz = mesh.feature().create('size1','Size')
sz.set('hmax', float(H_air/10)); sz.set('hmaxactive', True)
ftri = mesh.feature().create('ftri1','FreeTri'); ftri.selection().set(p2b)
sw = mesh.feature().create('sw1','Sweep'); sw.selection().set(al2_domains + air_domains)
mesh.run(); print('Mesh:', mesh.getNumElem(), flush=True)
try: print('shelllist after mesh=', ltr.getString('shelllist'), flush=True)
except Exception: pass

# Study
study = jm.study().create('std1'); study.create('step1','Wavelength')
step = study.feature('step1'); bind_wavelength_step(step, 'wl')
jm.param().set('wl', f'{wl0}[m]')
print('Solving wl=5um eps=2.1...', flush=True)
t0=time.time(); jm.study('std1').run(); t1=time.time()
R = require_passive_reflection(
    require_spectrum(m.evaluate('ewfd.Rtotal'), [wl0], 'Rtotal'), 'Rtotal'
)[0]
print(f'Solve OK {t1-t0:.2f}s Rtotal={R:.6f}', flush=True)

output_dir = recipe_output_dir()
save_required(m.java, output_dir / "MIM_lml.mph")
try: client.disconnect()
except Exception: pass
print('Done.', flush=True)
