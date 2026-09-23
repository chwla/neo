"""Presentation effort, independent of research budgets and evidence standards.

These are selection and generation allowances, never length targets. A report with one
supported answer should stay short in every mode. Safety-relevant findings are never cut
to make a report concise.
"""

from dataclasses import dataclass

from app.services.research.types import DepthMode


@dataclass(frozen=True)
class ReportDepthPolicy:
    findings_per_question: int | None
    details_per_claim: int
    summary_answers: int
    #: Extra finishing capacity for prose and its audit, inside the existing run ceiling.
    #: Mandatory verification retains first use of this allowance.
    writing_reserve: int
    guidance: str


REPORT_DEPTH = {
    DepthMode.QUICK: ReportDepthPolicy(
        findings_per_question=1,
        details_per_claim=0,
        summary_answers=3,
        writing_reserve=0,
        guidance="Give a concise direct answer and the essential qualifications.",
    ),
    DepthMode.STANDARD: ReportDepthPolicy(
        findings_per_question=3,
        details_per_claim=2,
        summary_answers=4,
        writing_reserve=3,
        guidance=(
            "Give a detailed report: explain the main findings, quantitative results, "
            "subject differences, mechanisms and material limitations where supported."
        ),
    ),
    DepthMode.DEEP: ReportDepthPolicy(
        findings_per_question=None,
        details_per_claim=5,
        summary_answers=5,
        writing_reserve=7,
        guidance=(
            "Give a comprehensive report using all distinct useful verified findings. "
            "Develop context, cross-source synthesis, quantitative findings, cases and examples, "
            "supported mechanisms, scope and implementation differences, competing findings, "
            "methodological limitations, caveats and nuanced conclusions. Include only aspects "
            "the evidence supports; never invent a section or repeat a fact to add length."
        ),
    ),
}
