from django.db import migrations, models


def backfill_seq_no(apps, schema_editor):
    """게이트별 created_at 순으로 1..N 부여 — 기존 Run 의 #번호 채우기."""
    QualityGate = apps.get_model("qualitygates", "QualityGate")
    QualityGateRun = apps.get_model("qualitygates", "QualityGateRun")
    for gate in QualityGate.objects.all():
        runs = QualityGateRun.objects.filter(gate=gate).order_by("created_at", "id")
        for i, run in enumerate(runs, start=1):
            run.seq_no = i
            run.save(update_fields=["seq_no"])


def reverse_backfill(apps, schema_editor):
    QualityGateRun = apps.get_model("qualitygates", "QualityGateRun")
    QualityGateRun.objects.update(seq_no=0)


class Migration(migrations.Migration):

    dependencies = [
        ('qualitygates', '0019_qualitygate_parallel_test'),
    ]

    operations = [
        migrations.AddField(
            model_name='qualitygaterun',
            name='seq_no',
            field=models.PositiveIntegerField(default=0),
        ),
        migrations.RunPython(backfill_seq_no, reverse_backfill),
        migrations.AddConstraint(
            model_name='qualitygaterun',
            constraint=models.UniqueConstraint(
                condition=models.Q(('seq_no__gt', 0)),
                fields=('gate', 'seq_no'),
                name='qualitygates_run_gate_seq_no_uniq',
            ),
        ),
    ]
