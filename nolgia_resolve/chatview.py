# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 NOLGIA Inc
"""How the NOLGIA window in DaVinci Resolve shows the NOLGIA Agent chat.

The chat itself (the API calls, one message per turn, the pin to this
Resolve, what is remembered) is core/chat.py, the same file as the Blender
plugin's. This is the Resolve side: what to suggest before the first
message, and the thread as the HTML the window's read-only TextEdit shows.
"""

import html
import re

# Before the first message: things this plugin and NOLGIA's DaVinci Resolve
# workflows do. A click puts the whole sentence in the box. The short label
# is the button's text, kept short so it never widens the window.
EXAMPLES = (
    ("Add my newest NOLGIA clip to the timeline", "Add my newest NOLGIA clip to the end of the timeline."),
    ("Give every clip the Kodak Portra 400 look", "Give every clip on this timeline the Kodak Portra 400 look."),
    ("Send this frame to my NOLGIA library", "Export the current frame as a PNG to my NOLGIA library."),
)


def anchor(message_id):
    """The name of the anchor a message starts with (for ScrollToAnchor)."""
    return "m" + re.sub(r"[^0-9A-Za-z]", "", str(message_id or ""))


END = "end"


def thread_html(view):
    """The thread (core/chat.py's view()) as HTML. A reply still being
    worked on is not drawn: the line under the thread says what it is doing."""
    out = []
    if view["hidden"]:
        out.append('<p style="color:#8a8a8a">Earlier messages are on nolgia.ai.</p>')
    for row in view["rows"]:
        if row["role"] == "agent" and row["status"] == "pending":
            continue
        out.append('<a name="%s"></a>' % anchor(row["id"]))
        if row["role"] != "agent":
            who = "BACKGROUND WORK FINISHED" if row["background"] else "YOU"
            text = html.escape(row["text"]).replace("\n", "<br>")
            out.append('<p style="margin-top:10px"><span style="color:#8a8a8a; font-size:small"><b>%s</b></span>'
                       "<br>%s</p>" % (who, text))
            continue
        out.append('<p style="margin-top:10px"><span style="color:#ff6a1a; font-size:small"><b>NOLGIA AGENT</b>'
                   "</span></p>")
        for block in row["blocks"] or []:
            body = "".join(_part_html(p) for p in block["parts"])
            out.append("<p>%s%s</p>" % ("&#8226; " if block["type"] == "li" else "", body))
        if row["failure"]:
            out.append('<p style="color:#ff8a80">%s</p>' % html.escape(row["failure"]))
        if row["question"]:
            out.append('<p style="color:#8a8a8a">Answer with a button below, or type your own.</p>')
        if row["credits"] is not None and row["status"] == "complete":
            out.append('<p style="color:#8a8a8a; font-size:small">%s credits</p>' % _number(row["credits"]))
    # The end of the thread, for ScrollToAnchor (an anchor needs a block after
    # it to land on).
    out.append('<p><a name="%s"></a>&nbsp;</p>' % END)
    return "".join(out)


def scroll_target(view):
    """Where the thread is scrolled after it changed: a finished reply is read
    from its start, anything else from the end."""
    shown = [r for r in view["rows"] if not (r["role"] == "agent" and r["status"] == "pending")]
    newest = shown[-1] if shown else None
    pending = len(shown) != len(view["rows"])
    if newest is not None and newest["role"] == "agent" and not pending:
        return anchor(newest["id"])
    return END


def _part_html(part):
    text = html.escape(part["text"])
    if part.get("href"):
        return '<a href="%s">%s</a>' % (html.escape(part["href"], quote=True), text)
    if part.get("bold"):
        return "<b>%s</b>" % text
    return text


def _number(value):
    return str(int(value)) if float(value).is_integer() else str(value)
