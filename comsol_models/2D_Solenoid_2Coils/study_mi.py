import sys
if sys.platform == 'win32': sys.stdout.reconfigure(encoding='utf-8', errors='replace')

import mph

client = mph.Client(cores=4)
model = client.load(r'C:\Users\nguye\2D_Solenoid_2Coils\2D_Solenoid_2Coils.mph')
jm = model.java

# Try setting model inputs on the study step
study = jm.study().get('std1')
step1 = study.feature().get('step1')

# Check what properties are available on the study step
for prefix in ['', 'modify', 'model', 'Model']:
    try:
        keys = step1.getEntryKeys(prefix)
        print(f'prefix "{prefix}": {list(keys)[:20]}')
    except Exception as e:
        pass

# Try setting pressure on study step
for prop in ['pressure', 'Pressure', 'P', 'p0']:
    try:
        step1.set(prop, '1[atm]')
        print(f'step1.{prop} = 1[atm] ✓')
    except:
        pass

# Try to set modifyModelInputs type
for iname in ['modModelInputs', 'ModModelInputs', 'modifyModelInputs']:
    try:
        inp = step1.create(iname, iname)
        print(f'step1.create({iname}) = {inap}')
        inp.set('pressure', '1[atm]')
        print(f'  pressure set ✓')
        break
    except Exception as e:
        print(f'step1.create({iname}): {str(e)[:60]}')

model.save(r'C:\Users\nguye\2D_Solenoid_2Coils\2D_Solenoid_2Coils.mph')
print('Saved')
