# ruff: noqa
def open_blockers(nodes):
    return [
        node for node in nodes if node["node_type"] == "blocker" and node["status"] != "resolved"
    ]
