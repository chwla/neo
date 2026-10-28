"""Disclosure provenance and authorization.

Public surface for labelling values (:mod:`labels`), authorizing outbound
disclosure (:mod:`gate`) and recording workspace-artifact restrictions
(:mod:`artifacts`). See docs/security/data-isolation.md.
"""

from app.services.disclosure.artifacts import ArtifactLabels
from app.services.disclosure.gate import (
    Approval,
    Decision,
    Destination,
    DisclosureBlocked,
    Grant,
    GrantStore,
    authorize,
    require,
)
from app.services.disclosure.labels import (
    PUBLIC_BASES,
    Label,
    Labeled,
    content_hash,
    from_json,
    gather_labeled,
    invocation_label,
    join,
    label_of,
    public,
    run_in_thread_labeled,
    to_json,
    wrap_outputs,
)

__all__ = [
    "Approval",
    "ArtifactLabels",
    "Decision",
    "Destination",
    "DisclosureBlocked",
    "Grant",
    "GrantStore",
    "Label",
    "Labeled",
    "PUBLIC_BASES",
    "authorize",
    "content_hash",
    "from_json",
    "gather_labeled",
    "invocation_label",
    "join",
    "label_of",
    "public",
    "require",
    "run_in_thread_labeled",
    "to_json",
    "wrap_outputs",
]
