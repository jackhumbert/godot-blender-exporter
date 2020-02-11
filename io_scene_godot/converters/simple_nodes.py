"""
Any exporters that can be written in a single function can go in here.
Anything more complex should go in it's own file
"""

import math
import re
import os
import logging
from ..structures import (
    NodeTemplate, InstanceTemplate, ExternalResource, fix_directional_transform, gamma_correct
)
from .animation import export_animation_data, AttributeConvertInfo

def _find_scene_in_subtree(folder, scene_filename):
    """Searches for godot ecene that match a blender empty. If found,
    it returns (path, type) otherwise it returns None"""
    candidates = []

    for dir_path, _subdirs, files in os.walk(folder):
        if scene_filename in files:
            candidates.append(os.path.join(dir_path, scene_filename))

    # Checks it is a scene and finds out what type
    valid_candidates = []
    for candidate in candidates:
        with open(candidate) as scene_file:
            first_line = scene_file.readline()
            if "gd_scene" in first_line:
                valid_candidates.append((candidate, "PackedScene"))

    if not valid_candidates:
        return None
    if len(valid_candidates) > 1:
        logging.warning("Multiple scenes found for %s", scene_filename)
    return valid_candidates[0]

def find_scene(export_settings, scene_filename):
    """Searches for an existing Godot scene"""
    search_type = export_settings["material_search_paths"]
    if search_type == "PROJECT_DIR":
        search_dir = export_settings["project_path_func"]()
    elif search_type == "EXPORT_DIR":
        search_dir = os.path.dirname(export_settings["path"])
    else:
        search_dir = None

    if search_dir is None:
        return None
    return _find_scene_in_subtree(search_dir, scene_filename)

def use_external_scene(escn_file, export_settings, scene_name):
    external_scene = find_scene(export_settings, scene_name)
    if external_scene is not None:
        resource_id = escn_file.get_external_resource(scene_name)
        if resource_id is None:
            ext_scene = ExternalResource(
                external_scene[0],
                external_scene[1],
            )
            resource_id = escn_file.add_external_resource(ext_scene, scene_name)
        return "ExtResource({})".format(resource_id)

    logging.warning(
        "Unable to find '%s' in project", scene_name
    )
    return None

def export_empty_node(escn_file, export_settings, node, parent_gd_node):
    """Converts an empty (or any unknown node) into a spatial"""
    if "EMPTY" not in export_settings['object_types']:
        return parent_gd_node

    match = re.match('(.*\.[te]scn)', node.name)
    if match:
        scene_name = match.group(1)
        instance = use_external_scene(escn_file, export_settings, scene_name)
        if instance:
            instance_node = InstanceTemplate(node.name, instance, parent_gd_node)
            instance_node['transform'] = node.matrix_local
            escn_file.add_node(instance_node)

            return instance_node

    empty_node = NodeTemplate(node.name, "Spatial", parent_gd_node)
    empty_node['transform'] = node.matrix_local
    escn_file.add_node(empty_node)

    return empty_node


class CameraNode(NodeTemplate):
    """Camera node in godot scene"""
    _cam_attr_conv = [
        # blender attr, godot attr, converter lambda, type
        AttributeConvertInfo('clip_end', 'far', lambda x: x),
        AttributeConvertInfo('clip_start', 'near', lambda x: x),
        AttributeConvertInfo('ortho_scale', 'size', lambda x: x),
    ]

    def __init__(self, name, parent):
        super().__init__(name, "Camera", parent)

    @property
    def attribute_conversion(self):
        """Get a list of quaternary tuple
        (blender_attr, godot_attr, lambda converter, attr type)"""
        return self._cam_attr_conv


