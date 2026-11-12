"""Source provenance and what each evidence item can do for its claim.

Provenance answers a question about a source, independent of any claim: how well is it
established who published it? The judge may propose a role and issuer, but a small model often
proposes nothing, so the role is also inferred from structure: who the page says publishes it,
whether the registrable domain *is* a research subject's name (the "verified domain" signal),
public-sector and academic suffixes, news markup, and discussion-style addresses. Ownership is
claimed conservatively: a page is a subject's own record only when its domain is exactly that
subject's name and the page names the subject as its publisher, so a lookalike such as
"<name>-features.net" never qualifies however it describes itself. What a page says about
itself can be copied, so grade A needs support from other, independent pages read in the same
run. There are no lists of known sites, domains or topics anywhere in this module.

Claim fit answers a question about one evidence item and one target: does this item establish
the claim, support it, or only give context? The same excellent source can establish one claim
and be only context for another.
"""

from __future__ import annotations

import re
from collections import defaultdict
from typing import Literal

from app.services.research.textutil import (
    canonicalize_url,
    compact,
    host_group,
    host_of,
    jaccard,
    mentions_any,
    name_forms,
    normalize_ws,
    scope_conflict,
    token_set,
)
from app.services.research.types import (
    AuthorityKind,
    ClaimFit,
    ClaimScope,
    EvidenceItem,
    EvidenceProfile,
    EvidenceTarget,
    FreshnessFit,
    LeadStatus,
    OriginLead,
    Provenance,
    RecencyKind,
    ResearchPlan,
    ResearchSource,
    SourceAssessment,
    SourceRole,
    Support,
    TargetForm,
)

_PUBLIC_SECTOR = frozenset({"gov", "mil", "gouv", "gob", "govt", "go"})
_INSTITUTIONAL = frozenset({"edu", "ac"})


def public_suffix_class(host: str) -> Literal["government", "academic"] | None:
    """Public-sector and academic naming conventions of a host: a weak signal, never enough."""
    labels = [label for label in host.lower().split(".") if label]
    if len(labels) < 2:
        return None
    tld = labels[-1]
    second = labels[-2] if len(labels) >= 3 else ""
    if tld in ("gov", "mil") or (len(tld) == 2 and second in _PUBLIC_SECTOR):
        return "government"
    if tld == "edu" or (len(tld) == 2 and second in _INSTITUTIONAL):
        return "academic"
    return None


def _name_matches(value: str, forms: set[str], *, allow_suffix: bool) -> bool:
    """Whether a self-declared name is the issuer's.

    Site names must equal a name or alias exactly: a lookalike can call itself
    "<name> <anything>". Organization names may carry a short legal-form suffix.
    """
    candidate = compact(value)
    if len(candidate) < 3:
        return False
    for form in forms:
        if candidate == form:
            return True
        if allow_suffix and candidate.startswith(form) and len(candidate) - len(form) <= 12:
            return True
    return False


# --- Provenance and claim fit ---------------------------------------------------------------
#
# Provenance answers one question about a source, independent of any claim: how well is it
# established who published it? Signals a page states about itself can be copied by a mirror or
# a lookalike, so they reach at most grade B. Grade A needs something outside the page: another,
# independent source that cites it, links to its site naming its issuer, carries its document
# identifier, or declares it canonical. Nothing here looks at hostnames resembling names.

ISSUER_ROLES = frozenset({SourceRole.ISSUER_RECORD, SourceRole.INSTITUTIONAL})
#: Address segments of discussion pages: forums, threads, comments, Q&A.
_DISCUSSION_PATH = re.compile(
    r"/(?:forums?|threads?|comments|community|communities|discussions?|questions|answers|"
    r"topic|t|r|board|boards|showthread\.php|viewtopic\.php)(?:/|$|\?)",
    re.IGNORECASE,
)
_DISCUSSION_HOST = re.compile(r"^(?:forums?|community|answers|discuss|boards?)\.", re.IGNORECASE)
#: Publisher names that declare a reference work.
_REFERENCE_NAME = re.compile(r"wikipedia|encyclop(?:a)?edia", re.IGNORECASE)
_NEWS_SCHEMA = re.compile(r"NewsArticle|ReportageNewsArticle|AnalysisNewsArticle", re.IGNORECASE)
#: Language a publisher uses to rate itself; a subject's own page saying it is not evidence.
_SELF_PRAISE = re.compile(
    r"\b(?:best|better|leading|superior|top|#1|number one|world'?s|award[- ]winning|"
    r"most (?:popular|trusted|reliable|advanced)|unmatched|unbeatable)\b",
    re.IGNORECASE,
)
_UNTRUSTED_ROLES = frozenset({SourceRole.AGGREGATOR, SourceRole.PROMOTIONAL})
_IDENTITY_SIGNALS = frozenset(
    {"issuer_self_id", "cross_page_self_id", "document_metadata", "dataset_schema",
     "verified_domain"}
)
_POOR_EXTRACTION = 0.35


