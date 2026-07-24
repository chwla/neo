def is_ready(checks):
    return bool(checks) and all(check["status"] == "passed" for check in checks)
