"""End-to-end ParallelPlateCapacitor verification with clientapi-correct calls."""
import mph
import jpype

client = mph.Client(version='6.3')
m = client.create('ParallelPlateCap')
jm = m.java

# params
for name, val in (('L','0.01[m]'), ('d','0.001[m]'), ('epsr','2.1'), ('V0','1[V]')):
    jm.param().set(name, val)

# component + geometry + block
comp = jm.component().create('comp1', True)
geom = comp.geom().create('geom1', 3)
blk = geom.feature().create('blk1', 'Block')
blk.set('size', jpype.JArray(jpype.JDouble)([0.01, 0.01, 0.001]))
blk.set('pos',  jpype.JArray(jpype.JDouble)([0, 0, 0]))
geom.run()
print('geom: ndom=', geom.getNDomains(), 'nbnd=', geom.getNBoundaries())

sdim = str(geom.getSDim())
es = comp.physics().create('es', 'Electrostatics', sdim)

# ChargeConservation + material (eps_r=2.1) on domain 1
ccn = es.feature().create('ccn1', 'ChargeConservation', int(sdim))
ccn.selection().set([1])
ccn.set('materialType', 'from_mat')
mat = comp.material().create('mat1', 'Common')
mat.label('dielectric')
mat.propertyGroup('def').set('relpermittivity', '2.1')
mat.selection().set([1])
print('physics+material set')

# BC: Ground on bnd 3 (z=0), ElectricPotential V0 on bnd 4 (z=d)
gnd = es.feature().create('gnd1', 'Ground', 2)
gnd.selection().set([3])
ep = es.feature().create('ep1', 'ElectricPotential', 2)
ep.selection().set([4])
ep.set('V0', 'V0')
print('BC set: gnd3, ep4')

# mesh sequence
mesh_seq = comp.mesh().create('mesh1')
mesh_seq.feature().create('ftr1', 'FreeTet')
mesh_seq.run()
print('mesh built, nelem=', mesh_seq.getNumElem())

# study (clientapi: full name "Stationary")
study = jm.study().create('std1')
study.create('step1', 'Stationary')
print('study created')

# solve (clientapi: study.run() builds solver + runs)
jm.study('std1').run()
print('solved')

# evaluate capacitance C = 2*intWe / V^2
import mph
C = m.evaluate('2*es.intWe/(1[V])^2', 'pF')
print('C [pF] =', C)

# theoretical: eps0*epsr*L^2/d
import math
eps0 = 8.8541878128e-12
Cth = eps0 * 2.1 * (0.01**2) / 0.001 * 1e12
print('C_theory [pF] =', Cth)

client.disconnect()