def assess_sources(
    sources: list[ResearchSource],
    roles: dict,
    leads: list[OriginLead],
    *,
    sources_citing_leads: set[str] | frozenset[str] = frozenset(),
    plan: ResearchPlan | None = None,
) -> None:
    """Give every readable source its provenance, in place.

    ``roles`` maps source ids to the judge's labels (anything with ``role``, ``issuer`` and
    ``review_status``); a source the judge said nothing about gets the role its structure
    shows. Sources are reassessed on every call: a page read later can cite or link one read
    earlier, and that raises the earlier page's standing.
    """
    readable = [s for s in sources if s.reviewed and s.rejection_reason is None]
    for source in readable:
        label = roles.get(source.id)
        previous = source.assessment or SourceAssessment()
        role = _coerce_role(label.role) if label is not None else previous.role
        # Only an issuer the judge named is checked against the domain; one inferred on an
        # earlier pass is not evidence of anything.
        judged_issuer = normalize_ws(label.issuer)[:120] if label and label.issuer else ""
        inferred_role, inferred_issuer, owned = infer_role(source, plan, judged_issuer)
        issuer = judged_issuer or previous.issuer
        if role == SourceRole.ISSUER_RECORD and not owned:
            # A page may call itself anyone's; without the verified domain it is not theirs.
            role = SourceRole.UNKNOWN
        if role == SourceRole.UNKNOWN:
            role = inferred_role
        source.assessment = SourceAssessment(
            role=role,
            issuer=issuer or inferred_issuer,
            review_status=(
                normalize_ws(label.review_status).lower()[:40]
                if label and label.review_status
                else previous.review_status
            ),
        )
        entities_owned = [o for o in owned if o != "issuer"]
        if entities_owned:
            source.official_for = sorted(set(source.official_for) | set(entities_owned))
        if owned and source.assessment.role == SourceRole.ISSUER_RECORD:
            source.class_signals = sorted(set(source.class_signals) | {"verified_domain"})
    for source in readable:
        assessment = source.assessment
        forms = name_forms([assessment.issuer]) if assessment.issuer else set()
        assessment.self_asserted = _self_signals(source, forms, readable)
        if "verified_domain" in source.class_signals:
            assessment.self_asserted.append("verified_domain")
        assessment.negative = _negative_signals(source)
    for source in readable:
        source.assessment.corroborated = _corroborated_signals(source, readable, leads)
    for source in readable:
        _grade(source, readable, sources_citing_leads, leads)
    _resolve_issuer_conflicts(readable, sources_citing_leads)


def _domain_label(source: ResearchSource) -> str:
    """The registrable name of a source's site: "instagram" for help.instagram.com."""
    return compact(source.host_group.split(".", 1)[0])


def infer_role(
    source: ResearchSource, plan: ResearchPlan | None, issuer: str = ""
) -> tuple[SourceRole, str, list[str]]:
    """The role a page's own structure shows, the issuer it names, and the subjects it is
    verifiably the publisher of (empty unless ownership is confirmed).

    A subject's own record needs two signals that a lookalike cannot both fake: the site's
    registrable domain is exactly the subject's name (or the named issuer's), and the page names
    that subject or issuer as its publisher. Anything less is not ownership.
    """
    label = _domain_label(source)
    publisher = source.citation_meta.get("publisher", "")
    named = [source.site_name, *source.organization_names, publisher]
    owned: list[str] = []
    owner_name = ""
    candidates: list[tuple[str, list[str]]] = []
    if plan is not None:
        # Ownership is claimed only by a subject's full name or an alias that is not a piece
        # of it: "stark.in" is not House Stark's site because "Stark" is part of the name.
        candidates = [
            (
                entity.id,
                [
                    entity.name,
                    *[a for a in entity.aliases if compact(a) not in compact(entity.name)],
                ],
            )
            for entity in plan.understanding.entities
        ]
    if issuer:
        candidates.append(("", [issuer]))
    for entity_id, names in candidates:
        forms = name_forms(names)
        if not forms or label not in forms:
            continue
        title_tail = source.title.split("|")[-1].split(" - ")[-1]
        if _names_issuer(named, forms, allow_suffix=True) or _names_issuer(
            [title_tail], forms, allow_suffix=False
        ):
            # The name the page itself uses, so its other self-identification is read in it.
            owner_name = next(
                (
                    value
                    for value in [*named, title_tail]
                    if value and _names_issuer([value], forms, allow_suffix=True)
                ),
                names[0],
            )
            if entity_id:
                owned.append(entity_id)
            else:
                owned.append("issuer")
    if owned:
        return SourceRole.ISSUER_RECORD, owner_name, owned
    if any(_REFERENCE_NAME.search(value or "") for value in named):
        return SourceRole.REFERENCE, source.site_name or "", []
    path = (source.canonical_url or source.url or "").split("://", 1)[-1]
    path = path[path.find("/") :] if "/" in path else "/"
    if _DISCUSSION_PATH.search(path) or _DISCUSSION_HOST.search(source.host):
        return SourceRole.COMMUNITY, "", []
    if public_suffix_class(source.host) is not None:
        return SourceRole.INSTITUTIONAL, source.site_name or "", []
    if any(_NEWS_SCHEMA.search(kind) for kind in source.schema_types):
        return SourceRole.REPORTING, source.site_name or "", []
    return SourceRole.UNKNOWN, "", []


