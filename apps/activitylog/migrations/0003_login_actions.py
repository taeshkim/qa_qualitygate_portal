from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("activitylog", "0002_alter_activitylog_target_type"),
    ]

    operations = [
        migrations.AlterField(
            model_name="activitylog",
            name="action",
            field=models.CharField(
                choices=[
                    ("create", "생성"),
                    ("update", "수정"),
                    ("delete", "삭제"),
                    ("run", "실행"),
                    ("cancel", "취소"),
                    ("retry", "재시도"),
                    ("login", "로그인"),
                    ("logout", "로그아웃"),
                    ("login_failed", "로그인 실패"),
                ],
                max_length=20,
            ),
        ),
        migrations.AlterField(
            model_name="activitylog",
            name="target_type",
            field=models.CharField(
                choices=[
                    ("gate_folder", "Quality Gate 인스턴스"),
                    ("gate", "Quality Gate"),
                    ("gate_run", "Quality Gate Run"),
                    ("suite", "Test Suite"),
                    ("suite_run", "Test Suite Run"),
                    ("reliability_test", "Reliability Test"),
                    ("reliability_schedule", "Reliability Test 스케줄"),
                    ("user", "사용자"),
                ],
                max_length=30,
            ),
        ),
    ]
