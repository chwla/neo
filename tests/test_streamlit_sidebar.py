import streamlit as st

from streamlit_app import (
    chat_row_html,
    close_memory_dialog,
    project_chat_link_html,
    project_folder_html,
)


def test_delete_x_is_inside_chat_rectangle_markup() -> None:
    html = chat_row_html("whats my name", chat_id=42, active=False)

    row_start = html.index('class="chat-item')
    title_start = html.index('class="chat-item-title"')
    delete_start = html.index('class="chat-item-delete"')
    row_end = html.rindex("</div>")

    assert row_start < title_start < row_end
    assert row_start < delete_start < row_end
    assert 'href="?request_delete_chat=42"' in html
    assert "X" in html


def test_project_folder_has_delete_x_in_header() -> None:
    project = type("ProjectStub", (), {"id": 7, "name": "DLB Project"})()

    html = project_folder_html(project, [], selected_project_id=7)

    summary_start = html.index("<summary>")
    delete_start = html.index('class="project-folder-delete"')
    summary_end = html.index("</summary>")
    assert summary_start < delete_start < summary_end
    assert 'href="?request_delete_project=7"' in html
    assert 'class="project-folder-icon"' in html
    assert "&gt;" not in html
    assert "DLB Project" in html


def test_project_chat_link_does_not_render_div_markup() -> None:
    html = project_chat_link_html("Fix file paths", chat_id=9, active=True)

    assert "<div" not in html
    assert "</div>" not in html
    assert 'href="?open_chat=9"' in html
    assert "project-chat-link active" in html


def test_memory_close_resets_dialog_and_skill_dropdown() -> None:
    st.session_state.show_memory = True
    st.session_state.reset_skills_v2 = False

    close_memory_dialog()

    assert st.session_state.show_memory is False
    assert st.session_state.reset_skills_v2 is True
