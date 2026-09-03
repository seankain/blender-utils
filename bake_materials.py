#!/usr/bin/env python3
r"""Bake every mesh in a Blender scene down to a single game-ready texture set.

For each mesh object the script bakes all of its material slots into one
image per map type (diffuse / metallic / emission by default), writes the
images to disk and — unless disabled — replaces the object's materials with a
single Principled BSDF material driven by those images.

Typical use, headless:

    blender -b scene.blend -P bake_materials.py -- --output-dir baked

    blender -b -P bake_materials.py -- \
        --import-file source/fab.fbx \
        --output-dir baked --resolution 2048 --save-blend baked/scene.blend

It also runs unmodified from the Text Editor inside Blender (defaults apply,
output goes next to the .blend file).

Notes
-----
* Cycles has no native metallic pass. The metallic map is produced by
  temporarily routing whatever drives the Principled BSDF "Metallic" input
  into an Emission shader and baking an EMIT pass. The original node links
  are restored afterwards.
* The diffuse map is baked the same way, from the "Base Color" input
  (--diffuse-mode basecolor, the default). Cycles' own Diffuse/Color pass
  returns base colour * (1 - metallic), so metal areas come out black and get
  darkened a second time when the metallic map is applied in an engine; it is
  still available as --diffuse-mode diffuse-pass.
* Emission uses the native EMIT pass, so it captures emission colour times
  strength for any shader, Principled or not.
* Baking several materials into one image needs a non-overlapping UV layout,
  so by default a dedicated UV map ("BakeUV") is generated with Smart UV
  Project. Use --uv-mode existing to bake into the UVs the asset ships with.
* Objects with no material, and extra objects sharing an already baked mesh,
  are skipped. Alpha/transparency is not baked.
"""

from __future__ import annotations

import argparse
import math
import os
import sys
import time

import bpy

# --------------------------------------------------------------------------
# Pass definitions
# --------------------------------------------------------------------------
PASS_NAMES = ("diffuse", "metallic", "emission", "roughness", "normal")
DEFAULT_PASSES = ("diffuse", "metallic", "emission")

# Shader input names looked up (in order) when a pass is baked by temporarily
# routing that input through an Emission shader.
BASE_COLOR_SOCKETS = ("Base Color", "Color")
METALLIC_SOCKETS = ("Metallic",)

FILE_EXTENSIONS = {
    "PNG": ".png",
    "JPEG": ".jpg",
    "TARGA": ".tga",
    "OPEN_EXR": ".exr",
}

BAKE_NODE_LABEL = "BAKE_TARGET"
TEMP_NODE_LABEL = "BAKE_TEMP"


def log(message: str) -> None:
    print("[bake] %s" % message, flush=True)


def pass_plan(pass_name: str, args):
    """Return ``(bake_type, is_data, rewire_sockets_or_None)`` for a pass.

    ``rewire_sockets`` is a tuple of shader input names that are temporarily
    routed through an Emission shader and captured with an EMIT bake, which is
    how inputs Cycles has no bake pass for (Metallic) are produced.
    """
    if pass_name == "diffuse":
        if args.diffuse_mode == "basecolor":
            return "EMIT", False, BASE_COLOR_SOCKETS
        return "DIFFUSE", False, None
    if pass_name == "metallic":
        return "EMIT", True, METALLIC_SOCKETS
    if pass_name == "emission":
        return "EMIT", False, None
    if pass_name == "roughness":
        return "ROUGHNESS", True, None
    if pass_name == "normal":
        return "NORMAL", True, None
    raise ValueError("unknown pass: %s" % pass_name)


