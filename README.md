# RenderDoc community contributed extensions

This repository contains UI extensions and custom display shaders that have been written by the community for [RenderDoc](https://github.com/baldurk/renderdoc).

Note that by design the code here is directly contributed by the community and is **not** written by the RenderDoc authors. Ensure that you trust the extension authors or verify that the code doesn't do anything dangerous, since unless it is obvious the code is **not** vetted for security or safety.

# Installing

To install, you can download or clone this project under your `qrenderdoc` settings folder in an `extensions` subdirectory. For example:

* Windows: `%APPDATA%\qrenderdoc\extensions\renderdoc-contrib`
* Linux: `~/.local/share/qrenderdoc/extensions/renderdoc-contrib`

RenderDoc will populate all compatible extensions on next startup, and you can manage these from `Tools` &rarr; `Manage Extensions`. Extensions can be loaded for a single session by clicking 'load', and loaded for all sessions by then selecting 'always load'.

For more information on setting up extensions consult the [RenderDoc documentation](https://renderdoc.org/docs/how/how_python_extension.html). For writing extensions there is [specific documentation](https://renderdoc.org/docs/python_api/ui_extensions.html).

To use a custom shader you can either configure a new path in the settings window under `Texture Viewer` and `Custom shader directories`, or else copy the file you want to use into your `qrenderdoc` settings folder as above (the parent folder of the `extensions` subdirectory).

# Contributing

Community-written extensions are welcome and this repository is intended to collate them so more people can find useful extensions. You can open a PR to add or update your extension and it will be merged here as soon as possible.

For more information on contributing see [the contributing guidelines](docs/CONTRIBUTING.md)
