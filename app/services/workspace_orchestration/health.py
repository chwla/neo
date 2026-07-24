def score(failed_checks, open_blockers):
    return max(0, 100 - failed_checks * 8 - open_blockers * 15)
