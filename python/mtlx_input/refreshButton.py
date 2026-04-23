import os
import time
import logging
import imath
import IECore
import Gaffer
import GafferScene
import GafferArnold
import MaterialX as mx
import Shading
import GafferOSL

from DTFGaffer import output as nodeOutput

logging.basicConfig(level=os.environ.get('PIPE_LOGLEVEL', 'INFO').upper(), format="%(levelname)s : [%(name)s] %(message)s")
logger = logging.getLogger(__name__)

PATH_NAME_STRIP_LENGTH = 3


def fix_str(name):
    characters = ":/"
    if "/" in name:
        split_list = name.split("/")
        if len(split_list) >= PATH_NAME_STRIP_LENGTH - 1:
            split_list = split_list[-PATH_NAME_STRIP_LENGTH:]
            name = "_".join(split_list)
    for char in characters:
        name = name.replace(char, "_")
    return str(name)


def material_list(MXnode):
    return [i for i in MXnode.children() if i.isInstanceOf(Gaffer.Box)]


def attribute_list(MXnode):
    return [i for i in MXnode.children() if i.isInstanceOf(GafferArnold.ArnoldAttributes)]


def path_filter_list(MXnode):
    return [i for i in MXnode.children() if i.isInstanceOf(GafferScene.PathFilter)]


def startBuild(MXnode):
    mtlx_doc = mx.createDocument()
    if valid_mtlx(MXnode, mtlx_doc):
        MtlXreader(MXnode, mtlx_doc)
        if look_finder(MXnode, mtlx_doc):
            clear_existing_data(MXnode)
            load_mtlx(MXnode, mtlx_doc)
        else:
            print("error")


def load_mtlx(MXnode, mtlx_doc):
    x = time.time()
    if MXnode["applyMaterials"].getValue():
        setup_materials(MXnode, mtlx_doc)
    if MXnode["applyAssignments"].getValue():
        setup_assignments(MXnode, mtlx_doc, MXnode["mtlXLook"].getValue())
    if MXnode["applyAttributes"].getValue():
        setup_attributes(MXnode, mtlx_doc, MXnode["mtlXLook"].getValue())
    MXnode["status"].setValue("%d Materials loaded in %.2f seconds" % (len(material_list(MXnode)), time.time() - x))


def valid_mtlx(MXnode, mtlx_doc):
    try:
        mx.readFromXmlFile(mtlx_doc, MXnode["mtlXPath"].getValue())
        return True
    except mx.ExceptionFileMissing as err:
        logger.error("File not found: %s" % err)
        return False


def MtlXreader(MXnode, mtlx_doc):
    mx.readFromXmlFile(mtlx_doc, MXnode["mtlXPath"].getValue())


def look_finder(MXnode, mtlx_doc):
    for idx, look in enumerate(mtlx_doc.getLooks()):
        look_name = str(look.getName())
        Gaffer.Metadata.registerPlugValue(MXnode["mtlXLook"], "preset:" + look_name, idx)
    return True


def clear_existing_data(MXnode):
    for material in material_list(MXnode):
        MXnode.removeChild(material)
    for attribute in attribute_list(MXnode):
        MXnode.removeChild(attribute)
    for path_filter in path_filter_list(MXnode):
        MXnode.removeChild(path_filter)


def replaceMX(category):
    """Mapping catégorie MaterialX -> nom shader OSL MaterialX bundled dans Gaffer."""
    dic = {
        'tiledimage'       : 'image_color',
        'image'            : 'image_color',
        'constant'         : 'constant_color',
        'multiply'         : 'mx_multiply_color3',
        'add'              : 'mx_add_color3',
        'mix'              : 'mx_mix_color3',
        'normalmap'        : 'mx_normalmap',
        'convert'          : 'mx_convert',
    }
    return dic.get(category, category)


def _get_shader_inputs_from_material(material_node):
    """Retourne [(input, connected_shader_node), ...] depuis un noeud surfacematerial."""
    results = []
    for inp in material_node.getInputs():
        connected = inp.getConnectedNode()
        if connected is not None:
            results.append((inp, connected))
    return results


