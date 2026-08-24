"""Shared, tool-adjacent infrastructure that outlived the connector system.

``permissions.py`` gates every agent tool call (built-in included), and
``security.py`` provides DNS-rebinding defence for anything fetching a remote
URL. Both are imported directly as submodules -- nothing is re-exported here.
"""
