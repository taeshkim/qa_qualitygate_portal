from django.core.management.base import BaseCommand
from apps.services.models import Service, Environment


class Command(BaseCommand):
    help = "Seed initial data (services and environments)"

    def handle(self, *args, **options):
        # Environments
        envs = [
            {
                "name": "Dev",
                "trigger_type": "jenkins_trigger",
                "gate_mode": "blocking",
                "next_env_control": "Stg 배포 버튼 제어",
            },
            {
                "name": "Stg",
                "trigger_type": "slack_only",
                "gate_mode": "blocking",
                "next_env_control": "Prd 배포 버튼 제어",
            },
            {
                "name": "Prd",
                "trigger_type": "slack_only",
                "gate_mode": "monitoring",
                "next_env_control": "",
            },
        ]

        for env_data in envs:
            env, created = Environment.objects.get_or_create(
                name=env_data["name"], defaults=env_data,
            )
            status = "created" if created else "exists"
            self.stdout.write(f"  Environment: {env.name} ({status})")

        # Services
        services = [
            {"name": "잡코리아", "code": "JK", "tribe_name": "잡코리아 트라이브"},
            {"name": "알바몬", "code": "AM", "tribe_name": "알바몬 트라이브"},
            {"name": "게임잡", "code": "GJ", "tribe_name": "게임잡 트라이브"},
            {"name": "하이어링센터", "code": "HC", "tribe_name": "하이어링센터 트라이브"},
        ]

        for svc_data in services:
            svc, created = Service.objects.get_or_create(
                code=svc_data["code"], defaults=svc_data,
            )
            status = "created" if created else "exists"
            self.stdout.write(f"  Service: {svc.name} ({status})")

        self.stdout.write(self.style.SUCCESS("Seed completed."))