def _collect_upstream_nodes(root_node):
    """
    Collecte récursivement tous les noeuds en amont d'un noeud racine,
    en suivant les connexions directes (nodename) ET les NodeGraphs (nodegraph/output).
    Retourne un dict {node_name: mx.Node}.
    MX 1.38+ : traverseGraph() est une méthode d'instance sur le noeud,
    mais elle ne traverse pas les NodeGraphs — on le fait manuellement.
    """
    collected = {}
    visited = set()

    def _recurse_node(node):
        name = node.getName()
        if name in visited:
            return
        visited.add(name)
        collected[fix_str(name)] = node

        for inp in node.getInputs():
            # Connexion directe noeud->noeud
            upstream = inp.getConnectedNode()
            if upstream is not None:
                _recurse_node(upstream)

            # Connexion via NodeGraph : nodegraph="NG_xxx" output="out_color"
            ng_name = inp.getAttribute("nodegraph")
            out_name = inp.getAttribute("output")
            if ng_name and out_name:
                doc = node.getDocument()
                ng = doc.getNodeGraph(ng_name)
                if ng is not None:
                    _recurse_nodegraph(ng, out_name)

    def _recurse_nodegraph(nodegraph, output_name):
        """Collecte les noeuds d'un NodeGraph en partant d'un output donné."""
        ng_name = nodegraph.getName()
        output = nodegraph.getOutput(output_name)
        if output is None:
            return
        # Le noeud connecté à cet output
        upstream = output.getConnectedNode()
        if upstream is not None:
            # Préfixe le nom avec le NodeGraph pour éviter les collisions
            _recurse_node_in_graph(nodegraph, upstream)

    def _recurse_node_in_graph(nodegraph, node):
        ng_prefix = fix_str(nodegraph.getName())
        node_key = ng_prefix + "__" + fix_str(node.getName())
        if node_key in visited:
            return
        visited.add(node_key)
        collected[node_key] = node

        for inp in node.getInputs():
            upstream = inp.getConnectedNode()
            if upstream is not None:
                _recurse_node_in_graph(nodegraph, upstream)

    _recurse_node(root_node)
    return collected


def _get_connection_key(input_parm, root_node):
    """
    Résout le nom de noeud Gaffer cible d'une connexion MX.
    Gère les deux cas :
      - nodename direct  : input_parm.getNodeName()
      - nodegraph/output : input_parm.getAttribute("nodegraph") + output -> noeud
    Retourne (gaffer_node_key, channel_out) ou (None, None).
    """
    # Cas 1 : connexion directe
    node_name = input_parm.getNodeName()
    if node_name:
        return fix_str(node_name), str(input_parm.getAttribute("channels"))

    # Cas 2 : connexion via NodeGraph
    ng_name = input_parm.getAttribute("nodegraph")
    out_name = input_parm.getAttribute("output")
    if ng_name and out_name:
        doc = root_node.getDocument()
        ng = doc.getNodeGraph(ng_name)
        if ng is not None:
            output = ng.getOutput(out_name)
            if output is not None:
                upstream = output.getConnectedNode()
                if upstream is not None:
                    ng_prefix = fix_str(ng_name)
                    node_key = ng_prefix + "__" + fix_str(upstream.getName())
                    return node_key, str(input_parm.getAttribute("channels"))

    return None, None


