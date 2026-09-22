# COMSOL 6.4 surrogate-training and DNN capability matrix (S0)

Status: proven on a licensed isolated session; gate satisfied.
Release: `0.7.5` / `alpha7.5`
Evidence root: `D:\mcp_tests\a75s0p06`
Primary receipt: `s0_capability.json`
Receipt SHA-256: `9581c9ef32eba3e012ead3f63c7a17ed19a65e04accc7d5fec735edfbc604678`
Probe source: `development_kit/scripts/surrogate_dnn_capability_probe.py`

## 1. Proven environment identity

| Fact | Value |
| --- | --- |
| COMSOL build | `6.4.0.293` (`COMSOL Multiphysics 6.4 (开发版本: 293)`) |
| Backend root | `D:\COMSOL64\Multiphysics` |
| JVM | `D:\COMSOL64\Multiphysics\java\win64\jre\bin\server\jvm.dll` |
| Java runtime | Eclipse Adoptium 21.0.7 |
| Client mode | standalone, `port = null`, no server |
| Python | 3.14.6 |
| MPh | 1.3.1 |

Cleanup: lease absent, collision absent, zero active jobs, zero owned
descendants, zero owned listeners. `cleanup.passed = true`.

## 2. Confirmed creation hierarchy

```text
model.study().create("std1")                                  -> study
study.feature().create("smt1", "SurrogateModelTraining")      -> study step
model.func().create("dnn1", "DNN")                            -> DNN function
```

- `study.feature().create(tag, type)` is the working form. The alternative
  `study.create(tag, type)` form in the ClientAPI reference text was attempted
  first and is **not** the confirmed path.
- Java classes: study step `com.comsol.clientapi.impl.StudyFeatureClient`,
  DNN function `com.comsol.clientapi.impl.FunctionFeatureClient`.
- `getType()` returns `SurrogateModelTraining` and `DNN` respectively.
- Save/reload preserves both: after `client.clear()` + `client.load()`,
  `study().tags() == ['std1']`, `func().tags() == ['dnn1']`, and both
  `getType()` values are unchanged.

## 3. Property availability

| Object | Documented properties readable | Property enumeration |
| --- | --- | --- |
| `SurrogateModelTraining` step | 69 / 69 | 172 names via `properties()` |
| `DNN` function | 30 / 30 | 75 names via `properties()` |
| DNN properties not in the reference table | 22 / 23 | `exportfilename` unreadable |

`getAllowedPropertyValues(name)` returns `null` (not an error) for properties
that are not enumerated choices: `innermostparameter`,
`qoiconfigurestudyinput`, `qoisolutionindv`, `qoisolutionouterindv` (step) and
`colscale` (DNN). These are string maps or free scalars, not closed enums.

## 4. Training-lifecycle methods — decisive architectural finding

The **study step owns only data generation and study execution**; the
**training lifecycle belongs to the DNN function**:

| Method | DNN function | Study step |
| --- | --- | --- |
| `run()` | `void run()` | absent |
| `continueRun()` | `void continueRun()` | absent |
| `runTest()` | `void runTest()` | absent |
| `export(String)` | `void export(java.lang.String)` | absent |
| `exportData(String)` | present | present |
| `importData()`, `importData(String)` | present | present |
| `discardData()` | present | present |
| `saveFile(String)` | absent | `boolean saveFile(String)` |
| `loadFile(String)` | absent | `boolean loadFile(String)` |

Consequence: the S4 adapter must attach train/test/continue/export to the DNN
function object, not to the study step. No `trainModel`, `testModel`, or
`onnx*` method exists; ONNX export is the `export(path)` call on the DNN
function. `continueRun()` exists and takes no arguments.

`set` is heavily overloaded (12 signatures: `String`, `double`, `int`,
`boolean`, and their array/matrix forms). `setIndex` and `setEntry` likewise.
A bare method name is not a binding; the adapter must select the typed
overload explicitly.

## 5. Determinism and split control — reproducibility resolved

