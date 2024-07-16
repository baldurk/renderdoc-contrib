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
from typing import Callable, Tuple, List


class PixelHistoryData:
    def __init__(self):
        self.x = 0
        self.y = 0
        self.id = rd.ResourceId()
        self.tex_display = rd.TextureDisplay()
        self.history: List[rd.PixelModification] = []
        self.view = rd.ReplayController.NoPreference
        self.last_eid = 0


class ResultStep:
    def __init__(self, *, msg='', tex_display=rd.TextureDisplay(), pixel_history=PixelHistoryData(),
                 pipe_stage=qrd.PipelineStage.ComputeShader, mesh_view=rd.MeshDataStage.Count):
        self.msg = msg
        # force copy the input, so it can be modified without changing the one in this step
        self.tex_display = rd.TextureDisplay(tex_display)
        self.pixel_history = pixel_history
        self.pipe_stage = pipe_stage
        self.mesh_view = mesh_view

    def has_details(self) -> bool:
        return self.tex_display.resourceId != rd.ResourceId() or \
               self.pixel_history.id != rd.ResourceId() or \
               self.pipe_stage != qrd.PipelineStage.ComputeShader or \
               self.mesh_view != rd.MeshDataStage.Count


class AnalysisFinished(Exception):
    pass


class Analysis:
    # Do the expensive analysis on the replay thread
    def __init__(self, ctx: qrd.CaptureContext, eid: int, r: rd.ReplayController):
        self.analysis_steps = []
        self.ctx = ctx
        self.eid = eid
        self.r = r

        print("On replay thread, analysing @{} with current @{}".format(self.eid, self.ctx.CurEvent()))

        self.r.SetFrameEvent(self.eid, True)

        self.drawcall = self.ctx.GetAction(self.eid)
        self.api_properties = self.r.GetAPIProperties()
        self.textures = self.r.GetTextures()
        self.buffers = self.r.GetBuffers()
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
        self.targets = [t for t in self.pipe.GetOutputTargets() if t.resource != rd.ResourceId.Null()]
        self.depth = self.pipe.GetDepthTarget()
        if self.depth.resource != rd.ResourceId.Null():
            self.targets.append(self.depth)

        dim = (1, 1)
        self.target_descs = []
        for t in self.targets:
            desc = self.get_tex(t.resource)
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

        self.vert_ndc = [(vert[0] / vert[3], vert[1] / vert[3], vert[2] / vert[3]) for vert in self.postvs_positions if
                         vert[3] != 0.0]

        # Create a temporary offscreen output we'll use for
        self.out = self.r.CreateOutput(rd.CreateHeadlessWindowingData(dim[0], dim[1]), rd.ReplayOutputType.Texture)

        self.tex_display = rd.TextureDisplay()

        name = self.drawcall.GetName(self.ctx.GetStructuredFile())

        # We're not actually trying to catch exceptions here, we just want a finally: to shutdown the output
        try:
            self.analysis_steps = []

            # If there are no bound targets at all, stop as there's no rendering we can analyse
            if len(self.targets) == 0:
                self.analysis_steps.append(
                    ResultStep(msg='No output render targets or depth target are bound at {}.'.format(self.eid)))

                raise AnalysisFinished

            # if the drawcall has a parameter which means no work happens, alert the user
            if (self.drawcall.flags & rd.ActionFlags.Instanced) and self.drawcall.numInstances == 0:
                self.analysis_steps.append(ResultStep(msg='The drawcall {} is instanced, but the number of instances '
                                                          'specified is 0.'.format(name)))

                raise AnalysisFinished

            if self.drawcall.numIndices == 0:
                vert_name = 'indices' if self.drawcall.flags & rd.ActionFlags.Indexed else 'vertices'
                self.analysis_steps.append(
                    ResultStep(msg='The drawcall {} specifies 0 {}.'.format(name, vert_name)))

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
        self.tex_display.resourceId = self.targets[0].resource
        self.tex_display.subresource.mip = self.targets[0].firstMip
        self.tex_display.subresource.slice = self.targets[0].firstSlice
        self.tex_display.typeCast = self.targets[0].format.compType
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

        texmin, texmax = self.get_overlay_minmax(rd.DebugOverlay.Drawcall)

        if texmax.floatValue[0] < 0.5:
            self.analysis_steps.append(ResultStep(
                msg='The highlight drawcall overlay shows nothing for this draw, meaning it is off-screen or doesn\'t '
                    'cover enough of a pixel.'))

            self.check_offscreen()
        else:
            self.analysis_steps.append(ResultStep(
                msg='The highlight drawcall overlay shows the draw, meaning it is rendering but failing some tests.',
                tex_display=self.tex_display))

            self.check_onscreen()

        # If we got here, we didn't find a specific problem! Add a note about that
        self.analysis_steps.append(ResultStep(msg='Sorry, I couldn\'t prove precisely what was wrong! I\'ve noted the '
                                                  'steps and checks I took below, as well as anything suspicious I '
                                                  'found along the way.\n\n'
                                                  'If you think this is something I should have caught with more '
                                                  'checks please report an issue.'))

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
                raise
            else:
                self.analysis_steps.append(
                    ResultStep(msg='Some or all of the draw passes the scissor test, which is enabled',
                               tex_display=self.tex_display))

        texmin, texmax = self.get_overlay_minmax(rd.DebugOverlay.BackfaceCull)

        # If there are no green pixels at all, this completely failed
        if texmax.floatValue[1] < 0.5:
            self.check_failed_backface_culling()

            # Regardless of whether we finihsed the analysis above, don't do any more checking.
            raise AnalysisFinished
        else:
            self.analysis_steps.append(
                ResultStep(msg='Some or all of the draw passes backface culling',
                           tex_display=self.tex_display))

        texmin, texmax = self.get_overlay_minmax(rd.DebugOverlay.Depth)

        # If there are no green pixels at all, this completely failed
        if texmax.floatValue[1] < 0.5:
            self.check_failed_depth()

            # Regardless of whether we finihsed the analysis above, don't do any more checking.
            raise AnalysisFinished
        else:
            self.analysis_steps.append(
                ResultStep(msg='Some or all of the draw passes depth testing',
                           tex_display=self.tex_display))

        texmin, texmax = self.get_overlay_minmax(rd.DebugOverlay.Stencil)

        # If there are no green pixels at all, this completely failed
        if texmax.floatValue[1] < 0.5:
            self.check_failed_stencil()

            # Regardless of whether we finihsed the analysis above, don't do any more checking.
            raise AnalysisFinished
        else:
            self.analysis_steps.append(
                ResultStep(msg='Some or all of the draw passes stencil testing',
                           tex_display=self.tex_display))

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
            self.analysis_steps.append(ResultStep(
                msg='The sample mask is set to 0, which will discard all samples.',
                pipe_stage=qrd.PipelineStage.SampleMask))

            raise AnalysisFinished
        elif (sample_mask & 0xff) != 0xff:
            self.analysis_steps.append(
                ResultStep(msg='The sample mask {:08x} is non-zero, meaning at least some samples will '
                               'render.\n\nSome bits are disabled so make sure you check the correct samples.'
                               .format(sample_mask),
                           tex_display=self.tex_display))
        else:
            self.analysis_steps.append(
                ResultStep(msg='The sample mask {:08x} is non-zero or disabled, meaning at least some samples will '
                               'render. '
                               .format(sample_mask),
                           tex_display=self.tex_display))

        # On GL, check the sample coverage value for MSAA targets
        if self.api == rd.GraphicsAPI.OpenGL:
            rs_state = self.glpipe.rasterizer.state
            if sample_count > 1 and rs_state.multisampleEnable and rs_state.sampleCoverage:
                if rs_state.sampleCoverageInvert and rs_state.sampleCoverageValue >= 1.0:
                    self.analysis_steps.append(ResultStep(
                        msg='Sample coverage is enabled, set to invert, and the value is {}. This results in a '
                            'coverage mask of 0.'.format(rs_state.sampleCoverageValue),
                        pipe_stage=qrd.PipelineStage.Rasterizer))

                    raise AnalysisFinished
                elif not rs_state.sampleCoverageInvert and rs_state.sampleCoverageValue <= 0.0:
                    self.analysis_steps.append(ResultStep(
                        msg='Sample coverage is enabled, and the value is {}. This results in a '
                            'coverage mask of 0.'.format(rs_state.sampleCoverageValue),
                        pipe_stage=qrd.PipelineStage.Rasterizer))

                    raise AnalysisFinished
            else:
                self.analysis_steps.append(
                    ResultStep(msg='The sample coverage value seems to be set correctly.',
                               tex_display=self.tex_display))

        blends = self.pipe.GetColorBlends()
        targets = self.pipe.GetOutputTargets()

        # Consider a write mask enabled if the corresponding target is unbound, to avoid false positives
        enabled_color_masks = []
        color_blends = []
        for i, b in enumerate(blends):
            if i >= len(targets) or targets[i].resource == rd.ResourceId.Null():
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
                self.analysis_steps.append(ResultStep(
                    msg='All bound output targets have a write mask set to 0 - which means no color will be '
                        'written.\n\n '
                        'This may not be the problem if no color output is expected, as depth writes are enabled.',
                    pipe_stage=qrd.PipelineStage.Blending))
            else:
                self.analysis_steps.append(ResultStep(
                    msg='All bound output targets have a write mask set to 0 - which means no color will be '
                        'written.\n\n '
                        'Depth writes are also disabled so this draw will not output anything.',
                    pipe_stage=qrd.PipelineStage.Blending))

                raise AnalysisFinished

        # if only some color masks are disabled, alert the user since they may be wondering why nothing is being output
        # to that target
        elif not all(enabled_color_masks):
            self.analysis_steps.append(ResultStep(
                msg='Some output targets have a write mask set to 0 - which means no color will be '
                    'written to those targets.\n\n '
                    'This may not be a problem if no color output is expected on those targets.',
                pipe_stage=qrd.PipelineStage.Blending))
        else:
            self.analysis_steps.append(
                ResultStep(msg='The color write masks seem to be set normally.',
                           pipe_stage=qrd.PipelineStage.Blending))

        def is_zero(mul: rd.BlendMultiplier):
            if mul == rd.BlendMultiplier.Zero:
                return True
            if mul == rd.BlendMultiplier.FactorAlpha and blend_factor[3] == 0.0:
                return True
            if mul == rd.BlendMultiplier.FactorRGB and blend_factor[0:3] == (0.0, 0.0, 0.0):
                return True
            if mul == rd.BlendMultiplier.InvFactorAlpha and blend_factor[3] == 1.0:
                return True
            if mul == rd.BlendMultiplier.InvFactorRGB and blend_factor[0:3] == (1.0, 1.0, 1.0):
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
            self.analysis_steps.append(ResultStep(
                msg='Some color blending state is strange, but not necessarily unintentional. This is worth '
                    'checking if you haven\'t set this up deliberately:\n\n{}'.format(blend_warnings),
                pipe_stage=qrd.PipelineStage.Blending))
        elif any([b is not None and b.enabled for b in color_blends]):
            self.analysis_steps.append(
                ResultStep(msg='The blend equations seem to be set up to allow rendering.',
                           pipe_stage=qrd.PipelineStage.Blending))
        else:
            self.analysis_steps.append(
                ResultStep(msg='Blending is disabled.',
                           pipe_stage=qrd.PipelineStage.Blending))

        # Nothing else has indicated a problem. Let's try the clear before draw on black and white backgrounds. If we
        # see any change that will tell us that the draw is working but perhaps outputting the color that's already
        # there.
        for t in targets:
            if t.resource != rd.ResourceId():
                for sample in range(self.get_tex(t.resource).msSamp):
                    self.tex_display.resourceId = t.resource
                    self.tex_display.subresource = rd.Subresource(t.firstMip, t.firstSlice, sample)
                    self.tex_display.backgroundColor = rd.FloatVector(0.0, 0.0, 0.0, 0.0)
                    self.tex_display.rangeMin = 0.0
                    self.tex_display.rangeMax = 1.0

                    self.get_overlay_minmax(rd.DebugOverlay.ClearBeforeDraw)
                    texmin, texmax = self.r.GetMinMax(t.resource, rd.Subresource(), t.format.compType)

                    tex_desc = self.get_tex(t.resource)

                    c = min(3, tex_desc.format.compCount)

                    if any([_ != 0.0 for _ in texmin.floatValue[0:c]]) or \
                       any([_ != 0.0 for _ in texmax.floatValue[0:c]]):
                        self.analysis_steps.append(ResultStep(
                            msg='The target {} did show a change in RGB when selecting the \'clear before draw\' '
                                'overlay on a black background. Perhaps your shader is outputting the color that is '
                                'already there?'.format(t.resource),
                            tex_display=self.tex_display))

                        raise AnalysisFinished

                    if tex_desc.format.compCount == 4 and (texmin.floatValue[3] != 0.0 or texmax.floatValue[3] != 0.0):
                        self.analysis_steps.append(ResultStep(
                            msg='The target {} did show a change in alpha when selecting the \'clear before draw\' '
                                'overlay on a black background. Perhaps your shader is outputting the color that is '
                                'already there, or your blending state isn\'t as expected?'.format(t.resource),
                            tex_display=self.tex_display))

                        raise AnalysisFinished

                    self.tex_display.backgroundColor = rd.FloatVector(1.0, 1.0, 1.0, 1.0)

                    self.get_overlay_minmax(rd.DebugOverlay.ClearBeforeDraw)
                    texmin, texmax = self.r.GetMinMax(t.resource, rd.Subresource(), t.format.compType)

                    if any([_ != 1.0 for _ in texmin.floatValue[0:c]]) or \
                       any([_ != 1.0 for _ in texmax.floatValue[0:c]]):
                        self.analysis_steps.append(ResultStep(
                            msg='The target {} did show a change in RGB when selecting the \'clear before draw\' '
                                'overlay on a white background. Perhaps your shader is outputting the color that is '
                                'already there?'.format(t.resource),
                            tex_display=self.tex_display))

                        raise AnalysisFinished

                    if tex_desc.format.compCount == 4 and (texmin.floatValue[3] != 1.0 or texmax.floatValue[3] != 1.0):
                        self.analysis_steps.append(ResultStep(
                            msg='The target {} did show a change in alpha when selecting the \'clear before draw\' '
                                'overlay on a white background. Perhaps your shader is outputting the color that is '
                                'already there, or your blending state isn\'t as expected?'.format(t.resource),
                            tex_display=self.tex_display))

                        raise AnalysisFinished

        self.analysis_steps.append(
            ResultStep(msg='Using the \'clear before draw\' overlay didn\'t show any output on either black or white.'))

        # No obvious failures, if we can run a pixel history let's see if the shader discarded or output something that
        # would
        if self.api_properties.pixelHistory:
            self.tex_display.overlay = rd.DebugOverlay.Drawcall
            self.out.SetTextureDisplay(self.tex_display)
            overlay = self.out.GetDebugOverlayTexID()

            sub = rd.Subresource(self.tex_display.subresource.mip, 0, 0)

            drawcall_overlay_data = self.r.GetTextureData(overlay, sub)

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

                history = self.r.PixelHistory(self.targets[0].resource, covered[0], covered[1],
                                              self.tex_display.subresource,
                                              self.tex_display.typeCast)

                # if we didn't get any hits from this event that's strange but not much we can do about it
                if len(history) == 0 or history[-1].eventId != self.eid:
                    continue
                elif history[-1].Passed():
                    alpha = history[-1].shaderOut.col.floatValue[3]
                    blend = color_blends[0]
                    if blend is None:
                        blend = rd.ColorBlend()
                        blend.enabled = False
                    if alpha <= 0.0 and blend.enabled and blend.colorBlend.source == rd.BlendMultiplier.SrcAlpha:
                        self.analysis_steps.append(ResultStep(
                            msg='Running pixel history on {} it showed that a fragment outputted alpha of 0.0.\n\n '
                                'Your blend setup is such that this means the shader output is multiplied by 0'))
                    else:
                        self.analysis_steps.append(ResultStep(
                            msg='Running pixel history on {} it showed that a fragment passed.\n\n '
                                'Double check if maybe the draw is outputting something but it\'s invisible '
                                '(e.g. blending to nothing)'.format(covered)))
                    break
                else:
                    this_draw = [h for h in history if h.eventId == self.eid]

                    # We can't really infer anything strongly from this, it's just one pixel. This is just a random
                    # guess to help guide the user
                    if all([h.shaderDiscarded for h in this_draw]):
                        discarded_pixels.append(covered)

            if len(discarded_pixels) > 0:
                self.analysis_steps.append(ResultStep(
                    msg='Pixel history on {} pixels showed that in {} of them all fragments were discarded.\n\n '
                        'This may not mean every other pixel discarded, but it is worth checking in case your '
                        'shader is always discarding.'.format(attempts, len(discarded_pixels))))
            else:
                self.analysis_steps.append(
                    ResultStep(msg='Pixel history didn\'t detect any pixels discarding.'))
        else:
            self.analysis_steps.append(
                ResultStep(msg='Pixel history could narrow down things further but this API doesn\'t support pixel '
                               'history.'))

    def check_failed_scissor(self):
        v = self.pipe.GetViewport(0)
        s = self.pipe.GetScissor(0)

        s_right = s.x + s.width
        s_bottom = s.y + s.height
        v_right = v.x + v.width
        v_bottom = v.y + v.height

        # if the scissor is empty that's certainly not intentional.
        if s.width == 0 or s.height == 0:
            self.analysis_steps.append(ResultStep(
                msg='The scissor region {},{} to {},{} is empty so nothing will be rendered.'
                .format(s.x, s.y, s_right, s_bottom),
                pipe_stage=qrd.PipelineStage.ViewportsScissors))

            raise AnalysisFinished

        # If the scissor region doesn't intersect the viewport, that's a problem
        if s_right < v.x or s.x > v_right or s.x > v_right or s.y > v_bottom:
            self.analysis_steps.append(ResultStep(
                msg='The scissor region {},{} to {},{} is completely outside the viewport of {},{} to {},{} so all '
                    'pixels will be scissor clipped'.format(s.x, s.y, s_right, s_bottom, v.x, v.y, v_right, v_bottom),
                pipe_stage=qrd.PipelineStage.Rasterizer))

            raise AnalysisFinished

        self.analysis_steps.append(ResultStep(
            msg='The draw is outside of the scissor region, so it has been clipped.\n\n'
                'If this isn\'t intentional, check your scissor state.',
            pipe_stage=qrd.PipelineStage.ViewportsScissors))

        raise AnalysisFinished

    def check_offscreen(self):
        v = self.pipe.GetViewport(0)

        if v.width <= 1.0 or abs(v.height) <= 1.0:
            self.analysis_steps.append(
                ResultStep(msg='Viewport 0 is {}x{} so nothing will be rendered.'.format(v.width, v.height),
                           pipe_stage=qrd.PipelineStage.ViewportsScissors))

            raise AnalysisFinished
        elif v.x >= self.target_descs[0].width or v.y >= self.target_descs[0].height:
            self.analysis_steps.append(
                ResultStep(msg='Viewport 0 is placed at {},{} which is out of bounds for the target dimension {}x{}.'
                               .format(v.x, v.y, self.target_descs[0].width, self.target_descs[0].height),
                           pipe_stage=qrd.PipelineStage.ViewportsScissors))

            raise AnalysisFinished
        else:
            self.analysis_steps.append(
                ResultStep(msg='Viewport 0 looks normal, it\'s {}x{} at {},{}.'.format(v.width, v.height, v.x, v.y),
                           pipe_stage=qrd.PipelineStage.ViewportsScissors))

        if self.api == rd.GraphicsAPI.Vulkan:
            ra = self.vkpipe.currentPass.renderArea

            # if the render area is empty that's certainly not intentional.
            if ra.width == 0 or ra.height == 0:
                self.analysis_steps.append(
                    ResultStep(msg='The render area is {}x{} so nothing will be rendered.'.format(ra.width, ra.height),
                               pipe_stage=qrd.PipelineStage.ViewportsScissors))

                raise AnalysisFinished

            # Other invalid render areas outside of attachment dimensions would be invalid behaviour that renderdoc
            # doesn't account for
            else:
                self.analysis_steps.append(ResultStep(
                    msg='The vulkan render area {}x{} at {},{} is fine.'.format(ra.width, ra.height, ra.x, ra.y),
                    pipe_stage=qrd.PipelineStage.Rasterizer))

        # Check rasterizer discard state
        if (self.glpipe is not None and self.glpipe.vertexProcessing.discard) or (
                self.vkpipe is not None and self.vkpipe.rasterizer.rasterizerDiscardEnable):
            self.analysis_steps.append(ResultStep(
                msg='Rasterizer discard is enabled. This API state disables rasterization for the drawcall.',
                pipe_stage=qrd.PipelineStage.Rasterizer))

            raise AnalysisFinished
        elif self.glpipe is not None or self.vkpipe is not None:
            self.analysis_steps.append(ResultStep(
                msg='Rasterizer discard is not enabled, so that should be fine.',
                pipe_stage=qrd.PipelineStage.Rasterizer))

        # Check position was written to
        vsrefl = self.pipe.GetShaderReflection(rd.ShaderStage.Vertex)
        dsrefl = self.pipe.GetShaderReflection(rd.ShaderStage.Domain)
        gsrefl = self.pipe.GetShaderReflection(rd.ShaderStage.Geometry)
        lastrefl = None

        if lastrefl is None:
            lastrefl = gsrefl
        if lastrefl is None:
            lastrefl = dsrefl
        if lastrefl is None:
            lastrefl = vsrefl

        if lastrefl is None:
            self.analysis_steps.append(ResultStep(
                msg='No vertex, tessellation or geometry shader is bound.',
                mesh_view=self.postvs_stage))

            raise AnalysisFinished

        pos_found = False
        for sig in lastrefl.outputSignature:
            if sig.systemValue == rd.ShaderBuiltin.Position:
                pos_found = True

        if not pos_found:
            self.analysis_steps.append(ResultStep(
                msg='The last post-transform shader {} does not write to the position builtin.'
                    .format(lastrefl.resourceId),
                mesh_view=self.postvs_stage))

            raise AnalysisFinished

        if len(self.vert_ndc) == 0 and len(self.postvs_positions) != 0:
            self.analysis_steps.append(ResultStep(
                msg='All of the post-transform vertex positions have W=0.0 which is invalid, you should check your '
                    'vertex tranformation setup.',
                mesh_view=self.postvs_stage))
        elif len(self.vert_ndc) < len(self.postvs_positions):
            self.analysis_steps.append(ResultStep(
                msg='Some of the post-transform vertex positions have W=0.0 which is invalid, you should check your '
                    'vertex tranformation setup.',
                mesh_view=self.postvs_stage))

        vert_ndc_x = list(filter(lambda _: math.isfinite(_), [vert[0] for vert in self.vert_ndc]))
        vert_ndc_y = list(filter(lambda _: math.isfinite(_), [vert[1] for vert in self.vert_ndc]))

        if len(vert_ndc_x) == 0 or len(vert_ndc_y) == 0:
            self.analysis_steps.append(ResultStep(
                msg='The post-transform vertex positions are all NaN or infinity, when converted to normalised device '
                    'co-ordinates (NDC) by dividing XYZ by W.',
                mesh_view=self.postvs_stage))

            self.check_invalid_verts()

            raise AnalysisFinished

        v_min = [min(vert_ndc_x), min(vert_ndc_y)]
        v_max = [max(vert_ndc_x), max(vert_ndc_y)]

        # We can't really easily write a definitive algorithm to determine "reasonable transform, but offscreen" and
        # "broken transform". As a heuristic we see if the bounds are within a reasonable range of the NDC box
        # (broken floats are very likely to be outside this range) and that the area of the bounds is at least one pixel
        # (if the input data is broken/empty the vertices may all be transformed to a point).

        # project the NDC min/max onto the viewport and see how much of a pixel it covers
        v = self.pipe.GetViewport(0)
        top_left = ((v_min[0] * 0.5 + 0.5) * v.width, (v_min[1]*0.5 + 0.5) * v.height)
        bottom_right = ((v_max[0] * 0.5 + 0.5) * v.width, (v_max[1]*0.5 + 0.5) * v.height)

        area = (bottom_right[0] - top_left[0]) * (bottom_right[1] - top_left[1])

        # if the area is below a pixel but we're in the clip region, this might just be a tiny draw or it might be
        # broken
        if 0.0 < area < 1.0 and v_min[0] >= -1.0 and v_min[1] >= -1.0 and v_max[0] <= 1.0 and v_max[1] <= 1.0:
            self.analysis_steps.append(ResultStep(
                msg='The calculated area covered by this draw is only {} of a pixel, meaning this draw may be too small'
                    'to render.'.format(area),
                mesh_view=self.postvs_stage))

            self.check_invalid_verts()
        else:
            # if we ARE off screen but we're within 10% of the guard band (which is already *huge*) and
            # the area is bigger than a pixel then we assume this is a normal draw that's just off screen.
            if max([abs(_) for _ in v_min + v_max]) < 32767.0 / 10.0 and area > 1.0:
                self.analysis_steps.append(ResultStep(
                    msg='The final position outputs from the vertex shading stages looks reasonable but off-screen.\n\n'
                        'Check that your transformation and vertex shading is working as expected, or perhaps this '
                        'drawcall should be off-screen.',
                    mesh_view=self.postvs_stage))

                raise AnalysisFinished
            # if we're in the outer regions of the guard band or the area is tiny, assume broken and check for invalid
            # inputs if we can
            else:
                self.analysis_steps.append(ResultStep(
                    msg='The final position outputs seem to be invalid or degenerate, when converted to normalised '
                        'device co-ordinates (NDC) by dividing XYZ by W.',
                    mesh_view=self.postvs_stage))

                self.check_invalid_verts()

    def check_invalid_verts(self):
        vs = self.pipe.GetShader(rd.ShaderStage.Vertex)

        # There should be at least a vertex shader bound
        if vs == rd.ResourceId.Null():
            self.analysis_steps.append(ResultStep(
                msg='No valid vertex shader is bound.',
                pipe_stage=qrd.PipelineStage.VertexShader))

            raise AnalysisFinished

        prev_len = len(self.analysis_steps)

        # if there's an index buffer bound, we'll bounds check it then calculate the indices
        if self.drawcall.flags & rd.ActionFlags.Indexed:
            ib = self.pipe.GetIBuffer()
            if ib.resourceId == rd.ResourceId.Null() or ib.byteStride == 0:
                self.analysis_steps.append(ResultStep(
                    msg='This draw is indexed, but there is no valid index buffer bound.',
                    pipe_stage=qrd.PipelineStage.VertexInput))

                raise AnalysisFinished

            ibSize = ib.byteSize
            ibOffs = ib.byteOffset + self.drawcall.indexOffset * ib.byteStride
            # if the binding is unbounded, figure out how much is left in the buffer
            if ibSize == 0xFFFFFFFFFFFFFFFF:
                buf = self.get_buf(ib.resourceId)
                if buf is None or ibOffs > buf.length:
                    ibSize = 0
                else:
                    ibSize = buf.length - ibOffs

            ibNeededSize = self.drawcall.numIndices * ib.byteStride
            if ibSize < ibNeededSize:
                explanation = 'The index buffer is bound with a {} byte range'.format(ib.byteSize)
                if ib.byteSize == 0xFFFFFFFFFFFFFFFF:
                    buf = self.get_buf(ib.resourceId)
                    if buf is None:
                        buf = rd.BufferDescription()
                    explanation = ''
                    explanation += 'The index buffer is {} bytes in size.\n'.format(buf.length)
                    explanation += 'It is bound with an offset of {}.\n'.format(ib.byteOffset)
                    explanation += 'The drawcall specifies an offset of {} indices (each index is {} bytes)\n'.format(
                        self.drawcall.indexOffset, ib.byteStride)
                    explanation += 'Meaning only {} bytes are available'.format(ibSize)

                self.analysis_steps.append(ResultStep(
                    msg='This draw reads {} {}-byte indices from {}, meaning total {} bytes are needed, but '
                        'only {} bytes are available. This is unlikely to be intentional.\n\n{}'
                        .format(self.drawcall.numIndices, ib.byteStride, ib.resourceId, ibNeededSize,
                                ibSize, explanation),
                    pipe_stage=qrd.PipelineStage.VertexInput))

            read_bytes = min(ibSize, ibNeededSize)

            # Fetch the data
            if read_bytes > 0:
                ibdata = self.r.GetBufferData(ib.resourceId, ibOffs, read_bytes)
            else:
                ibdata = bytes()

            # Get the character for the width of index
            index_fmt = 'B'
            if ib.byteStride == 2:
                index_fmt = 'H'
            elif ib.byteStride == 4:
                index_fmt = 'I'

            avail_indices = int(len(ibdata) / ib.byteStride)

            # Duplicate the format by the number of indices
            index_fmt = '=' + str(min(avail_indices, self.drawcall.numIndices)) + index_fmt

            # Unpack all the indices
            indices = struct.unpack_from(index_fmt, ibdata)

            restart_idx = self.pipe.GetRestartIndex() & ((1 << (ib.byteStride*8)) - 1)
            restart_enabled = self.pipe.IsRestartEnabled() and rd.IsStrip(self.pipe.GetPrimitiveTopology())

            # Detect restart indices and map them to None, otherwise apply basevertex
            indices = [None if restart_enabled and i == restart_idx else i + self.drawcall.baseVertex for i in indices]
        else:
            indices = [i + self.drawcall.vertexOffset for i in range(self.drawcall.numIndices)]

        # what's the maximum index? for bounds checking
        max_index = max(indices)
        max_index_idx = indices.index(max_index)
        max_inst = max(self.drawcall.numInstances - 1, 0)

        vsinputs = self.pipe.GetVertexInputs()
        vbuffers = self.pipe.GetVBuffers()
        avail = [0] * len(vbuffers)

        # Determine the available bytes in each vertex buffer
        for i, vb in enumerate(vbuffers):
            vbSize = vb.byteSize
            vbOffs = vb.byteOffset
            # if the binding is unbounded, figure out how much is left in the buffer
            if vbSize == 0xFFFFFFFFFFFFFFFF:
                buf = self.get_buf(vb.resourceId)
                if buf is None or vbOffs > buf.length:
                    vbSize = 0
                else:
                    vbSize = buf.length - vbOffs
            avail[i] = vbSize

        # bounds check each attribute against the maximum available
        for attr in vsinputs:
            if not attr.used:
                continue

            if attr.vertexBuffer >= len(vbuffers) or vbuffers[attr.vertexBuffer].resourceId == rd.ResourceId.Null():
                self.analysis_steps.append(ResultStep(
                    msg='Vertex attribute {} references vertex buffer slot {} which has no buffer bound.'
                        .format(attr.name, attr.vertexBuffer),
                    pipe_stage=qrd.PipelineStage.VertexInput))

                continue

            vb: rd.BoundVBuffer = vbuffers[attr.vertexBuffer]

            avail_bytes = avail[attr.vertexBuffer]
            used_bytes = attr.format.ElementSize()

            if attr.perInstance:
                max_inst_offs = max(self.drawcall.instanceOffset + max_inst, 0)

                if attr.byteOffset + max_inst_offs * vb.byteStride + used_bytes > avail_bytes:
                    explanation = ''
                    explanation += 'The vertex buffer {} has {} bytes available'.format(attr.vertexBuffer, avail_bytes)

                    if vb.byteSize == 0xFFFFFFFFFFFFFFFF:
                        buf = self.get_buf(vb.resourceId)
                        if buf is None:
                            buf = rd.BufferDescription()
                        explanation += ' because it is {} bytes long, ' \
                                       'and is bound at offset {} bytes'.format(buf.length, vb.byteOffset)
                    explanation += '.\n'

                    explanation += 'The maximum instance index is {}'.format(max_inst)
                    if self.drawcall.instanceOffset > 0:
                        explanation += ' (since the draw renders {} instances starting at {})'.format(
                            self.drawcall.numInstances, self.drawcall.instanceOffset)
                    explanation += '.\n'

                    explanation += 'Meaning the highest offset read from is {}.\n'.format(max_inst_offs * vb.byteStride)
                    explanation += 'The attribute reads {} bytes at offset {} from that.\n'.format(used_bytes,
                                                                                                   attr.byteOffset)

                    self.analysis_steps.append(ResultStep(
                        msg='Per-instance vertex attribute {} reads out of bounds on vertex buffer slot {}:\n\n{}'
                            .format(attr.name, attr.vertexBuffer, explanation),
                        pipe_stage=qrd.PipelineStage.VertexInput))
            else:
                max_idx_offs = max(self.drawcall.baseVertex + max_index, 0)

                if attr.byteOffset + max_idx_offs * vb.byteStride + used_bytes > avail_bytes:
                    explanation = ''
                    explanation += 'The vertex buffer {} has {} bytes available'.format(attr.vertexBuffer, avail_bytes)

                    if vb.byteSize == 0xFFFFFFFFFFFFFFFF:
                        buf = self.get_buf(vb.resourceId)
                        if buf is None:
                            buf = rd.BufferDescription()
                        explanation += ' because it is {} bytes long, ' \
                                       'and is bound at offset {} bytes'.format(buf.length, vb.byteOffset)
                    explanation += '.\n'

                    if self.drawcall.flags & rd.ActionFlags.Indexed:
                        explanation += 'The maximum index is {} (found at vertex {}'.format(max_index,
                                                                                            max_index_idx)
                        base = self.drawcall.baseVertex
                        if base != 0:
                            explanation += ' by adding base vertex {} to index {}'.format(base, max_index - base)
                        explanation += ').\n'
                    else:
                        explanation += 'The maximum vertex is {}'.format(max_index)
                        if self.drawcall.vertexOffset > 0:
                            explanation += ' (since the draw renders {} vertices starting at {})'.format(
                                self.drawcall.numIndices, self.drawcall.vertexOffset)
                        explanation += '.\n'

                    explanation += 'Meaning the highest offset read from is {}.\n'.format(max_idx_offs * vb.byteStride)
                    explanation += 'The attribute reads {} bytes at offset {} from that.\n'.format(used_bytes,
                                                                                                   attr.byteOffset)

                    self.analysis_steps.append(ResultStep(
                        msg='Per-vertex vertex attribute {} reads out of bounds on vertex buffer slot {}:\n\n{}'
                            .format(attr.name, attr.vertexBuffer, explanation),
                        pipe_stage=qrd.PipelineStage.VertexInput))

        # This is a bit of a desperation move but it might help some people. Look for any matrix parameters that
        # are obviously broken because they're all 0.0. Don't look inside structs or arrays because they might be
        # optional/unused

        vsrefl = self.pipe.GetShaderReflection(rd.ShaderStage.Vertex)

        for cb in self.pipe.GetConstantBlocks(rd.ShaderStage.Vertex):
            if vsrefl.constantBlocks[cb.access.index].bindArraySize <= 1:
                cb_vars = self.r.GetCBufferVariableContents(self.pipe.GetGraphicsPipelineObject(), vs,
                                                            rd.ShaderStage.Vertex,
                                                            self.pipe.GetShaderEntryPoint(rd.ShaderStage.Vertex),
                                                            cb.access.index, cb.descriptor.resource,
                                                            cb.descriptor.byteOffset, cb.descriptor.byteSize)

                for v in cb_vars:
                    if v.rows > 1 and v.columns > 1:
                        suspicious = True
                        value = ''

                        if v.type == rd.VarType.Float:
                            vi = 0
                            for r in range(v.rows):
                                for c in range(v.columns):
                                    if v.value.f32v[vi] != 0.0:
                                        suspicious = False
                                    value += '{:.3}'.format(v.value.f32v[vi])
                                    vi += 1
                                    if c < v.columns - 1:
                                        value += ', '
                                value += '\n'
                        elif v.type == rd.VarType.Half:
                            vi = 0
                            for r in range(v.rows):
                                for c in range(v.columns):
                                    x = rd.HalfToFloat(v.value.u16v[vi])
                                    if x != 0.0:
                                        suspicious = False
                                    value += '{:.3}'.format(x)
                                    vi += 1
                                    if c < v.columns - 1:
                                        value += ', '
                                value += '\n'
                        elif v.type == rd.VarType.Double:
                            vi = 0
                            for r in range(v.rows):
                                for c in range(v.columns):
                                    if v.value.f64v[vi] != 0.0:
                                        suspicious = False
                                    value += '{:.3}'.format(v.value.f64v[vi])
                                    vi += 1
                                    if c < v.columns - 1:
                                        value += ', '
                                value += '\n'

                        if suspicious:
                            self.analysis_steps.append(ResultStep(
                                msg='Vertex constant {} in {} is an all-zero matrix which looks suspicious.\n\n{}'
                                    .format(v.name, vsrefl.constantBlocks[i].name, value),
                                pipe_stage=qrd.PipelineStage.VertexShader))

        # In general we can't know what the user will be doing with their vertex inputs to generate output, so we can't
        # say that any input setup is "wrong". However we can certainly try and guess at the problem, so we look for
        # any and all attributes with 'pos' in the name, and see if they're all zeroes or all identical
        for attr in vsinputs:
            if not attr.used:
                continue

            if 'pos' not in attr.name.lower():
                continue

            if attr.vertexBuffer >= len(vbuffers):
                continue

            vb: rd.BoundVBuffer = vbuffers[attr.vertexBuffer]

            if vb.resourceId == rd.ResourceId.Null():
                continue

            if attr.perInstance:
                self.analysis_steps.append(ResultStep(
                    msg='Attribute \'{}\' is set to be per-instance. If this is a vertex '
                        'position attribute then that might be unintentional.'.format(attr.name),
                    pipe_stage=qrd.PipelineStage.VertexInput))
            else:
                max_idx_offs = max(self.drawcall.baseVertex + max_index - 1, 0)
                data = self.r.GetBufferData(vb.resourceId,
                                            vb.byteOffset + attr.byteOffset + max_idx_offs * vb.byteStride, 0)

                elem_size = attr.format.ElementSize()
                vert_bytes = []
                offs = 0
                while offs + elem_size <= len(data):
                    vert_bytes.append(data[offs:offs+elem_size])
                    offs += vb.byteStride

                if len(vert_bytes) > 1:
                    # get the unique set of vertices
                    unique_vertices = list(set(vert_bytes))

                    # get all usages of the buffer before this event
                    buf_usage = [u for u in self.r.GetUsage(vb.resourceId) if u.eventId < self.eid]

                    # trim to only write usages
                    write_usages = [rd.ResourceUsage.VS_RWResource, rd.ResourceUsage.HS_RWResource,
                                    rd.ResourceUsage.DS_RWResource, rd.ResourceUsage.GS_RWResource,
                                    rd.ResourceUsage.PS_RWResource, rd.ResourceUsage.CS_RWResource,
                                    rd.ResourceUsage.All_RWResource,
                                    rd.ResourceUsage.Copy, rd.ResourceUsage.StreamOut,
                                    rd.ResourceUsage.CopyDst, rd.ResourceUsage.Discard, rd.ResourceUsage.CPUWrite]
                    buf_usage = [u for u in buf_usage if u.usage in write_usages]

                    if len(buf_usage) >= 1:
                        buffer_last_mod = '{} was last modified with {} at @{}, you could check that it wrote ' \
                                          'what you expected.'.format(vb.resourceId, buf_usage[-1].usage,
                                                                      buf_usage[-1].eventId)
                    else:
                        buffer_last_mod = '{} hasn\'t been modified in this capture, check that you initialised it ' \
                                          'with the correct data or wrote it before the beginning of the ' \
                                          'capture.'.format(vb.resourceId)

                    # If all vertices are 0s, give a more specific error message
                    if not any([any(v) for v in unique_vertices]):
                        self.analysis_steps.append(ResultStep(
                            msg='Attribute \'{}\' all members are zero. '
                                'If this is a vertex position attribute then that might be unintentional.\n\n{}'
                                .format(attr.name, buffer_last_mod),
                            mesh_view=rd.MeshDataStage.VSIn))
                    # otherwise error if we only saw one vertex (maybe it's all 0xcccccccc or something)
                    elif len(unique_vertices) <= 1:
                        self.analysis_steps.append(ResultStep(
                            msg='Attribute \'{}\' all members are identical. '
                                'If this is a vertex position attribute then that might be unintentional.\n\n{}'
                                .format(attr.name, buffer_last_mod),
                            mesh_view=rd.MeshDataStage.VSIn))

        if len(self.analysis_steps) == prev_len:
            self.analysis_steps.append(
                ResultStep(msg='Didn\'t find any problems with the vertex input setup!'))

    def check_failed_backface_culling(self):
        cull_mode = rd.CullMode.NoCull
        front = 'Front CW'
        if self.api == rd.GraphicsAPI.OpenGL:
            cull_mode = self.glpipe.rasterizer.state.cullMode
            if self.glpipe.rasterizer.state.frontCCW:
                front = 'Front: CCW'
        elif self.api == rd.GraphicsAPI.Vulkan:
            cull_mode = self.vkpipe.rasterizer.cullMode
            if self.vkpipe.rasterizer.frontCCW:
                front = 'Front: CCW'
        elif self.api == rd.GraphicsAPI.D3D11:
            cull_mode = self.d3d11pipe.rasterizer.state.cullMode
            if self.d3d11pipe.rasterizer.state.frontCCW:
                front = 'Front: CCW'
        elif self.api == rd.GraphicsAPI.D3D12:
            cull_mode = self.d3d12pipe.rasterizer.state.cullMode
            if self.d3d12pipe.rasterizer.state.frontCCW:
                front = 'Front: CCW'

        self.analysis_steps.append(ResultStep(
            msg='The backface culling overlay shows red, so the draw is completely backface culled.\n\n'
                'Check your polygon winding ({}) and front-facing state ({}).'.format(front, str(cull_mode)),
            tex_display=self.tex_display))

        raise AnalysisFinished

    def check_failed_depth(self):
        self.analysis_steps.append(ResultStep(
            msg='The depth test overlay shows red, so the draw is completely failing a depth test.',
            tex_display=self.tex_display))

        v = self.pipe.GetViewport(0)

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

        # Check for state setups that will always fail
        if depth_func == rd.CompareFunction.Never:
            self.analysis_steps.append(ResultStep(
                msg='Depth test is set to Never, meaning it always fails for this draw.',
                pipe_stage=qrd.PipelineStage.DepthTest))

            raise AnalysisFinished

        # Calculate the min/max NDC bounds of the vertices in z
        vert_ndc_z = list(filter(lambda _: math.isfinite(_), [vert[2] for vert in self.vert_ndc]))
        vert_bounds = [min(vert_ndc_z), max(vert_ndc_z)]

        self.analysis_steps.append(ResultStep(
            msg='From the vertex output data I calculated the vertices lie within {:.4} and {:.4} in NDC z'
                .format(vert_bounds[0], vert_bounds[1]),
            pipe_stage=qrd.PipelineStage.Rasterizer))

        state_name = 'Depth Clip' if rd.IsD3D(self.api) else 'Depth Clamp'

        # if depth clipping is enabled (aka depth clamping is disabled), this happens regardless of if
        # depth testing is enabled
        if not depth_clamp:
            # If the largest vertex NDC z is lower than the NDC range, the whole draw is near-plane clipped
            if vert_bounds[1] < ndc_bounds[0]:
                self.analysis_steps.append(ResultStep(
                    msg='All of the drawcall vertices are in front of the near plane, and the '
                        'current {} state means these vertices get clipped.'.format(state_name),
                    mesh_view=self.postvs_stage))

                raise AnalysisFinished
            else:
                self.analysis_steps.append(ResultStep(
                    msg='At least some of the vertices are on the passing side of the near plane',
                    mesh_view=self.postvs_stage))

            # Same for the smallest z being above the NDC range
            if vert_bounds[0] > ndc_bounds[1]:
                self.analysis_steps.append(ResultStep(
                    msg='All of the drawcall vertices are behind the far plane, and the '
                        'current {} state means these vertices get clipped.'.format(state_name),
                        mesh_view=self.postvs_stage))

                raise AnalysisFinished
            else:
                self.analysis_steps.append(ResultStep(
                    msg='At least some of the vertices are on the passing side of the far plane',
                    mesh_view=self.postvs_stage))
        else:
            self.analysis_steps.append(ResultStep(
                msg='The current {} state means the near/far planes are ignored for clipping'.format(state_name)))

        # all other checks should only run if depth test is enabled
        if not depth_enabled:
            self.analysis_steps.append(ResultStep(
                msg='Depth test stage is disabled! Normally this means the depth test should always pass.\n\n'
                    'Sorry I couldn\'t figure out the exact problem. Please check your {} '
                    'setup and report an issue so we can narrow this down in future.',
                pipe_stage=qrd.PipelineStage.DepthTest))

            raise AnalysisFinished

        # Check that the viewport depth range doesn't trivially fail depth bounds
        if depth_bounds and (v.minDepth > depth_bounds[1] or v.maxDepth < depth_bounds[0]):
            self.analysis_steps.append(ResultStep(
                msg='The viewport depth range ({} to {}) are outside the depth bounds range ({} to {}), '
                    'which is enabled'.format(v.minDepth, v.maxDepth, depth_bounds[0], depth_bounds[1]),
                pipe_stage=qrd.PipelineStage.ViewportsScissors))

            raise AnalysisFinished
        elif depth_bounds:
            self.analysis_steps.append(ResultStep(
                msg='The viewport depth range ({} to {}) is within the depth bounds range ({} to {})'
                    .format(v.minDepth, v.maxDepth, depth_bounds[0], depth_bounds[1]),
                mesh_view=self.postvs_stage))

        # If the vertex NDC z range does not intersect the depth bounds range, and depth bounds test is
        # enabled, the draw fails the depth bounds test
        if depth_bounds and (vert_bounds[0] > depth_bounds[1] or vert_bounds[1] < depth_bounds[0]):
            self.analysis_steps.append(ResultStep(
                msg='All of the drawcall vertices are outside the depth bounds range ({} to {}), '
                    'which is enabled'.format(depth_bounds[0], depth_bounds[1]),
                pipe_stage=qrd.PipelineStage.Rasterizer))

            raise AnalysisFinished
        elif depth_bounds:
            self.analysis_steps.append(ResultStep(
                msg='Some vertices are within the depth bounds range ({} to {})'
                    .format(v.minDepth, v.maxDepth, depth_bounds[0], depth_bounds[1]),
                mesh_view=self.postvs_stage))

        # Equal depth testing is often used but not equal is rare - flag it too
        if depth_func == rd.CompareFunction.NotEqual:
            self.analysis_steps.append(ResultStep(
                msg='The depth function of {} is not a problem but is unusual.'.format(depth_func),
                pipe_stage=qrd.PipelineStage.DepthTest))

        if v.minDepth != 0.0 or v.maxDepth != 1.0:
            self.analysis_steps.append(ResultStep(
                msg='The viewport min and max depth are set to {} and {}, which is unusual.'.format(v.minDepth,
                                                                                                    v.maxDepth),
                pipe_stage=qrd.PipelineStage.ViewportsScissors))
        else:
            self.analysis_steps.append(
                ResultStep(msg='The viewport depth bounds are {} to {} which is normal.'.format(v.minDepth, v.maxDepth),
                           pipe_stage=qrd.PipelineStage.ViewportsScissors))

        self.check_previous_depth_stencil(depth_func)

    def check_previous_depth_stencil(self, depth_func):
        val_name = 'depth' if depth_func is not None else 'stencil'
        test_name = '{} test'.format(val_name)
        result_stage = qrd.PipelineStage.DepthTest if depth_func is not None else qrd.PipelineStage.StencilTest

        # If no depth buffer is bound, all APIs spec that depth/stencil test should always pass! This seems
        # quite strange.
        if self.depth.resource == rd.ResourceId.Null():
            self.analysis_steps.append(ResultStep(
                msg='No depth buffer is bound! Normally this means the {} should always pass.\n\n'
                    'Sorry I couldn\'t figure out the exact problem. Please check your {} '
                    'setup and report an issue so we can narrow this down in future.'.format(test_name, test_name),
                pipe_stage=result_stage))

            raise AnalysisFinished

        # Get the last clear of the current depth buffer
        usage = self.r.GetUsage(self.depth.resource)

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
                        self.analysis_steps.append(ResultStep(
                            msg='The last depth-stencil clear of {} at {} had scissor enabled, but the scissor rect '
                                '{},{} to {},{} is empty so nothing will get cleared.'
                            .format(str(self.depth.resource), clear_eid, s.x, s.y, s_right, s_bottom),
                            pipe_stage=qrd.PipelineStage.ViewportsScissors))

                    if s.x >= self.target_descs[-1].width or s.y >= self.target_descs[-1].height:
                        self.analysis_steps.append(ResultStep(
                            msg='The last depth-stencil clear of {} at {} had scissor enabled, but the scissor rect '
                                '{},{} to {},{} doesn\'t cover the depth-stencil target so it won\'t get cleared.'
                            .format(str(self.depth.resource), clear_eid, s.x, s.y, s_right, s_bottom),
                            pipe_stage=qrd.PipelineStage.ViewportsScissors))

                    # if the clear's scissor doesn't overlap the viewport at the time of the draw,
                    # warn the user
                    elif v.x < s.x or v.y < s.y or v.x + v_right or v_bottom > s_bottom:
                        self.analysis_steps.append(ResultStep(
                            msg='The last depth-stencil clear of {} at {} had scissor enabled, but the scissor rect '
                                '{},{} to {},{} is smaller than the current viewport {},{} to {},{}. '
                                'This may mean not every pixel was properly cleared.'
                            .format(str(self.depth.resource), clear_eid, s.x, s.y, s_right, s_bottom, v.x, v.y,
                                    v_right, v_bottom),
                            pipe_stage=qrd.PipelineStage.ViewportsScissors))

            # If this was a clear then we expect the depth value to be uniform, so pick the pixel to
            # get the depth clear value.
            clear_color = self.r.PickPixel(self.depth.resource, 0, 0,
                                           rd.Subresource(self.depth.firstMip, self.depth.firstSlice, 0),
                                           self.depth.format.compType)

            self.r.SetFrameEvent(self.eid, True)

            if depth_func is not None:
                if clear_eid > 0 and (
                        clear_color.floatValue[0] == 1.0 and depth_func == rd.CompareFunction.Greater) or (
                        clear_color.floatValue[0] == 0.0 and depth_func == rd.CompareFunction.Less):
                    self.analysis_steps.append(ResultStep(
                        msg='The last depth clear of {} at @{} cleared depth to {:.4}, but the depth comparison '
                            'function is {} which is impossible to pass.'.format(str(self.depth.resource),
                                                                                 clear_eid,
                                                                                 clear_color.floatValue[0],
                                                                                 str(depth_func).split('.')[-1]),
                        pipe_stage=qrd.PipelineStage.DepthTest))

                    raise AnalysisFinished

                v = self.pipe.GetViewport(0)

                if clear_eid > 0 and ((clear_color.floatValue[0] >= max(v.minDepth, v.maxDepth) and
                                       depth_func == rd.CompareFunction.Greater) or
                                      (clear_color.floatValue[0] <= min(v.minDepth, v.maxDepth) and
                                       depth_func == rd.CompareFunction.Less) or
                                      (clear_color.floatValue[0] > min(v.minDepth, v.maxDepth) and
                                       depth_func == rd.CompareFunction.GreaterEqual) or
                                      (clear_color.floatValue[0] < min(v.minDepth, v.maxDepth) and
                                       depth_func == rd.CompareFunction.LessEqual)):
                    self.analysis_steps.append(ResultStep(
                        msg='The last depth clear of {} at @{} cleared depth to {:.4}, but the viewport '
                            'min/max bounds ({:.4} to {:.4}) mean this draw can\'t compare {}.'
                            .format(str(self.depth.resource), clear_eid, clear_color.floatValue[0], v.minDepth,
                                    v.maxDepth, str(depth_func).split('.')[-1]),
                        pipe_stage=qrd.PipelineStage.DepthTest))

                    raise AnalysisFinished

                # This isn't necessarily an error but is unusual - flag it
                if clear_eid > 0 and (
                        clear_color.floatValue[0] == 1.0 and depth_func == rd.CompareFunction.GreaterEqual) or (
                        clear_color.floatValue[0] == 0.0 and depth_func == rd.CompareFunction.LessEqual):
                    self.analysis_steps.append(ResultStep(
                        msg='The last depth clear of {} at EID {} cleared depth to {}, but the depth comparison '
                            'function is {} which is highly unlikely to pass. This is worth checking'
                        .format(str(self.depth.resource), clear_eid, clear_color.floatValue[0],
                                str(depth_func).split('.')[-1]),
                        pipe_stage=qrd.PipelineStage.DepthTest))
                else:
                    self.analysis_steps.append(ResultStep(
                        msg='The last depth clear of {} at @{} cleared depth to {}, which is reasonable.'
                            .format(str(self.depth.resource), clear_eid, clear_color.floatValue[0])))

        # If there's no depth/stencil clear found at all, that's a red flag
        else:
            self.analysis_steps.append(ResultStep(
                msg='The depth-stencil target was not cleared prior to this draw, so it may contain unexpected '
                    'contents.'))

        # Nothing seems obviously broken, this draw might just be occluded. See if we can get some pixel
        # history results to confirm or guide the user
        if self.api_properties.pixelHistory:
            self.tex_display.overlay = rd.DebugOverlay.Drawcall
            self.out.SetTextureDisplay(self.tex_display)
            overlay = self.out.GetDebugOverlayTexID()

            sub = rd.Subresource(self.tex_display.subresource.mip, 0, 0)

            drawcall_overlay_data = self.r.GetTextureData(overlay, sub)

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
                sub = rd.Subresource(self.targets[-1].firstMip, self.targets[-1].firstSlice)
                history = self.r.PixelHistory(self.targets[-1].resource, covered[0], covered[1], sub,
                                              self.targets[-1].format.compType)

                if len(history) == 0 or history[-1].eventId != self.eid or history[-1].Passed():
                    self.analysis_steps.append(ResultStep(
                        msg='I tried to run pixel history on the draw to get more information but on {} '
                            'I didn\'t get valid results!\n\n '
                            'This is a bug, please report it so it can be investigated.'.format(covered)))
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

                    history_package = PixelHistoryData()
                    history_package.x = covered[0]
                    history_package.y = covered[1]
                    history_package.id = self.targets[-1].resource
                    history_package.tex_display = rd.TextureDisplay(self.tex_display)
                    history_package.tex_display.resourceId = self.targets[-1].resource
                    history_package.tex_display.subresource = sub
                    history_package.tex_display.typeCast = self.targets[-1].format.compType
                    history_package.last_eid = last_draw_eid
                    history_package.view = rd.ReplayController.NoPreference
                    history_package.history = history

                    if last_draw_eid > 0:
                        self.analysis_steps.append(ResultStep(
                            msg='Pixel history on {} showed that {} fragments were outputted but their {} '
                                'values all failed against the {} before the draw of {:.4}.\n\n '
                                'The draw which outputted that depth value is at @{}.'
                            .format(covered, len(this_draw), val_name, val_name, pre_draw_val, last_draw_eid),
                            pixel_history=history_package))
                    else:
                        self.analysis_steps.append(ResultStep(
                            msg='Pixel history on {} showed that {} fragments outputted but their {} '
                                'values all failed against the {} before the draw of {:.4}.\n\n '
                                'No previous draw was detected that wrote that {} value.'
                            .format(covered, len(this_draw), val_name, val_name, pre_draw_val, val_name),
                            pixel_history=history_package))
            else:
                self.analysis_steps.append(ResultStep(
                    msg='I tried to run pixel history on the draw to get more information but couldn\'t '
                        'find a pixel covered!\n\n '
                        'This is a bug, please report it so it can be investigated.'))

        self.tex_display.overlay = rd.DebugOverlay.Depth if depth_func is not None else rd.DebugOverlay.Stencil

        self.analysis_steps.append(ResultStep(
            msg='This drawcall appears to be failing the {} normally. Check to see what else '
                'rendered before it, and whether it should be occluded or if something else is in the '
                'way.'.format(test_name),
            tex_display=self.tex_display))

    def check_failed_stencil(self):
        self.analysis_steps.append(ResultStep(
            msg='The stencil test overlay shows red, so the draw is completely failing a stencil test.',
            tex_display=self.tex_display))

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
            self.analysis_steps.append(ResultStep(
                msg='Depth test stage is disabled! Normally this means the depth test should always '
                    'pass.\n\n'
                    'Sorry I couldn\'t figure out the exact problem. Please check your {} '
                    'setup and report an issue so we can narrow this down in future.',
                pipe_stage=qrd.PipelineStage.DepthTest))

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
                self.analysis_steps.append(ResultStep(msg=msg.format(test='test', s=front),
                                                      pipe_stage=qrd.PipelineStage.StencilTest))

                raise AnalysisFinished
            elif checks[0]:
                msg += ' If your draw relies on back faces then this could be the problem.'
                self.analysis_steps.append(ResultStep(msg=msg.format(test='back face test', s=front),
                                                      pipe_stage=qrd.PipelineStage.StencilTest))
            elif checks[1]:
                msg += ' If your draw relies on front faces then this could be the problem.'
                self.analysis_steps.append(ResultStep(msg=msg.format(test='front face test', s=back),
                                                      pipe_stage=qrd.PipelineStage.StencilTest))

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

    def get_buf(self, resid: rd.ResourceId):
        for b in self.buffers:
            if b.resourceId == resid:
                return b
        return None


def analyse_draw(ctx: qrd.CaptureContext, eid: int, finished_callback):
    # define a local function that wraps the detail of needing to invoke back/forth onto replay thread
    def _replay_callback(r: rd.ReplayController):
        analysis = Analysis(ctx, eid, r)

        # Invoke back onto the UI thread to display the results
        ctx.Extensions().GetMiniQtHelper().InvokeOntoUIThread(lambda: finished_callback(analysis.get_steps()))

    ctx.Replay().AsyncInvoke('where_is_my_draw', _replay_callback)