def claim_fit(
    item: EvidenceItem, plan: ResearchPlan, source: ResearchSource | None
) -> tuple[ClaimFit, bool]:
    """What one evidence item can do for its target, and whether its publisher is a party to it.

    The same excellent source can establish one claim and be only context for another: a
    vendor's documentation establishes its own product's behaviour but not that it beats a
    rival, and a study establishes what it reported but not a field's consensus.
    """
    target = plan.target(item.question_id)
    profile = target.profile if target is not None else EvidenceProfile()
    assessment = (source.assessment if source is not None else None) or SourceAssessment()
    provenance = assessment.provenance
    if provenance == Provenance.D or item.support == Support.NONE:
        return ClaimFit.INSUFFICIENT, False
    if target is not None and target.scope_note and scope_conflict(item.scope, target.scope_note):
        return ClaimFit.CONTEXT, False
    if profile.recency.kind == RecencyKind.CURRENT and item.freshness_fit == FreshnessFit.STALE:
        return ClaimFit.CONTEXT, False
    interested = _interested_party(assessment, plan, target, source, item)
    if interested:
        return ClaimFit.CONTEXT, True

    evidence_scope = item.evidence_scope
    if evidence_scope == ClaimScope.GENERAL and (
        item.single_report or (assessment.role == SourceRole.COMMUNITY and not _has_figure(item))
    ):
        evidence_scope = ClaimScope.INSTANCE
    best = ClaimFit.SUPPORTS if item.support == Support.PARTIAL else ClaimFit.ESTABLISHES

    if profile.claim_scope == ClaimScope.GENERAL:
        if evidence_scope == ClaimScope.INSTANCE:
            return ClaimFit.SUPPORTS, False
        if profile.authority_kind == AuthorityKind.CONSENSUS:
            # What a field has established is stated by a body speaking for it, not by any
            # one paper: a single study establishes only what that study reported.
            speaks_for_the_field = (
                assessment.role == SourceRole.INSTITUTIONAL
                or assessment.review_status == "official"
            )
            if (
                provenance == Provenance.A
                and speaks_for_the_field
                and assessment.review_status in ("peer_reviewed", "official")
                and evidence_scope == ClaimScope.GENERAL
            ):
                return best, False
            return ClaimFit.SUPPORTS, False
        if (
            evidence_scope == ClaimScope.GENERAL
            and item.method_stated
            and provenance in (Provenance.A, Provenance.B)
        ):
            return best, False
        return ClaimFit.SUPPORTS, False

    if assessment.role in ISSUER_ROLES and provenance == Provenance.A:
        return best, False
    if (
        profile.claim_scope == ClaimScope.INSTANCE
        and evidence_scope == ClaimScope.INSTANCE
        and item.single_report
    ):
        # The report itself is the record of an attributed claim that a report was made.
        return best, False
    return ClaimFit.SUPPORTS, False


def _coerce_role(value: str | None) -> SourceRole:
    try:
        return SourceRole(str(value or "").strip().lower())
    except ValueError:
        return SourceRole.UNKNOWN


def _names_issuer(values: list[str], forms: set[str], *, allow_suffix: bool) -> bool:
    return bool(forms) and any(
        _name_matches(value, forms, allow_suffix=allow_suffix) for value in values if value
    )


