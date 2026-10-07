"""Real browser acceptance of schedules through the platform API."""

from datetime import timedelta

from playwright.sync_api import expect

from controlplane.application.schedules import ScheduleDispatcher
from controlplane.tests.test_ui import page, server, shot  # noqa: F401, F811


def test_create_pause_resume_edit_and_run_origin(page, server):  # noqa: F811
    page.goto(f"{server.url}/ui/#/projects/credit-risk/schedules")
    page.get_by_role("button", name="Create schedule", exact=True).click()
    dialog = page.get_by_role("dialog")
    dialog.get_by_label("Name", exact=True).fill("daily-score")
    dialog.get_by_label("Target", exact=True).select_option("training")
    dialog.get_by_label("Frequency", exact=True).select_option("advanced")
    dialog.get_by_label("Cron", exact=True).fill("* * * * *")
    dialog.get_by_label("Timezone", exact=True).fill("Europe/Istanbul")
    expect(dialog.get_by_role("button", name="Save schedule", exact=True)).to_be_enabled()
    shot(page, "schedule-create")
    dialog.get_by_role("button", name="Save schedule", exact=True).click()
    expect(page.get_by_role("heading", name="daily-score", exact=True)).to_be_visible()
    page.get_by_role("button", name="Pause", exact=True).click()
    expect(page.get_by_role("button", name="Resume", exact=True)).to_be_visible()
    page.get_by_role("button", name="Resume", exact=True).click()
    expect(page.get_by_role("button", name="Pause", exact=True)).to_be_visible()
    page.get_by_role("button", name="Edit schedule", exact=True).click()
    dialog.get_by_label("Frequency", exact=True).select_option("advanced")
    dialog.get_by_label("Cron", exact=True).fill("* * * * *")
    dialog.get_by_role("button", name="Save schedule", exact=True).click()
    expect(dialog).not_to_be_visible()
    with server.demo.uow_factory() as uow:
        schedule = uow.schedules.list(None, 1, 0)[0]
    now = schedule.next_run_at + timedelta(seconds=1)
    dispatcher = ScheduleDispatcher(server.demo.uow_factory, lambda: now)
    assert dispatcher.tick() == {"DISPATCHED": 1}
    assert dispatcher.tick() == {}
    execution = server.demo.app.state.schedules.history(schedule.id)[0]
    page.reload()
    expect(page.get_by_test_id("schedule-executions")).to_contain_text("dispatched")
    shot(page, "schedule-history")
    page.set_viewport_size({"width": 390, "height": 844})
    page.get_by_role("button", name="Menu", exact=True).click()
    shot(page, "schedule-mobile")
    page.goto(f"{server.url}/ui/#/projects/credit-risk/pipeline-runs/{execution.pipeline_run_id}")
    expect(page.get_by_test_id("run-schedule-origin")).to_be_visible()
    expect(page.get_by_test_id("run-schedule-origin").get_by_role("link")).to_be_visible()
