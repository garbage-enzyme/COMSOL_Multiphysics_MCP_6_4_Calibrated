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

## 10. S4 adapter findings — resolved against live COMSOL

The typed adapter (`comsol_mcp/surrogate/dnn_adapter.py`) and its licensed
bridge (`comsol_mcp/surrogate/dnn_clientapi_backend.py`) were applied to a real
COMSOL 6.4.0.293 session. Licensed receipt:
`D:\mcp_tests\a75s4g13\s4_gate.json`
(sha256 `985a61abc59f3cc4523077b51ef6aeb9d79905ecd52ba4e46513a3312278cc34`).

Findings that the reference tables did **not** state and that only the live run
could reveal:

| Finding | Consequence |
| --- | --- |
| JPype cannot resolve `set(String,int)` against `set(String,boolean)` for a Python `int` and raises an ambiguous-overload error. | Numeric and boolean scalars must be wrapped in explicit Java types (`JInt`, `JDouble`, `JBoolean`, `JString`). |
| `model.java.func(tag)` returns the feature, while `model.java.func()` returns a non-callable list container. | Tag lookup must use the tag overload directly; the zero-argument form is only for `tags()`. |
| `args` is an alternating key/value property written through `setEntry`, and COMSOL refuses it until data columns exist. | The `args` write is **deferred** with an explicit `data_source_not_bound` reason instead of being approximated, then resolved after import. |
| `globaldnnfunction` rejects the nested-array form and accepts the flat alternating form. | Keyed string maps use a separate writer selected from the property's declared value type. |
| COMSOL derives its own column keys (`col1, col2, col3`) and classifies them (`arg, arg, value`); the file header is not reused as the key. | Argument binding is positional when names do not match, and observed keys, declared columns, and binding mode are all recorded as evidence. |
| A repeated `importData()` raises "Unsupported function operation" although the columns are present. | Import failure is tolerated when COMSOL still reports column keys. |
| `getString` on a JPype Java string yields a character-iterable object. | Readback rendering is chosen from the accessor name, not by duck-typing. |

Verified on the licensed run: all 17 readback properties landed exactly as
written (`activation=tanh`, `layertype=input, dense, dense`,
`outfeatures=2, 4, 1`, `lr=0.001`, `batchsize=8`, `epochs=3`,
`useseed=manual`, `rndseed=17`, `validation=table`, `test=table`,
`gputraining=off`); save/reload preserved both tags and both node types; an
injected mid-batch failure rolled back every created node leaving zero tags; a
duplicate-tag apply was refused with zero nodes created; and the owned session,
lease, descendants, and listeners were all released.

## 11. S6 training lifecycle findings — resolved against live COMSOL

Training, held-out test evaluation, and continuation were run on a real
COMSOL 6.4.0.293 session. Licensed receipts:
`D:\mcp_tests\a75s6d01\diag.json` (data-format diagnostic) and
`D:\mcp_tests\a75s6g09\s6_gate.json`
(sha256 `4c2919bb0ff508467e2fe5f28fbead9805b6eb6237bff9ba29c5ddc6f36af2e9`).

Four findings blocked training and were resolved by experiment rather than
inference. Each one failed with a message that did **not** name the real cause:

| Finding | Consequence |
| --- | --- |
| `activation` is a per-layer array whose length must equal `layertype` (the COMSOL default is `none, tanh` for an input+dense pair). A single-element array fails at training time with "数组长度错误". | The write plan emits one activation per layer: `none` for the input layer and the configured activation for every dense layer. |
| `table` validation/test modes require a bound COMSOL result table and fail with "未选择结果表". | The gate uses COMSOL-internal subsetting (`fraction`/`random`) for training-time model selection. The authoritative group-disjoint split remains this project's own manifest, because COMSOL's internal subset membership is not retrievable and must never be reported as the group-disjoint split. |
| A leading text header row imports cleanly but makes training fail with "读取训练数据时出错 - 线条数: 1". A headerless file trains successfully. | Dataset files are written without a header and bound positionally against COMSOL's own `col1, col2, col3` keys, with the binding mode recorded as evidence. |
| `sourcetype` is **not** a DNN property: it is absent from the S0 enumeration and from every documented reference table. | The probe that wrote it was unproven mutation and was removed. This is the reason the S0 gate requires the live enumerated surface, not the reference tables, as authority. |

Verified on the licensed training run:

- `train_seed17`: training succeeded and the held-out test executed
  independently, reporting `trainingloss=0.0262`, `validationloss=0.0275`,
  `testloss=0.0206`, `layerconfig=[2,16,8,1]`, `trained_ninput=2`,
  `trained_noutput=1`, and `trained_chksum=-8938453606443985335`.
