"""The campaign system: a campaign collects, mines and checks the advisories of chosen ecosystems, can be paused and resumed, and several can run side by side.

``repo`` stores them in local SQLite; the rest of the package (plan, stages, runner, control, admission, preflight, analytics) is built on it.
"""