| Control | Object | Allowed values | Verified |
| --- | --- | --- | --- |
| `useseed` | DNN | `manual`, `currenttime` | set + read back `manual` |
| `rndseed` | DNN | free scalar | set + read back `17` |
| `useseedvalidation` | DNN | `manual`, `currenttime` | set + read back `manual` |
| `rndseedvalidation` | DNN | free scalar | set + read back `17` |
| `validation` | DNN | `random`, `fraction`, `last`, **`table`** | enumerated |
| `test` | DNN | `none`, `random`, **`table`** | enumerated |
| `useseed` | step | `automatic`, `manual`, `currenttime` | enumerated |
| `surrogatemodel` | step | `none`, `gp`, `pce`, `dnn`, `lsq` | enumerated |
| `computeaction` | step | `recompute`, `append` | enumerated |

All four seed controls round-trip through save/reload with their values intact
(`useseed = manual`, `rndseed = 17`, `useseedvalidation = manual`,
`rndseedvalidation = 17`).

**This resolves the plan's open split question.** Because `validation` and
`test` both accept `table`, the 0.7.5 contract can supply externally
prepartitioned train/validation/test tables and does **not** have to rely on
COMSOL's internal random subsetting. Group-disjoint membership is therefore
exactly controllable and provable from our own split manifest, and the
`scientific_holdout` stays entirely outside COMSOL's training data. The
narrowed claim in the plan ("if membership cannot be retrieved, do not claim
exact split reproducibility") is not needed for the table-based path.

## 6. Architecture and optimizer controls

| Property | Object | Allowed values | Default |
| --- | --- | --- | --- |
| `layertype` | DNN / step | `input`, `dense` | `{dense}` / `{"input","dense"}` |
| `activation` | DNN / step | `none`, `relu`, `elu`, `sigmoid`, `tanh`, `softplus`, `leakyrelu`, `gelu` | `{"none","tanh"}` |
| `outfeatures` | DNN / step | positive int array | `{1}` / `{0,0}` |
| `optmethod` | DNN | `adam`, `sgd` | `adam` |
| `loss` | DNN | `mse`, `mae` | `mse` |
| `batchsize` | DNN | positive int | 512 |
| `lr` | DNN | positive scalar | `1e-3` |
| `epochs` | DNN | integer | 1000 |
| `momentum` | DNN | nonnegative scalar | 0 |
| `gputraining` | DNN | boolean | false |
| `weightdecay` | DNN | discovered | not in reference table |
| `source` | DNN | `file`, `resultTable` | `file` |
| `sourcetype` | DNN | `model`, `user` | discovered |

Only `dense` hidden layers exist — no convolutional, recurrent, or custom
layers. This matches the plan's bounded fully-connected requirement and rules
out architecture search by construction.

## 7. Discovered properties absent from the ClientAPI reference

Readable and relevant to a truthful model card:
`trained_chksum`, `trained_info`, `trained_ninput`, `trained_noutput`,
`layerconfig`, `modelres`, `information`, `testloss`, `trainingloss`,
`validationloss`, `testtable`, `validationtable`, `testpredictiontable`,
`testfraction`, `weightdecay`, `isexporting`, `isuploaded`, `funcs`, `unit`,
`dseparator`, `columnType`, `columnKeys`, `setupfromstudy`.

`trained_chksum` is a COMSOL-owned trained-weight checksum and is the natural
identity for the continuation-eligibility rule (S6). `exportfilename` was the
single unreadable property.

## 8. Explicitly unsupported / not present

- No `trainModel`, `testModel`, or any method containing `onnx` exists; ONNX
  export is `export(String)` on the DNN function.
- No custom, convolutional, or recurrent layer type.
- No property-based epistemic uncertainty output was observed.
- `exportfilename` is not readable.
- GPU training is a boolean switch (`gputraining`); no device enumeration or
  memory readback was observed. S9 stays deferred.

## 9. Gate disposition

S0 gate satisfied: capability matrix reviewed against bounded receipts, exact
property/method/default/allowed-value mappings resolved, and no generic
mutation was used to approximate an unavailable API. Two limitations are
frozen into the public contract:

1. `exportfilename` is not readable, so export evidence must hash the produced
   file rather than trust a stored filename property.
2. Enumeration is by reflection over the Java object; the reference tables and
   the live surface disagree (75 vs 30 documented DNN properties), so the live
   readback is authoritative.

Licensed receipt: `D:\mcp_tests\a75s0p06\s0_capability.json`.