# --------------------------------------------------------------------------
# Command line
# --------------------------------------------------------------------------
def parse_args(argv=None) -> argparse.Namespace:
    """Parse arguments given after ``--`` on the Blender command line."""
    if argv is None:
        if "--" in sys.argv:
            argv = sys.argv[sys.argv.index("--") + 1:]
        elif not bpy.app.binary_path:
            # Running against the `bpy` pip module rather than the Blender
            # executable: there are no Blender arguments to skip past.
            argv = sys.argv[1:]
        else:
            argv = []

    parser = argparse.ArgumentParser(
        prog="bake_materials.py",
        description="Bake each mesh's materials down to a single texture set.",
    )
    parser.add_argument("--import-file", default=None,
                        help="Import this file (.fbx/.obj/.gltf/.glb/.dae) into "
                             "the current scene before baking.")
    parser.add_argument("--output-dir", default="baked",
                        help="Directory the baked textures are written to "
                             "(default: %(default)s).")
    parser.add_argument("--passes", default=",".join(DEFAULT_PASSES),
                        help="Comma separated subset of %s (default: %%(default)s)."
                             % ", ".join(PASS_NAMES))
    parser.add_argument("--diffuse-mode", choices=("basecolor", "diffuse-pass"),
                        default="basecolor",
                        help="How the diffuse map is produced (default: "
                             "%(default)s). 'basecolor' bakes the raw Base Color "
                             "input; 'diffuse-pass' uses Cycles' Diffuse/Color "
                             "pass, which returns base colour * (1 - metallic) "
                             "and therefore darkens metals.")
    parser.add_argument("--resolution", type=int, default=1024,
                        help="Baked texture size in pixels, square "
                             "(default: %(default)s).")
    parser.add_argument("--samples", type=int, default=8,
                        help="Cycles samples per bake (default: %(default)s). "
                             "Colour/data passes do not need many.")
    parser.add_argument("--margin", type=int, default=8,
                        help="Bake margin in pixels (default: %(default)s).")
    parser.add_argument("--device", choices=("CPU", "GPU"), default="CPU",
                        help="Cycles device (default: %(default)s).")
    parser.add_argument("--format", dest="file_format", default="PNG",
                        choices=sorted(FILE_EXTENSIONS),
                        help="Image file format (default: %(default)s).")
    parser.add_argument("--uv-mode", choices=("smart", "pack", "existing"),
                        default="smart",
                        help="How to obtain non-overlapping bake UVs "
                             "(default: %(default)s). 'existing' bakes into the "
                             "mesh's current active UV map.")
    parser.add_argument("--uv-name", default="BakeUV",
                        help="Name of the generated UV map (default: %(default)s).")
    parser.add_argument("--uv-margin", type=float, default=0.005,
                        help="Island margin for the generated UV map "
                             "(default: %(default)s).")
    parser.add_argument("--uv-angle-limit", type=float, default=66.0,
                        help="Smart UV Project angle limit in degrees "
                             "(default: %(default)s).")
    parser.add_argument("--selected-only", action="store_true",
                        help="Only process currently selected mesh objects.")
    parser.add_argument("--only", default=None,
                        help="Comma separated list of object names to process.")
    parser.add_argument("--no-replace-materials", dest="replace_materials",
                        action="store_false",
                        help="Bake the textures but keep the original materials.")
    parser.add_argument("--keep-linked-duplicates", action="store_true",
                        help="Bake every object even when several share the same "
                             "mesh data (by default a shared mesh is baked once).")
    parser.add_argument("--save-blend", default=None,
                        help="Save the resulting .blend file to this path.")
    parser.add_argument("--dry-run", action="store_true",
                        help="List what would be baked and exit.")
    parser.set_defaults(replace_materials=True)

    args = parser.parse_args(argv)

    args.passes = [p.strip().lower() for p in args.passes.split(",") if p.strip()]
    unknown = [p for p in args.passes if p not in PASS_NAMES]
    if unknown:
        parser.error("unknown pass(es): %s" % ", ".join(unknown))
    if not args.passes:
        parser.error("no passes requested")
    args.only = ([n.strip() for n in args.only.split(",") if n.strip()]
                 if args.only else None)
    return args


