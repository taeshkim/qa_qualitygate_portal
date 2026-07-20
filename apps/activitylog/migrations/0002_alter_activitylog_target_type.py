from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("activitylog", "0001_initial"),
    ]

    operations = [
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
                ],
                max_length=30,
            ),
        ),
    ]
