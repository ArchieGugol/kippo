import datetime
from collections import defaultdict
from typing import Dict, List

from django.conf import settings
from django.utils import timezone
from django.db.models import Sum, Value, Count
from django.db.models.functions import Coalesce

from qlu.core import QluTaskScheduler, QluTask, QluMilestone, QluTaskEstimates
from accounts.models import KippoUser
from projects.models import KippoProject, KippoMilestone
from .models import KippoTask, KippoTaskStatus


def get_github_issue_estimate_label(issue, prefix=settings.DEFAULT_GITHUB_ISSUE_LABEL_ESTIMATE_PREFIX) -> int:
    """
    Parse the estimate label into an estimate value
    Estimate labels follow the scheme: {prefix}N{suffix}
    WHERE:
    - {prefix} estimate label identifier
    - N is a positive integer representing number of days
    - {suffix} one of ('d', 'day', 'days', 'h', 'hour', 'hours')

    .. note::

        Only integer values are supported.
        (fractional days are not represented at the moment)

    :param issue: github issue object
    :param prefix: This identifies the github issue label as being an estimate
    :return: parsed estimate result in days
    """
    estimate = None
    valid_label_suffixes = ('d', 'day', 'days', 'h', 'hour', 'hours')
    for label in issue.labels:
        if label.name.startswith(prefix):
            estimate_str_value = label.name.split(prefix)[-1]
            for suffix in valid_label_suffixes:
                if estimate_str_value.endswith(suffix):  # d = days, h = hours
                    estimate_str_value = estimate_str_value.split(suffix)[0]
            estimate = int(estimate_str_value)

            if label.name.endswith(('h', 'hour', 'hours')):
                # all estimates are normalized to days
                # if hours convert to a days
                estimate = int(round((estimate/settings.DAY_WORKHOURS) + .5, 0))

    return estimate


def get_github_issue_category_label(issue, prefix=settings.DEFAULT_GITHUB_ISSUE_LABEL_CATEGORY_PREFIX) -> str:
    """
    Parse the category label into the category value
    Category Labels follow the scheme:
        category:CATEGORY_NAME
        WHERE:
            CATEGORY_NAME should match the VALID_TASK_CATEGORIES value in models.py
    :param issue: github issue object
    :param prefix: This identifies the github issue label as being a category
    :return: parsed category result
    """
    category = None
    for label in issue.labels:
        if label.name.startswith(prefix):
            category = label.name.split(prefix)[-1].strip()
    return category


class ProjectDatesError(Exception):
    pass


class TaskStatusError(Exception):
    pass


def get_project_weekly_effort(project: KippoProject, current_date: datetime.date=None):
    """
    Obtain the project weekly effort
    :param project:
    :param current_date:
    :return:
    """
    if not current_date:
        current_date = timezone.now().date()
    elif isinstance(current_date, datetime.datetime):
        current_date = current_date.date()

    if not project.start_date or not project.target_date:
        raise ProjectDatesError(f'{project.name} required dates not set: start_date={project.start_date}, target_date={project.target_date}')

    # get project participants
    participants = set(KippoTaskStatus.objects.filter(task__project=project,
                                                      task__assignee__github_login__isnull=False,
                                                      effort_date__gte=project.start_date,
                                                      effort_date__lte=current_date).values_list('task__assignee__github_login', flat=True))
    # get latest effort status
    # -- only a single entry per date

    # prepare dates
    search_dates = []
    start_date_calendar_info = project.start_date.isocalendar()
    start_date_year, start_date_week, _ = start_date_calendar_info
    initial_week_start_date = datetime.datetime.strptime(f'{start_date_year}-{start_date_week}-{TUESDAY_WEEKDAY}', '%Y-%W-%w').date()
    current_week_start_date = initial_week_start_date

    while current_week_start_date <= project.target_date:
        search_dates.append(current_week_start_date)
        last_week_start_date = current_week_start_date
        current_week_start_date += datetime.timedelta(days=7)
        if last_week_start_date < current_date < current_week_start_date:
            # add the current date (to show the current status)
            search_dates.append(current_date)
    if project.target_date not in search_dates:
        search_dates.append(project.target_date)

    active_column_names = project.columnset.get_active_column_names()
    all_status_entries = []  # state__in=GITHUB_ACTIVE_TASK_STATES
    for current_week_start_date in search_dates:
        previous_status_entries = KippoTaskStatus.objects.filter(task__project=project,
                                                                 task__assignee__github_login__isnull=False,
                                                                 effort_date=current_week_start_date,
                                                                 state__in=active_column_names).values('task__project', 'effort_date', 'task__assignee__github_login').annotate(task_count=Count('task'), estimate_days_sum=Coalesce(Sum('estimate_days'), Value(0)))

        all_status_entries.extend(list(previous_status_entries))

    if not all_status_entries:
        raise TaskStatusError(f'No TaskStatus found for project({project.name}) in ranges: {project.start_date} to {project.target_date}')

    return all_status_entries, search_dates