# --------------------------------------------------------------------------
# Scene / render setup
# --------------------------------------------------------------------------
def setup_cycles(scene, args) -> None:
    """Switch the scene to Cycles and configure it for texture baking."""
    scene.render.engine = "CYCLES"
    cycles = scene.cycles
    cycles.device = args.device
    cycles.samples = args.samples
    if hasattr(cycles, "use_adaptive_sampling"):
        cycles.use_adaptive_sampling = False
    if hasattr(cycles, "use_denoising"):
        cycles.use_denoising = False
    # Bake results are written straight into the image buffer, but keeping the
    # view transform neutral avoids surprises in any preview render.
    try:
        scene.view_settings.view_transform = "Standard"
    except TypeError:
        pass

    if args.device == "GPU":
        prefs = bpy.context.preferences.addons.get("cycles")
        if prefs is not None:
            cprefs = prefs.preferences
            for backend in ("OPTIX", "CUDA", "HIP", "METAL", "ONEAPI"):
                try:
                    cprefs.compute_device_type = backend
                except TypeError:
                    continue
                cprefs.get_devices()
                if any(d.type == backend for d in cprefs.devices):
                    for device in cprefs.devices:
                        device.use = device.type != "CPU"
                    log("using GPU backend %s" % backend)
                    break

    bake = scene.render.bake
    bake.use_selected_to_active = False
    bake.use_clear = True
    bake.margin = args.margin
    if hasattr(bake, "margin_type"):
        bake.margin_type = "ADJACENT_FACES"
    if hasattr(bake, "target"):
        bake.target = "IMAGE_TEXTURES"
    log("render engine set to CYCLES (%s, %d samples)" % (args.device, args.samples))


def import_source(path: str) -> None:
    """Import a mesh file into the current scene."""
    path = os.path.abspath(path)
    if not os.path.isfile(path):
        raise SystemExit("import file not found: %s" % path)
    ext = os.path.splitext(path)[1].lower()
    if ext == ".fbx":
        if hasattr(bpy.ops.wm, "fbx_import"):
            bpy.ops.wm.fbx_import(filepath=path)
        else:
            bpy.ops.import_scene.fbx(filepath=path)
    elif ext == ".obj":
        if hasattr(bpy.ops.wm, "obj_import"):
            bpy.ops.wm.obj_import(filepath=path)
        else:
            bpy.ops.import_scene.obj(filepath=path)
    elif ext in (".gltf", ".glb"):
        bpy.ops.import_scene.gltf(filepath=path)
    elif ext == ".dae":
        bpy.ops.wm.collada_import(filepath=path)
    else:
        raise SystemExit("unsupported import format: %s" % ext)
    log("imported %s" % path)


def target_objects(args):
    """Return the mesh objects that should be baked, in scene order."""
    view_layer = bpy.context.view_layer
    objects = [o for o in view_layer.objects if o.type == "MESH"]
    if args.selected_only:
        objects = [o for o in objects if o.select_get()]
    if args.only:
        wanted = set(args.only)
        objects = [o for o in objects if o.name in wanted]

    result, seen_meshes = [], set()
    for obj in objects:
        if not obj.data.polygons:
            log("skipping %s: no faces" % obj.name)
            continue
        if not any(slot.material for slot in obj.material_slots):
            log("skipping %s: no materials" % obj.name)
            continue
        if not args.keep_linked_duplicates:
            key = obj.data.name
            if key in seen_meshes:
                log("skipping %s: mesh '%s' already baked" % (obj.name, key))
                continue
            seen_meshes.add(key)
        result.append(obj)
    return result


# --------------------------------------------------------------------------
# Object state helpers
# --------------------------------------------------------------------------
def isolate_object(obj):
    """Make ``obj`` the only selected + active object, unhiding it if needed.

    Returns a restore callable.
    """
    view_layer = bpy.context.view_layer
    previous = [(o, o.select_get()) for o in view_layer.objects]
    previous_active = view_layer.objects.active
    hidden = (obj.hide_get(), obj.hide_viewport, obj.hide_render)

    for other, _ in previous:
        other.select_set(False)
    obj.hide_set(False)
    obj.hide_viewport = False
    obj.hide_render = False
    obj.select_set(True)
    view_layer.objects.active = obj

    def restore():
        obj.hide_set(hidden[0])
        obj.hide_viewport = hidden[1]
        obj.hide_render = hidden[2]
        for other, was_selected in previous:
            try:
                other.select_set(was_selected)
            except RuntimeError:
                pass
        view_layer.objects.active = previous_active

    return restore


