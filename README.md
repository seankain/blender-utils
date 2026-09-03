# blender-utils
A place to put blender utils that were probably vibe slopped out of necessity.

## bake_materials.py

Bakes every mesh in a scene down to a single game-ready texture set. Asset
packs usually ship each mesh with several materials (body / glass / optics /
wheel, each with its own texture); this collapses them into one atlased
diffuse + metallic + emission set per mesh and rebuilds the object with a
single material.

What it does, per mesh object:

1. switches the render engine to Cycles,
2. generates a non-overlapping UV map (`BakeUV`, Smart UV Project),
3. bakes every material slot into one image per pass,
4. writes the images to disk,
5. replaces the object's materials with one Principled BSDF wired to them.

### Usage

```sh
# bake an existing .blend
blender -b scene.blend -P bake_materials.py -- --output-dir baked

# import a pack and bake it in one go
blender -b -P bake_materials.py -- \
    --import-file source/fab.fbx \
    --output-dir baked \
    --resolution 2048 \
    --save-blend baked/pack_baked.blend

# see what would be processed
blender -b scene.blend -P bake_materials.py -- --dry-run
```

Running it from Blender's Text Editor works too — everything falls back to
defaults (1024 px, `diffuse,metallic,emission`, output in `./baked`).

Textures are written as `<Object>_<pass>.png`, e.g.
`Compact_Body_diffuse.png`.

### Options

| Flag | Default | Purpose |
| --- | --- | --- |
| `--import-file` | – | Import `.fbx/.obj/.gltf/.glb/.dae` before baking |
| `--output-dir` | `baked` | Where the textures go |
| `--passes` | `diffuse,metallic,emission` | Also supports `roughness`, `normal` |
| `--diffuse-mode` | `basecolor` | `basecolor` or `diffuse-pass` (see below) |
| `--resolution` | `1024` | Square texture size |
| `--samples` | `8` | Cycles samples per bake |
| `--margin` | `8` | Bake margin in pixels |
| `--device` | `CPU` | `CPU` or `GPU` |
| `--format` | `PNG` | `PNG`, `JPEG`, `TARGA`, `OPEN_EXR` |
| `--uv-mode` | `smart` | `smart`, `pack`, or `existing` |
| `--uv-name` | `BakeUV` | Name of the generated UV map |
| `--uv-margin` | `0.005` | Island margin |
| `--uv-angle-limit` | `66` | Smart UV Project angle limit, degrees |
| `--selected-only` | off | Only bake selected objects |
| `--only` | – | Comma separated object names |
| `--no-replace-materials` | off | Bake textures, keep original materials |
| `--keep-linked-duplicates` | off | Bake objects sharing one mesh more than once |
| `--save-blend` | – | Save the result to a `.blend` |
| `--dry-run` | off | List what would be baked and exit |

### Implementation notes

* **Metallic** has no native Cycles bake pass. Whatever drives the Principled
  `Metallic` input (a texture, or the constant value) is temporarily routed
  through an Emission shader and captured with an `EMIT` bake; the original
  node links are restored afterwards.
* **Diffuse** is baked the same way from `Base Color`. Cycles' own
  `DIFFUSE`/`COLOR` pass returns `base colour * (1 - metallic)`, so metal
  areas bake black and get darkened again when the metallic map is applied in
  an engine. Use `--diffuse-mode diffuse-pass` if you want Blender's pass
  instead.
* **Emission** uses the native `EMIT` pass, so it works for any shader.
* Non-Principled materials (Diffuse/Glass/Emission BSDF) fall back to their
  `Color` input; a material with no usable shader bakes black rather than
  failing.
* Objects with no materials are skipped, as are extra objects sharing a mesh
  that was already baked. Empty material slots get a temporary placeholder so
  the bake operator accepts them.
* Alpha/transparency is not baked.
