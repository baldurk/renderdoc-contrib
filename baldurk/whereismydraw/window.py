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
        self.resultsOutput = mqt.CreateOutputRenderingWidget()
        self.resultsSpacer = mqt.CreateSpacer(False)
        mqt.AddWidget(vert, self.resultsText)
        self.resultsNavigationBar = mqt.CreateHorizontalContainer()
        self.resultsPrev = mqt.CreateButton(lambda c, w, d: self.gotoPreviousStep())
        mqt.SetWidgetText(self.resultsPrev, "Previous Step")
        self.resultsNext = mqt.CreateButton(lambda c, w, d: self.gotoNextStep())
        mqt.SetWidgetText(self.resultsNext, "Next Step")
        self.resultsNext = mqt.CreateButton(lambda c, w, d: self.gotoDetails())
        mqt.SetWidgetText(self.resultsNext, "Go to details")
        mqt.AddWidget(self.resultsNavigationBar, self.resultsPrev)
        mqt.AddWidget(self.resultsNavigationBar, self.resultsNext)
        mqt.AddWidget(vert, self.resultsNavigationBar)
        mqt.AddWidget(vert, self.resultsOutput)
        mqt.AddWidget(vert, self.resultsSpacer)

        # Reset state using this to avoid duplicated logic
        self.OnCaptureClosed()

        ctx.AddDockWindow(self.topWindow, qrd.DockReference.MainToolArea, None)
        ctx.AddCaptureViewer(self)

    def OnCaptureLoaded(self):
        pass

    def OnCaptureClosed(self):
        mqt.SetWidgetText(self.analyseButton, "Analyse draw")
        mqt.SetWidgetEnabled(self.analyseButton, False)

        mqt.SetWidgetVisible(self.resultsText, True)
        mqt.SetWidgetVisible(self.resultsNavigationBar, False)
        mqt.SetWidgetVisible(self.resultsOutput, False)
        mqt.SetWidgetVisible(self.resultsSpacer, True)

        mqt.SetWidgetText(self.resultsText, "No results available.")

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
        eid = self.ctx.CurEvent()
        print("Analysing {}".format(eid))
        mqt.SetWidgetText(self.analyseLabel, "Analysis in progress, please wait!")
        mqt.SetWidgetEnabled(self.analyseButton, False)

        analyse.analyse_draw(self.ctx, eid, lambda results: self.finish_analysis(results))

    def finish_analysis(self, results):
        mqt.SetWidgetText(self.analyseLabel, "")
        mqt.SetWidgetEnabled(self.analyseButton, True)

        print("Analysis results: {}".format(results))

    def gotoPreviousStep(self):
        pass

    def gotoNextStep(self):
        pass

    def gotoDetails(self):
        pass


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