def ensure_bake_uv(obj, args) -> str:
    """Make sure the object has a UV map suitable for baking and activate it.

    Returns the name of the UV map used for the bake.
    """
    uv_layers = obj.data.uv_layers
    if args.uv_mode == "existing":
        if not uv_layers:
            raise RuntimeError("%s has no UV map and --uv-mode is 'existing'"
                               % obj.name)
        uv = uv_layers.active or uv_layers[0]
        uv_layers.active = uv
        uv.active_render = True
        return uv.name

    uv = uv_layers.get(args.uv_name)
    created = uv is None
    if created:
        if len(uv_layers) >= 8:
            raise RuntimeError("%s already has 8 UV maps, cannot add '%s'"
                               % (obj.name, args.uv_name))
        uv = uv_layers.new(name=args.uv_name, do_init=True)
    uv_name = uv.name
    uv_layers.active = uv
    uv.active_render = True

    if created:
        unwrap(obj, args)
        # Entering edit mode adds internal attribute layers, which invalidates
        # the reference above; look the UV map up again by name.
        uv = uv_layers.get(uv_name)
        if uv is None:
            raise RuntimeError("UV map '%s' disappeared while unwrapping %s"
                               % (uv_name, obj.name))
        uv_layers.active = uv
        uv.active_render = True
    return uv_name


def unwrap(obj, args) -> None:
    """Run Smart UV Project / Lightmap Pack on the object's active UV map."""
    bpy.ops.object.mode_set(mode="EDIT")
    try:
        bpy.ops.mesh.select_all(action="SELECT")
        if args.uv_mode == "pack":
            # PREF_MARGIN_DIV is a fraction of the UV space, hard limited to
            # [0.001, 1.0]; the default 0.1 corresponds to --uv-margin 0.005.
            margin_div = min(max(args.uv_margin * 20.0, 0.001), 1.0)
            bpy.ops.uv.lightmap_pack(PREF_CONTEXT="ALL_FACES",
                                     PREF_MARGIN_DIV=margin_div)
        else:
            bpy.ops.uv.smart_project(
                angle_limit=math.radians(args.uv_angle_limit),
                island_margin=args.uv_margin,
                correct_aspect=True,
                scale_to_bounds=False,
            )
    finally:
        bpy.ops.object.mode_set(mode="OBJECT")


def object_materials(obj):
    """Return the unique materials used by the object's slots."""
    seen, materials = set(), []
    for slot in obj.material_slots:
        mat = slot.material
        if mat is None or mat.name in seen:
            continue
        seen.add(mat.name)
        if mat.node_tree is None:
            mat.use_nodes = True
        materials.append(mat)
    return materials


def fill_empty_slots(obj):
    """Give empty material slots a placeholder so the bake operator accepts them.

    Returns a restore callable.
    """
    empty = [slot for slot in obj.material_slots if slot.material is None]
    if not empty:
        return lambda: None
    placeholder = bpy.data.materials.get("BAKE_PLACEHOLDER")
    if placeholder is None:
        placeholder = bpy.data.materials.new("BAKE_PLACEHOLDER")
        if placeholder.node_tree is None:
            placeholder.use_nodes = True
    for slot in empty:
        slot.material = placeholder

    def restore():
        for slot in empty:
            if slot.material is placeholder:
                slot.material = None

    return restore


# --------------------------------------------------------------------------
# Node graph plumbing
# --------------------------------------------------------------------------
def material_output(node_tree):
    outputs = [n for n in node_tree.nodes
               if n.bl_idname == "ShaderNodeOutputMaterial"]
    for node in outputs:
        if node.is_active_output:
            return node
    return outputs[0] if outputs else None


