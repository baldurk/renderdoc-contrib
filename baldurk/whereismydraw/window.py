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
from typing import Optional
from . import analyse

mqt: qrd.MiniQtHelper


def format_mod(mod: rd.ModificationValue):
    if mod.stencil < 0:
        return 'Depth: {:.4}\n'.format(mod.depth)
    else:
        return 'Depth: {:.4} Stencil: {:02x}\n'.format(mod.depth, mod.stencil)


class Window(qrd.CaptureViewer):
    def __init__(self, ctx: qrd.CaptureContext, version: str):
        super().__init__()

        print("Creating WIMD window")

        self.ctx = ctx
        self.version = version
        self.topWindow = mqt.CreateToplevelWidget("Where is my Draw?", lambda c, w, d: closed())

        vert = mqt.CreateVerticalContainer()
        mqt.AddWidget(self.topWindow, vert)

        self.analyseButton = mqt.CreateButton(lambda c, w, d: self.start_analysis())
        self.analyseLabel = mqt.CreateLabel()
        # Add inside a horizontal container to left align it
        horiz = mqt.CreateHorizontalContainer()
        mqt.AddWidget(horiz, self.analyseButton)
        mqt.AddWidget(horiz, self.analyseLabel)
        mqt.AddWidget(horiz, mqt.CreateSpacer(True))
        mqt.AddWidget(vert, horiz)

        self.results = mqt.CreateGroupBox(False)
        mqt.SetWidgetText(self.results, "Results")
        mqt.AddWidget(vert, self.results)

        vert = mqt.CreateVerticalContainer()
        mqt.AddWidget(self.results, vert)
        self.summaryText = mqt.CreateLabel()
        self.stepText = mqt.CreateLabel()
        self.texOutWidget = mqt.CreateOutputRenderingWidget()
        self.meshOutWidget = mqt.CreateOutputRenderingWidget()
        self.resultsSpacer = mqt.CreateSpacer(False)
        self.navSpacer = mqt.CreateSpacer(True)
        self.resultsNavigationBar = mqt.CreateHorizontalContainer()
        self.resultsPrev = mqt.CreateButton(lambda c, w, d: self.goto_previous_step())
        mqt.SetWidgetText(self.resultsPrev, "Previous Step")
        self.resultsNext = mqt.CreateButton(lambda c, w, d: self.goto_next_step())
        mqt.SetWidgetText(self.resultsNext, "Next Step")
        self.showDetails = mqt.CreateButton(lambda c, w, d: self.goto_details())
        mqt.SetWidgetText(self.showDetails, "Show More Info")
        mqt.AddWidget(self.resultsNavigationBar, self.resultsPrev)
        mqt.AddWidget(self.resultsNavigationBar, self.resultsNext)
        mqt.AddWidget(self.resultsNavigationBar, self.showDetails)
        mqt.AddWidget(self.resultsNavigationBar, self.navSpacer)
        mqt.AddWidget(vert, self.summaryText)
        mqt.AddWidget(vert, self.resultsNavigationBar)
        mqt.AddWidget(vert, self.stepText)
        mqt.AddWidget(vert, self.texOutWidget)
        mqt.AddWidget(vert, self.meshOutWidget)
        mqt.AddWidget(vert, self.resultsSpacer)

        self.texOut: rd.ReplayOutput
        self.texOut = None
        self.meshOut: rd.ReplayOutput
        self.meshOut = None

        self.eid = 0

        self.cur_result = 0
        self.results = []

        # Reset state using this to avoid duplicated logic
        self.OnCaptureClosed()

        ctx.AddDockWindow(self.topWindow, qrd.DockReference.MainToolArea, None)
        ctx.AddCaptureViewer(self)

    def OnCaptureLoaded(self):
        self.reset()

        tex_data: rd.WindowingData = mqt.GetWidgetWindowingData(self.texOutWidget)
        mesh_data: rd.WindowingData = mqt.GetWidgetWindowingData(self.meshOutWidget)

        def set_widgets():
            mqt.SetWidgetReplayOutput(self.texOutWidget, self.texOut)
            mqt.SetWidgetReplayOutput(self.meshOutWidget, self.meshOut)

        def make_out(r: rd.ReplayController):
            self.texOut = r.CreateOutput(tex_data, rd.ReplayOutputType.Texture)
            self.meshOut = r.CreateOutput(mesh_data, rd.ReplayOutputType.Texture)
            mqt.InvokeOntoUIThread(set_widgets)

        self.ctx.Replay().AsyncInvoke('', make_out)

    def OnCaptureClosed(self):
        self.reset()

    def reset(self):
        mqt.SetWidgetText(self.analyseButton, "Analyse draw")
        mqt.SetWidgetEnabled(self.analyseButton, False)

        mqt.SetWidgetVisible(self.summaryText, True)
        mqt.SetWidgetVisible(self.stepText, False)
        mqt.SetWidgetVisible(self.resultsNavigationBar, False)
        mqt.SetWidgetVisible(self.texOutWidget, False)
        mqt.SetWidgetVisible(self.meshOutWidget, False)
        mqt.SetWidgetVisible(self.resultsSpacer, True)

        mqt.SetWidgetText(self.summaryText, "No analysis available.")
        mqt.SetWidgetText(self.stepText, "")

        self.cur_result = 0
        self.results = []

        def shutdown_out(r: rd.ReplayController):
            if self.texOut is not None:
                self.texOut.Shutdown()
            if self.meshOut is not None:
                self.meshOut.Shutdown()
            self.texOut = None
            self.meshOut = None

        self.ctx.Replay().AsyncInvoke('', shutdown_out)

    def closed(self):
        self.reset()

    def OnSelectedEventChanged(self, event):
        pass

    def OnEventChanged(self, event):
        draw = self.ctx.GetAction(event)

        if draw is not None and (draw.flags & rd.ActionFlags.Drawcall):
            mqt.SetWidgetText(self.analyseButton, "Analyse draw {}: {}".format(draw.eventId, self.get_action_name(draw)))
            mqt.SetWidgetEnabled(self.analyseButton, True)
        else:
            mqt.SetWidgetText(self.analyseButton, "Can't analyse {}, select a draw".format(event))
            mqt.SetWidgetEnabled(self.analyseButton, False)

        self.refresh_result()

    def get_action_name(self, draw: rd.ActionDescription):
        return draw.GetName(self.ctx.GetStructuredFile())

    def start_analysis(self):
        self.eid = self.ctx.CurEvent()
        print("Analysing {}".format(self.eid))
        mqt.SetWidgetEnabled(self.analyseButton, False)

        mqt.SetWidgetText(self.summaryText, "Analysis in progress, please wait!")
        mqt.SetWidgetText(self.stepText, "")
        mqt.SetWidgetVisible(self.summaryText, True)
        mqt.SetWidgetVisible(self.stepText, False)
        mqt.SetWidgetVisible(self.resultsNavigationBar, False)
        mqt.SetWidgetVisible(self.texOutWidget, False)
        mqt.SetWidgetVisible(self.meshOutWidget, False)
        mqt.SetWidgetVisible(self.resultsSpacer, True)

        analyse.analyse_draw(self.ctx, self.eid, lambda results: self.finish_analysis(results))

    def finish_analysis(self, results):
        print("Analysis finished")
        mqt.SetWidgetEnabled(self.analyseButton, True)

        mqt.SetWidgetVisible(self.stepText, True)
        mqt.SetWidgetVisible(self.resultsNavigationBar, True)

        self.results = results
        self.cur_result = 0

        draw = self.ctx.GetAction(self.eid)

        if len(self.results) == 0:
            mqt.SetWidgetText(self.summaryText,
                              "Analysis failed for {}: {}!".format(self.eid, self.get_action_name(draw)))
        else:
            mqt.SetWidgetText(self.summaryText, "Conclusion of analysis for {}: {}:\n\n{}"
                              .format(self.eid, self.get_action_name(draw), self.format_step_text(-1)))

        self.refresh_result()

    def goto_previous_step(self):
        self.cur_result = max(self.cur_result - 1, 0)

        self.refresh_result()

    def goto_next_step(self):
        self.cur_result = min(self.cur_result + 1, len(self.results) - 1)

        self.refresh_result()

    def refresh_result(self):
        if len(self.results) == 0:
            mqt.SetWidgetText(self.summaryText, "No results available.")
            mqt.SetWidgetText(self.stepText, "")
            mqt.SetWidgetVisible(self.summaryText, True)
            mqt.SetWidgetVisible(self.stepText, False)
            mqt.SetWidgetVisible(self.resultsNavigationBar, False)
            mqt.SetWidgetVisible(self.texOutWidget, False)
            mqt.SetWidgetVisible(self.meshOutWidget, False)
            mqt.SetWidgetVisible(self.resultsSpacer, True)
            return

        step = analyse.ResultStep()
        if self.cur_result in range(len(self.results)):
            step = self.results[self.cur_result]

        mqt.SetWidgetEnabled(self.resultsPrev, self.cur_result > 0)
        mqt.SetWidgetEnabled(self.resultsNext, self.cur_result < len(self.results) - 1)

        mqt.SetWidgetEnabled(self.showDetails, step.has_details())

        text = self.format_step_text(self.cur_result)

        if self.ctx.GetAction(self.eid):
            text = "Analysis step {} of {}:\n\n{}".format(self.cur_result + 1, len(self.results), text)

        mqt.SetWidgetVisible(self.texOutWidget, False)
        mqt.SetWidgetVisible(self.meshOutWidget, False)
        mqt.SetWidgetVisible(self.resultsSpacer, True)

        display = False
        if step.tex_display.resourceId != rd.ResourceId.Null():
            display = True

            self.ctx.Replay().AsyncInvoke('', lambda _: self.texOut.SetTextureDisplay(step.tex_display))

            mqt.SetWidgetVisible(self.texOutWidget, True)
            mqt.SetWidgetVisible(self.resultsSpacer, False)

        if display and self.eid != self.ctx.CurEvent():
            mqt.SetWidgetVisible(self.texOutWidget, False)
            mqt.SetWidgetVisible(self.meshOutWidget, False)
            mqt.SetWidgetVisible(self.resultsSpacer, True)

            selected_eid = self.ctx.CurEvent()
            selected_draw = self.ctx.GetAction(selected_eid)

            text += '\n\n'
            text += 'Can\'t display visualisation for this step while another event {}: {} is selected' \
                .format(selected_eid, self.get_action_name(selected_draw))

        mqt.SetWidgetText(self.stepText, text)

    def format_step_text(self, step_index: int):
        step = self.results[step_index]

        text = step.msg

        if step.pixel_history.id != rd.ResourceId():
            text += '\n\n'
            text += 'Full pixel history results at {},{} on {}:\n\n'.format(step.pixel_history.x, step.pixel_history.y,
                                                                            step.pixel_history.id)

            # filter the history only to the event in question, and the last prior passing event.
            history = [h for h in step.pixel_history.history if
                       h.eventId == self.eid or h.eventId == step.pixel_history.last_eid]

            # remove any failing fragments from the previous draw
            history = [h for h in history if h.Passed() or h.eventId == self.eid]

            # remove all but the last fragment from the previous draw
            while len(history) > 2 and history[0].eventId == history[1].eventId == step.pixel_history.last_eid:
                del history[0]

            prev_eid = 0

            for h in history:
                d = self.ctx.GetAction(h.eventId)

                if d is None:
                    name = '???'
                else:
                    name = self.get_action_name(d)

                if prev_eid != h.eventId:
                    text += "* @{}: {}\n".format(h.eventId, name)
                    prev_eid = h.eventId

                prim = 'Unknown primitive'
                if h.primitiveID != 0xffffffff:
                    prim = 'Primitive {}'.format(h.primitiveID)
                text += '  - {}:\n'.format(prim)
                text += '    Before: {}'.format(format_mod(h.preMod))
                text += '    Fragment: Depth: {:.4}\n'.format(h.shaderOut.depth)
                if h.sampleMasked:
                    text += '    The sample mask did not include this sample.\n'
                elif h.backfaceCulled:
                    text += '    The primitive was backface culled.\n'
                elif h.depthClipped:
                    text += '    The fragment was clipped by near/far plane.\n'
                elif h.depthBoundsFailed:
                    text += '    The fragment was clipped by the depth bounds.\n'
                elif h.scissorClipped:
                    text += '    The fragment was clipped by the scissor region.\n'
                elif h.shaderDiscarded:
                    text += '    The pixel shader discarded this fragment.\n'
                elif h.depthTestFailed:
                    text += '    The fragment failed the depth test outputting.\n'
                elif h.stencilTestFailed:
                    text += '    The fragment failed the stencil test.\n'
                text += '    After: {}'.format(format_mod(h.postMod))

        return text

    def goto_details(self):
        step: analyse.ResultStep = self.results[self.cur_result]

        if step.pipe_stage != qrd.PipelineStage.ComputeShader:
            self.ctx.ShowPipelineViewer()
            panel = self.ctx.GetPipelineViewer()
            panel.SelectPipelineStage(step.pipe_stage)

            self.ctx.RaiseDockWindow(panel.Widget())
            return

        if step.tex_display.resourceId != rd.ResourceId.Null():
            self.ctx.ShowTextureViewer()
            panel = self.ctx.GetTextureViewer()
            panel.ViewTexture(step.tex_display.resourceId, step.tex_display.typeCast, True)
            panel.SetSelectedSubresource(step.tex_display.subresource)
            panel.SetTextureOverlay(step.tex_display.overlay)
            panel.SetZoomLevel(True, 1.0)

            self.ctx.RaiseDockWindow(panel.Widget())
            return

        if step.mesh_view != rd.MeshDataStage.Count:
            self.ctx.ShowMeshPreview()
            panel = self.ctx.GetMeshPreview()
            panel.ScrollToRow(0, step.mesh_view)

            panel.SetPreviewStage(step.mesh_view)

            self.ctx.RaiseDockWindow(panel.Widget())
            return

        if step.pixel_history.id != rd.ResourceId():
            panel = self.ctx.ViewPixelHistory(step.pixel_history.id, step.pixel_history.x, step.pixel_history.y,
                                              step.pixel_history.view, step.pixel_history.tex_display)
            panel.SetHistory(step.pixel_history.history)

            self.ctx.AddDockWindow(panel.Widget(), qrd.DockReference.AddTo, self.topWindow)
            return


cur_window: Optional[Window] = None


def closed():
    global cur_window
    if cur_window is not None:
        cur_window.closed()
        cur_window.ctx.RemoveCaptureViewer(cur_window)
    cur_window = None


def get_window(ctx, version):
    global cur_window, mqt

    mqt = ctx.Extensions().GetMiniQtHelper()

    if cur_window is None:
        cur_window = Window(ctx, version)

    return cur_window.topWindow