def setup_materials(MXnode, mtlx_doc):
    x = time.time()
    shader_count = 0

    material_nodes = [
        n for n in mtlx_doc.getNodes()
        if n.getCategory() in ("surfacematerial", "volumematerial", "material")
    ]

    for material in material_nodes:

        material_name = fix_str(material.getName())
        box_in  = Gaffer.BoxIn()
        box_out = Gaffer.BoxOut()
        material_box = Gaffer.Box(material_name)

        for mat_input, shader_node in _get_shader_inputs_from_material(material):

            shader_name     = fix_str(shader_node.getName())
            shader_category = shader_node.getCategory()

            shader = GafferArnold.ArnoldShader(shader_name)
            shader.loadShader(shader_category)

            shader_assignment = GafferScene.ShaderAssignment()
            shader_assignment["shader"].setInput(shader["out"])

            box_in.setup(shader_assignment["in"])
            box_out.setup(shader_assignment["out"])
            shader_assignment["in"].setInput(box_in["out"])
            box_out["in"].setInput(shader_assignment["out"])

            path_filter = GafferScene.PathFilter()
            shader_assignment["filter"].setInput(path_filter["out"])

            if mat_input.getName() == "displacementshader" or shader_category == "displacement":
                dsp_shader = GafferArnold.ArnoldDisplacement()
                shader_assignment["shader"].setInput(dsp_shader["out"])
                dsp_shader["map"].setInput(shader["out"])
                material_box.addChild(dsp_shader)

            material_box.addChild(shader)
            material_box.addChild(box_in)
            material_box.addChild(box_out)
            material_box.addChild(path_filter)
            material_box.addChild(shader_assignment)

            box_in.setupPromotedPlug()
            box_out.setupPromotedPlug()

            mtl_list = material_list(MXnode)
            if mtl_list:
                if material_box != mtl_list[-1]:
                    material_box["in"].setInput(mtl_list[-1]["out"])
                mtl_list[0]["in"].setInput(MXnode["in"])
                MXnode["out"].setInput(material_box["out"])
            MXnode.addChild(material_box)

            # Valeurs des inputs du shader root
            for input_parm in shader_node.getInputs():
                value = input_parm.getValue()
                if value is not None and not input_parm.getNodeName() and not input_parm.getAttribute("nodegraph"):
                    shader_parm = shader["parameters"]
                    input_name  = str(input_parm.getName())
                    if input_name in shader_parm:
                        set_input_value(shader_parm[input_name], value)

            # ------------------------------------------------------------------
            # Collecte et création de tous les noeuds upstream
            # (connexions directes + NodeGraphs)
            # ------------------------------------------------------------------
            upstream_nodes = _collect_upstream_nodes(shader_node)
            # Retire le shader root lui-même (déjà créé)
            upstream_nodes.pop(shader_name, None)

            for node_key, node in upstream_nodes.items():

                shader_list = [i.getName() for i in material_box.children()
                               if isinstance(i, (GafferOSL.OSLShader, GafferArnold.ArnoldShader, Shading.OslRamp))]

                if node_key not in shader_list:

                    category = node.getCategory()

                    if category in ("ramp_rgb", "ramp_float"):
                        child_shader = Shading.OslRamp(node_key)
                    else:
                        try:
                            child_shader = GafferArnold.ArnoldShader(node_key)
                            child_shader.loadShader(category)
                        except Exception:
                            child_shader = GafferOSL.OSLShader(node_key)
                            child_shader.loadShader("MaterialX/mx_%s" % replaceMX(category))

                    material_box.addChild(child_shader)
                    shader_count += 1

                    # Valeurs des inputs
                    posSpline = []; colSpline = []; floatSpline = []
                    colorRamp = []; floatRamp = []

                    for input_parm in node.getInputs():
                        input_name = str(input_parm.getName())

                        if category not in ("ramp_rgb", "ramp_float"):
                            try:
                                shader_parm = child_shader["parameters"]
                            except Exception:
                                shader_parm = child_shader
                            if input_name in shader_parm:
                                value = input_parm.getValue()
                                if value is not None:
                                    set_input_value(shader_parm[input_name], value)

                        elif category == "ramp_rgb":
                            child_shader["outType"].setValue(0)
                            if input_name not in ("position", "color") and input_name in child_shader:
                                value = input_parm.getValue()
                                if value is not None:
                                    set_input_value(child_shader[input_name], value)
                            if input_name == "position":
                                posSpline.append(input_parm.getValue())
                            if input_name == "color":
                                value = input_parm.getValue()
                                value_list = [value[x:x+3] for x in range(0, len(value), 3)]
                                colSpline.append([imath.Color3f(v) for v in value_list])
                            if colSpline and posSpline:
                                colorRamp.append(map(lambda x, y: (x, y), posSpline[0], colSpline[0]))

                        elif category == "ramp_float":
                            child_shader["outType"].setValue(1)
                            if input_name == "position":
                                posSpline.append(input_parm.getValue())
                            if input_name == "value":
                                floatSpline.append(input_parm.getValue())
                            if floatSpline and posSpline:
                                floatRamp.append(map(lambda x, y: (x, y), posSpline[0], floatSpline[0]))

                    if colorRamp:
                        tup = tuple(colorRamp[0])
                        sv = Gaffer.SplineDefinitionfColor3f(tup, Gaffer.SplineDefinitionInterpolation.CatmullRom)
                        child_shader["ramp"].clearPoints()
                        child_shader["ramp"].setValue(sv)
                    elif floatRamp:
                        tup = tuple(floatRamp[0])
                        sv = Gaffer.SplineDefinitionff(tup, Gaffer.SplineDefinitionInterpolation.CatmullRom)
                        child_shader["framp"].clearPoints()
                        child_shader["framp"].setValue(sv)

    # --------------------------------------------------------------------------
    # Connexions
    # --------------------------------------------------------------------------
    for material in material_nodes:

        material_box = MXnode[fix_str(material.getName())]

        for mat_input, shader_node in _get_shader_inputs_from_material(material):

            shader_name = fix_str(shader_node.getName())
            shader = material_box[shader_name]
            try:
                shader_parm = shader["parameters"]
            except Exception:
                shader_parm = shader

            # Connexions des inputs du shader root
            for input_parm in shader_node.getInputs():
                input_name = str(input_parm.getName())
                node_key, channel_out = _get_connection_key(input_parm, shader_node)
                if node_key and input_name in shader_parm:
                    try:
                        set_input_connection(shader_parm[input_name],
                                             nodeOutput(material_box[node_key]),
                                             channel_out or "")
                    except Exception as e:
                        logger.warning("Connection failed %s -> %s : %s" % (node_key, input_name, e))

            # Connexions dans les noeuds upstream
            upstream_nodes = _collect_upstream_nodes(shader_node)
            upstream_nodes.pop(shader_name, None)

            for node_key, node in upstream_nodes.items():
                try:
                    up_shader = material_box[node_key]
                except Exception:
                    continue
                try:
                    shader_parm = up_shader["parameters"]
                except Exception:
                    shader_parm = up_shader

                for input_parm in node.getInputs():
                    input_name = str(input_parm.getName())
                    target_key, channel_out = _get_connection_key(input_parm, node)
                    if target_key and input_name in shader_parm:
                        try:
                            set_input_connection(shader_parm[input_name],
                                                 nodeOutput(material_box[target_key]),
                                                 channel_out or "")
                        except Exception as e:
                            logger.warning("Connection failed %s -> %s : %s" % (target_key, input_name, e))

    logger.info("%s Loaded %d shaders in %.2f seconds" % (MXnode.getName(), shader_count, time.time() - x))


