import logging
from django.contrib import admin, messages
from django.utils.html import format_html
from django.http import HttpResponseRedirect
from django.utils.translation import ugettext_lazy as _
from common.admin import UserCreatedBaseModelAdmin
from ghorgs.managers import GithubOrganizationManager
from .functions import collect_existing_github_projects
from .models import KippoProject, ActiveKippoProject, KippoMilestone, ProjectColumnSet, ProjectColumn


logger = logging.getLogger(__name__)


class KippoMilestoneReadOnlyInline(admin.TabularInline):
    model = KippoMilestone
    extra = 0
    fields = (
        'title',
        'start_date',
        'target_date',
        'actual_date',
        'allocated_staff_days',
        'description',
    )
    readonly_fields = (
        'title',
        'start_date',
        'target_date',
        'actual_date',
        'allocated_staff_days',
        'description',
    )

    def has_add_permission(self, request):  # No Add button
        return False

    def get_queryset(self, request):
        # order milestones as expected
        qs = super().get_queryset(request).order_by('target_date')
        return qs


class KippoMilestoneAdminInline(admin.TabularInline):
    model = KippoMilestone
    extra = 0
    fields = (
        'title',
        'start_date',
        'target_date',
        'actual_date',
        'allocated_staff_days',
        'description',
    )

    def get_queryset(self, request):
        # clear the queryset so that no EDIABLE entries are displayed
        qs = super().get_queryset(request).none()
        return qs


def collect_existing_github_projects_action(modeladmin, request, queryset) -> None:
    """
    Admin Action to discover existing github projects and add to kippo as KippoProject objects
    """
    # get request user organization
    organization = request.user.organization
    added_projects = collect_existing_github_projects(organization)
    modeladmin.message_user(
        request,
        message=f'({len(added_projects)}) KippoProjects created from GitHub Organizational Projects',
        level=messages.INFO,
    )
collect_existing_github_projects_action.short_description = _('Collect Github Projects')  # noqa


def create_github_organizational_project_action(modeladmin, request, queryset) -> None:
    """
    Admin Action command to create a github organizational project from the selected KippoProject(s)

    Where an existing Github Organization project does not exist (not assigned)
    """
    successful_creation_projects = []
    skipping = []
    for kippo_project in queryset:
        if kippo_project.github_project_url:
            message = f'{kippo_project.name} already has GitHub Project set ({kippo_project.github_project_url}), SKIPPING!'
            logger.warning(message)
            skipping.append(message)
        else:
            if not kippo_project.columnset:
                modeladmin.message_user(
                    request,
                    message=f'ProjectColumnSet not defined for {kippo_project}, cannot create Github Project!',
                    level=messages.ERROR,
                )
                return

            columns = kippo_project.get_column_names()
            github_manager = GithubOrganizationManager(organization=kippo_project.github_organization_name,
                                                       token=kippo_project.githubaccesstoken.token)
            # create the organizational project in github
            # create_organizational_project(organization: str, name: str, description: str, columns: list=None) -> Tuple[str, List[object]]:
            url, _ = github_manager.create_organizational_project(
                name=kippo_project.github_project_name,
                description=kippo_project.github_project_description,
                columns=columns,
            )
            kippo_project.github_project_url = url
            kippo_project.save()
            successful_creation_projects.append((kippo_project.name, url))
    if skipping:
        for m in skipping:
            modeladmin.message_user(
                request,
                message=m,
                level=messages.WARNING,
            )
    if successful_creation_projects:
        modeladmin.message_user(
            request,
            message=f'({len(successful_creation_projects)}) GitHub Projects Created: {successful_creation_projects}',
            level=messages.INFO,
        )
create_github_organizational_project_action.short_description = _('Create Github Organizational Project(s) for selected')  # noqa


class KippoProjectAdmin(UserCreatedBaseModelAdmin):
    list_display = (
        'name',
        'category',
        'project_manager',
        'display_as_active',
        'show_github_project_url',
        'start_date',
        'target_date',
        'updated_by',
        'updated_datetime',
    )
    search_fields = (
        'name',
        'category',
        'problem_definition',
    )
    actions = [
        create_github_organizational_project_action,
    ]

    def show_github_project_url(self, obj):
        url = ''
        if obj.github_project_url:
            url = format_html('<a href="{url}">{url}</a>', url=obj.github_project_url)
        return url
    show_github_project_url.short_description = _('GitHub Project URL')

    def get_form(self, request, obj=None, **kwargs):
        # update user field with logged user as default
        form = super().get_form(request, obj, **kwargs)
        form.base_fields['project_manager'].initial = request.user.id
        return form

    def save_model(self, request, obj, form, change):
        obj.organization = request.user.organization
        super().save_model(request, obj, form, change)


class KippoMilestoneAdmin(UserCreatedBaseModelAdmin):
    list_display = (
        'title',
        'get_project_name',
        'is_complete',
        'start_date',
        'target_date',
        'actual_date',
        'updated_by',
        'updated_datetime',
    )
    search_fields = (
        'title',
        'description',
    )
    ordering = (
        'project',
        'target_date',
    )

    def get_project_name(self, obj):
        return obj.project.name
    get_project_name.short_description = _('Project')

    def response_add(self, request, obj, post_url_continue=None):
        """Overridding Redirect to the KippoProject page after edit.
        """
        project_url = obj.project.get_admin_url()
        return HttpResponseRedirect(project_url)

    def response_change(self, request, obj):
        """Overridding Redirect to the KippoProject page after edit.
        """
        project_url = obj.project.get_admin_url()
        return HttpResponseRedirect(project_url)


class ProjectColumnInline(admin.TabularInline):
    model = ProjectColumn
    extra = 3


class ProjectColumnSetAdmin(UserCreatedBaseModelAdmin):
    list_display = (
        'name',
        'get_column_names',
    )
    inlines = [ProjectColumnInline]


admin.site.register(KippoProject, KippoProjectAdmin)
admin.site.register(ActiveKippoProject, KippoProjectAdmin)
admin.site.register(ProjectColumnSet, ProjectColumnSetAdmin)