- Seed control: re-running seed 17 on a fresh model reproduced the identical
  `trained_chksum`, and seed 29 produced a different one, so the seed provably
  controls the trained artifact.
- Continuation: identical contract identities permitted `continueRun`, which
  produced a new `trained_chksum`; a changed `comsol_build` was refused as a
  `nonidentical_continuation` that creates a new lineage.
- Baseline: a non-DNN `constant_mean` baseline was fitted on the training split
  only (`fit_row_count=33`) and scored on the same held-out rows, giving
  `rmse=0.7102`; the DNN's `testloss=0.0206` beats it by 97.1% on identical
  splits. The comparison records `accepted_on_training_loss=false`.
- Cleanup: session, lease, descendants, and listeners were all released with
  zero active durable jobs.

One limitation is frozen: a trained checksum is a COMSOL-owned integer identity,
so it binds the artifact but is not a content hash of the weights.

A second attempt at stronger evidence was abandoned: loading COMSOL's own
shipped `tubular_reactor_surrogate.mph` example to read a known-good
configuration fails with "初始化物理场接口失败" because the model requires
physics interfaces that are not licensed on this host. The accepted contract is
therefore derived from the bounded experiment above, not from the example.

## 12. S7 export findings — resolved against live COMSOL

Export, artifact hashing, consistency, and registry integration were run on a
real COMSOL 6.4.0.293 session. Licensed receipt:
`D:\mcp_tests\a75s7g02\s7_gate.json`
(sha256 `6bb58f1ac657e546d0796260992e7d63af5824d067b1959738299c9408a59613`).

One finding corrected an assumption that had been carried since S0:

| Finding | Consequence |
| --- | --- |
| COMSOL's DNN `export(path)` writes **ONNX protobuf regardless of the filename extension**. Exporting the same network to `model_a.onnx`, `model_b.onnx`, and `probe.txt` produced three byte-identical ONNX payloads (2105 bytes, sha256 `a417ee48…`). | There is no separate internal text export format. The invented `mph_internal` format was removed, ONNX became the single required format, and an unavailable ONNX export now **blocks** registration instead of being an acceptable omission. |

Verified on the licensed export run:

- The exported payload is genuine ONNX: producer `COMSOL 6.4.0.293`, operators
  `Gemm`, `Tanh`, `Mul`, `Add`, and all six weight/bias initializers
  (`node_Gemm.weight`, `node_Gemm.bias`, `node_Gemm_1.weight`,
  `node_Gemm_1.bias`, `node_Gemm_2.weight`, `node_Gemm_2.bias`) for the trained
  `2 → 16 → 8 → 1` network.
- Export is deterministic: two exports of the same trained model hashed
  identically, so a content hash is a stable artifact identity.
- The export manifest binds the trained checksum
  (`-8938453606443985335`), the architecture hash, and the artifact content hash
  (`a417ee48…`, 2105 bytes). Because `exportfilename` is unreadable, the file's
  content hash is the only trustworthy export evidence, and the manifest records
  `trusted_property_filename=false`.
- Consistency was evaluated against 64 real float32 values decoded from the
  exported ONNX payload: agreement scored `consistent` and registered, while a
  perturbed copy scored `inconsistent` with `violating_rows=[0]` and was refused
  registration with escalation required.
- An out-of-domain state registered the consistent artifact but still demanded
  fresh FEM escalation, so a prediction never becomes FEM evidence.
- Session, lease, descendants, and listeners were all released with zero active
  durable jobs.

Frozen limitation: consistency is checked against the exported payload's own
weight bytes, which proves the artifact is stable and non-degenerate, but does
not independently prove that a third-party ONNX runtime reproduces the same
predictions. That would require an ONNX runtime, which this project does not
depend on.

## 13. S8 bounded campaign findings — resolved against live COMSOL

The full prediction-to-FEM escalation path was run on a real COMSOL 6.4.0.293
session. Licensed receipt: `D:\mcp_tests\a75s8g03\s8_gate.json`
(sha256 `5f77791eae32274163caeae356a51aac715e8f628dc893d74f3b6c9902cfdd14`).

Three findings determined the accepted design:

| Finding | Consequence |
| --- | --- |
| A trained COMSOL DNN function **cannot be evaluated** in a geometry-free surrogate model: `model.evaluate` fails with "Could not determine default dataset", a Point dataset cannot be created ("在这个情景中不能创建本操作"), and an `Eval` numerical node silently returns an empty array. | Screening predictions are produced by evaluating the network's own **exported ONNX artifact** with a pure-standard-library evaluator (`onnx_runtime.py`). This also proves the export is a working network rather than inert bytes. |
| COMSOL exports PyTorch-convention ONNX: one batched input tensor named `input`, Gemm nodes carrying `transB=1` (weights stored `[out, in]`), and explicit `Mul`/`Add` input-normalization nodes. | The evaluator decodes node attributes, honours `transB`, binds the caller's ordered feature names to the single batched input, and applies the scale/bias nodes. A `transB` misreading or a mis-bound input would produce predictions unrelated to the target, so the real-artifact regression test would fail. |
| A COMSOL result dataset **retains the parameters it was solved at**. Evaluating a stored dataset after changing parameters returns the *first* solve's value for every candidate, which looked like a successful escalation while silently reporting one number three times. | Every escalated candidate triggers its own `study.run()` before evaluation, and the gate independently verifies that each measurement reproduces the analytic target at *its own* coordinates and that all measurements are distinct. Without that check the false pass would have been reported as verified evidence. |

Verified on the licensed campaign run:

- Screening produced seven `predicted`-state records and no record claimed FEM
  evidence; all seven were scored by the exported ONNX network.
- Ranking selected a bounded top-3 and escalated the out-of-domain candidate
  (a1=9, a2=9) regardless of rank, with reason `out_of_domain`; ranking and
  selection are explicitly marked as not evidence.
- Each escalated candidate received a fresh COMSOL solve. The measured values
  were distinct and each matched its own coordinates exactly:
  `(3.5, 4.5) → 1.648944`, `(9.0, 9.0) → 7.724506`, `(3.0, 4.0) → 1.107758`.
- The invariant check proved no prediction was promoted without verified FEM
  evidence and an artifact hash; `verified_requires_fresh_fem` is true and
  `upgrades_fem_evidence` is false in both the invariant and the summary.
- The surrogate's error was measured against those fresh FEM results and
  reported honestly: mean absolute error 2.59 and worst 6.86, with all three
  verified results flagged as disagreements. The two out-of-domain points
  dominate that error, which is precisely the behaviour the escalation rule
  exists to catch. No accuracy claim is made from the screening stage.
- Session, lease, descendants, and listeners were all released with zero active
  durable jobs.

Frozen limitation: the verification model is a genuine solvable 1D
coefficient-form PDE model, and the evaluated expression is the analytic target
the surrogate was trained on. This proves the escalation, evidence-separation,
and error-measurement machinery on real COMSOL; it is not a physical validation
of any metasurface or device model.

## 14. S10 public surface findings — resolved without a solver

The S10 surface exposes five bounded solver-free tools:
`surrogate_dataset_validate`, `surrogate_training_preview`,
`surrogate_model_inspect`, `surrogate_model_verify`, and
`surrogate_prediction_validate`. Training and evaluation remain inside durable
jobs, and no tool here starts COMSOL, acquires a solver lease, or imports MPh,
JPype, ONNX, or a numerical stack.

| Finding | Consequence for this repository |
| --- | --- |
| Contained-path enforcement is driven by **per-tool flat argument-name tables** in `path_policy.py`, not by type annotations or by walking a request object. | A path nested inside a request object would silently bypass containment. Every surrogate path is therefore a top-level string argument, and all five tools are listed in both `_ARTIFACT_READ_ARGUMENTS` and `_ALWAYS_ENFORCED_TOOLS` so they cannot inherit the full profile's legacy broad-path behaviour. |
| A surrogate model card carries its identities **nested** under `identities` and seals itself with `entry_sha256`; a registry entry maps artifact names to hashes; an export manifest lists artifacts and seals with `manifest_sha256`. | The evidence layer resolves identities from either layout and collects artifact hashes from both shapes. An expectation the document cannot satisfy is reported `unavailable`, never as satisfied, so verification cannot pass vacuously. |
| `evaluate_onnx_model` bound each per-feature graph input to a **bare string** (`dict(zip(graph_inputs, names))`), so the consumer iterated the string's characters and failed with `KeyError: 'a'`. | A real latent defect in already-committed S7 code. The COMSOL single-batched layout masked it entirely. Fixed to bind a one-element list per tensor, with a regression test whose tensor names deliberately differ from the feature names so the branch is reachable. |
| A decoded graph with **no nodes** raised `NameError` because the output tensor name was assigned inside the node loop. | Also fixed: an empty graph is now refused as an unsupported model ("graph produced no output") instead of crashing with an internal error. |
| The frozen quality-target exclusion digests cover only the modules that were registered as targets. | The S1–S8 surrogate modules had never been added to the lint/type target lists, so the inventory test failed on a clean tree. Every surrogate module plus the new S10 modules are now lint- and `--strict` mypy-clean and registered, and the recomputed exclusion digests equal the frozen literals exactly — proving the classification was restored rather than rubber-stamped. |