def setup_assignments(MXnode, mtlx_doc, look_idx=0):
    x = time.time()
    assign_count = 0
    MXnode["mtlXLook"].setValue(look_idx)

    if mtlx_doc is None:
        if not valid_mtlx(MXnode, mx.createDocument()):
            return

    for idx, look in enumerate(mtlx_doc.getLooks()):
        look_name = str(look.getName())
        Gaffer.Metadata.registerPlugValue(MXnode["mtlXLook"], "preset:" + look_name, idx)

        if look_idx == idx:
            mtl_list = material_list(MXnode)
            for mat in mtl_list:
                mat["PathFilter"]["paths"].setValue(IECore.StringVectorData([]))

            for mat_assign in look.getMaterialAssigns():
                referenced = mat_assign.getMaterial()
                if referenced is None:
                    continue
                mat_assign_name = fix_str(referenced)
                for mat in mtl_list:
                    if mat_assign_name == mat.getName():
                        value = mat["PathFilter"]["paths"].getValue()
                        if value is not None:
                            geom_name  = mat_assign.getGeom()
                            split_name = geom_name.split("/")
                            if split_name:
                                value.append(geom_name.replace(split_name[-1], ""))
                                mat["PathFilter"]["paths"].setValue(value)
                                assign_count += 1

    logger.info("%s Loaded %d assignments in %.2f seconds" % (MXnode.getName(), assign_count, time.time() - x))