def find_principled(node_tree):
    """Return the Principled BSDF feeding the material output, if any."""
    output = material_output(node_tree)
    if output is None:
        return None
    surface = output.inputs.get("Surface")
    if surface is None or not surface.is_linked:
        return next((n for n in node_tree.nodes
                     if n.bl_idname == "ShaderNodeBsdfPrincipled"), None)

    stack, visited = [surface.links[0].from_node], set()
    while stack:
        node = stack.pop()
        if node.name in visited:
            continue
        visited.add(node.name)
        if node.bl_idname == "ShaderNodeBsdfPrincipled":
            return node
        for socket in node.inputs:
            for link in socket.links:
                stack.append(link.from_node)
    return None


def new_image(name: str, size: int, is_data: bool):
    """Create (or reset) a square 8-bit image to bake into."""
    existing = bpy.data.images.get(name)
    if existing is not None:
        bpy.data.images.remove(existing)
    image = bpy.data.images.new(name, width=size, height=size,
                                alpha=False, float_buffer=False)
    image.generated_color = (0.0, 0.0, 0.0, 1.0)
    if is_data:
        image.colorspace_settings.name = "Non-Color"
    return image


def add_bake_targets(materials, image, uv_name):
    """Add the active image texture node every material bakes into.

    Returns a restore callable.
    """
    created = []
    for mat in materials:
        node_tree = mat.node_tree
        previous_active = node_tree.nodes.active
        previous_selection = [n for n in node_tree.nodes if n.select]
        for node in previous_selection:
            node.select = False

        tex = node_tree.nodes.new("ShaderNodeTexImage")
        tex.label = BAKE_NODE_LABEL
        tex.image = image
        tex.location = (-400, -600)
        uv_node = node_tree.nodes.new("ShaderNodeUVMap")
        uv_node.label = TEMP_NODE_LABEL
        uv_node.uv_map = uv_name
        uv_node.location = (-600, -600)
        node_tree.links.new(uv_node.outputs["UV"], tex.inputs["Vector"])

        tex.select = True
        node_tree.nodes.active = tex
        created.append((node_tree, tex, uv_node, previous_active,
                        previous_selection))

    def restore():
        for node_tree, tex, uv_node, previous_active, previous_selection in created:
            node_tree.nodes.remove(uv_node)
            node_tree.nodes.remove(tex)
            for node in previous_selection:
                node.select = True
            if previous_active is not None:
                node_tree.nodes.active = previous_active

    return restore


def find_input_socket(node_tree, socket_names):
    """Find the shader input driving one of ``socket_names``.

    The Principled BSDF feeding the material output is preferred; any other
    shader node exposing one of the names is used as a fallback so that
    non-Principled setups (Diffuse/Glass/Emission BSDF) still bake something
    meaningful.
    """
    principled = find_principled(node_tree)
    if principled is not None:
        for name in socket_names:
            socket = principled.inputs.get(name)
            if socket is not None:
                return socket
    for node in node_tree.nodes:
        if node.label == TEMP_NODE_LABEL:
            continue  # never match the nodes this script inserts
        if not node.bl_idname.startswith("ShaderNodeBsdf") and \
                node.bl_idname != "ShaderNodeEmission":
            continue
        for name in socket_names:
            socket = node.inputs.get(name)
            if socket is not None:
                return socket
    return None


