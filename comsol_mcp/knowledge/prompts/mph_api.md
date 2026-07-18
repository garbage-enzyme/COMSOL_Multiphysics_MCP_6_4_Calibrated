# MPh 1.3.1 and COMSOL 6.4 clientapi reference

This is a compact reference for the Python API used by this server. The verified
runtime is MPh 1.3.1 with COMSOL 6.4.0.293. Other COMSOL or MPh builds require
their own compatibility checks.

## Safety and ownership

- One Python process can construct only one MPh client because JPype permits one
  JVM lifecycle.
- When using this MCP server, call `solver_status` and `solver_preflight` before
  `comsol_start`; do not instantiate a separate `mph.Client` in parallel.
- Treat loaded source models as immutable. Work on a derived copy and save to a
  separate path.
- A direct `mph.Client(port=...)` connection does not by itself provide safe
  shared Desktop ownership, model locking, or detach-preservation semantics.
  Use the explicit default-off `desktop_shared` profile and its preflight,
  exact adoption, revision lock, Save Copy, and detach tools for the supported
  local shared lifecycle; see `docs/interactive_shared_session/`.

## Installation

The server declares the supported MPh range and should normally be installed as
a non-editable package:

```powershell
python -m pip install .
```

For direct API experiments in a separate environment:

```powershell
python -m pip install "mph>=1.3.1,<1.4"
```

## Client

```python
import mph

# Standalone client. Starting COMSOL can take 30-90 seconds.
client = mph.Client(cores=1, version="6.4")
```

In a fresh Python process, the alternative direct Server connection is:

```python
import mph

# Use only when ownership and cleanup are managed outside this example.
client = mph.Client(port=2036, host="localhost")
```

After constructing exactly one of those clients:

```python
client.version       # discovered COMSOL version string
client.standalone    # True for standalone, False for Server connection
client.cores         # configured core count
client.host          # connected Server host, otherwise None
client.port          # connected Server port, otherwise None
client.models()      # loaded model names
```

Use `client.create(name)`, `client.load(path)`, `client.remove(model)`, and
`client.clear()` for client-owned models. `client.disconnect()` is for a Server
connection; it is not a substitute for verified cleanup of an MCP-owned
standalone worker.

## Model wrapper

```python
model = client.load("capacitor.mph")

model.name()
model.file()
model.version()
model.parameters()
model.physics()
model.geometries()
model.meshes()
model.studies()
model.datasets()
model.plots()
model.exports()
model.materials()
```

The returned names are normally labels used by the MPh node wrapper. Clientapi
tags are separate and should be read from `model.java` when exact identity is
required.

## Parameters and evaluation

```python
value = model.parameter("U")
model.parameter("U", "5[V]")
values = model.parameters(evaluate=True)

model.description("U", "Applied voltage")
descriptions = model.descriptions()

field = model.evaluate("es.normE", "V/m", dataset="Study 1//Solution 1")
columns = model.evaluate(["x", "y", "T"], dataset="Study 1//Solution 1")
indices, inner_values = model.inner(dataset="Study 1//Solution 1")
outer_indices, outer_values = model.outer(dataset="Study 1//Solution 1")
```

Specify the dataset and inner solution when more than one solution exists.
Multi-point outer sweeps are not reliably indexed by every clientapi path;
staged one-point solves are preferred for durable work.

## Model tree nodes

```python
geometry = model / "geometries" / "Geometry 1"
physics = model / "physics" / "Electrostatics"

block = geometry.create("Block", name="Block 1")
block.property("size", [1, 1, 1])
size = block.property("size")
block.remove()
```

Node paths use labels and may be localized. For version-sensitive construction,
inspect a trusted model or use the direct clientapi object rather than guessing
labels, tags, feature types, or overloads.

## Direct COMSOL clientapi

MPh standalone exposes `model.java` as a
`com.comsol.clientapi.impl.ModelClient`, not a direct
`com.comsol.model.Model`. Important overloads include:

```python
jm = model.java
component = jm.component().create("comp1", True)
geometry = component.geom().create("geom1", 3)
physics = component.physics().create(
    "ewfd", "ElectromagneticWavesFrequencyDomain", "3"
)
feature = physics.feature().create("bc1", "FeatureType", 2)
```

- Convert Java tag arrays explicitly with `list(collection.tags())`.
- Resolve an item by string tag; integer `get(index)` is commonly unsupported.
- Use `feature().size()` rather than `len(feature())`.
- Physics-interface creation takes a string spatial dimension such as `"3"`;
  child features take an integer entity dimension.
- Run a study with `jm.study("std1").run()`.
- Use exact capitalization such as `getSDim()`, `getNumElem()`, and
  `getNumVertex()`.

## Solve, export, and save

```python
model.build("Geometry 1")
model.mesh("Mesh 1")
model.solve("Study 1")
model.export("Data 1", "result.txt")
```

For an absolute Unicode destination, save through the Java clientapi:

```python
from pathlib import Path

destination = Path(r"C:\path\to\derived_model.mph").resolve()
model.java.save(str(destination))
```

Do not overwrite the immutable source. A loaded `.mph` may also be file-locked,
so derived models and checkpoints should use unique paths.

## Common failures

- `No matching overloads`: a direct-Model overload was used on `ModelClient`.
- `Operation_cannot_be_created_in_this_context`: wrong owning component, feature
  type, or entity dimension.
- List indexing errors: iterate tags and call `.get(tag)`.
- Unexpected vacuum electrostatics: the default `FreeSpace` feature ignores a
  material's relative permittivity; add `ChargeConservation` with
  `materialType="from_mat"`.
- Wrong transient results: select the intended dataset and inner solution
  explicitly.