def setup_attributes(MXnode, mtlx_doc, look_idx=0):
    x = time.time()
    attribute_count = 0
    MXnode["mtlXLook"].setValue(look_idx)

    if mtlx_doc is None:
        if not valid_mtlx(MXnode, mx.createDocument()):
            return

    for idx, look in enumerate(mtlx_doc.getLooks()):
        if look_idx != idx:
            continue

        mtl_list = material_list(MXnode)
        attribute_assignment = None

        for vis_idx, visibility in enumerate(look.getVisibilities()):
            attrib_list = attribute_list(MXnode)

            if vis_idx % 8 == 0:
                attribute_assignment = GafferArnold.ArnoldAttributes()
                path_filter = GafferScene.PathFilter()
                attribute_assignment["filter"].setInput(path_filter["out"])

                if attrib_list:
                    attribute_assignment["in"].setInput(attrib_list[-1]["out"])
                elif mtl_list:
                    attribute_assignment["in"].setInput(mtl_list[-1]["out"])
                else:
                    attribute_assignment["in"].setInput(MXnode["in"])

                path_filter["paths"].setValue(IECore.StringVectorData([visibility.getGeom()]))
                MXnode.addChild(attribute_assignment)
                MXnode.addChild(path_filter)
                attribute_count += 1

            vis_type   = visibility.getVisibilityType()
            is_visible = visibility.getVisible()
            attributes = attribute_assignment["attributes"]

            vis_map = {
                "camera"            : "cameraVisibility",
                "shadow"            : "shadowVisibility",
                "diffuse_transmit"  : "diffuseTransmissionVisibility",
                "specular_transmit" : "specularTransmissionVisibility",
                "volume"            : "volumeVisibility",
                "diffuse_reflect"   : "diffuseReflectionVisibility",
                "specular_reflect"  : "specularReflectionVisibility",
                "subsurface"        : "subsurfaceVisibility",
            }
            if vis_type in vis_map:
                attr_name = vis_map[vis_type]
                attributes[attr_name]["enabled"].setValue(True)
                attributes[attr_name]["value"].setValue(is_visible)

        if attribute_assignment is not None:
            MXnode["out"].setInput(attribute_assignment["out"])

    logger.info("%s Loaded %d attributes in %.2f seconds" % (MXnode.getName(), attribute_count, time.time() - x))


def set_input_value(input_plug, value):
    assert(input_plug.isInstanceOf(Gaffer.Plug))
    try:
        if input_plug.isInstanceOf(Gaffer.Color3fPlug):
            input_plug.setValue(imath.Color3f(value[0], value[1], value[2]))
        elif input_plug.isInstanceOf(Gaffer.Color4fPlug):
            input_plug.setValue(imath.Color4f(value[0], value[1], value[2], value[3]))
        elif input_plug.isInstanceOf(Gaffer.V3fPlug):
            input_plug.setValue(imath.V3f(value[0], value[1], value[2]))
        else:
            input_plug.setValue(value)
    except Exception as err:
        logger.warning("Failed to set value %s -> %s\n%s" % (input_plug, value, err))


def set_input_connection(input_plug, output_plug, ch_out=""):
    assert(input_plug.isInstanceOf(Gaffer.Plug))
    assert(output_plug.isInstanceOf(Gaffer.Plug))
    try:
        float_types = (Gaffer.FloatPlug, Gaffer.IntPlug)
        color_types = (Gaffer.Color3fPlug, Gaffer.Color4fPlug)

        out_is_float  = output_plug.isInstanceOf(float_types[0]) or output_plug.isInstanceOf(float_types[1])
        out_is_color  = output_plug.isInstanceOf(color_types[0]) or output_plug.isInstanceOf(color_types[1])
        out_is_v3f    = output_plug.isInstanceOf(Gaffer.V3fPlug)
        in_is_float   = input_plug.isInstanceOf(float_types[0])  or input_plug.isInstanceOf(float_types[1])
        in_is_color   = input_plug.isInstanceOf(color_types[0])  or input_plug.isInstanceOf(color_types[1])
        in_is_v3f     = input_plug.isInstanceOf(Gaffer.V3fPlug)

        if out_is_float and in_is_color:
            input_plug[input_plug.keys()[0]].setInput(output_plug)
        elif in_is_float and out_is_color:
            ch = ch_out if ch_out else output_plug.keys()[0]
            input_plug.setInput(output_plug[ch])
        elif in_is_float and out_is_v3f:
            ch = ch_out if ch_out else output_plug.keys()[0]
            input_plug.setInput(output_plug[ch])
        elif out_is_float and in_is_v3f:
            input_plug[input_plug.keys()[0]].setInput(output_plug)
        elif (in_is_color or in_is_v3f) and (out_is_color or out_is_v3f):
            # Connexion composante par composante si types mixtes
            in_keys  = input_plug.keys()
            out_keys = output_plug.keys()
            for i in range(min(3, len(in_keys), len(out_keys))):
                input_plug[in_keys[i]].setInput(output_plug[out_keys[i]])
        else:
            input_plug.setInput(output_plug)
    except Exception as err:
        logger.warning("Failed to connect %s -> %s\n%s" % (output_plug, input_plug, err))


MXnode = plug.node()
startBuild(MXnode)
