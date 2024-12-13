import qrenderdoc as qrd

# This extension is a completely empty example showing the bare bones of what
# is needed to register an extension with RenderDoc and get a callback to run
# custom code.

# For more information see:
# https://renderdoc.org/docs/how/how_python_extension.html
#
# for the process of registering this extension with the UI, and:
# https://renderdoc.org/docs/python_api/ui_extensions.html
#
# for information on how to get started accessing the python API.

def register(version: str, ctx: qrd.CaptureContext):
    print("Registering empty extension")

def unregister():
    print("Unregistering empty extension")