def route_inputs_to_emission(materials, socket_names, fallback):
    """Temporarily emit a shader input so an EMIT bake can capture it.

    Whatever drives one of ``socket_names`` (a link, or the socket's constant
    value) is connected to an Emission shader wired into the material output.
    ``fallback`` is called with the material when the input cannot be found and
    must return an RGBA tuple. Returns a restore callable that puts the
    original node graph back.
    """
    changes = []
    for mat in materials:
        node_tree = mat.node_tree
        output = material_output(node_tree)
        if output is None:
            continue
        surface = output.inputs["Surface"]
        original_source = (surface.links[0].from_socket
                           if surface.is_linked else None)

        # Resolve the source before inserting anything, so the lookup cannot
        # match the temporary node below.
        socket = find_input_socket(node_tree, socket_names)

        emission = node_tree.nodes.new("ShaderNodeEmission")
        emission.label = TEMP_NODE_LABEL
        emission.location = (output.location.x - 200, output.location.y - 400)
        emission.inputs["Strength"].default_value = 1.0

        if socket is not None and socket.is_linked:
            node_tree.links.new(socket.links[0].from_socket,
                                emission.inputs["Color"])
        else:
            value = socket.default_value if socket is not None else fallback(mat)
            try:
                colour = tuple(value)[:3] + (1.0,)
            except TypeError:
                colour = (value, value, value, 1.0)
            emission.inputs["Color"].default_value = colour

        node_tree.links.new(emission.outputs["Emission"], surface)
        changes.append((node_tree, emission, output, original_source))

    def restore():
        for node_tree, emission, output, original_source in changes:
            node_tree.nodes.remove(emission)
            if original_source is not None:
                node_tree.links.new(original_source, output.inputs["Surface"])

    return restore


# --------------------------------------------------------------------------
# Baking
# --------------------------------------------------------------------------
def bake_pass(obj, pass_name: str, image, uv_name: str, args) -> None:
    """Bake a single pass for one object into ``image``."""
    bake_type, _is_data, rewire_sockets = pass_plan(pass_name, args)
    materials = object_materials(obj)

    def fallback(mat):
        # Used only when the material exposes no matching shader input.
        if pass_name == "diffuse":
            return tuple(mat.diffuse_color)
        return 0.0

    restore_targets = add_bake_targets(materials, image, uv_name)
    restore_graph = (route_inputs_to_emission(materials, rewire_sockets, fallback)
                     if rewire_sockets else lambda: None)
    try:
        kwargs = dict(type=bake_type, margin=args.margin, use_clear=True,
                      use_selected_to_active=False)
        if bake_type == "DIFFUSE":
            kwargs["pass_filter"] = {"COLOR"}
            bake_settings = bpy.context.scene.render.bake
            bake_settings.use_pass_direct = False
            bake_settings.use_pass_indirect = False
            bake_settings.use_pass_color = True
        bpy.ops.object.bake(**kwargs)
    finally:
        restore_graph()
        restore_targets()


def save_image(image, directory: str, file_format: str) -> str:
    """Write the baked image to disk and point the datablock at the file."""
    path = os.path.join(directory, image.name + FILE_EXTENSIONS[file_format])
    image.file_format = file_format
    image.filepath_raw = path
    image.save()
    image.filepath = path
    return path


def build_baked_material(obj, images, args):
    """Replace the object's materials with one Principled BSDF using the bakes."""
    name = "%s_baked" % obj.name
    existing = bpy.data.materials.get(name)
    if existing is not None:
        bpy.data.materials.remove(existing)
    mat = bpy.data.materials.new(name)
    if mat.node_tree is None:  # Blender < 5.0 needs this switched on explicitly
        mat.use_nodes = True
    node_tree = mat.node_tree
    node_tree.nodes.clear()

    output = node_tree.nodes.new("ShaderNodeOutputMaterial")
    output.location = (400, 0)
    principled = node_tree.nodes.new("ShaderNodeBsdfPrincipled")
    principled.location = (100, 0)
    node_tree.links.new(principled.outputs["BSDF"], output.inputs["Surface"])

    slots = {
        "diffuse": ("Base Color", 300),
        "metallic": ("Metallic", 0),
        "roughness": ("Roughness", -300),
        "emission": ("Emission Color", -600),
        "normal": ("Normal", -900),
    }
    for pass_name, image in images.items():
        socket_name, y = slots.get(pass_name, (None, 0))
        if socket_name is None:
            continue
        socket = (principled.inputs.get(socket_name)
                  or principled.inputs.get("Emission"))
        if socket is None:
            continue
        tex = node_tree.nodes.new("ShaderNodeTexImage")
        tex.image = image
        tex.label = pass_name
        tex.location = (-500, y)
        if pass_name == "normal":
            normal_map = node_tree.nodes.new("ShaderNodeNormalMap")
            normal_map.location = (-200, y)
            node_tree.links.new(tex.outputs["Color"], normal_map.inputs["Color"])
            node_tree.links.new(normal_map.outputs["Normal"], socket)
        else:
            node_tree.links.new(tex.outputs["Color"], socket)
        if pass_name == "emission":
            strength = principled.inputs.get("Emission Strength")
            if strength is not None:
                strength.default_value = 1.0

    object_slot_links = [slot for slot in obj.material_slots
                         if slot.link == "OBJECT"]
    if object_slot_links:
        log("  warning: %s has object-linked material slots; they are replaced "
            "on the mesh data" % obj.name)

    mesh = obj.data
    mesh.materials.clear()
    mesh.materials.append(mat)
    for polygon in mesh.polygons:
        polygon.material_index = 0
    return mat


