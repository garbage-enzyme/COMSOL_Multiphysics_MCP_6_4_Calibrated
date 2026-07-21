import sys
if sys.platform == 'win32': sys.stdout.reconfigure(encoding='utf-8', errors='replace')

import mph

client = mph.Client(cores=4)
model = client.load(r'C:\Users\nguye\2D_Solenoid_2Coils\2D_Solenoid_2Coils.mph')
jm = model.java

phys = jm.physics().get('mf')

# Try different ways to create model inputs on physics features
for feat_tag in ['fsp1']:
    feat = phys.feature().get(feat_tag)
    print(f'{feat_tag}:')
    for name in ['ModelInput', 'modelinput', 'modModelInput', 'ModModelInput', 'Pressure', 'pressure']:
        try:
            child = feat.create(name, name)
            print(f'  create({name}, {name}): {child.tag()}')
        except Exception as e:
            print(f'  create({name}, {name}): {str(e)[:60]}')

# Try on physics level
for name in ['ModelInput', 'modelinput', 'ModModelInput', 'PressureInput']:
    try:
        child = phys.create(name, name)
        print(f'phys.create({name}, {name}): {child.tag()}')
    except Exception as e:
        print(f'phys.create({name}, {name}): {str(e)[:60]}')

model.remove()
