# Custom shader templates
 
These are simple shader templates. All they do is invert (1 - x) the sampled texture data, but they handle all APIs and texture types correctly so it can be useful as a template to build a shader from.

__NOTE__: These shaders are based on the new binding abstraction macros added in v1.19. See the history on this directory for templates that worked on previous versions, though note that e.g. the HLSL template will *not* work on Vulkan.
