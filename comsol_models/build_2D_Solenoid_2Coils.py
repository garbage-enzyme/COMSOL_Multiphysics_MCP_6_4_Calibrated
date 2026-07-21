import sys
if sys.platform == 'win32': sys.stdout.reconfigure(encoding='utf-8', errors='replace')

import mph
import numpy as np

client = mph.Client(cores=4)

model = client.load(r'C:\Users\nguye\New folder (2)\EC_NDT_Model.mph')
model.rename('2D_Solenoid_2Coils')
jm = model.java

# === GEOMETRY (mm) ===
geom = jm.geom().get('geom1')
for feat in list(geom.feature().tags()):
    if feat != 'fin': geom.feature().remove(feat)

r1 = geom.create('r1', 'Rectangle')
r1.set('size', [2.0, 10.0]); r1.set('pos', [6.0, 0.0]); r1.set('base', 'center')
r1.label('coil+'); r1.set('selresult', True)

r2 = geom.create('r2', 'Rectangle')
r2.set('size', [2.0, 10.0]); r2.set('pos', [-6.0, 0.0]); r2.set('base', 'center')
r2.label('coil-'); r2.set('selresult', True)

r3 = geom.create('r3', 'Rectangle')
r3.set('size', [80.0, 80.0]); r3.set('pos', [0.0, 0.0]); r3.set('base', 'center')
r3.label('air'); r3.set('selresult', True)

geom.run()
print('Geometry: 2x10mm coils at +/-6mm, 80x80mm air')

# === PHYSICS ===
phys = jm.physics().get('mf')

# Remove coil features and als1 (recreatable)
for tag in list(phys.feature().tags()):
    if tag.startsWith('coil') or tag == 'als1':
        phys.feature().remove(tag)

def sel(feat, sid): feat.selection().named(sid)

# Free Space - reuse existing, set frequency
fsp1 = phys.feature().get('fsp1')
fsp1.set('f_typ', '1[kHz]')

# Magnetic Insulation - reuse existing (selection is read-only)
mi1 = phys.feature().get('mi1')

# Initial Values - reuse existing
init1 = phys.feature().get('init1')

# Ampère's Law on air domain with Langevin
als1 = phys.create('als1', 'AmperesLaw')
sel(als1, 'geom1_r3_dom')
als1.set('MagnetizationModel', 'LangevinFunction')
als1.set('Lfunction', '1.5e6[A/m]*((200*mf.normHeff)/((200*mf.normHeff)+1.5e6[A/m]))')
als1.set('mur', '1'); als1.set('T', '293.15[K]')
als1.set('epsilonr', '1'); als1.set('epsilonr_mat', 'userdef')
als1.label("Ampère's Law 1")

# Coil+ on domain 3 (right)
coil1 = phys.create('coil1', 'Coil')
sel(coil1, 'geom1_r1_dom')
coil1.set('ICoil', '1[A]'); coil1.set('N', '300')
coil1.set('ConductorModel', 'Multi')
coil1.set('sigmaCoil', '6e7[S/m]')
coil1.set('coilWindDiameter', '1[mm]')
coil1.set('FillingFactor', '0.5')
coil1.set('RCoilDC', '50[ohm]')
coil1.set('epsilonr', '1'); coil1.set('epsilonr_mat', 'userdef')
coil1.label('coil+')

# Coil- on domain 2 (left)
coil2 = phys.create('coil2', 'Coil')
sel(coil2, 'geom1_r2_dom')
coil2.set('ICoil', '-1[A]'); coil2.set('N', '300')
coil2.set('ConductorModel', 'Multi')
coil2.set('sigmaCoil', '6e7[S/m]')
coil2.set('coilWindDiameter', '1[mm]')
coil2.set('FillingFactor', '0.5')
coil2.set('RCoilDC', '50[ohm]')
coil2.set('epsilonr', '1'); coil2.set('epsilonr_mat', 'userdef')
coil2.label('coil-')

jm.param().set('nu', '1[kHz]')
jm.param().set('p0', '1[atm]')

# === MATERIALS ===
for tag in list(jm.material().tags()): jm.material().remove(tag)

mat_air = jm.material().create('mat1', 'Common')
mat_air.label('Air'); sel(mat_air, 'geom1_r3_dom')
mat_air.propertyGroup('def').set('relpermeability', '1')
mat_air.propertyGroup('def').set('electricconductivity', '0[S/m]')

mat_cu = jm.material().create('mat2', 'Common')
mat_cu.label('Copper'); mat_cu.selection().set([2, 3])
mat_cu.propertyGroup('def').set('relpermeability', '1')
mat_cu.propertyGroup('def').set('electricconductivity', '6e7[S/m]')

# === MESH ===
for tag in list(jm.mesh().tags()): jm.mesh().remove(tag)
m = jm.mesh().create('mesh1', 'geom1')
m.feature().create('ftri1', 'FreeTri'); m.run()

# === STUDY ===
study = jm.study().get('std1')
for f in list(study.feature().tags()): study.feature().remove(f)
study.feature().create('step1', 'Frequency').set('plist', '1[kHz]')
study.run()
print('Solved')

# === RESULTS ===
B = np.atleast_1d(np.array(model.evaluate('mf.normB', 'T'), dtype=float))
print(f'Bmax = {float(np.max(B))*1000:.3f} mT')

for tag in list(jm.result().tags()): jm.result().remove(tag)
pg = jm.result().create('pg1', 'PlotGroup2D')
pg.label('Magnetic Flux Density (1 kHz)')
surf = pg.create('surf1', 'Surface')
surf.set('expr', 'mf.normB')
surf.set('unit', 'T')

model.save(r'C:\Users\nguye\2D_Solenoid_2Coils\2D_Solenoid_2Coils.mph')
print('Saved')
