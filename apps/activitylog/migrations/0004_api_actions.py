"""ActivityLog.Action 에 API_KEY_CREATED / API_KEY_REVOKED / API_RUN_TRIGGERED 추가.

API v2 시작과 함께 도입. TextChoices 라 데이터 백필 불필요.
"""

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("activitylog", "0003_login_actions"),
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
                    ("api_key_created", "API 키 발급"),
                    ("api_key_revoked", "API 키 회수"),
                    ("api_run_triggered", "API 로 Run 트리거"),
                ],
                max_length=20,
            ),
        ),
    ]
