from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('qualitygates', '0015_qualitygate_gate_type'),
    ]

    operations = [
        migrations.AddField(
            model_name='qualitygate',
            name='auto_mode',
            field=models.BooleanField(default=False, verbose_name='자동 진행'),
        ),
    ]
