# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 NOLGIA Inc
"""Pure Python parts of the NOLGIA plugin for DaVinci Resolve.

A copy of the Blender plugin's core/ (nolgiainc/nolgia-blender): the same
files, kept the same by tests/test_core_copy.py, which lists the few places they may differ
(this file's constants, the per-app argument checks in commands.py, and the
app's name in messages). Nothing in this package talks to Resolve.
"""

PLUGIN_VERSION = "0.2.5"
APP = "resolve"
DEFAULT_API_URL = "https://api.nolgia.ai/v1"
DEVICE_CLIENT_ID = "nolgia-resolve"
DEVICE_SCOPE = "bridge assets:read assets:write"

# nolgia.ai pages the panel opens.
AGENT_URL = "https://nolgia.ai/agent"
BILLING_URL = "https://nolgia.ai/billing"

# The line under the chat box: NOLGIA Agent first, at the person's own rate
# when NOLGIA tells it (GET /agent/models for the Agent's brain), then the
# agents people already know.
OTHER_AGENTS = "Claude, Cursor, Codex or another agent"
COST_LINE_RATE = (
    "NOLGIA Agent works right here, each turn from {credits} credits on your model. "
    "You can also use " + OTHER_AGENTS + " with NOLGIA connected."
)
COST_LINE = COST_LINE_RATE.replace("{credits}", "10")
