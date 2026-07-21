import sys
if sys.platform == 'win32': sys.stdout.reconfigure(encoding='utf-8', errors='replace')

import mph

client = mph.Client(cores=4)
model = client.load(r'C:\Users\nguye\2D_Solenoid_2Coils\2D_Solenoid_2Coils.mph')

# Find component in MPh tree
comp_node = None
for item in model:
    if item.tag() == 'modelNode':
        for comp in item:
            comp_node = comp
            break
        break

if comp_node:
    print(f'comp_node: {comp_node.tag()}')
    for name in ['ModelInputs', 'model_inputs', 'ModModelInputs']:
        try:
            mi = comp_node.create(name)
            print(f'Created {name}: {mi}')
            if mi:
                mi.set('pressure', '1[atm]')
                print('  Pressure set')
                break
        except Exception as e:
            print(f'{name}: {str(e)[:80]}')
else:
    print('Component not found')

model.save(r'C:\Users\nguye\2D_Solenoid_2Coils\2D_Solenoid_2Coils.mph')
print('Saved')