def _self_signals(
    source: ResearchSource, forms: set[str], readable: list[ResearchSource]
) -> list[str]:
    signals: list[str] = []
    publisher = source.citation_meta.get("publisher", "")
    if _names_issuer([source.site_name], forms, allow_suffix=False) or _names_issuer(
        [*source.organization_names, publisher], forms, allow_suffix=True
    ):
        signals.append("issuer_self_id")
    # A document carries no site name of its own, so what the rest of its site says about who
    # publishes it is that document's self-identification too.
    same_site = [
        other
        for other in readable
        if other.id != source.id
        and other.host_group == source.host_group
        and other.canonical_url != source.canonical_url
        and other.duplicate_of is None
    ]
    if any(
        _names_issuer([other.site_name], forms, allow_suffix=False)
        or _names_issuer(other.organization_names, forms, allow_suffix=True)
        for other in same_site
    ):
        signals.append("cross_page_self_id")
    meta = source.citation_meta
    if source.scholarly or any(meta.get(key) for key in ("title", "author", "doi")):
        signals.append("document_metadata")
    elif source.document_kind == "pdf" and source.title and source.author:
        signals.append("document_metadata")
    if any("Dataset" in kind for kind in source.schema_types):
        signals.append("dataset_schema")
    if public_suffix_class(source.host) is not None:
        signals.append("institutional_suffix")
    return signals


def _negative_signals(source: ResearchSource) -> list[str]:
    signals: list[str] = []
    declared_elsewhere = bool(
        source.declared_canonical
        and host_group(host_of(source.declared_canonical)) != source.host_group
    )
    if source.duplicate_of or declared_elsewhere:
        signals.append("copy")
    if source.assessment and source.assessment.role in _UNTRUSTED_ROLES:
        signals.append("promotional_structure")
    if source.content_words and source.extraction_quality < _POOR_EXTRACTION:
        signals.append("poor_extraction")
    return signals


def _corroborated_signals(
    source: ResearchSource, readable: list[ResearchSource], leads: list[OriginLead]
) -> list[str]:
    """What sources other than this one, and independent of it, establish about it."""
    signals: list[str] = []
    assessment = source.assessment
    forms = name_forms([assessment.issuer]) if assessment.issuer else set()
    own = {source.canonical_url, source.declared_canonical} - {None}
    title_tokens = token_set(source.citation_meta.get("title") or source.title)
    witnesses = [
        other
        for other in readable
        if other.independence_key != source.independence_key
        and other.host_group != source.host_group
        and other.assessment is not None
        and other.assessment.role not in _UNTRUSTED_ROLES
        and "copy" not in other.assessment.negative
    ]
    witness_keys = {other.independence_key for other in witnesses}

    for lead in leads:
        if lead.independent_of not in witness_keys:
            continue
        by_link = bool(lead.href and canonicalize_url(lead.href) in own)
        # A title alone says nothing about who published a page: a copy carries the same one.
        # Confirming provenance by citation needs the issuer the citation names as well.
        by_title = bool(
            lead.title
            and title_tokens
            and jaccard(token_set(lead.title), title_tokens) >= 0.8
            and lead.issuer
            and forms
            and _names_issuer([lead.issuer], forms, allow_suffix=True)
        )
        if by_link or by_title:
            signals.append("lead_match")
            break

    for other in witnesses:
        for link in other.links:
            if host_group(host_of(link.href)) != source.host_group:
                continue
            anchor = token_set(link.text)
            if (forms and mentions_any(link.text, forms)) or (
                title_tokens and anchor and jaccard(anchor, title_tokens) >= 0.6
            ):
                signals.append("host_endorsement")
                break
        if "host_endorsement" in signals:
            break

    identifiers = {
        value.lower()
        for key in ("doi", "pdf_url")
        if (value := normalize_ws(source.citation_meta.get(key, "")))
    }
    if identifiers and any(
        identifier in link.href.lower()
        for other in witnesses
        for link in other.links
        for identifier in identifiers
    ):
        signals.append("identifier_match")

    if any(
        other.host_group != source.host_group
        and other.declared_canonical
        and other.declared_canonical in own
        for other in readable
    ):
        signals.append("canonical_consolidation")
    return signals


def _copy_of_unreadable_record(source: ResearchSource, leads: list[OriginLead]) -> bool:
    """Whether this page is another copy of a record the run located but could not read.

    A door that would not open does not make the document behind it disappear: a page
    carrying the same document is worth reading and citing. It is still a copy, so it stands
    for the record without ever standing for its publisher.
    """
    title_tokens = token_set(source.citation_meta.get("title") or source.title)
    if not title_tokens:
        return False
    return any(
        lead.status == LeadStatus.UNREADABLE
        and lead.title
        and lead.independent_of != source.independence_key
        and jaccard(token_set(lead.title), title_tokens) >= 0.8
        for lead in leads
    )


