import uuid
import django.db.models.deletion
from django.db import migrations, models


def create_auth_subfolders(apps, schema_editor):
    """기존 인스턴스마다 'auth' 하위폴더를 만들고, 소속 Gate 들을 그 안으로 옮긴다."""
    QualityGateFolder = apps.get_model("qualitygates", "QualityGateFolder")
    QualityGateSubFolder = apps.get_model("qualitygates", "QualityGateSubFolder")
    QualityGate = apps.get_model("qualitygates", "QualityGate")

    for folder in QualityGateFolder.objects.all():
        sub, _ = QualityGateSubFolder.objects.get_or_create(
            folder=folder, name="auth",
        )
        QualityGate.objects.filter(folder=folder, subfolder__isnull=True).update(
            subfolder=sub,
        )


def reverse_auth_subfolders(apps, schema_editor):
    QualityGateSubFolder = apps.get_model("qualitygates", "QualityGateSubFolder")
    QualityGate = apps.get_model("qualitygates", "QualityGate")
    QualityGate.objects.update(subfolder=None)
    QualityGateSubFolder.objects.filter(name="auth").delete()


class Migration(migrations.Migration):

    dependencies = [
        ("qualitygates", "0016_qualitygate_auto_mode"),
    ]

    operations = [
        migrations.CreateModel(
            name="QualityGateSubFolder",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("name", models.CharField(max_length=200, verbose_name="하위폴더명")),
                ("description", models.CharField(blank=True, max_length=500, verbose_name="설명")),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "folder",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="subfolders",
                        to="qualitygates.qualitygatefolder",
                        verbose_name="인스턴스",
                    ),
                ),
            ],
            options={
                "db_table": "qualitygates_subfolder",
                "ordering": ["name"],
                "unique_together": {("folder", "name")},
            },
        ),
        migrations.AddField(
            model_name="qualitygate",
            name="subfolder",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.CASCADE,
                related_name="gates",
                to="qualitygates.qualitygatesubfolder",
                verbose_name="하위폴더",
            ),
        ),
        migrations.RunPython(create_auth_subfolders, reverse_auth_subfolders),
    ]
