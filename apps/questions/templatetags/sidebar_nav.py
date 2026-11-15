"""Explicit, maintainable mapping of URL routes to student sidebar items.

The student sidebar highlights exactly one item per page. We map by the fully
namespaced route name (``namespace:url_name``) rather than substring matching on
``url_name`` — several routes share the ``practice_`` prefix (practice_bank,
practice_history, ...), so broad matching would highlight the wrong item.

To add a page to a sidebar section, add its exact route name to the relevant set
below. Routes that never render the sidebar (file/image endpoints) are harmless to
list, but they are grouped with their owning section for clarity.
"""
from django import template

register = template.Library()

# sidebar key -> the exact namespaced routes that belong to that item.
SIDEBAR_SECTIONS = {
    "dashboard": {
        "core:dashboard_router", "core:student_dashboard", "core:teacher_dashboard",
        "core:admin_dashboard", "core:super_admin_dashboard",
    },
    "pdf_practice": {
        "questions:practice_list", "questions:practice_upload", "questions:practice_settings",
        "questions:practice_pdf", "questions:practice_delete", "questions:practice_figure_pool",
        "questions:practice_question_add", "questions:practice_answer_suggestion",
        "questions:practice_answer_request", "questions:practice_answer_decision",
        "questions:practice_question_edit", "questions:practice_question_include",
        "questions:practice_figures", "questions:practice_source_region",
        "questions:practice_source_page", "questions:practice_extracted_image",
        "questions:practice_figure_file",
    },
    "question_bank": {
        "questions:practice_bank", "questions:practice_bank_test", "questions:practice_bank_edit",
        "questions:practice_bank_delete", "questions:practice_bank_image", "questions:practice_bank_figure",
    },
    "practice_history": {
        "questions:practice_history", "questions:practice_result", "questions:practice_report",
        "questions:practice_estimate_start", "questions:practice_estimate_step",
        "questions:practice_estimate_delete",
    },
    "plans": {
        "payments:plans", "payments:checkout_start", "payments:checkout",
        "payments:verify", "payments:cancel_subscription",
    },
    "exams": {
        "exams:exam_catalog",
    },
    # Student: "Join Test with Code".
    "join": {
        "exams:classroom_join",
    },
    # Teacher: classroom tests workspace and its session pages.
    "classroom": {
        "exams:classroom_hub", "exams:classroom_session_create", "exams:classroom_session",
        "exams:classroom_monitor", "exams:classroom_control", "exams:classroom_qr",
        "exams:classroom_leaderboard", "exams:classroom_export", "exams:classroom_take",
    },
}

# Reverse index: namespaced route name -> sidebar key (built once at import).
_ROUTE_TO_SECTION = {
    route: key for key, routes in SIDEBAR_SECTIONS.items() for route in routes
}


@register.filter
def sidebar_key(resolver_match):
    """Return the active sidebar key for the current request, or "".

    Usage: ``{% with active=request.resolver_match|sidebar_key %}``.
    Matching is exact on ``namespace:url_name``, so only one item is ever active.
    """
    if resolver_match is None:
        return ""
    view_name = getattr(resolver_match, "view_name", "") or ""
    return _ROUTE_TO_SECTION.get(view_name, "")