def _grade(
    source: ResearchSource,
    readable: list[ResearchSource],
    sources_citing_leads: set[str] | frozenset[str],
    leads: list[OriginLead] = (),
) -> None:
    assessment = source.assessment
    identity = set(assessment.self_asserted) & _IDENTITY_SIGNALS
    corroborated = set(assessment.corroborated)
    negative = set(assessment.negative)
    role = assessment.role
    assessment.copy_of = source.duplicate_of

    stands_in = _copy_of_unreadable_record(source, list(leads))
    if stands_in and "poor_extraction" not in negative and "issuer_conflict" not in negative:
        # It carries a record the run located and could not read. Never the publisher's own.
        if "copy" not in assessment.negative:
            assessment.negative = [*assessment.negative, "copy"]
        assessment.notes = [*assessment.notes, "carries a record that could not be read at source"]
        assessment.provenance = Provenance.B
        return
    if role in _UNTRUSTED_ROLES or "poor_extraction" in negative or "issuer_conflict" in negative:
        grade = Provenance.D
    elif "copy" in negative:
        # A copy stands in for its original only when it carries the identifier that
        # independent sources cite for that original.
        if "identifier_match" in corroborated:
            grade = Provenance.A
        elif corroborated or source.duplicate_of:
            grade = Provenance.B
        else:
            grade = Provenance.C
    elif role in ISSUER_ROLES and corroborated and identity:
        grade = Provenance.A
    elif role in ISSUER_ROLES and len(identity) >= 2:
        grade = Provenance.B
    elif (
        role in (SourceRole.INDEPENDENT_EXPERT, SourceRole.INSTITUTIONAL, SourceRole.REPORTING)
        and (source.author or source.organization_names or source.site_name)
        and (source.published_date or source.modified_date)
        and source.id in sources_citing_leads
    ):
        grade = Provenance.B
    else:
        # Nothing known against it and readable text: unverified, not weak. Weak (D) is kept
        # for pages with something against them -- promotion, copying, unreadable text.
        grade = Provenance.C
    assessment.provenance = grade


def _resolve_issuer_conflicts(
    readable: list[ResearchSource], sources_citing_leads: set[str] | frozenset[str]
) -> None:
    """When sites on different hosts claim one issuer, those without outside support lose.

    A lookalike can copy an issuer's name and metadata but not other sites' links to the real
    one, so once any claimant is corroborated, uncorroborated claimants are graded as weak.
    """
    claimants: dict[str, list[ResearchSource]] = defaultdict(list)
    for source in readable:
        assessment = source.assessment
        if (
            assessment.role in ISSUER_ROLES
            and assessment.issuer
            and "copy" not in assessment.negative
        ):
            claimants[compact(assessment.issuer)].append(source)
    for group in claimants.values():
        if len({source.host_group for source in group}) < 2:
            continue
        supported_hosts = {s.host_group for s in group if s.assessment.corroborated}
        if not supported_hosts:
            continue
        for source in group:
            if source.host_group not in supported_hosts:
                if "issuer_conflict" not in source.assessment.negative:
                    source.assessment.negative.append("issuer_conflict")
                _grade(source, readable, sources_citing_leads)


def _interested_party(
    assessment: SourceAssessment,
    plan: ResearchPlan,
    target: EvidenceTarget | None,
    source: ResearchSource | None,
    item: EvidenceItem | None = None,
) -> bool:
    """A subject's own publisher speaking about how that subject compares or performs.

    What a publisher documents about its own product -- a feature, a limit, a date -- is the
    record of it, and stands for that subject's own question. What it says about how good it
    is, or about how it compares, is never evidence of that.
    """
    if target is None or source is None or assessment.role not in ISSUER_ROLES:
        return False
    judging = (
        target.form == TargetForm.JOINT or target.profile.authority_kind == AuthorityKind.EVALUATIVE
    )
    if not judging:
        return False
    own_subject_fact = (
        target.form == TargetForm.SINGLE
        and len(target.subject_ids) == 1
        and item is not None
        and not _SELF_PRAISE.search(f"{item.excerpt} {item.claim}")
    )
    if own_subject_fact:
        return False
    issuer_forms = name_forms([assessment.issuer]) if assessment.issuer else set()
    for subject_id in target.subject_ids:
        entity = plan.entity(subject_id)
        if subject_id in source.official_for:
            return True
        if (
            entity is not None
            and issuer_forms
            and any(_name_matches(name, issuer_forms, allow_suffix=True) for name in entity.names())
        ):
            return True
    return False


def _has_figure(item: EvidenceItem) -> bool:
    return bool(item.value and re.search(r"\d", item.value))
