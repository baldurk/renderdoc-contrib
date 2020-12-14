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
import random
from typing import Callable, Tuple


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

            # If there are no bound targets at all, stop as there's no rendering we can analyse
            if len(self.targets) == 0:
                self.analysis_steps.append({
                    'msg': 'No output render targets or depth target are bound at {}.'.format(self.eid)
                })

                raise AnalysisFinished

            self.check_draw()
        except AnalysisFinished:
            pass
        finally:
            self.out.Shutdown()

            self.r.SetFrameEvent(self.eid, False)

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

        if self.api == rd.GraphicsAPI.Vulkan:
            ra = self.vkpipe.currentPass.renderArea

            # if the render area is empty that's certainly not intentional.
            if ra.width == 0 or ra.height == 0:
                self.analysis_steps.append({
                    'msg': 'The render area is {}x{} so nothing will be rendered.'
                    .format(ra.width, ra.height),
                    'pipe_stage': qrd.PipelineStage.ViewportsScissors,
                })

                raise AnalysisFinished

            # Other invalid render areas outside of attachment dimensions would be invalid behaviour that renderdoc
            # doesn't account for

        texmin, texmax = self.get_overlay_minmax(rd.DebugOverlay.Drawcall)

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

    def get_overlay_minmax(self, overlay: rd.DebugOverlay):
        self.tex_display.overlay = overlay
        self.out.SetTextureDisplay(self.tex_display)
        overlay = self.out.GetDebugOverlayTexID()
        return self.r.GetMinMax(overlay, rd.Subresource(), rd.CompType.Typeless)

    def get_overlay_histogram(self, tex_display, overlay: rd.DebugOverlay, minmax: Tuple[float, float],
                              channels: Tuple[bool, bool, bool, bool]):
        tex_display.overlay = overlay
        self.out.SetTextureDisplay(tex_display)
        overlay = self.out.GetDebugOverlayTexID()
        return self.r.GetHistogram(overlay, rd.Subresource(), rd.CompType.Typeless, minmax[0], minmax[1], channels)

    def check_onscreen(self):
        self.analysis_steps.append({
            'msg': 'The highlight drawcall overlay shows the draw, meaning it is rendering but failing some '
                   'tests.',
            # copy the TextureDisplay object so we can modify it without changing the one in this step
            'tex_display': rd.TextureDisplay(self.tex_display),
        })

        # It's on-screen we debug the rasterization/testing/blending states

        if self.pipe.GetScissor(0).enabled:
            # Check if we're outside scissor first. This overlay is a bit messy because it's not pure red/green,
            # so instead of getting the min and max and seeing if there are green pixels, we get the histogram
            # because any green will show up in the green channel as distinct from white and black.
            texhist = self.get_overlay_histogram(self.tex_display, rd.DebugOverlay.ViewportScissor, (0.0, 1.0),
                                                 (False, True, False, False))

            buckets = list(map(lambda _: _[0], filter(lambda _: _[1] > 0, list(enumerate(texhist)))))

            # drop the top buckets, for white, as well as any buckets lower than 20% for the small amounts of green
            # in other colors
            buckets = [b for b in buckets if len(texhist) // 5 < b < len(texhist) - 1]

            # If there are no green pixels at all, this completely failed
            if len(buckets) == 0:
                self.check_failed_scissor()

                # Regardless of whether we finihsed the analysis above, don't do any more checking.
                raise AnalysisFinished

        texmin, texmax = self.get_overlay_minmax(rd.DebugOverlay.BackfaceCull)

        # If there are no green pixels at all, this completely failed
        if texmax.floatValue[1] < 0.5:
            self.check_failed_backface_culling()

            # Regardless of whether we finihsed the analysis above, don't do any more checking.
            raise AnalysisFinished

        texmin, texmax = self.get_overlay_minmax(rd.DebugOverlay.Depth)

        # If there are no green pixels at all, this completely failed
        if texmax.floatValue[1] < 0.5:
            self.check_failed_depth()

            # Regardless of whether we finihsed the analysis above, don't do any more checking.
            raise AnalysisFinished

        texmin, texmax = self.get_overlay_minmax(rd.DebugOverlay.Stencil)

        # If there are no green pixels at all, this completely failed
        if texmax.floatValue[1] < 0.5:
            self.check_failed_stencil()

            # Regardless of whether we finihsed the analysis above, don't do any more checking.
            raise AnalysisFinished

        sample_count = self.target_descs[0].msSamp

        # OK we've exhausted the help we can get overlays!

        # Check that the sample mask isn't 0, which will cull the draw
        sample_mask = 0xFFFFFFFF
        if self.api == rd.GraphicsAPI.OpenGL:
            # GL only applies the sample mask in MSAA scenarios
            if (sample_count > 1 and self.glpipe.rasterizer.state.multisampleEnable and
                    self.glpipe.rasterizer.state.sampleMask):
                sample_mask = self.glpipe.rasterizer.state.sampleMaskValue
        elif self.api == rd.GraphicsAPI.Vulkan:
            sample_mask = self.vkpipe.multisample.sampleMask
        elif self.api == rd.GraphicsAPI.D3D11:
            # D3D always applies the sample mask
            sample_mask = self.d3d11pipe.outputMerger.blendState.sampleMask
        elif self.api == rd.GraphicsAPI.D3D12:
            sample_mask = self.d3d12pipe.rasterizer.sampleMask

        if sample_mask == 0:
            self.analysis_steps.append({
                'msg': 'The sample mask is set to 0, which will discard all samples.',
                # copy the TextureDisplay object so we can modify it without changing the one in this step
                'pipe_stage': qrd.PipelineStage.Rasterizer,
            })

        # On GL, check the sample coverage value for MSAA targets
        if self.api == rd.GraphicsAPI.OpenGL:
            rs_state = self.glpipe.rasterizer.state
            if sample_count > 1 and rs_state.multisampleEnable and rs_state.sampleCoverage:
                if rs_state.sampleCoverageInvert and rs_state.sampleCoverageValue >= 1.0:
                    self.analysis_steps.append({
                        'msg': 'Sample coverage is enabled, set to invert, and the value is {}. This results in a '
                               'coverage mask of 0.'.format(rs_state.sampleCoverageValue),
                        # copy the TextureDisplay object so we can modify it without changing the one in this step
                        'pipe_stage': qrd.PipelineStage.Rasterizer,
                    })

                    raise AnalysisFinished
                elif not rs_state.sampleCoverageInvert and rs_state.sampleCoverageValue <= 0.0:
                    self.analysis_steps.append({
                        'msg': 'Sample coverage is enabled, and the value is {}. This results in a '
                               'coverage mask of 0.'.format(rs_state.sampleCoverageValue),
                        # copy the TextureDisplay object so we can modify it without changing the one in this step
                        'pipe_stage': qrd.PipelineStage.Rasterizer,
                    })

                    raise AnalysisFinished

        blends = self.pipe.GetColorBlends()
        targets = self.pipe.GetOutputTargets()

        # Consider a write mask enabled if the corresponding target is unbound, to avoid false positives
        enabled_color_masks = []
        color_blends = []
        for i, b in enumerate(blends):
            if i >= len(targets) or targets[i].resourceId == rd.ResourceId.Null():
                color_blends.append(None)
            else:
                enabled_color_masks.append(b.writeMask != 0)
                color_blends.append(b)

        blend_factor = (0.0, 0.0, 0.0, 0.0)
        depth_writes = False
        if self.api == rd.GraphicsAPI.OpenGL:
            blend_factor = self.glpipe.framebuffer.blendState.blendFactor
            if self.glpipe.depthState.depthEnable:
                depth_writes = self.glpipe.depthState.depthWrites
        elif self.api == rd.GraphicsAPI.Vulkan:
            blend_factor = self.vkpipe.colorBlend.blendFactor
            if self.vkpipe.depthStencil.depthTestEnable:
                depth_writes = self.vkpipe.depthStencil.depthWriteEnable
        elif self.api == rd.GraphicsAPI.D3D11:
            blend_factor = self.d3d11pipe.outputMerger.blendState.blendFactor
            if self.d3d11pipe.outputMerger.depthStencilState.depthEnable:
                depth_writes = self.d3d11pipe.outputMerger.depthStencilState.depthWrites
        elif self.api == rd.GraphicsAPI.D3D12:
            blend_factor = self.d3d12pipe.outputMerger.blendState.blendFactor
            if self.d3d12pipe.outputMerger.depthStencilState.depthEnable:
                depth_writes = self.d3d12pipe.outputMerger.depthStencilState.depthWrites

        # if all color masks are disabled, at least warn - or consider the case solved if depth writes are also disabled
        if not any(enabled_color_masks):
            if depth_writes:
                self.analysis_steps.append({
                    'msg': 'All bound output targets have a write mask set to 0 - which means no color will be '
                           'written.\n\n '
                           'This may not be the problem if no color output is expected, as depth writes are enabled.',
                    'pipe_stage': qrd.PipelineStage.Blending,
                })
            else:
                self.analysis_steps.append({
                    'msg': 'All bound output targets have a write mask set to 0 - which means no color will be '
                           'written.\n\n '
                           'Depth writes are also disabled so this draw will not output anything.',
                    'pipe_stage': qrd.PipelineStage.Blending,
                })

                raise AnalysisFinished

        # if only some color masks are disabled, alert the user since they may be wondering why nothing is being output
        # to that target
        elif not all(enabled_color_masks):
            self.analysis_steps.append({
                'msg': 'Some output targets have a write mask set to 0 - which means no color will be '
                       'written to those targets.\n\n '
                       'This may not be a problem if no color output is expected on those targets.',
                'pipe_stage': qrd.PipelineStage.Blending,
            })

        def is_zero(mul: rd.BlendMultiplier):
            if mul == rd.BlendMultiplier.Zero:
                return True
            if rd.BlendMultiplier.FactorAlpha and blend_factor[3] == 0.0:
                return True
            if rd.BlendMultiplier.FactorRGB and blend_factor[0:3] == (0.0, 0.0, 0.0):
                return True
            if rd.BlendMultiplier.InvFactorAlpha and blend_factor[3] == 1.0:
                return True
            if rd.BlendMultiplier.InvFactorRGB and blend_factor[0:3] == (1.0, 1.0, 1.0):
                return True
            return False

        def uses_src(mul: rd.BlendMultiplier):
            return mul in [rd.BlendMultiplier.SrcCol, rd.BlendMultiplier.InvSrcCol, rd.BlendMultiplier.SrcAlpha,
                           rd.BlendMultiplier.InvSrcAlpha,
                           rd.BlendMultiplier.SrcAlphaSat, rd.BlendMultiplier.Src1Col, rd.BlendMultiplier.InvSrc1Col,
                           rd.BlendMultiplier.Src1Alpha, rd.BlendMultiplier.InvSrc1Alpha]

        # Look for any enabled color blend equations that would work out to 0, or not use the source data
        blend_warnings = ''
        for i, b in enumerate(color_blends):
            if b is not None:
                if b.enabled:
                    # All operations use both operands in some sense so we can't count out any blend equation with just
                    # that
                    if is_zero(b.colorBlend.source) and not uses_src(b.colorBlend.destination):
                        blend_warnings += 'Blending on output {} effectively multiplies the source color by zero, ' \
                                          'and the destination color is multiplied by {}, so the source color is ' \
                                          'completely unused.'.format(i, b.colorBlend.destination)

                    # we don't warn on alpha state since it's sometimes only used for the color
                elif b.logicOperationEnabled:
                    if b.logicOperation == rd.LogicOperation.NoOp:
                        blend_warnings += 'Blending on output {} is set to use logic operations, and the operation ' \
                                          'is no-op.\n'.format(i)

        if blend_warnings != '':
            self.analysis_steps.append({
                'msg': 'Some color blending state is strange, but not necessarily unintentional. This is worth '
                       'checking if you haven\'t set this up deliberately:\n\n{}'.format(blend_warnings),
                'pipe_stage': qrd.PipelineStage.Blending,
            })

        # Nothing else has indicated a problem. Let's try the clear before draw on black and white backgrounds. If we
        # see any change that will tell us that the draw is working but perhaps outputting the color that's already
        # there.
        for t in targets:
            if t.resourceId != rd.ResourceId():
                for sample in range(self.get_tex(t.resourceId).msSamp):
                    self.tex_display.resourceId = t.resourceId
                    self.tex_display.subresource = rd.Subresource(t.firstMip, t.firstSlice, sample)
                    self.tex_display.backgroundColor = rd.FloatVector(0.0, 0.0, 0.0, 0.0)
                    self.tex_display.rangeMin = 0.0
                    self.tex_display.rangeMax = 1.0

                    self.get_overlay_minmax(rd.DebugOverlay.ClearBeforeDraw)
                    texmin, texmax = self.r.GetMinMax(t.resourceId, rd.Subresource(), t.typeCast)

                    tex_desc = self.get_tex(t.resourceId)

                    c = min(3, tex_desc.format.compCount)

                    if any([_ != 0.0 for _ in texmin.floatValue[0:c]]) or \
                       any([_ != 0.0 for _ in texmax.floatValue[0:c]]):
                        self.analysis_steps.append({
                            'msg': 'The target {} did show a change in RGB when selecting the \'clear before draw\' overlay '
                                   'on a black background. Perhaps your shader is outputting the color that is already '
                                   'there?'.format(t.resourceId),
                            'tex_display': rd.TextureDisplay(self.tex_display)
                        })

                        raise AnalysisFinished

                    if tex_desc.format.compCount == 4 and (texmin.floatValue[3] != 0.0 or texmax.floatValue[3] != 0.0):
                        self.analysis_steps.append({
                            'msg': 'The target {} did show a change in alpha when selecting the \'clear before draw\' '
                                   'overlay on a black background. Perhaps your shader is outputting the color that is '
                                   'already there, or your blending state isn\'t as expected?'.format(t.resourceId),
                            'tex_display': rd.TextureDisplay(self.tex_display
                        })

                        raise AnalysisFinished

                    self.tex_display.backgroundColor = rd.FloatVector(1.0, 1.0, 1.0, 1.0)

                    self.get_overlay_minmax(rd.DebugOverlay.ClearBeforeDraw)
                    texmin, texmax = self.r.GetMinMax(t.resourceId, rd.Subresource(), t.typeCast)

                    if any([_ != 1.0 for _ in texmin.floatValue[0:c]]) or \
                       any([_ != 1.0 for _ in texmax.floatValue[0:c]]):
                        self.analysis_steps.append({
                            'msg': 'The target {} did show a change when selecting the \'clear before draw\' overlay '
                                   'on a white background. Perhaps your shader is outputting the color that is already '
                                   'there?'.format(t.resourceId),
                            'tex_display': rd.TextureDisplay(self.tex_display)
                        })

                        raise AnalysisFinished

                    if tex_desc.format.compCount == 4 and (texmin.floatValue[3] != 1.0 or texmax.floatValue[3] != 1.0):
                        self.analysis_steps.append(ResultStep(
                            'msg': 'The target {} did show a change in alpha when selecting the \'clear before draw\' '
                                   'overlay on a white background. Perhaps your shader is outputting the color that is '
                                   'already there, or your blending state isn\'t as expected?'.format(t.resourceId),
                            'tex_display': rd.TextureDisplay(self.tex_display)
                        })

                        raise AnalysisFinished

        # No obvious failures, if we can run a pixel history let's see if the shader discarded
        if self.api_properties.pixelHistory:
            self.tex_display.overlay = rd.DebugOverlay.Drawcall
            self.out.SetTextureDisplay(self.tex_display)
            overlay = self.out.GetDebugOverlayTexID()

            drawcall_overlay_data = self.r.GetTextureData(overlay, self.tex_display.subresource)

            dim = self.out.GetDimensions()

            # Scan for all pixels that are covered, since we'll have to try a few
            covered_list = []
            for y in range(dim[1]):
                for x in range(dim[0]):
                    pixel_data = struct.unpack_from('4H', drawcall_overlay_data, (y * dim[0] + x) * 8)
                    if pixel_data[0] != 0:
                        covered_list.append((x, y))

            # Shuffle the covered pixels
            random.shuffle(covered_list)

            # how many times should we try? Let's go conservative
            attempts = 5
            discarded_pixels = []

            attempts = min(attempts, len(covered_list))

            for attempt in range(attempts):
                covered = covered_list[attempt]

                history = self.r.PixelHistory(self.targets[0].resourceId, covered[0], covered[1],
                                              self.tex_display.subresource,
                                              self.tex_display.typeCast)

                # if we didn't get any hits from this event that's strange but not much we can do about it
                if len(history) == 0 or history[-1].eventId != self.eid:
                    continue
                elif history[-1].Passed():
                    self.analysis_steps.append({
                        'msg': 'Running pixel history on {} it showed that a fragment passed.\n\n '
                               'Double check if maybe the draw is outputting something but it\'s invisible '
                               '(e.g. rendering black on black)'.format(covered),
                    })
                    break
                else:
                    this_draw = [h for h in history if h.eventId == self.eid]

                    # We can't really infer anything strongly from this, it's just one pixel. This is just a random
                    # guess to help guide the user
                    if all([h.shaderDiscarded for h in this_draw]):
                        discarded_pixels.append(covered)

            if len(discarded_pixels) > 0:
                self.analysis_steps.append({
                    'msg': 'Pixel history on {} pixels showed that in {} of them all fragments were discarded.\n\n '
                           'This may not mean every other pixel discarded, but it is worth checking in case your '
                           'shader is always discarding.'.format(attempts, len(discarded_pixels)),
                })

    def check_failed_scissor(self):
        v = self.pipe.GetViewport(0)
        s = self.pipe.GetScissor(0)

        s_right = s.x + s.width
        s_bottom = s.y + s.height
        v_right = v.x + v.width
        v_bottom = v.y + v.height

        # if the scissor is empty that's certainly not intentional.
        if s.width == 0 or s.height == 0:
            self.analysis_steps.append({
                'msg': 'The scissor region {},{} to {},{} is empty so nothing will be rendered.'
                .format(s.x, s.y, s_right, s_bottom),
                'pipe_stage': qrd.PipelineStage.ViewportsScissors,
            })

            raise AnalysisFinished

        # If the scissor region doesn't intersect the viewport, that's a problem
        if s_right < v.x or s.x > v_right or s.x > v_right or s.y > v_bottom:
            self.analysis_steps.append({
                'msg': 'The scissor region {},{} to {},{} is completely outside the viewport of {},{} to {},{} so all '
                       'pixels will be scissor clipped'
                .format(s.x, s.y, s_right, s_bottom, v.x, v.y, v_right, v_bottom),
                # copy the TextureDisplay object so we can modify it without changing the one in this step
                'pipe_stage': qrd.PipelineStage.Rasterizer,
            })

            raise AnalysisFinished

        self.analysis_steps.append({
            'msg': 'The draw is outside of the scissor region, so it has been clipped.\n\n'
                   'If this isn\'t intentional, check your scissor state.',
            # copy the TextureDisplay object so we can modify it without changing the one in this step
            'tex_display': rd.TextureDisplay(self.tex_display),
            'pipe_stage': qrd.PipelineStage.ViewportsScissors,
        })

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
        depth_enabled = False
        depth_clamp = True
        if self.api == rd.GraphicsAPI.OpenGL:
            depth_enabled = self.glpipe.depthState.depthEnable
            if self.glpipe.depthState.depthBounds:
                depth_bounds = [self.glpipe.depthState.nearBound, self.glpipe.depthState.farBound]
            depth_func = self.glpipe.depthState.depthFunction
            depth_clamp = self.glpipe.rasterizer.state.depthClamp
            if self.glpipe.vertexProcessing.clipNegativeOneToOne:
                ndc_bounds = [-1.0, 1.0]
        elif self.api == rd.GraphicsAPI.Vulkan:
            depth_enabled = self.vkpipe.depthStencil.depthTestEnable
            if self.vkpipe.depthStencil.depthBoundsEnable:
                depth_bounds = [self.vkpipe.depthStencil.minDepthBounds,
                                self.vkpipe.depthStencil.maxDepthBounds]
            depth_func = self.vkpipe.depthStencil.depthFunction
            depth_clamp = self.vkpipe.rasterizer.depthClampEnable
        elif self.api == rd.GraphicsAPI.D3D11:
            depth_enabled = self.d3d11pipe.outputMerger.depthStencilState.depthEnable
            depth_func = self.d3d11pipe.outputMerger.depthStencilState.depthFunction
            depth_clamp = not self.d3d11pipe.rasterizer.state.depthClip
        elif self.api == rd.GraphicsAPI.D3D12:
            depth_enabled = self.d3d12pipe.outputMerger.depthStencilState.depthEnable
            if self.d3d12pipe.outputMerger.depthStencilState.depthBoundsEnable:
                depth_bounds = [self.d3d12pipe.outputMerger.depthStencilState.minDepthBounds,
                                self.d3d12pipe.outputMerger.depthStencilState.maxDepthBounds]
            depth_func = self.d3d12pipe.outputMerger.depthStencilState.depthFunction
            depth_clamp = not self.d3d12pipe.rasterizer.state.depthClip

        if not depth_enabled:
            self.analysis_steps.append({
                'msg': 'Depth test stage is disabled! Normally this means the depth test should always '
                       'pass.\n\n'
                       'Sorry I couldn\'t figure out the exact problem. Please check your {} '
                       'setup and report an issue so we can narrow this down in future.',
                'pipe_stage': qrd.PipelineStage.DepthTest,
            })

            raise AnalysisFinished

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

        # Equal depth testing is often used but not equal is rare - flag it too
        if depth_func == rd.CompareFunction.NotEqual:
            self.analysis_steps.append({
                'msg': 'The depth function of {} is not a problem but is unusual.'.format(depth_func),
                'pipe_stage': qrd.PipelineStage.DepthTest,
            })

        self.check_previous_depth_stencil(depth_func)

    def check_previous_depth_stencil(self, depth_func):
        val_name = 'depth' if depth_func is not None else 'stencil'
        test_name = '{} test'.format(val_name)
        result_stage = qrd.PipelineStage.DepthTest if depth_func is not None else qrd.PipelineStage.StencilTest

        # If no depth buffer is bound, all APIs spec that depth/stencil test should always pass! This seems
        # quite strange.
        if self.depth.resourceId == rd.ResourceId.Null():
            self.analysis_steps.append({
                'msg': 'No depth buffer is bound! Normally this means the {} should always '
                       'pass.\n\n'
                       'Sorry I couldn\'t figure out the exact problem. Please check your {} '
                       'setup and report an issue so we can narrow this down in future.'.format(test_name, test_name),
                'pipe_stage': result_stage,
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
                    if s.width == 0 or s.height == 0:
                        self.analysis_steps.append({
                            'msg': 'The last depth-stencil clear of {} at {} had scissor enabled, but the scissor rect '
                                   '{},{} to {},{} is empty so nothing will get cleared.'
                            .format(str(self.depth.resourceId), clear_eid, s.x, s.y, s_right, s_bottom),
                            'pipe_stage': qrd.PipelineStage.ViewportsScissors,
                        })

                    if s.x >= self.target_descs[-1].width or s.y >= self.target_descs[-1].height:
                        self.analysis_steps.append({
                            'msg': 'The last depth-stencil clear of {} at {} had scissor enabled, but the scissor rect '
                                   '{},{} to {},{} doesn\'t cover the depth-stencil target so it won\'t get cleared.'
                            .format(str(self.depth.resourceId), clear_eid, s.x, s.y, s_right, s_bottom),
                            'pipe_stage': qrd.PipelineStage.ViewportsScissors,
                        })

                    # if the clear's scissor doesn't overlap the viewport at the time of the draw,
                    # warn the user
                    elif v.x < s.x or v.y < s.y or v.x + v_right or v_bottom > s_bottom:
                        self.analysis_steps.append({
                            'msg': 'The last depth-stencil clear of {} at {} had scissor enabled, but the scissor rect '
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

            if depth_func is not None:
                if clear_eid > 0 and (
                        clear_color.floatValue[0] == 1.0 and depth_func == rd.CompareFunction.Greater) or (
                        clear_color.floatValue[0] == 0.0 and depth_func == rd.CompareFunction.Less):
                    self.analysis_steps.append({
                        'msg': 'The last depth clear of {} at EID {} cleared depth to {:.4}, but the depth comparison '
                               'function is {} which is impossible to pass.'.format(str(self.depth.resourceId),
                                                                                    clear_eid,
                                                                                    clear_color.floatValue[0],
                                                                                    depth_func),
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

        # If there's no depth/stencil clear found at all, that's a red flag
        else:
            self.analysis_steps.append({
                'msg': 'The depth-stencil target was not cleared prior to this draw, so it may contain unexpected '
                       'contents.',
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
                    pre_draw_val = this_draw[0].preMod.depth if depth_func is not None else this_draw[0].preMod.stencil
                    last_draw_eid = 0
                    for h in reversed(history):
                        # Skip this draw itself
                        if h.eventId == self.eid:
                            continue

                        # Skip any failed events
                        if not h.Passed():
                            continue

                        if depth_func is not None:
                            if h.preMod.depth != pre_draw_val and h.postMod.depth == pre_draw_val:
                                last_draw_eid = h.eventId
                                break
                        else:
                            if h.preMod.stencil != pre_draw_val and h.postMod.stencil == pre_draw_val:
                                last_draw_eid = h.eventId
                                break

                    if last_draw_eid > 0:
                        self.analysis_steps.append({
                            'msg': 'Pixel history on {} showed that {} fragments were outputted but their {} '
                                   'values all failed against the {} before the draw of {:.4}.\n\n '
                                   'The draw which outputted that depth value is at event {}.'
                            .format(covered, len(this_draw), val_name, val_name, pre_draw_val, last_draw_eid),
                            'pixel_history': history,
                        })
                    else:
                        self.analysis_steps.append({
                            'msg': 'Pixel history on {} showed that {} fragments outputted but their {} '
                                   'values all failed against the {} before the draw of {:.4}.\n\n '
                                   'No previous draw was detected that wrote that {} value.'
                            .format(covered, len(this_draw), val_name, val_name, pre_draw_val, val_name),
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
            'msg': 'This drawcall appears to be failing the {} normally. Check to see what else '
                   'rendered before it, and whether it should be occluded or if something else is in the '
                   'way.'.format(test_name),
            # copy the TextureDisplay object so we can modify it without changing the one in this step
            'tex_display': rd.TextureDisplay(self.tex_display),
        })

    def check_failed_stencil(self):
        self.analysis_steps.append({
            'msg': 'The stencil test overlay shows red, so the draw is completely failing a stencil test.',
            # copy the TextureDisplay object so we can modify it without changing the one in this step
            'tex_display': rd.TextureDisplay(self.tex_display),
        })

        # Get the cull mode. If culling is enabled we know which stencil state is in use and can narrow our analysis,
        # if culling is disabled then unfortunately we can't automatically narrow down which side is used.
        cull_mode = rd.CullMode.NoCull
        stencil_enabled = False
        front = back = rd.StencilFace()
        if self.api == rd.GraphicsAPI.OpenGL:
            cull_mode = self.glpipe.rasterizer.state.cullMode
            stencil_enabled = self.glpipe.stencilState.stencilEnable
            front = self.glpipe.stencilState.frontFace
            back = self.glpipe.stencilState.backFace
        elif self.api == rd.GraphicsAPI.Vulkan:
            cull_mode = self.vkpipe.rasterizer.cullMode
            stencil_enabled = self.vkpipe.depthStencil.stencilTestEnable
            front = self.vkpipe.depthStencil.frontFace
            back = self.vkpipe.depthStencil.backFace
        elif self.api == rd.GraphicsAPI.D3D11:
            cull_mode = self.d3d11pipe.rasterizer.state.cullMode
            stencil_enabled = self.d3d11pipe.outputMerger.depthStencilState.stencilEnable
            front = self.d3d11pipe.outputMerger.depthStencilState.frontFace
            back = self.d3d11pipe.outputMerger.depthStencilState.backFace
        elif self.api == rd.GraphicsAPI.D3D12:
            cull_mode = self.d3d12pipe.rasterizer.state.cullMode
            stencil_enabled = self.d3d12pipe.outputMerger.depthStencilState.stencilEnable
            front = self.d3d12pipe.outputMerger.depthStencilState.frontFace
            back = self.d3d12pipe.outputMerger.depthStencilState.backFace

        # To simplify code, we're going to check if both faces are the same anyway so if one side is being culled we
        # just pretend that face has the same state as the other (which isn't culled)
        if cull_mode == rd.CullMode.Front:
            front = back
        elif cull_mode == rd.CullMode.Back:
            back = front

        if not stencil_enabled:
            self.analysis_steps.append({
                'msg': 'Depth test stage is disabled! Normally this means the depth test should always '
                       'pass.\n\n'
                       'Sorry I couldn\'t figure out the exact problem. Please check your {} '
                       'setup and report an issue so we can narrow this down in future.',
                'pipe_stage': qrd.PipelineStage.DepthTest,
            })

            raise AnalysisFinished

        # Each of these checks below will check for two cases: first that the states are the same between front and
        # back, meaning EITHER that both were the same in the application so we don't need to know whether front or
        # back faces are in the draw, OR that one face is being culled so after we've eliminated a backface culling
        # possibility a stencil failure must be from the other face.
        #
        # In this first case, we can be sure of the problem.
        #
        # In the second case we check if one of the states matches, in which case we can't be sure of the problem but
        # we can alert the users about it. This potentially has false positives if e.g. someone doesn't set backface
        # culling but also doesn't configure the backface stencil state.

        def check_faces(msg: str, check: Callable[[rd.StencilFace], None]):
            checks = check(front), check(back)

            if all(checks):
                self.analysis_steps.append({
                    'msg': msg.format(test='test', s=front),
                    'pipe_stage': qrd.PipelineStage.StencilTest,
                })

                raise AnalysisFinished
            elif checks[0]:
                msg += ' If your draw relies on back faces then this could be the problem.'
                self.analysis_steps.append({
                    'msg': msg.format(test='back face test', s=front),
                    'pipe_stage': qrd.PipelineStage.StencilTest,
                })
            elif checks[1]:
                msg += ' If your draw relies on front faces then this could be the problem.'
                self.analysis_steps.append({
                    'msg': msg.format(test='front face test', s=back),
                    'pipe_stage': qrd.PipelineStage.StencilTest,
                })

        # Check simple cases that can't ever be true
        check_faces('The stencil {test} is set to Never, meaning it always fails.',
                    lambda x: x.function == rd.CompareFunction.Never)
        check_faces('The stencil {test} is set to {s.function} than {s.reference}, which is impossible.',
                    lambda x: (x.function == rd.CompareFunction.Less and x.reference == 0) or (
                            x.function == rd.CompareFunction.Greater and x.reference == 255))
        check_faces('The stencil {test} is set to {s.function} than {s.reference}, which is impossible.',
                    lambda x: (x.function == rd.CompareFunction.LessEqual and x.reference < 0) or (
                            x.function == rd.CompareFunction.GreaterEqual and x.reference > 255))

        # compareMask being 0 is almost certainly a problem, but we can't *prove* it except in certain circumstances.
        # e.g. having a compareMask of 0 and a reference of 0 would pass, or less than a non-zero reference.
        # Fortunately, most of the cases we can prove will catch common errors. At least errors that cause a draw to
        # not show up.

        # if the compareMask is set such that the reference value can never be achieved, that's a guaranteed failure
        check_faces(
            'The stencil {test} is set to compare equal to {s.reference}, but the compare mask is {s.compareMask:x} '
            'meaning it never can.',
            lambda x: x.function == rd.CompareFunction.Equal and (
                        (x.compareMask & x.reference) != x.reference) and x.reference != 0)

        # The compareMask is the largest value that can be read, if the test is such that only larger values would pass,
        # that's also broken.
        check_faces(
            'The stencil {test} is set to compare greater than {s.reference}, but the compare mask is '
            '{s.compareMask:x} meaning it never can.',
            lambda x: x.function == rd.CompareFunction.Greater and x.compareMask <= x.reference)
        check_faces(
            'The stencil {test} is set to compare greater than or equal to {s.reference}, but the compare mask is '
            '{s.compareMask:x} meaning it never can.',
            lambda x: x.function == rd.CompareFunction.GreaterEqual and x.compareMask < x.reference)

        # Equal stencil testing is often used but not equal is rare - flag it too
        try:
            check_faces('The stencil {test} is set to Not Equal, which is not a problem but is unusual.',
                        lambda x: x.function == rd.CompareFunction.Never)
        except AnalysisFinished:
            # we're not actually finished even if both faces were not equal!
            pass

        self.check_previous_depth_stencil(None)

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
