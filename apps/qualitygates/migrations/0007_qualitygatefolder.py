import uuid

import django.db.models.deletion
from django.db import migrations, models


def migrate_gates_to_default_folder(apps, schema_editor):
    """기존 QualityGate 들을 '기본 폴더' 에 소속시키고 자격증명을 복사한다.
    여러 Gate 의 자격증명이 다를 수 있으므로 첫 Gate 것을 폴더 기본값으로 사용한다."""
    QualityGate = apps.get_model("qualitygates", "QualityGate")
    QualityGateFolder = apps.get_model("qualitygates", "QualityGateFolder")

    gates = list(QualityGate.objects.all())
    if not gates:
        return

    first = gates[0]
    folder = QualityGateFolder.objects.create(
        name="기본 폴더",
        description="기존 QualityGate 들이 자동 이관된 폴더",
        dev_jenkins_url=getattr(first, "deploy_jenkins_url", "") or "",
        dev_jenkins_user=getattr(first, "deploy_jenkins_user", "") or "",
        dev_jenkins_token=getattr(first, "deploy_jenkins_token", "") or "",
        prod_jenkins_url=getattr(first, "prod_jenkins_url", "") or "",
        prod_jenkins_user=getattr(first, "prod_jenkins_user", "") or "",
        prod_jenkins_token=getattr(first, "prod_jenkins_token", "") or "",
    )
    for g in gates:
        g.folder = folder
        g.save(update_fields=["folder"])


def noop_reverse(apps, schema_editor):
    QualityGateFolder = apps.get_model("qualitygates", "QualityGateFolder")
    QualityGateFolder.objects.filter(name="기본 폴더").delete()


class Migration(migrations.Migration):

    dependencies = [
        ("qualitygates", "0006_alter_qualitygaterun_status"),
    ]

    operations = [
        migrations.CreateModel(
            name="QualityGateFolder",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("name", models.CharField(max_length=200, unique=True, verbose_name="인스턴스명")),
                ("description", models.CharField(blank=True, max_length=500, verbose_name="설명")),
                ("dev_jenkins_url", models.URLField(blank=True, max_length=500, verbose_name="Dev Jenkins URL")),
                ("dev_jenkins_user", models.CharField(blank=True, max_length=200, verbose_name="Dev Jenkins User (Email)")),
                ("dev_jenkins_token", models.CharField(blank=True, max_length=200, verbose_name="Dev Jenkins API Token")),
                ("prod_jenkins_url", models.URLField(blank=True, max_length=500, verbose_name="Prod Jenkins URL")),
                ("prod_jenkins_user", models.CharField(blank=True, max_length=200, verbose_name="Prod Jenkins User (Email)")),
                ("prod_jenkins_token", models.CharField(blank=True, max_length=200, verbose_name="Prod Jenkins API Token")),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
            ],
            options={
                "db_table": "qualitygates_folder",
                "ordering": ["name"],
            },
        ),
        migrations.AddField(
            model_name="qualitygate",
            name="folder",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.CASCADE,
                related_name="gates",
                to="qualitygates.qualitygatefolder",
            ),
        ),
        migrations.RunPython(migrate_gates_to_default_folder, noop_reverse),
        migrations.RemoveField(model_name="qualitygate", name="deploy_jenkins_url"),
        migrations.RemoveField(model_name="qualitygate", name="deploy_jenkins_user"),
        migrations.RemoveField(model_name="qualitygate", name="deploy_jenkins_token"),
        migrations.RemoveField(model_name="qualitygate", name="prod_jenkins_url"),
        migrations.RemoveField(model_name="qualitygate", name="prod_jenkins_user"),
        migrations.RemoveField(model_name="qualitygate", name="prod_jenkins_token"),
    ]