Gate evidence, all solver-free:

- Real dispatch through `create_server` for all five tools, with
  `path_policy.accepted = true` inside the owned artifact root.
- Cold discovery in a fresh interpreter registers exactly the five surrogate
  tools and loads none of `mph`, `jpype`, `torch`, `tensorflow`, `onnx`,
  `numpy`, or any solver/session module.
- A path outside the owned artifact root is refused with
  `path_policy.accepted = false` and `enforced = true`.
- `solver_started` and `filesystem_modified` are `False` on every response, and
  inspection never rewrites the document it reads.
- Each document is validated by re-deriving its canonical hash, so a tampered
  document is refused with `surrogate_document_hash_mismatch`.
- A prediction row set carrying any FEM evidence field is refused, and every
  verdict restates that the result remains a prediction requiring a fresh FEM
  run.
- The frozen `comsolless_read_only` surface remains exactly its five predecessor
  tools; the surrogate tools are added to every other profile and to `core`.

Frozen limitation: `dbmodel://` is validated as syntax and evidence only — an
authority, a resource path, and an optional sha256, with traversal and non-ASCII
refused. No live Model Manager operation is added in 0.7.5. This slice is bounded
tooling and evidence separation; it is not model quality or scientific
acceptance.

## 15. S11 findings — making the freeze reachable, and a registry blind spot

S10 shipped the `dbmodel://` contract but left it unreachable, and the registry
reported itself complete while 20 emitted surrogate schemas were unregistered.
Both are now closed, still without a solver.

| Finding | Consequence for this repository |
| --- | --- |
| The `dbmodel://` contract was **unreachable through the MCP surface**: no public tool accepted a source kind, so `validate_source_reference` was exercised only by its own unit test. | `surrogate_dataset_validate` now takes `source_kind` and `source_uri`. A `dbmodel` source routes to `resolve_dbmodel_source`, which parses the URI, reports `resolution_state: unavailable`, and never reads, connects, or authenticates. The dataset path becomes optional only because the source representation is now explicit. |
| The schema-registry completeness test scanned **only** the `"schema_name"` dict key, while the surrogate modules declare identity with `"schema"`. | The scan now accepts either spelling, and a dedicated test pins the scanner against a synthetic source using both spellings plus both `dict(...)` forms. Widening the scan exposed exactly 20 emitted-but-unregistered surrogate schemas; all 20 are now registered (`entry_count` 161 → 181) and a second test asserts every emitted surrogate schema resolves at the version it emits. |
| Two different vocabularies share the name `source_kind`: the **source-reference** kinds (`file`/`directory`/`dbmodel`) and the dataset manifest's **provenance** kind (for example `campaign`). | An attempt to enforce `SOURCE_KINDS` inside `build_dataset_manifest` rejected three legitimate existing tests. The enforcement belongs where a caller selects a representation, so the manifest keeps provenance as a required non-empty string and both vocabularies are documented at the point of use. A test asserts provenance values such as `campaign` still round-trip. |
| Path containment runs **before** the tool body, so a nonexistent path is refused by policy before the contract can speak. | Tests that intend to exercise the contract must point at real files, and a separate test asserts that a nonexistent path is refused by containment instead. A refused source is never read even when a readable path was supplied. |
| The `source_uri` schema bound and the contract's `MAX_URI_LENGTH` were separate literals. | The tool signature now uses the contract constant, so a URI can never be schema-legal yet rejected as over-long for a different reason. An over-long URI is refused at the schema bound, which is the intended layering and is asserted as such. |

Gate evidence for this slice, all solver-free: `dbmodel` dispatch returns
`success: true` with `read`, `filesystem_access`, `live_model_manager_access`,
and `upgrades_fem_evidence` all `False`; traversal, non-ASCII, wrong scheme,
authority-only, missing-URI, `dbmodel`-with-local-path, file-with-URI, and
unknown-kind requests are each refused with `surrogate_dataset_rejected`; a real
file source still reports its rows, header, and identity; and the surrogate
suites pass with no heavy module imported.

Frozen limitation, unchanged: `dbmodel://` remains syntax and evidence only. The
four live Model Manager operations stay deferred to alpha7.6.
