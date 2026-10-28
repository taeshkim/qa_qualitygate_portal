"""accounts 앱 테스트 — 포털 설정(메뉴 표시 토글) · 어드민 사용자 직접 추가."""
from django.contrib.auth import get_user_model
from django.test import TestCase

from apps.activitylog.models import ActivityLog

from .models import PortalSettings

User = get_user_model()


class PortalSettingsMenuTests(TestCase):
    """Quality Gate 메뉴 표시 토글 — context processor + 사이드바 분기 + 관리 화면."""

    def setUp(self):
        self.admin = User.objects.create_user(
            username="pa", password="x", role="qpe_admin", is_active=True)
        self.client.force_login(self.admin)

    def test_qg_menu_shown_by_default(self):
        """설정 row 없거나 기본값 → 사이드바에 Quality Gate 메뉴(fa-shield-alt) 노출."""
        resp = self.client.get("/")
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "fa-shield-alt")          # QG 메뉴 아이콘
        self.assertContains(resp, 'href="/qualitygates/"')  # QG 메뉴 링크

    def test_qg_menu_hidden_when_disabled(self):
        """show_quality_gate=False → 사이드바에서 QG 메뉴 사라짐."""
        ps = PortalSettings.get_solo()
        ps.show_quality_gate = False
        ps.save(update_fields=["show_quality_gate"])
        resp = self.client.get("/")
        self.assertEqual(resp.status_code, 200)
        self.assertNotContains(resp, "fa-shield-alt")
        self.assertNotContains(resp, 'href="/qualitygates/"')

    def test_portal_settings_page_and_toggle(self):
        """관리 화면 렌더 + 체크박스 유무로 on/off 저장."""
        resp = self.client.get("/accounts/portal-settings/")
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "Quality Gate")

        # 체크박스 없이 POST → 숨김
        self.client.post("/accounts/portal-settings/", {})
        self.assertFalse(PortalSettings.get_solo().show_quality_gate)

        # 체크박스 on → 표시
        self.client.post("/accounts/portal-settings/", {"show_quality_gate": "on"})
        self.assertTrue(PortalSettings.get_solo().show_quality_gate)

    def test_portal_settings_admin_only(self):
        """non-admin 은 포털 설정 접근 차단."""
        viewer = User.objects.create_user(
            username="v", password="x", role="viewer", is_active=True)
        self.client.force_login(viewer)
        resp = self.client.get("/accounts/portal-settings/")
        self.assertIn(resp.status_code, (403, 302))


