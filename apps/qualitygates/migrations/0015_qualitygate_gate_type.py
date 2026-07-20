# Generated for gate_type field

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('qualitygates', '0014_qualitygaterun_deploy_argocd_operation_message_and_more'),
    ]

    operations = [
        migrations.AddField(
            model_name='qualitygate',
            name='gate_type',
            field=models.CharField(
                max_length=10,
                choices=[('dev_stg', 'Dev → Stg'), ('stg_prd', 'Stg → Prd')],
                default='dev_stg',
                verbose_name='게이트 배포 구성',
            ),
        ),
    ]
