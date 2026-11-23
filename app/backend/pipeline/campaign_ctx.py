"""The campaign the running process works for.

The campaign engine starts one job process per campaign and puts the campaign's id in that process's environment (``ZTSA_CAMPAIGN_ID``). The stores read it here to
stamp what the campaign touches (a fix commit it queued or mined carries the id), so the work of a campaign can be told apart afterwards. A process that is not a
campaign (a manual job, the API) has no id and stamps nothing.
"""
from __future__ import annotations
import os

ENV = "ZTSA_CAMPAIGN_ID"


def current() -> str | None:
    """The id of the campaign this process runs for, or None."""
    return os.environ.get(ENV) or None