class ViewerAccessTests(TestCase):
    """Viewer 는 Dashboard/Suite Runs 영역만 열람(Visualization 은 QPE Admin 전용) — 그 외 GET 차단 + 메뉴 숨김."""

    def setUp(self):
        self.viewer = User.objects.create_user(
            username="vw", password="x", role="viewer", is_active=True)
        self.ops = User.objects.create_user(
            username="op", password="x", role="qpe_ops", is_active=True)

    def test_viewer_allowed_pages_200(self):
        self.client.force_login(self.viewer)
        for path in ("/", "/suites/runs/"):
            self.assertEqual(self.client.get(path).status_code, 200, f"{path} 200 기대")

    def test_viewer_blocked_pages_redirect_to_dashboard(self):
        self.client.force_login(self.viewer)
        for path in ("/suites/", "/robustness/", "/logs/", "/suites/queue/", "/qualitygates/", "/visualization/", "/visualization/data/"):
            resp = self.client.get(path)
            self.assertEqual(resp.status_code, 302, f"{path} 차단(302) 기대")
            self.assertEqual(resp.headers.get("Location"), "/", f"{path} → 대시보드 리다이렉트")

    def test_viewer_force_url_blocked(self):
        """메뉴를 숨겨도 URL 직접 입력(강제 접근) 시 서버에서 차단 — deny-by-default."""
        self.client.force_login(self.viewer)
        forced = [
            "/suites/11111111-1111-1111-1111-111111111111/",  # Suite 설정 상세
            "/suites/credentials/",                            # 자격증명 관리
            "/accounts/users/",                                # Admin 사용자 관리
            "/accounts/api-owners/",                           # Admin 담당자 관리
            "/qualitygates/",                                  # Quality Gate
            "/logs/",                                          # Activity Log
        ]
        for path in forced:
            resp = self.client.get(path)
            self.assertEqual(resp.status_code, 302, f"{path} 강제 접근 차단(302) 기대")
            self.assertEqual(resp.headers.get("Location"), "/")

    def test_viewer_xhr_blocked_silently(self):
        """XHR(전역 폴링)로 차단 페이지 접근 시 메시지 누적 없이 리다이렉트."""
        self.client.force_login(self.viewer)
        resp = self.client.get("/logs/", HTTP_X_REQUESTED_WITH="XMLHttpRequest")
        self.assertEqual(resp.status_code, 302)

    def test_non_viewer_not_restricted(self):
        """일반 역할(ops)은 GET 제한 없음 — 기존 동작 유지."""
        self.client.force_login(self.ops)
        self.assertEqual(self.client.get("/suites/").status_code, 200)
        self.assertEqual(self.client.get("/robustness/").status_code, 200)

    def test_viewer_sidebar_only_three_menus(self):
        self.client.force_login(self.viewer)
        html = self.client.get("/").content.decode()
        # 허용 메뉴
        self.assertIn('href="/suites/runs/"', html)
        self.assertNotIn('href="/visualization/"', html)  # Visualization (QPE Admin 전용)
        # 숨겨야 할 메뉴
        self.assertNotIn('href="/suites/"', html)        # Test Suites
        self.assertNotIn('href="/robustness/"', html)    # Reliability
        self.assertNotIn('href="/suites/queue/"', html)  # Queue
        self.assertNotIn('href="/logs/"', html)          # Activity Log
        self.assertNotIn('href="/qualitygates/"', html)  # Quality Gates

    def test_visualization_is_qpe_admin_only(self):
        """Visualization: QPE Admin(은 화면·피드 200), ops 는 대시보드로 리다이렉트/피드 403, 메뉴도 숨김."""
        admin = User.objects.create_user(username="qa", password="x", role="qpe_admin", is_active=True)
        self.client.force_login(admin)
        self.assertEqual(self.client.get("/visualization/").status_code, 200)
        self.assertEqual(self.client.get("/visualization/data/").status_code, 200)
        self.assertIn('href="/visualization/"', self.client.get("/").content.decode())
        self.client.force_login(self.ops)
        resp = self.client.get("/visualization/")
        self.assertEqual((resp.status_code, resp.headers.get("Location")), (302, "/"))
        self.assertEqual(self.client.get("/visualization/data/").status_code, 403)
        self.assertNotIn('href="/visualization/"', self.client.get("/").content.decode())

    def test_non_viewer_sidebar_has_all_menus(self):
        self.client.force_login(self.ops)
        html = self.client.get("/").content.decode()
        self.assertIn('href="/suites/"', html)
        self.assertIn('href="/robustness/"', html)
        self.assertIn('href="/logs/"', html)


