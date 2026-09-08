from aiogram.fsm.state import State, StatesGroup


class ReportForm(StatesGroup):
    choosing_city = State()
    choosing_unit = State()
    choosing_granularity = State()
    choosing_channel = State()
    choosing_format = State()
    confirming = State()
    clarification = State()
    ask_make_repeating = State()


class ScheduledReportForm(StatesGroup):
    """Form for creating scheduled (weekly or monthly) reports."""

    waiting_query = State()
    choosing_city = State()
    choosing_unit = State()
    choosing_granularity = State()
    choosing_channel = State()
    choosing_frequency = State()
    choosing_weekday = State()
    choosing_day_of_month = State()
    choosing_time = State()
    choosing_format = State()
    confirming = State()


# Alias for backward compatibility
WeeklyReportForm = ScheduledReportForm
