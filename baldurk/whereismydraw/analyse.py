###############################################################################
# The MIT License (MIT)
#
# Copyright (c) 2021 Baldur Karlsson
#
# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:
#
# The above copyright notice and this permission notice shall be included in
# all copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN
# THE SOFTWARE.
###############################################################################

import qrenderdoc as qrd
import renderdoc as rd
import struct
import math


class AnalysisFinished(Exception):
    pass


class Analysis:
    # Do the expensive analysis on the replay thread
    def __init__(self, ctx: qrd.CaptureContext, eid: int, r: rd.ReplayController):
        self.analysis_steps = []
        self.ctx = ctx
        self.eid = eid
        self.r = r

        print("On replay thread, analysing eid {} with current event {}".format(self.eid, self.ctx.CurEvent()))

        self.r.SetFrameEvent(self.eid, True)

        self.drawcall = self.ctx.GetDrawcall(self.eid)
        self.api_properties = self.r.GetAPIProperties()
        self.textures = self.r.GetTextures()
        self.api = self.api_properties.pipelineType

        self.pipe = self.r.GetPipelineState()
        self.glpipe = self.vkpipe = self.d3d11pipe = self.d3d12pipe = None
        if self.api == rd.GraphicsAPI.OpenGL:
            self.glpipe = self.r.GetGLPipelineState()
        elif self.api == rd.GraphicsAPI.Vulkan:
            self.vkpipe = self.r.GetVulkanPipelineState()
        elif self.api == rd.GraphicsAPI.D3D11:
            self.d3d11pipe = self.r.GetD3D11PipelineState()
        elif self.api == rd.GraphicsAPI.D3D12:
            self.d3d12pipe = self.r.GetD3D12PipelineState()

        self.vert_ndc = []

        # Enumerate all bound targets, with depth last
        self.targets = [t for t in self.pipe.GetOutputTargets() if t.resourceId != rd.ResourceId.Null()]
        self.depth = self.pipe.GetDepthTarget()
        if self.depth.resourceId != rd.ResourceId.Null():
            self.targets.append(self.depth)

        dim = (1, 1)
        self.target_descs = []
        for t in self.targets:
            desc = self.get_tex(t.resourceId)
            self.target_descs.append(desc)
            w = max(dim[0], desc.width)
            h = max(dim[1], desc.height)
            dim = (w, h)

        self.postvs_stage = rd.MeshDataStage.GSOut
        if self.pipe.GetShader(rd.ShaderStage.Geometry) == rd.ResourceId.Null() and self.pipe.GetShader(
                rd.ShaderStage.Hull) == rd.ResourceId.Null():
            self.postvs_stage = rd.MeshDataStage.VSOut

        # Gather all the postvs positions together
        self.postvs_positions = []
        for inst in range(max(1, self.drawcall.numInstances)):
            for view in range(max(1, self.pipe.MultiviewBroadcastCount())):
                postvs = self.r.GetPostVSData(inst, view, self.postvs_stage)
                pos_data = self.r.GetBufferData(postvs.vertexResourceId, postvs.vertexByteOffset,
                                                postvs.vertexByteStride * postvs.numIndices)
                for vert in range(postvs.numIndices):
                    if vert * postvs.vertexByteStride + 16 < len(pos_data):
                        self.postvs_positions.append(struct.unpack_from("4f", pos_data, vert * postvs.vertexByteStride))

        self.vert_ndc = [(vert[0] / vert[3], vert[1] / vert[3], vert[2] / vert[3]) for vert in self.postvs_positions]

        # Create a temporary offscreen output we'll use for
        self.out = self.r.CreateOutput(rd.CreateHeadlessWindowingData(dim[0], dim[1]), rd.ReplayOutputType.Texture)

        self.tex_display = rd.TextureDisplay()

        # We're not actually trying to catch exceptions here, we just want a finally: to shutdown the output
        try:
            self.analysis_steps = []

            # If there are no targets at all, stop as there's no rendering we can analyse
            if self.targets[0].resourceId == rd.ResourceId.Null():
                self.analysis_steps.append({
                    'msg': 'No output render targets or depth target are bound at {}.'.format(self.eid)
                })

                raise AnalysisFinished

            self.check_draw()
        except AnalysisFinished:
            pass
        finally:
            self.out.Shutdown()

    def check_draw(self):
        # Render a highlight overlay on the first target. If no color targets are bound this will be the depth
        # target.
        self.tex_display.resourceId = self.targets[0].resourceId
        self.tex_display.subresource.mip = self.targets[0].firstMip
        self.tex_display.subresource.slice = self.targets[0].firstSlice
        self.tex_display.typeCast = self.targets[0].typeCast
        self.tex_display.scale = -1.0
        texmin, texmax = self.r.GetMinMax(self.tex_display.resourceId, self.tex_display.subresource,
                                          self.tex_display.typeCast)

        comp_type = self.tex_display.typeCast
        if comp_type is rd.CompType.Typeless:
            comp_type = self.target_descs[0].format.compType

        if comp_type == rd.CompType.SInt:
            self.tex_display.rangeMin = float(min([texmin.intValue[x] for x in range(4)]))
            self.tex_display.rangeMax = float(max([texmax.intValue[x] for x in range(4)]))
        elif comp_type == rd.CompType.UInt:
            self.tex_display.rangeMin = float(min([texmin.uintValue[x] for x in range(4)]))
            self.tex_display.rangeMax = float(max([texmax.uintValue[x] for x in range(4)]))
        else:
            self.tex_display.rangeMin = min([texmin.floatValue[x] for x in range(4)])
            self.tex_display.rangeMax = max([texmax.floatValue[x] for x in range(4)])

        texmin, texmax = self.get_overlay_minmax(self.tex_display, rd.DebugOverlay.Drawcall)

        if texmax.floatValue[0] < 0.5:
            self.check_offscreen()
        else:
            self.check_onscreen()

        # If we got here, we didn't find a specific problem! Add a note about that
        self.analysis_steps.append({
            'msg': 'Sorry, I couldn\'t figure out what was wrong! Please report an issue to see if this is '
                   'something that should be added to my checks. You can see what I checked by clicking through '
                   'the steps.',
        })

        raise AnalysisFinished

    def get_overlay_minmax(self, tex_display, overlay: rd.DebugOverlay):
        tex_display.overlay = overlay
        self.out.SetTextureDisplay(tex_display)
        overlay = self.out.GetDebugOverlayTexID()
        return self.r.GetMinMax(overlay, rd.Subresource(), rd.CompType.Typeless)

    def check_onscreen(self):
        self.analysis_steps.append({
            'msg': 'The highlight drawcall overlay shows the draw, meaning it is rendering but failing some '
                   'tests.',
            # copy the TextureDisplay object so we can modify it without changing the one in this step
            'tex_display': rd.TextureDisplay(self.tex_display),
        })

        # It's on-screen we debug the rasterization/testing/blending states
        texmin, texmax = self.get_overlay_minmax(self.tex_display, rd.DebugOverlay.BackfaceCull)

        # If there are no green pixels at all, this completely failed
        if texmax.floatValue[1] < 0.5:
            self.check_failed_backface_culling()

            # Regardless of whether we finihsed the analysis above, don't do any more checking.
            raise AnalysisFinished

        texmin, texmax = self.get_overlay_minmax(self.tex_display, rd.DebugOverlay.Depth)

        # If there are no green pixels at all, this completely failed
        if texmax.floatValue[1] < 0.5:
            self.check_failed_depth()

            # Regardless of whether we finihsed the analysis above, don't do any more checking.
            raise AnalysisFinished

        texmin, texmax = self.get_overlay_minmax(self.tex_display, rd.DebugOverlay.Stencil)

        # If there are no green pixels at all, this completely failed
        if texmax.floatValue[1] < 0.5:
            self.check_failed_stencil()

            # Regardless of whether we finihsed the analysis above, don't do any more checking.
            raise AnalysisFinished

    def check_offscreen(self):
        self.analysis_steps.append({
            'msg': 'The highlight drawcall overlay shows nothing for this draw, meaning it is off-screen.',
        })

        # Check rasterizer discard state
        if (self.glpipe and self.glpipe.vertexProcessing.discard) or (
                self.vkpipe and self.vkpipe.rasterizer.rasterizerDiscardEnable):
            self.analysis_steps.append({
                'msg': 'Rasterizer discard is enabled. This API state disables rasterization for the drawcall.',
                # copy the TextureDisplay object so we can modify it without changing the one in this step
                'pipe_stage': qrd.PipelineStage.Rasterizer,
            })

            raise AnalysisFinished

        # TODO It's off-screen, we need to debug the transformation pipeline up to rasterizer state

    def check_failed_backface_culling(self):
        cull_mode = rd.CullMode.NoCull
        if self.api == rd.GraphicsAPI.OpenGL:
            cull_mode = self.glpipe.rasterizer.state.cullMode
        elif self.api == rd.GraphicsAPI.Vulkan:
            cull_mode = self.vkpipe.rasterizer.cullMode
        elif self.api == rd.GraphicsAPI.D3D11:
            cull_mode = self.d3d11pipe.rasterizer.state.cullMode
        elif self.api == rd.GraphicsAPI.D3D12:
            cull_mode = self.d3d12pipe.rasterizer.state.cullMode

        self.analysis_steps.append({
            'msg': 'The backface culling overlay shows red, so the draw is completely backface culled.\n\n'
                   'Check your polygon winding and front-facing state ({}).'.format(cull_mode),
            # copy the TextureDisplay object so we can modify it without changing the one in this step
            'tex_display': rd.TextureDisplay(self.tex_display),
            'pipe_stage': qrd.PipelineStage.Rasterizer,
        })

        raise AnalysisFinished

    def check_failed_stencil(self):
        self.analysis_steps.append({
            'msg': 'The stencil test overlay shows red, so the draw is completely failing a stencil test.',
            # copy the TextureDisplay object so we can modify it without changing the one in this step
            'tex_display': rd.TextureDisplay(self.tex_display),
        })

        # TODO: Check the stencil test state, last depth clear value, see if this draw is just occluded
        #  or if there's an obvious mistake in the state

    def check_failed_depth(self):
        self.analysis_steps.append({
            'msg': 'The depth test overlay shows red, so the draw is completely failing a depth test.',
            # copy the TextureDisplay object so we can modify it without changing the one in this step
            'tex_display': rd.TextureDisplay(self.tex_display),
        })

        # Gather API-specific state
        depth_func = rd.CompareFunction.AlwaysTrue
        ndc_bounds = [0.0, 1.0]
        depth_bounds = []
        depth_clamp = True
        if self.api == rd.GraphicsAPI.OpenGL:
            if self.glpipe.depthState.depthBounds:
                depth_bounds = [self.glpipe.depthState.nearBound, self.glpipe.depthState.farBound]
            depth_func = self.glpipe.depthState.depthFunction
            depth_clamp = self.glpipe.rasterizer.state.depthClamp
            if self.glpipe.vertexProcessing.clipNegativeOneToOne:
                ndc_bounds = [-1.0, 1.0]
        elif self.api == rd.GraphicsAPI.Vulkan:
            if self.vkpipe.depthStencil.depthBoundsEnable:
                depth_bounds = [self.vkpipe.depthStencil.minDepthBounds,
                                self.vkpipe.depthStencil.maxDepthBounds]
            depth_func = self.vkpipe.depthStencil.depthFunction
            depth_clamp = self.vkpipe.rasterizer.depthClampEnable
        elif self.api == rd.GraphicsAPI.D3D11:
            depth_func = self.d3d11pipe.outputMerger.depthStencilState.depthFunction
            depth_clamp = not self.d3d11pipe.rasterizer.state.depthClip
        elif self.api == rd.GraphicsAPI.D3D12:
            if self.d3d12pipe.outputMerger.depthStencilState.depthBoundsEnable:
                depth_bounds = [self.d3d12pipe.outputMerger.depthStencilState.minDepthBounds,
                                self.d3d12pipe.outputMerger.depthStencilState.maxDepthBounds]
            depth_func = self.d3d12pipe.outputMerger.depthStencilState.depthFunction
            depth_clamp = not self.d3d12pipe.rasterizer.state.depthClip

        # Check for state setups that will always fail
        if depth_func == rd.CompareFunction.Never:
            self.analysis_steps.append({
                'msg': 'Depth test is set to Never, meaning it always fails for this draw.',
                'pipe_stage': qrd.PipelineStage.DepthTest,
            })

            raise AnalysisFinished

        # Calculate the min/max NDC bounds of the vertices in z
        vert_ndc_z = list(filter(lambda _: math.isfinite(_), [vert[2] for vert in self.vert_ndc]))
        vert_bounds = [min(vert_ndc_z), max(vert_ndc_z)]

        # if depth clipping is enabled (aka depth clamping is disabled)
        if not depth_clamp:
            state_name = 'Depth Clip' if rd.IsD3D(self.api) else 'Depth Clamp'

            # If the largest vertex NDC z is lower than the NDC range, the whole draw is near-plane clipped
            if vert_bounds[1] <= ndc_bounds[0]:
                self.analysis_steps.append({
                    'msg': 'All of the drawcall vertices are in front of the near plane, and the '
                           'current {} state means these vertices get clipped.'.format(state_name),
                    'pipe_stage': qrd.PipelineStage.Rasterizer,
                    'mesh_view': self.postvs_stage,
                })

                raise AnalysisFinished

            # Same for the smallest z being above the NDC range
            if vert_bounds[0] >= ndc_bounds[1]:
                self.analysis_steps.append({
                    'msg': 'All of the drawcall vertices are behind the far plane, and the '
                           'current {} state means these vertices get clipped.'.format(state_name),
                    'pipe_stage': qrd.PipelineStage.Rasterizer,
                    'mesh_view': self.postvs_stage,
                })

                raise AnalysisFinished

        # If the vertex NDC z range does not intersect the depth bounds range, and depth bounds test is
        # enabled, the draw fails the depth bounds test
        if depth_bounds and (vert_bounds[0] > depth_bounds[1] or vert_bounds[1] < depth_bounds[0]):
            self.analysis_steps.append({
                'msg': 'All of the drawcall vertices are outside the depth bounds range ({} to {}), '
                       'which is enabled'.format(depth_bounds[0], depth_bounds[1]),
                'pipe_stage': qrd.PipelineStage.Rasterizer,
                'mesh_view': self.postvs_stage,
            })

            raise AnalysisFinished

        # If no depth buffer is bound, all APIs spec that depth test should always pass! This seems
        # quite strange.
        if self.depth.resourceId == rd.ResourceId.Null():
            self.analysis_steps.append({
                'msg': 'No depth buffer is bound! Normally this means the depth-test should always '
                       'pass.\n\n'
                       'Sorry I couldn\'t figure out the exact problem. Please check your depth test '
                       'setup and report an issue so we can narrow this down in future.',
                'pipe_stage': qrd.PipelineStage.DepthTest,
            })

            raise AnalysisFinished

        # Get the last clear of the current depth buffer
        usage = self.r.GetUsage(self.depth.resourceId)

        # Filter for clears before this event
        usage = [u for u in usage if u.eventId < self.eid and u.usage == rd.ResourceUsage.Clear]

        # If there's a prior clear
        if len(usage) > 0:
            clear_eid = usage[-1].eventId

            self.r.SetFrameEvent(clear_eid, True)

            # On GL the scissor test affects clears, check that
            if self.api == rd.GraphicsAPI.OpenGL:
                tmp_glpipe = self.r.GetGLPipelineState()
                s = tmp_glpipe.rasterizer.scissors[0]
                if s.enabled:
                    v = self.pipe.GetViewport(0)

                    s_right = s.x + s.width
                    s_bottom = s.y + s.height
                    v_right = v.x + v.width
                    v_bottom = v.y + v.height

                    # if the scissor is empty or outside the size of the target that's certainly not intentional.
                    if (s.width == 0 or s.height == 0 or s.x >= self.target_descs[-1].width or
                            s.y >= self.target_descs[-1].height):
                        self.analysis_steps.append({
                            'msg': 'The last depth clear of {} at {} had scissor enabled, but the scissor rect '
                                   '{},{} to {},{} doesn\'t cover the depth target so it won\'t get cleared.'
                                   .format(str(self.depth.resourceId), clear_eid, s.x, s.y, s_right, s_bottom),
                            'pipe_stage': qrd.PipelineStage.ViewportsScissors,
                        })

                    # if the clear's scissor doesn't overlap the viewport at the time of the draw,
                    # warn the user
                    elif v.x < s.x or v.y < s.y or v.x + v_right or v_bottom > s_bottom:
                        self.analysis_steps.append({
                            'msg': 'The last depth clear of {} at {} had scissor enabled, but the scissor rect '
                                   '{},{} to {},{} is smaller than the current viewport {},{} to {},{}. '
                                   'This may mean not every pixel was properly cleared.'
                                   .format(str(self.depth.resourceId), clear_eid, s.x, s.y, s_right, s_bottom, v.x, v.y,
                                           v_right, v_bottom),
                            'pipe_stage': qrd.PipelineStage.ViewportsScissors,
                        })

            # If this was a clear then we expect the depth value to be uniform, so pick the pixel to
            # get the depth clear value.
            clear_color = self.r.PickPixel(self.depth.resourceId, 0, 0,
                                           rd.Subresource(self.depth.firstMip, self.depth.firstSlice, 0),
                                           self.depth.typeCast)

            self.r.SetFrameEvent(self.eid, True)

            if clear_eid > 0 and (
                    clear_color.floatValue[0] == 1.0 and depth_func == rd.CompareFunction.Greater) or (
                    clear_color.floatValue[0] == 0.0 and depth_func == rd.CompareFunction.Less):
                self.analysis_steps.append({
                    'msg': 'The last depth clear of {} at EID {} cleared depth to {:.4}, but the depth comparison '
                           'function is {} which is impossible to pass.'.format(str(self.depth.resourceId), clear_eid,
                                                                                clear_color.floatValue[0], depth_func),
                    'pipe_stage': qrd.PipelineStage.DepthTest,
                })

                raise AnalysisFinished

            # This isn't necessarily an error but is unusual - flag it
            if clear_eid > 0 and (
                    clear_color.floatValue[0] == 1.0 and depth_func == rd.CompareFunction.GreaterEqual) or (
                    clear_color.floatValue[0] == 0.0 and depth_func == rd.CompareFunction.LessEqual):
                self.analysis_steps.append({
                    'msg': 'The last depth clear of {} at EID {} cleared depth to {:.4}, but the depth comparison '
                           'function is {} which is highly unlikely to pass. This is worth checking'
                    .format(str(self.depth.resourceId), clear_eid, clear_color.floatValue[0], depth_func),
                    'pipe_stage': qrd.PipelineStage.DepthTest,
                })

        # If there's no depth clear found at all, that's a red flag
        else:
            self.analysis_steps.append({
                'msg': 'The depth target was not cleared prior to this draw, so it may contain unexpected '
                       'contents.',
            })

        # Equal depth testing is often used but not equal is rare - flag it too
        if depth_func == rd.CompareFunction.NotEqual:
            self.analysis_steps.append({
                'msg': 'The depth function of {} is not a problem but is unusual.'.format(depth_func),
                'pipe_stage': qrd.PipelineStage.DepthTest,
            })

        # Nothing seems obviously broken, this draw might just be occluded. See if we can get some pixel
        # history results to confirm or guide the user
        if self.api_properties.pixelHistory:
            self.tex_display.overlay = rd.DebugOverlay.Drawcall
            self.out.SetTextureDisplay(self.tex_display)
            overlay = self.out.GetDebugOverlayTexID()

            drawcall_overlay_data = self.r.GetTextureData(overlay, self.tex_display.subresource)

            dim = self.out.GetDimensions()

            # Scan for a pixel that's covered
            covered = None
            for y in range(dim[1]):
                for x in range(dim[0]):
                    pixel_data = struct.unpack_from('4H', drawcall_overlay_data, (y * dim[0] + x) * 8)
                    if pixel_data[0] != 0:
                        covered = (x, y)
                        break
                if covered is not None:
                    break

            if covered:
                history = self.r.PixelHistory(self.targets[0].resourceId, covered[0], covered[1],
                                              self.tex_display.subresource,
                                              self.tex_display.typeCast)

                if len(history) == 0 or history[-1].eventId != self.eid or history[-1].Passed():
                    self.analysis_steps.append({
                        'msg': 'I tried to run pixel history on the draw to get more information but on {} '
                               'I didn\'t get valid results!\n\n '
                               'This is a bug, please report it so it can be investigated.'.format(covered),
                    })
                else:
                    this_draw = [h for h in history if h.eventId == self.eid]
                    pre_draw_depth = this_draw[0].preMod.depth
                    last_draw_eid = 0
                    for h in reversed(history):
                        # Skip this draw itself
                        if h.eventId == self.eid:
                            continue
                        # Skip any failed events
                        if not h.Passed():
                            continue
                        if h.preMod.depth != pre_draw_depth and h.postMod.depth == pre_draw_depth:
                            last_draw_eid = h.eventId
                            break

                    if last_draw_eid > 0:
                        self.analysis_steps.append({
                            'msg': 'Pixel history on {} showed that {} fragments outputted but their depth '
                                   'values all failed against the {} before the draw of {:.4}.\n\n '
                                   'The draw which outputted that depth value is at event {}.'
                            .format(covered, len(this_draw), pre_draw_depth, last_draw_eid),
                            'pixel_history': history,
                        })
                    else:
                        self.analysis_steps.append({
                            'msg': 'Pixel history on {} showed that {} fragments outputted but their depth '
                                   'values all failed against the {} before the draw of {:.4}.\n\n '
                                   'No previous draw was detected that wrote that depth value.'
                            .format(covered, len(this_draw), pre_draw_depth),
                            'pixel_history': history,
                        })
            else:
                self.analysis_steps.append({
                    'msg': 'I tried to run pixel history on the draw to get more information but couldn\'t '
                           'find a pixel covered!\n\n '
                           'This is a bug, please report it so it can be investigated.',
                })

        self.tex_display.overlay = rd.DebugOverlay.Depth if depth_func is not None else rd.DebugOverlay.Stencil

        self.analysis_steps.append({
            'msg': 'This drawcall appears to be failing the depth test normally. Check to see what else '
                   'rendered before it, and whether it should be occluded or if something else is in the '
                   'way.',
            # copy the TextureDisplay object so we can modify it without changing the one in this step
            'tex_display': rd.TextureDisplay(self.tex_display),
        })

    def get_steps(self):
        return self.analysis_steps

    def get_tex(self, resid: rd.ResourceId):
        for t in self.textures:
            if t.resourceId == resid:
                return t
        return None


def analyse_draw(ctx: qrd.CaptureContext, eid: int, finished_callback):
    # define a local function that wraps the detail of needing to invoke back/forth onto replay thread
    def _replay_callback(r: rd.ReplayController):
        analysis = Analysis(ctx, eid, r)

        # Invoke back onto the UI thread to display the results
        ctx.Extensions().GetMiniQtHelper().InvokeOntoUIThread(lambda: finished_callback(analysis.get_steps()))

    ctx.Replay().AsyncInvoke('where_is_my_draw', _replay_callback)
