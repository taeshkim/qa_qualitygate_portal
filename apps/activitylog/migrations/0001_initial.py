from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    initial = True

    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="ActivityLog",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("action", models.CharField(
                    choices=[
                        ("create", "생성"), ("update", "수정"), ("delete", "삭제"),
                        ("run", "실행"), ("cancel", "취소"), ("retry", "재시도"),
                    ],
                    max_length=20,
                )),
                ("target_type", models.CharField(
                    choices=[
                        ("gate_folder", "Quality Gate 인스턴스"),
                        ("gate", "Quality Gate"),
                        ("gate_run", "Quality Gate Run"),
                        ("suite", "Test Suite"),
                        ("suite_run", "Test Suite Run"),
                    ],
                    max_length=30,
                )),
                ("target_id", models.CharField(blank=True, default="", max_length=40)),
                ("target_name", models.CharField(blank=True, default="", max_length=300)),
                ("detail", models.CharField(blank=True, default="", max_length=500)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("user", models.ForeignKey(
                    blank=True, null=True,
                    on_delete=django.db.models.deletion.SET_NULL,
                    related_name="activity_logs",
                    to=settings.AUTH_USER_MODEL,
                )),
            ],
            options={
                "db_table": "activity_log",
                "ordering": ["-created_at"],
                "indexes": [
                    models.Index(fields=["-created_at"], name="activity_lo_created_idx"),
                    models.Index(fields=["target_type", "-created_at"], name="activity_lo_tgt_created_idx"),
                ],
            },
        ),
    ]
