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
from . import analyse

mqt: qrd.MiniQtHelper


class Window(qrd.CaptureViewer):
    def __init__(self, ctx: qrd.CaptureContext, version: str):
        super().__init__()

        print("Creating WIMD window")

        self.ctx = ctx
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
        self.resultsText = mqt.CreateLabel()
        self.texOutWidget = mqt.CreateOutputRenderingWidget()
        self.meshOutWidget = mqt.CreateOutputRenderingWidget()
        self.resultsSpacer = mqt.CreateSpacer(False)
        self.navSpacer = mqt.CreateSpacer(True)
        mqt.AddWidget(vert, self.resultsText)
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
        mqt.AddWidget(vert, self.resultsNavigationBar)
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

        mqt.SetWidgetVisible(self.resultsText, True)
        mqt.SetWidgetVisible(self.resultsNavigationBar, False)
        mqt.SetWidgetVisible(self.texOutWidget, False)
        mqt.SetWidgetVisible(self.meshOutWidget, False)
        mqt.SetWidgetVisible(self.resultsSpacer, True)

        mqt.SetWidgetText(self.resultsText, "No results available.")

        self.cur_result = 0
        self.results = []

    def OnSelectedEventChanged(self, event):
        pass

    def OnEventChanged(self, event):
        draw: rd.DrawcallDescription = self.ctx.GetDrawcall(event)

        if draw is not None and (draw.flags & rd.DrawFlags.Drawcall):
            mqt.SetWidgetText(self.analyseButton, "Analyse draw {}: {}".format(draw.eventId, draw.name))
            mqt.SetWidgetEnabled(self.analyseButton, True)
        else:
            mqt.SetWidgetText(self.analyseButton, "Can't analyse {}, select a draw".format(event))
            mqt.SetWidgetEnabled(self.analyseButton, False)

    def start_analysis(self):
        self.eid = self.ctx.CurEvent()
        print("Analysing {}".format(self.eid))
        mqt.SetWidgetText(self.analyseLabel, "Analysis in progress, please wait!")
        mqt.SetWidgetEnabled(self.analyseButton, False)

        analyse.analyse_draw(self.ctx, self.eid, lambda results: self.finish_analysis(results))

    def finish_analysis(self, results):
        print("Analysis finished")
        mqt.SetWidgetText(self.analyseLabel, "")
        mqt.SetWidgetEnabled(self.analyseButton, True)

        mqt.SetWidgetVisible(self.resultsNavigationBar, True)

        self.results = results
        self.cur_result = 0

        self.refresh_result()

    def goto_previous_step(self):
        self.cur_result = max(self.cur_result - 1, 0)

        self.refresh_result()

    def goto_next_step(self):
        self.cur_result = min(self.cur_result + 1, len(self.results) - 1)

        self.refresh_result()

    def refresh_result(self):
        r = self.results[self.cur_result]

        mqt.SetWidgetEnabled(self.resultsPrev, self.cur_result > 0)
        mqt.SetWidgetEnabled(self.resultsNext, self.cur_result < len(self.results) - 1)

        mqt.SetWidgetEnabled(self.showDetails,
                             any(x in r for x in ['pipe_stage', 'tex_display', 'mesh_view', 'pixel_history']))

        draw: rd.DrawcallDescription = self.ctx.GetDrawcall(self.eid)

        text = "Results for draw {}: {}. Analysis step {} of {}".format(self.eid, draw.name, self.cur_result + 1,
                                                                        len(self.results))
        text += '\n\n'
        text += r['msg']

        mqt.SetWidgetText(self.resultsText, text)

        mqt.SetWidgetVisible(self.texOutWidget, False)
        mqt.SetWidgetVisible(self.meshOutWidget, False)
        mqt.SetWidgetVisible(self.resultsSpacer, True)

        if 'tex_display' in r:
            tex: rd.TextureDisplay = r['tex_display']

            self.ctx.Replay().AsyncInvoke('', lambda _: self.texOut.SetTextureDisplay(tex))

            mqt.SetWidgetVisible(self.texOutWidget, True)
            mqt.SetWidgetVisible(self.resultsSpacer, False)

    def goto_details(self):
        r = self.results[self.cur_result]

        if 'pipe_stage' in r:
            self.ctx.ShowPipelineViewer()
            panel = self.ctx.GetPipelineViewer()
            panel.SelectPipelineStage(r['pipe_stage'])

            self.ctx.RaiseDockWindow(panel.Widget())
            return

        if 'tex_display' in r:
            tex: rd.TextureDisplay = r['tex_display']
            self.ctx.ShowTextureViewer()
            panel = self.ctx.GetTextureViewer()
            panel.ViewTexture(tex.resourceId, tex.typeCast, True)
            panel.SetSelectedSubresource(tex.subresource)
            panel.SetTextureOverlay(tex.overlay)
            panel.SetZoomLevel(True, 1.0)

            self.ctx.RaiseDockWindow(panel.Widget())
            return

        if 'mesh_view' in r:
            self.ctx.ShowMeshPreview()
            panel = self.ctx.GetMeshPreview()
            panel.ScrollToRow(0, r['mesh_view'])

            panel.SetPreviewStage(r['mesh_view'])

            self.ctx.RaiseDockWindow(panel.Widget())
            return

        if 'pixel_history' in r:
            history = r['pixel_history']
            panel = self.ctx.ViewPixelHistory(history['id'], history['x'], history['y'], history['tex_display'])
            panel.SetHistory(history['history'])

            self.ctx.AddDockWindow(panel.Widget(), qrd.DockReference.AddTo, self.topWindow)
            return


cur_window = None


def closed():
    global cur_window
    cur_window = None


def get_window(ctx, version):
    global cur_window, mqt

    mqt = ctx.Extensions().GetMiniQtHelper()

    if cur_window is None:
        cur_window = Window(ctx, version)

    return cur_window.topWindow