def bake_object(obj, args, output_dir: str) -> dict:
    """Bake every requested pass for one object. Returns {pass: image}."""
    restore_state = isolate_object(obj)
    restore_slots = fill_empty_slots(obj)
    images = {}
    try:
        uv_name = ensure_bake_uv(obj, args)
        log("%s: %d slot(s), UV map '%s'"
            % (obj.name, len(obj.material_slots), uv_name))
        safe_name = obj.name.replace(" ", "_").replace(os.sep, "_")
        for pass_name in args.passes:
            _bake_type, is_data, _rewire = pass_plan(pass_name, args)
            image = new_image("%s_%s" % (safe_name, pass_name),
                              args.resolution, is_data)
            start = time.time()
            bake_pass(obj, pass_name, image, uv_name, args)
            path = save_image(image, output_dir, args.file_format)
            log("  %-9s -> %s (%.1fs)"
                % (pass_name, os.path.basename(path), time.time() - start))
            images[pass_name] = image
    finally:
        restore_slots()
        restore_state()

    if args.replace_materials:
        build_baked_material(obj, images, args)
    return images


# --------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------
def main() -> int:
    args = parse_args()

    if args.import_file:
        import_source(args.import_file)

    scene = bpy.context.scene
    objects = target_objects(args)
    if not objects:
        log("nothing to bake")
        return 1

    output_dir = os.path.abspath(bpy.path.abspath(args.output_dir))
    log("%d mesh object(s), passes: %s, %dpx -> %s"
        % (len(objects), ", ".join(args.passes), args.resolution, output_dir))
    if args.dry_run:
        for obj in objects:
            log("  would bake %s (%d slots)" % (obj.name, len(obj.material_slots)))
        return 0

    os.makedirs(output_dir, exist_ok=True)
    setup_cycles(scene, args)

    if bpy.context.object is not None and bpy.context.object.mode != "OBJECT":
        bpy.ops.object.mode_set(mode="OBJECT")

    failures = []
    started = time.time()
    for index, obj in enumerate(objects, 1):
        log("[%d/%d] %s" % (index, len(objects), obj.name))
        try:
            bake_object(obj, args, output_dir)
        except Exception as exc:  # keep going, report at the end
            failures.append((obj.name, str(exc)))
            log("  FAILED: %s" % exc)

    placeholder = bpy.data.materials.get("BAKE_PLACEHOLDER")
    if placeholder is not None and placeholder.users == 0:
        bpy.data.materials.remove(placeholder)

    log("baked %d/%d object(s) in %.1fs"
        % (len(objects) - len(failures), len(objects), time.time() - started))
    for name, error in failures:
        log("  failed: %s (%s)" % (name, error))

    if args.save_blend:
        blend_path = os.path.abspath(bpy.path.abspath(args.save_blend))
        os.makedirs(os.path.dirname(blend_path) or ".", exist_ok=True)
        bpy.ops.wm.save_as_mainfile(filepath=blend_path)
        log("saved %s" % blend_path)

    return 1 if failures else 0


if __name__ == "__main__":
    exit_code = main()
    if bpy.app.background:
        # Only propagate an exit code for headless runs; raising SystemExit
        # inside the Text Editor would be reported as a script error.
        sys.exit(exit_code)