def export_camera_node(escn_file, export_settings, node, parent_gd_node):
    """Exports a camera"""
    cam_node = CameraNode(node.name, parent_gd_node)
    camera = node.data

    for item in cam_node.attribute_conversion:
        blender_attr, gd_attr, converter = item
        cam_node[gd_attr] = converter(getattr(camera, blender_attr))

    if camera.type == "PERSP":
        cam_node['projection'] = 0
    else:
        cam_node['projection'] = 1

    # `fov` does not go into `attribute_conversion`, because it can not
    # be animated
    cam_node['fov'] = math.degrees(camera.angle)

    cam_node['transform'] = fix_directional_transform(node.matrix_local)
    escn_file.add_node(cam_node)

    export_animation_data(escn_file, export_settings,
                          cam_node, node.data, 'camera')

    return cam_node


def find_shader_node(node_tree, name):
    """Find the shader node from the tree with the given name."""
    for node in node_tree.nodes:
        if node.bl_idname == name:
            return node
    logging.warning("%s node not found", name)
    return None


def node_input(node, name):
    """Get the named input value from the shader node."""
    for inp in node.inputs:
        if inp.name == name:
            return inp.default_value
    logging.warning("%s input not found in %s", name, node.bl_idname)
    return None


class LightNode(NodeTemplate):
    """Base class for godot light node"""
    _light_attr_conv = [
        AttributeConvertInfo(
            'specular_factor', 'light_specular', lambda x: x),
        AttributeConvertInfo('color', 'light_color', gamma_correct),
        AttributeConvertInfo('shadow_color', 'shadow_color', gamma_correct),
    ]
    _omni_attr_conv = [
        AttributeConvertInfo(
            'energy', 'light_energy', lambda x: abs(x / 100.0)),
        AttributeConvertInfo('cutoff_distance', 'omni_range', lambda x: x),
    ]
    _spot_attr_conv = [
        AttributeConvertInfo(
            'energy', 'light_energy', lambda x: abs(x / 100.0)),
        AttributeConvertInfo(
            'spot_size', 'spot_angle', lambda x: math.degrees(x/2)
        ),
        AttributeConvertInfo(
            'spot_blend', 'spot_angle_attenuation', lambda x: 0.2/(x + 0.01)
        ),
        AttributeConvertInfo('cutoff_distance', 'spot_range', lambda x: x),
    ]
    _directional_attr_conv = [
        AttributeConvertInfo('energy', 'light_energy', abs),
    ]

    @property
    def attribute_conversion(self):
        """Get a list of quaternary tuple
        (blender_attr, godot_attr, lambda converter, attr type)"""
        if self.get_type() == 'OmniLight':
            return self._light_attr_conv + self._omni_attr_conv
        if self.get_type() == 'SpotLight':
            return self._light_attr_conv + self._spot_attr_conv
        if self.get_type() == 'DirectionalLight':
            return self._light_attr_conv + self._directional_attr_conv
        return self._light_attr_conv


def export_light_node(escn_file, export_settings, node, parent_gd_node):
    """Exports lights - well, the ones it knows about. Other light types
    just throw a warning"""
    bl_light_to_gd_light = {
        "POINT": "OmniLight",
        "SPOT": "SpotLight",
        "SUN": "DirectionalLight",
    }

    light = node.data
    if light.type in bl_light_to_gd_light:
        light_node = LightNode(
            node.name, bl_light_to_gd_light[light.type], parent_gd_node)
    else:
        light_node = None
        logging.warning(
            "Unknown light type. Use Point, Spot or Sun: %s", node.name
        )

    if light_node is not None:
        for item in light_node.attribute_conversion:
            bl_attr, gd_attr, converter = item
            light_node[gd_attr] = converter(getattr(light, bl_attr))

        # Properties common to all lights
        # These cannot be set via AttributeConvertInfo as it will not handle
        # animations correctly
        light_node['transform'] = fix_directional_transform(node.matrix_local)
        light_node['light_negative'] = light.energy < 0
        light_node['shadow_enabled'] = (
            light.use_shadow and light.cycles.cast_shadow)

        escn_file.add_node(light_node)

    export_animation_data(escn_file, export_settings,
                          light_node, node.data, 'light')

    return light_node
