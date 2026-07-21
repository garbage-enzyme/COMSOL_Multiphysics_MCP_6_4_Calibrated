# 2D_Solenoid_2Coils — Build Instructions

## Overview
2D axisymmetric solenoid with two differential coils (no steel core), using COMSOL InductionCurrents physics (AC/DC Module) at 1 kHz.

## Base Model
**Must clone `EC_NDT_Model.mph`** as a starting point. `InductionCurrents` physics cannot be created from scratch (`create('mf', 'InductionCurrents')` fails). Clone provides the working physics interface.

## Geometry (mm scale)
- coil+ (right coil): 2×10 mm, centered at (+6, 0) mm
- coil- (left coil): 2×10 mm, centered at (−6, 0) mm
- air: 80×80 mm, centered at (0, 0)
- Geometry unit is `mm` (set raw values directly, NOT multiplied by 1e-3)

```
r1: Rectangle, size=[2, 10], pos=[6, 0], base=center, label='coil+', selresult=True
r2: Rectangle, size=[2, 10], pos=[-6, 0], base=center, label='coil-', selresult=True
r3: Rectangle, size=[80, 80], pos=[0, 0], base=center, label='air', selresult=True
```

`selresult=True` on each rectangle creates named selections (`geom1_r1_dom`, `geom1_r2_dom`, `geom1_r3_dom`) in the component.

## Domain Numbering (after Form Union)
| Region | Domain | Named Selection |
|--------|--------|-----------------|
| coil+ (right) | 3 | geom1_r1_dom |
| coil- (left)  | 2 | geom1_r2_dom |
| air           | 1 | geom1_r3_dom |

## Physics — InductionCurrents (`mf`)
| Feature | Type | Selection | Key Settings |
|---------|------|-----------|--------------|
| fsp1 | FreeSpace | all domains (default) | f_typ = 1 kHz |
| mi1 | MagneticInsulation | exterior boundaries | default |
| init1 | InitialValues | all domains | default |
| als1 | AmperesLaw | geom1_r3_dom (air/dom1) | Langevin, Lfunction=1.5e6[A/m]*((200*mf.normHeff)/((200*mf.normHeff)+1.5e6[A/m])), mur=1, T=293.15K, epsilonr=1 |
| coil1 | Coil | geom1_r1_dom (coil+/dom3) | ICoil=1A, N=300, Multi-turn, sigma=6e7 S/m, wireØ=1mm, fill=0.5, RDC=50Ω |
| coil2 | Coil | geom1_r2_dom (coil-/dom2) | ICoil=−1A, N=300, Multi-turn, sigma=6e7 S/m, wireØ=1mm, fill=0.5, RDC=50Ω |

**Note:** FreeSpace (`fsp1`) cannot be deleted — it is mandatory in InductionCurrents and its selection is read-only. It is overridden by Coil and AmperesLaw on their respective domains.

## Materials
| Name | Selection | Properties |
|------|-----------|------------|
| Air | geom1_r3_dom (dom1) | μr=1, σ=0 S/m |
| Copper | geometry domains [2, 3] (or named selections for r1+r2) | μr=1, σ=6e7 S/m |

**Important:** `epsilonr` must be set on the physics features (Coil, AmperesLaw) with `epsilonr_mat='userdef'` — materials do not natively expose `epsilonr` in their `def` property group.

## Mesh
- Free Triangular (`FreeTri`)

## Study
- Frequency Domain at 1 kHz
- `plist = '1[kHz]'`
- Parameter: `nu = '1[kHz]'`

## Results
- Expected Bmax ≈ 22 mT at the coil centers
- Surface plot of `mf.normB` in tesla

## File Locations
- Base: `C:\Users\nguye\EC_NDT_Model.mph`
- Output: `C:\Users\nguye\2D_Solenoid_2Coils.mph`
- Build script: `C:\Users\nguye\AppData\Local\Temp\opencode\build_named.py`
- COMSOL: `D:\COMSOL62\Multiphysics\bin\win64\comsol.exe`

## API Tips
- Use `feat.startsWith(...)` (Java string method, not Python `startswith`)
- Use `feat.selection().set([n])` for direct domain selection, or `feat.selection().named('geom1_rX_dom')` for named selection
- `model.evaluate('mf.normB', 'T')` returns numpy array of field values across all mesh points
- `model.java` gives access to the raw Java model object
- Always `model.remove()` when done but pass the correct node argument

## Common Pitfalls
1. **Domain numbering ≠ creation order** — verify by checking material/feature selections via `selection().entities(2)`
2. **`InductionCurrents` cannot be created from scratch** — always clone from EC_NDT_Model.mph
3. **File locking** — close COMSOL GUI before saving; use `Stop-Process -Name "comsol"` to force close
4. **`epsilonr` not on material** — set on physics features with `epsilonr_mat='userdef'`
5. **String methods** — Java string methods (`startsWith`) not Python methods (`startswith`)