class AdminUserCreateTests(TestCase):
    """어드민 "사용자 직접 추가"(/accounts/users/new/) — 가입 승인 절차 없이 즉시 활성 계정 생성."""

    VALID_PASSWORD = "Passw0rd!"

    def setUp(self):
        self.admin = User.objects.create_user(
            username="admin1", password="x", role="qpe_admin", is_active=True)

    def _valid_payload(self, **overrides):
        payload = {
            "username": "newbie",
            "first_name": "새유저",
            "email": "newbie@example.com",
            "role": "qpe_ops",
            "password1": self.VALID_PASSWORD,
            "password2": self.VALID_PASSWORD,
        }
        payload.update(overrides)
        return payload

    # 1) 정상 생성 — 활성/역할/실로그인/ActivityLog
    def test_admin_create_success(self):
        self.client.force_login(self.admin)
        resp = self.client.post("/accounts/users/new/", self._valid_payload())
        self.assertRedirects(resp, "/accounts/users/")

        user = User.objects.get(username="newbie")
        self.assertTrue(user.is_active)
        self.assertEqual(user.role, "qpe_ops")

        # 지정한 비밀번호로 실제 로그인 가능
        login_client = self.client_class()
        self.assertTrue(
            login_client.login(username="newbie", password=self.VALID_PASSWORD))

        log = ActivityLog.objects.filter(
            action=ActivityLog.Action.CREATE,
            target_type=ActivityLog.TargetType.USER,
            target_name="newbie",
        )
        self.assertEqual(log.count(), 1)

    # 2) 중복 ID / 이메일 → 폼 에러, 유저 미생성
    def test_duplicate_username_rejected(self):
        User.objects.create_user(
            username="dupuser", password="x", email="dupuser@example.com",
            role="viewer", is_active=True)
        self.client.force_login(self.admin)
        resp = self.client.post("/accounts/users/new/", self._valid_payload(
            username="dupuser", email="another@example.com"))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "이미 사용 중인 ID입니다")
        self.assertEqual(User.objects.filter(username="dupuser").count(), 1)

    def test_duplicate_email_rejected(self):
        User.objects.create_user(
            username="existing", password="x", email="dup@example.com",
            role="viewer", is_active=True)
        self.client.force_login(self.admin)
        resp = self.client.post("/accounts/users/new/", self._valid_payload(
            username="brandnew", email="dup@example.com"))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "이미 등록된 이메일입니다")
        self.assertFalse(User.objects.filter(username="brandnew").exists())

    # 3) 비밀번호 규칙 위반 → 폼 에러, 유저 미생성
    def test_password_too_short_rejected(self):
        # CharField(min_length=8) 검증이 클린 메서드보다 먼저 실패해 Django 기본
        # min_length 에러 메시지("최소 8 개의 글자")로 노출된다 — 실동작 기준.
        self.client.force_login(self.admin)
        resp = self.client.post("/accounts/users/new/", self._valid_payload(
            username="shortpw", email="shortpw@example.com",
            password1="Ab1!", password2="Ab1!"))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "최소 8 개의 글자")
        self.assertFalse(User.objects.filter(username="shortpw").exists())

    def test_password_without_special_char_rejected(self):
        self.client.force_login(self.admin)
        resp = self.client.post("/accounts/users/new/", self._valid_payload(
            username="nospecial", email="nospecial@example.com",
            password1="Password1", password2="Password1"))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "특수문자")
        self.assertFalse(User.objects.filter(username="nospecial").exists())

    def test_password_mismatch_rejected(self):
        self.client.force_login(self.admin)
        resp = self.client.post("/accounts/users/new/", self._valid_payload(
            username="mismatch", email="mismatch@example.com",
            password1=self.VALID_PASSWORD, password2="Different1!"))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "비밀번호가 일치하지 않습니다")
        self.assertFalse(User.objects.filter(username="mismatch").exists())

    # 4) 비관리자 / 미로그인 접근 차단
    def test_viewer_get_blocked(self):
        viewer = User.objects.create_user(
            username="viewer1", password="x", role="viewer", is_active=True)
        self.client.force_login(viewer)
        resp = self.client.get("/accounts/users/new/")
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(resp.headers.get("Location"), "/")

    def test_viewer_post_blocked_no_user_created(self):
        viewer = User.objects.create_user(
            username="viewer2", password="x", role="viewer", is_active=True)
        self.client.force_login(viewer)
        resp = self.client.post("/accounts/users/new/", self._valid_payload(
            username="viewerattempt", email="viewerattempt@example.com"))
        self.assertEqual(resp.status_code, 302)
        self.assertFalse(User.objects.filter(username="viewerattempt").exists())

    def test_non_admin_regular_role_forbidden(self):
        """viewer 외 일반 role(qpe_ops)은 미들웨어는 통과하나 _require_admin 이 403."""
        ops = User.objects.create_user(
            username="ops1", password="x", role="qpe_ops", is_active=True)
        self.client.force_login(ops)
        resp = self.client.get("/accounts/users/new/")
        self.assertEqual(resp.status_code, 403)
        resp = self.client.post("/accounts/users/new/", self._valid_payload(
            username="opsattempt", email="opsattempt@example.com"))
        self.assertIn(resp.status_code, (302, 403))  # 미들웨어(302) 또는 view(403) 차단
        self.assertFalse(User.objects.filter(username="opsattempt").exists())

    def test_anonymous_get_redirected_to_login(self):
        resp = self.client.get("/accounts/users/new/")
        self.assertEqual(resp.status_code, 302)
        self.assertIn("/accounts/login", resp.headers.get("Location", ""))

    def test_anonymous_post_blocked_no_user_created(self):
        resp = self.client.post("/accounts/users/new/", self._valid_payload(
            username="anonattempt", email="anonattempt@example.com"))
        self.assertEqual(resp.status_code, 302)
        self.assertIn("/accounts/login", resp.headers.get("Location", ""))
        self.assertFalse(User.objects.filter(username="anonattempt").exists())

    # 5) GET 폼 렌더 — 역할 select 포함
    def test_admin_get_renders_form_with_role_field(self):
        self.client.force_login(self.admin)
        resp = self.client.get("/accounts/users/new/")
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "사용자 직접 추가")
        self.assertContains(resp, 'name="role"')
        self.assertContains(resp, "Viewer")
        self.assertContains(resp, "QPE Ops")
