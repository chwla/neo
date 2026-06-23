from app.services.research.types import (
    DEPTH_CONFIG,
    DepthMode,
    JobStatus,
    ResearchJob,
    StartResearchRequest,
)

_JOB_EXPORTS = {"cancel_job", "create_job", "get_job", "start_job"}
_STORE_EXPORTS = {"clear_all_jobs", "initialize_research_tables", "list_jobs"}


def __getattr__(name: str):
    if name in _JOB_EXPORTS:
        from app.services.research import jobs

        return getattr(jobs, name)
    if name in _STORE_EXPORTS:
        from app.services.research import store

        return getattr(store, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = [
    "clear_all_jobs",
    "DEPTH_CONFIG",
    "DepthMode",
    "JobStatus",
    "ResearchJob",
    "StartResearchRequest",
    "cancel_job",
    "create_job",
    "get_job",
    "initialize_research_tables",
    "list_jobs",
    "start_job",
]
