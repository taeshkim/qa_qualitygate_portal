from django.contrib import admin
from .models import TestSuite, TestSuiteJob, TestSuiteRun, TestSuiteJobRun


class TestSuiteJobInline(admin.TabularInline):
    model = TestSuiteJob
    extra = 0


@admin.register(TestSuite)
class TestSuiteAdmin(admin.ModelAdmin):
    list_display = ["name", "jenkins_folder_path", "service", "is_active", "created_at"]
    inlines = [TestSuiteJobInline]


@admin.register(TestSuiteRun)
class TestSuiteRunAdmin(admin.ModelAdmin):
    list_display = ["suite", "status", "total_jobs", "passed_jobs", "failed_jobs", "started_at"]
    list_filter = ["status"]


@admin.register(TestSuiteJobRun)
class TestSuiteJobRunAdmin(admin.ModelAdmin):
    list_display = ["job", "suite_run", "status", "jenkins_build_number", "duration_ms"]
    list_filter = ["status"]
