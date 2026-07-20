# 2026-05-20 — 컬럼명/구조 변경:
# 1) `error_4xx_owner` → `fe_owner` (rename, 값 보존)
# 2) `error_5xx_owner` → `be_owner` (rename, 값 보존)
# 3) `error_4xx_team` / `error_5xx_team` 삭제 (담당팀 컬럼 제거 — 사용자 요청)
# 4) team 관련 index 도 함께 제거
#
# 자동 생성 마이그레이션이 Remove+Add 형태로 만들어 값이 날아가는 걸 막기 위해
# 수동 정리 — RenameField 로 owner 컬럼은 데이터 보존.

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('integrations', '0005_alter_apiendpointowner_options_and_more'),
    ]

    operations = [
        # team 인덱스 먼저 제거 (필드 삭제 전제 조건)
        migrations.RemoveIndex(
            model_name='apiendpointowner',
            name='integration_error_4_09bb52_idx',
        ),
        migrations.RemoveIndex(
            model_name='apiendpointowner',
            name='integration_error_5_83d3ea_idx',
        ),
        # owner 컬럼은 값 보존하면서 이름만 변경
        migrations.RenameField(
            model_name='apiendpointowner',
            old_name='error_4xx_owner',
            new_name='fe_owner',
        ),
        migrations.RenameField(
            model_name='apiendpointowner',
            old_name='error_5xx_owner',
            new_name='be_owner',
        ),
        # 새 컬럼명의 verbose_name 반영
        migrations.AlterField(
            model_name='apiendpointowner',
            name='fe_owner',
            field=models.CharField(blank=True, max_length=200, verbose_name='FE 담당자'),
        ),
        migrations.AlterField(
            model_name='apiendpointowner',
            name='be_owner',
            field=models.CharField(blank=True, max_length=200, verbose_name='BE 담당자'),
        ),
        # 담당팀 컬럼은 삭제 — 데이터 손실 OK (사용자 요청)
        migrations.RemoveField(
            model_name='apiendpointowner',
            name='error_4xx_team',
        ),
        migrations.RemoveField(
            model_name='apiendpointowner',
            name='error_5xx_team',
        ),
    ]
