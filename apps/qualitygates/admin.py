from django.contrib import admin
from .models import QualityGate, QualityGateTestSuite, QualityGateRun, QualityGateRunTest


class GateTestSuiteInline(admin.TabularInline):
    model = QualityGateTestSuite
    extra = 0


@admin.register(QualityGate)
class QualityGateAdmin(admin.ModelAdmin):
    list_display = ["name", "deploy_job_path", "test_suites_display", "is_active"]
    inlines = [GateTestSuiteInline]


@admin.register(QualityGateRun)
class QualityGateRunAdmin(admin.ModelAdmin):
    list_display = ["gate", "status", "deploy_build_number", "current_suite_index", "total_suites", "triggered_by", "created_at"]
    list_filter = ["status"]
