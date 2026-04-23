import os

import IECore
import Gaffer
import GafferScene

MtlXfolderPath = os.path.dirname(__file__)


class MtlXInputSerialiser(Gaffer.NodeSerialiser):
    """
    MtlXInput Serializer
    """
    def childNeedsSerialisation(self, child, serialisation):
        """
        Implementation of native method
        @param child: MtlXInput
        @param serialisation: Gaffer.Serialisation
        @return: 
        """
        if isinstance(child, Gaffer.Node):
            return True

        return Gaffer.NodeSerialiser.childNeedsSerialisation(self, child, serialisation)

    def childNeedsConstruction(self, child, serialisation):
        """
        Implementation of native method
        @param child: MtlXInput
        @param serialisation: Gaffer.Serialisation
        @return:
        """
        if isinstance(child, Gaffer.Node):
            return True

        return Gaffer.NodeSerialiser.childNeedsConstruction(self, child, serialisation)


class MtlXInput(GafferScene.SceneNode):
    """
    MaterialX Reader Node
    """
    def __init__(self, name="MtlXInput"):
        """
        @param name: str
        """
        GafferScene.SceneNode.__init__(self, name)

        # Status string
        self.status = str()

        # MaterialX document object
        self.mtlx_doc = None

        self["mtlXPath"] = Gaffer.StringPlug()
        self["refresh"]  = Gaffer.IntPlug("refresh", defaultValue = 0, flags = Gaffer.Plug.Flags.Default | Gaffer.Plug.Flags.Dynamic, )
        self["mtlXLook"] = Gaffer.IntPlug()
        self["status"]   = Gaffer.StringPlug()

        self["applyMaterials"]   = Gaffer.BoolPlug(defaultValue=True)
        self["applyAssignments"] = Gaffer.BoolPlug(defaultValue=True)
        self["applyAttributes"]  = Gaffer.BoolPlug(defaultValue=True)

        self["in"]  = GafferScene.ScenePlug("in", Gaffer.Plug.Direction.In, flags=Gaffer.Plug.Flags.Default)
        self["out"] = GafferScene.ScenePlug("out", Gaffer.Plug.Direction.Out)
        self["out"].setInput(self["in"])



IECore.registerRunTimeTyped(MtlXInput, typeName="MaterialX.LookLoader")



with open(os.path.join(MtlXfolderPath, "refreshButton.py"), 'r') as file:
    MtlXClicExp = file.read()
    Gaffer.Metadata.registerNode(

        MtlXInput,
        "description",

        """
        MaterialX Reader
        """,

        "icon", os.path.join(MtlXfolderPath, "icon/MaterialXLogoSmallA.png"),
        "graphEditor:childrenViewable", True,

        plugs = { "mtlXPath" : ["description",

                                """
                                MaterialX File Path
                                """,

                                "plugValueWidget:type", "GafferUI.FileSystemPathPlugValueWidget",
                                "path:leaf", True,
                                "path:valid", True,
                                "fileSystemPath:extensions", "mtlx",
                                "fileSystemPath:extensionsLabel", "Show only .mtlx files",
                                ],

                   "refresh" : [ "description",
                                """
                                May be incremented to force a reload if the file has
                                changed on disk - otherwise old contents may still
                                be loaded via Gaffer's cache.
                                """,
                                'nodule:type', '',
                                "plugValueWidget:type", "GafferUI.ButtonPlugValueWidget",
                                'buttonPlugValueWidget:clicked', '%s' %MtlXClicExp,
                                "layout:label", "",
                                "layout:accessory", True,
                                ],

                   "mtlXLook" : ["description",'The Look',
                                "plugValueWidget:type", "GafferUI.PresetsPlugValueWidget"
                                ],

                }
        )


Gaffer.Serialisation.registerSerialiser(MtlXInput, MtlXInputSerialiser())


def init(application):
    import GafferUI
    node_menu = GafferUI.NodeMenu.acquire(application)
    node_menu.append("/MaterialX/MtlXInput", lambda: MtlXInput(), searchText="MaterialXInput")