def prepare_project_plot_data(project: KippoProject, current_date: datetime.date=None):
    """
    Format data for easy plotting
    :param project:
    :param current_date:
    :return:
    """
    data = defaultdict(list)
    burndown_line = None
    if project.start_date and project.target_date and project.allocated_staff_days:
        start_date = project.start_date.strftime('%Y-%m-%d (%a)')
        data['effort_date'].append(start_date)
        end_date = project.target_date.strftime('%Y-%m-%d (%a)')
        start_staff_days = project.allocated_staff_days
        burndown_line_x = [start_date, end_date]
        burndown_line_y = [start_staff_days, 0]
        burndown_line = [burndown_line_x, burndown_line_y]

    status_entries, all_dates = get_project_weekly_effort(project, current_date)

    assignees = set()
    for entry in status_entries:
        effort_date = entry['effort_date'].strftime('%Y-%m-%d (%a)')
        assignee = entry['task__assignee__github_login']
        estimate_days = entry['estimate_days_sum']
        if effort_date not in data['effort_date']:
            data['effort_date'].append(effort_date)

        effort_date_index = data['effort_date'].index(effort_date)
        while len(data[assignee]) != effort_date_index:
            data[assignee].append(0.0)  # backfill
        data[assignee].append(estimate_days)
        assignees.add(assignee)

    # get max date
    max_date_str = max(data['effort_date'])
    all_date_strings = [d.strftime('%Y-%m-%d (%a)') for d in all_dates]
    unadded_dates = all_date_strings[all_date_strings.index(max_date_str) + 1:]
    for date_str in unadded_dates:
        print(date_str)
        data['effort_date'].append(date_str)
        for assignee in assignees:
            data[assignee].append(0.0)
    return data, sorted(list(assignees)), burndown_line


def get_engineer_project_load(schedule_start_date: datetime.date=None) -> Dict[KippoUser, List[KippoTask]]:
    """
    Schedule tasks to determine engineer work load
    :param schedule_start_date:
    :return: A dictionary of TelosUsers with assigned Tasks, where tasks have attached scheduled QluTask as Task.qlu_task
    """
    if not schedule_start_date:
        schedule_start_date = timezone.now().date()
    elif isinstance(schedule_start_date, datetime.datetime):
        schedule_start_date = schedule_start_date.date()

    engineer_project_load = {}
    telos_tasks = {}
    for developer in KippoUser.objects.filter(is_developer=True, is_active=True):

        # get active taskstatus
        active_taskstatus = KippoTaskStatus.objects.filter(task__assignee=developer,
                                                           task__is_closed=False,
                                                           task__project__is_closed=False,
                                                           task__assignee__github_login__isnull=False,
                                                           task__project__target_date__gt=schedule_start_date,
                                                           state__in=settings.GITHUB_ACTIVE_TASK_STATES).exclude(task__assignee__github_login=settings.UNASSIGNED_USER_GITHUB_LOGIN).order_by('task__project__target_date')

        # get related projects and tasks
        qlu_tasks = []
        qlu_milestones = []
        related_projects = []
        added_ids = []
        default_minimum = 1
        default_suggested = 3
        maximum_multiplier = 1.7

        for status in active_taskstatus:
            # create qlu estimates and tasks

            # - create estimates for task
            minimum_estimate = int(status.minimum_estimate_days) if status.minimum_estimate_days else default_minimum
            suggested_estimate = int(status.estimate_days) if status.estimate_days else default_suggested
            maximum_estimate = status.maximum_estimate_days
            if not maximum_estimate:
                maximum_estimate = int(round(suggested_estimate * maximum_multiplier, 0))
            qestimates = QluTaskEstimates(minimum_estimate,
                                          suggested_estimate,
                                          maximum_estimate)

            # QluTask Fields: (id: Any, absolute_priority, depends_on, estimates, assignee, project_id, milestone_id)
            related_milestone = status.task.milestone
            if related_milestone:
                milestone_id = related_milestone.id
            else:
                # treat the parent project as a milestone to get the task start/end
                milestone_id = f'p-{status.task.project.id}'  # matches below in milestone creation
                qlu_milestone = QluMilestone(milestone_id,
                                             status.task.project.start_date,
                                             status.task.project.target_date)
                qlu_milestones.append(qlu_milestone)

            telos_tasks[status.task.id] = status.task
            qtask = QluTask(
                status.task.id,
                absolute_priority=0,
                estimates=qestimates,
                assignee=developer.github_login,
                project_id=status.task.project.id,
                milestone_id=milestone_id,
            )
            qlu_tasks.append(qtask)

            # get project
            project = status.task.project
            if project.id not in added_ids:
                related_projects.append(project)
                added_ids.append(project.id)

        # get related milestones
        for project in related_projects:
            project_milestones = project.active_milestones()
            if not project_milestones:
                # create a single milestone covering the range of the project
                m = KippoMilestone()  # dummy holder for processing
                m.id = f'p-{project.id}'
                m.start_date = project.start_date
                m.target_date = project.target_date
                project_milestones = [m]

            for milestone in project_milestones:
                qlu_milestone = QluMilestone(milestone.id,
                                             milestone.start_date,
                                             milestone.target_date)
                qlu_milestones.append(qlu_milestone)

        if qlu_tasks:
            holidays = {developer.github_login: list(developer.personal_holiday_dates())}
            scheduler = QluTaskScheduler(milestones=qlu_milestones,
                                         personal_holidays=holidays,
                                         start_date=schedule_start_date)
            scheduled_results = scheduler.schedule(qlu_tasks)
            engineer_project_load[developer] = []
            for qlu_task in scheduled_results.tasks():
                telos_task_id = qlu_task.id
                telos_task = telos_tasks[telos_task_id]
                # attach qlu_task to telos task
                telos_task.qlu_task = qlu_task
                engineer_project_load[developer].append(telos_task)
        else:
            engineer_project_load[developer] = []
    return engineer_project_load


def prepare_engineering_load_plot_data(schedule_start_date: datetime.date=None):
    engineer_project_load = get_engineer_project_load(schedule_start_date)

    all_ordered_engineers = sorted(engineer_project_load)
    all_ordered_project_names = {t.project.name for t in engineer_project_load.values()}

    # TODO: finish populating!
    data = {
        'engineers': [],

    }
    for engineer in all_ordered_engineers:
        display_name = f'{engineer.first_name} {engineer.last_name} ({engineer.github_login})'

    # TODO FINISH!