"""Probe correct clientapi calls for study step + mesh sequence creation."""
import mph
import jpype

client = mph.Client(version='6.3')
m = client.create('Probe')
jm = m.java

# component + geometry + block
comp = jm.component().create('comp1', True)
geom = comp.geom().create('geom1', 3)
blk = geom.feature().create('blk1', 'Block')
blk.set('size', jpype.JArray(jpype.JDouble)([0.01, 0.01, 0.001]))
blk.set('pos',  jpype.JArray(jpype.JDouble)([0, 0, 0]))
geom.run()
print('geometry built; ndom=', geom.getNDomains(), 'nbnd=', geom.getNBoundaries())

# physics: electrostatics (bare, vacuum) just to have a physics for study
sdim = str(comp.geom('geom1').getSDim())
print('sdim=', sdim, type(sdim))
es = comp.physics().create('es', 'Electrostatics', sdim)
print('physics es created')

# try mesh sequence creation
print('--- mesh ---')
try:
    mesh = comp.mesh().create('mesh1')
    print('mesh seq created:', mesh)
    feat = mesh.feature().create('ftr1', 'FreeTet') if hasattr(mesh, 'feature') else None
    print('feat:', feat)
    # try build
    mesh.run()
    print('mesh run OK')
except Exception as e:
    print('mesh err:', repr(e))

# try study step creation with different type strings
print('--- study ---')
study = jm.study().create('std1')
print('study node:', study, 'class:', study.getClass().getName())
for typ in ['stat', 'Stationary', 'static', 'solstat', 'StationaryStep']:
    try:
        study.create('step1', typ)
        print('OK with type=', typ)
        break
    except Exception as e:
        print('fail type=', typ, '->', repr(e)[:200])

# inspect methods of study node
print('--- study node methods (create*) ---')
import jpype.types as jt
cls = study.getClass()
for meth in cls.getMethods():
    if 'create' in meth.getName().lower() or 'setStudy' in meth.getName():
        print(meth.toString())

client.disconnect()
