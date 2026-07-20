import base64
import json
import logging
import os
from concurrent.futures import ThreadPoolExecutor
from html.parser import HTMLParser
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen

logger = logging.getLogger(__name__)


def _safe_future_result(future, default):
    """Future 가 None 이거나 예외가 발생했을 때도 default 를 반환해주는 헬퍼."""
    if future is None:
        return default
    try:
        return future.result()
    except Exception as e:
        logger.info(f"Parallel choice fetch failed: {e}")
        return default


def _internal_credential_defaults():
    """DB 에 저장된 internal Jenkins 자격증명을 가져온다.
    DB/마이그레이션 미준비 시점에서도 안전하도록 광범위하게 예외를 삼킨다.
    """
    try:
        from apps.suites.models import SuiteJenkinsCredential
        cred = SuiteJenkinsCredential.objects.filter(
            kind=SuiteJenkinsCredential.Kind.INTERNAL,
        ).first()
    except Exception:
        return "", "", ""
    if not cred:
        return "", "", ""
    return cred.base_url, cred.user, cred.api_token


class JenkinsClient:
    def __init__(self, base_url=None, user=None, api_token=None):
        db_url, db_user, db_token = "", "", ""
        if not (base_url and user and api_token):
            db_url, db_user, db_token = _internal_credential_defaults()
        self.base_url = (
            base_url or db_url or os.getenv("JENKINS_DEFAULT_URL", "")
        ).rstrip("/")
        self.user = user or db_user or os.getenv("JENKINS_DEFAULT_USER", "")
        self.api_token = (
            api_token or db_token or os.getenv("JENKINS_DEFAULT_API_TOKEN", "")
        )

    @property
    def _auth_header(self):
        credentials = base64.b64encode(
            f"{self.user}:{self.api_token}".encode()
        ).decode()
        return f"Basic {credentials}"

    def _job_path_to_url(self, job_full_name: str) -> str:
        """
        Jenkins Full Project Name을 URL 경로로 변환.
        '100_jk_regression_test_group/Mobile App/dev_jk_regression_mapi_test/dev_jk_regression_mapi_test_1'
        → '/job/100_jk_regression_test_group/job/Mobile%20App/job/.../job/...'
        """
        parts = job_full_name.strip("/").split("/")
        return "/job/" + "/job/".join(quote(p, safe="") for p in parts)

    def _request(self, path: str, method: str = "GET", data: bytes = None) -> dict:
        url = f"{self.base_url}{path}"
        req = Request(url, method=method, data=data)
        req.add_header("Authorization", self._auth_header)
        if data:
            req.add_header("Content-Type", "application/x-www-form-urlencoded")

        try:
            with urlopen(req, timeout=8) as resp:
                body = resp.read()
                if body:
                    return json.loads(body)
                return {"status": resp.status, "url": url}
        except HTTPError as e:
            body = e.read().decode()[:500]
            location = ""
            try:
                location = e.headers.get("Location", "") or ""
            except Exception:
                pass
            # SSO 가 깔린 Jenkins 에서 basic auth 가 거부되면 (= 토큰이 revoke/만료)
            # 익명 처리되어 user-context endpoint 에서 commenceLogin 으로 302 redirect 가 떨어진다.
            # urllib 가 이걸 "redirect loop" 로 보고 HTTPError 로 올리는데, 메시지가 너무 모호해서
            # 운영자가 토큰을 갱신해야 한다는 사실을 알기 어렵다 — 명확한 메시지로 변환한다.
            reason_l = (e.reason or "").lower()
            looks_like_sso = (
                "commencelogin" in location.lower()
                or "securityrealm" in location.lower()
                or "infinite loop" in reason_l
                or "redirect error" in reason_l
            )
            if looks_like_sso:
                msg = (
                    "Jenkins 인증 실패 (basic auth 가 SSO 로그인으로 리다이렉트됨) — "
                    "저장된 API 토큰이 만료/취소된 것으로 보입니다. "
                    "Jenkins 우상단 사용자 메뉴 → Configure → API Token 에서 새 토큰을 발급받아 "
                    "Portal 의 Suite 자격증명(api_token) 을 갱신해주세요."
                )
                logger.error(f"Jenkins auth via basic auth rejected (302 -> SSO): {url} location={location}")
                raise JenkinsError(msg, status=401) from e
            logger.error(f"Jenkins API error: {e.code} {e.reason} - {body}")
            raise JenkinsError(f"HTTP {e.code}: {e.reason}", status=e.code) from e
        except URLError as e:
            logger.error(f"Jenkins connection error: {e.reason}")
            raise JenkinsError(f"Connection failed: {e.reason}") from e

    def test_connection(self) -> dict:
        data = self._request("/api/json")
        return {
            "ok": True,
            "primary_view": data.get("primaryView", {}).get("name"),
            "jobs_count": len(data.get("jobs", [])),
        }

    def list_folder_jobs(self, folder_full_name: str) -> list:
        """Jenkins 폴더 내 하위 Job 목록 조회. 각 Job의 전체 스테이지 수도 포함."""
        path = self._job_path_to_url(folder_full_name)
        tree = "jobs[name,url,color,buildable,lastBuild[number,result,duration,timestamp]]"
        data = self._request(f"{path}/api/json?tree={tree}")
        jobs = []
        for j in data.get("jobs", []):
            lb = j.get("lastBuild") or {}
            full_path = f"{folder_full_name}/{j['name']}"

            # 완료된 빌드에서 전체 스테이지 수 조회
            total_stages = 0
            if lb.get("number") and not lb.get("building", False):
                stages = self.get_build_stages(full_path, lb["number"])
                total_stages = len(stages)

            jobs.append({
                "name": j.get("name"),
                "full_path": full_path,
                "url": j.get("url"),
                "color": j.get("color"),
                "buildable": j.get("buildable", True),
                "last_build_number": lb.get("number"),
                "last_build_result": lb.get("result"),
                "last_build_duration": lb.get("duration"),
                "total_stages": total_stages,
            })
        return jobs

    def get_next_build_number(self, job_full_name: str) -> int:
        """트리거 전에 다음 빌드 번호를 조회."""
        path = self._job_path_to_url(job_full_name)
        data = self._request(f"{path}/api/json?tree=nextBuildNumber")
        return data.get("nextBuildNumber", 0)

    def _is_parameterized(self, job_full_name: str) -> bool:
        """Job에 파라미터가 정의되어 있는지 확인."""
        info = self.get_job_info(job_full_name)
        return len(info.get("parameters", [])) > 0

    def trigger_build(self, job_full_name: str, parameters: dict = None) -> dict:
        """Jenkins Job 빌드 트리거. 파라미터 유무에 따라 엔드포인트 자동 선택."""
        next_build = self.get_next_build_number(job_full_name)

        path = self._job_path_to_url(job_full_name)

        # 파라미터가 있는 Job → buildWithParameters, 없는 Job → build
        if parameters or self._is_parameterized(job_full_name):
            endpoint = f"{path}/buildWithParameters"
            data = urlencode(parameters).encode() if parameters else b""
        else:
            endpoint = f"{path}/build"
            data = None

        url = f"{self.base_url}{endpoint}"
        req = Request(url, method="POST", data=data if data else b"")
        req.add_header("Authorization", self._auth_header)
        if data:
            req.add_header("Content-Type", "application/x-www-form-urlencoded")

        try:
            with urlopen(req, timeout=30) as resp:
                queue_url = resp.headers.get("Location", "")
                return {"triggered": True, "queue_url": queue_url, "build_number": next_build}
        except HTTPError as e:
            if e.code == 201:
                queue_url = e.headers.get("Location", "")
                return {"triggered": True, "queue_url": queue_url, "build_number": next_build}
            body = e.read().decode()[:500]
            raise JenkinsError(f"HTTP {e.code}: {e.reason} - {body}", status=e.code) from e

    def get_job_info(self, job_full_name: str) -> dict:
        path = self._job_path_to_url(job_full_name)
        data = self._request(f"{path}/api/json")
        params = []
        for prop in data.get("property", []):
            for p in prop.get("parameterDefinitions", []):
                params.append({
                    "name": p.get("name"),
                    "type": p.get("type"),
                    "default": (p.get("defaultParameterValue") or {}).get("value", ""),
                    "choices": p.get("choices") or [],
                    "description": p.get("description", ""),
                })

        # Active Choices / Git Parameter 등 JSON API 로 choices 가 오지 않는 플러그인 대응:
        #   1) 빌드 폼 HTML 스크래프
        #   2) scriptText 로 Active Choices getChoices() 조회
        #   3) Git Parameter Plugin descriptor HTTP (param 당 1회)
        #   4) scriptText 로 GitParameterDefinition.getAllValueItems()
        # 각 경로는 서로 독립적이라 병렬로 실행. 순차 실행 시 10~20초 걸리던 게
        # 가장 느린 호출 시간으로 수렴.
        if any(not p["choices"] for p in params):
            initial_git_names = [p["name"] for p in params if "git" in (p["type"] or "").lower()]

            with ThreadPoolExecutor(max_workers=4) as executor:
                futures = {
                    "html": executor.submit(self._scrape_build_form_choices, job_full_name),
                    "active_script": executor.submit(self._get_active_choices_via_script, job_full_name),
                    "git_script": executor.submit(self._get_git_parameter_choices_via_script, job_full_name) if initial_git_names else None,
                }
                # Git Parameter 는 파라미터별 descriptor 엔드포인트가 따로 있어서 각각 submit
                git_http_futures = {
                    name: executor.submit(self._fetch_git_parameter_choices, job_full_name, name)
                    for name in initial_git_names
                }

                html_choices = _safe_future_result(futures["html"], {})
                active_script_choices = _safe_future_result(futures["active_script"], {})
                git_script_choices = _safe_future_result(futures["git_script"], {})
                git_http_choices = {
                    name: _safe_future_result(f, [])
                    for name, f in git_http_futures.items()
                }

            # 병합 우선순위: HTML 스크래프 > Active Choices 스크립트 > Git HTTP > Git 스크립트
            for p in params:
                if p["choices"]:
                    continue
                name = p["name"]
                if name in html_choices and html_choices[name]:
                    p["choices"] = html_choices[name]
                elif name in active_script_choices and active_script_choices[name]:
                    p["choices"] = active_script_choices[name]
                elif name in git_http_choices and git_http_choices[name]:
                    p["choices"] = git_http_choices[name]
                elif name in git_script_choices and git_script_choices[name]:
                    p["choices"] = git_script_choices[name]

        # Active Choices 렌더링 마커(:selected / :disabled) 제거.
        # CHECKBOX/RADIO 타입이 기본 체크 상태를 나타내기 위해 value 에 덧붙인 접미어라
        # 실제 빌드에 넘길 때는 벗겨야 한다.
        for p in params:
            if p["choices"]:
                cleaned = []
                for c in p["choices"]:
                    v = _strip_active_choice_marker(str(c))
                    if v and v not in cleaned:
                        cleaned.append(v)
                p["choices"] = cleaned
            if p["default"]:
                p["default"] = _strip_active_choice_marker(str(p["default"]))

        return {
            "name": data.get("displayName"),
            "url": data.get("url"),
            "buildable": data.get("buildable"),
            "last_build": data.get("lastBuild", {}).get("number") if data.get("lastBuild") else None,
            "color": data.get("color"),
            "parameters": params,
        }

    def _get_csrf_crumb(self) -> dict:
        """Jenkins CSRF crumb 조회. 실패 시 빈 dict."""
        try:
            url = f"{self.base_url}/crumbIssuer/api/json"
            req = Request(url, method="GET")
            req.add_header("Authorization", self._auth_header)
            with urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read())
                return {data["crumbRequestField"]: data["crumb"]}
        except (HTTPError, URLError, KeyError, ValueError) as e:
            logger.debug(f"crumb fetch failed: {e}")
            return {}

    def _get_active_choices_via_script(self, job_full_name: str) -> dict:
        """Jenkins Script Console(/scriptText) 로 Active Choices 파라미터의 choices 획득.
        API 토큰에 Overall/RunScripts 권한이 있어야 동작."""
        safe_name = job_full_name.replace("\\", "\\\\").replace("'", "\\'")
        groovy = (
            "import jenkins.model.*\n"
            f"def job = Jenkins.instance.getItemByFullName('{safe_name}')\n"
            "if (job == null) { return }\n"
            "def prop = job.getProperty(hudson.model.ParametersDefinitionProperty)\n"
            "if (prop == null) { return }\n"
            "prop.parameterDefinitions.each { p ->\n"
            "  try {\n"
            "    def choices = null\n"
            "    if (p.metaClass.respondsTo(p, 'getChoices')) {\n"
            "      choices = p.getChoices()\n"
            "    }\n"
            "    if ((choices == null || choices.isEmpty()) && p.metaClass.respondsTo(p, 'getTags')) {\n"
            "      choices = p.getTags()\n"
            "    }\n"
            "    if (choices instanceof Map) { choices = choices.keySet() as List }\n"
            "    if (choices != null && !choices.isEmpty()) {\n"
            "      def vals = choices.collect { it.toString() }.join('|\\u0001|')\n"
            "      println '@@QGP@@' + p.name + '=' + vals\n"
            "    }\n"
            "  } catch (Throwable t) {}\n"
            "}\n"
        )
        headers = {"Content-Type": "application/x-www-form-urlencoded"}
        headers.update(self._get_csrf_crumb())

        url = f"{self.base_url}/scriptText"
        data = urlencode({"script": groovy}).encode()
        req = Request(url, method="POST", data=data)
        req.add_header("Authorization", self._auth_header)
        for k, v in headers.items():
            req.add_header(k, v)
        try:
            with urlopen(req, timeout=30) as resp:
                output = resp.read().decode("utf-8", errors="replace")
        except (HTTPError, URLError) as e:
            logger.info(
                f"Active Choices via scriptText 조회 실패 ({job_full_name}): {e}. "
                "API 토큰에 Overall/RunScripts 권한이 필요합니다."
            )
            return {}

        result = {}
        for line in output.splitlines():
            if line.startswith("@@QGP@@") and "=" in line:
                rest = line[len("@@QGP@@"):]
                name, _, val = rest.partition("=")
                if name:
                    result[name] = [x for x in val.split("|\x01|") if x]
        return result

    def _fetch_git_parameter_choices(self, job_full_name: str, param_name: str) -> list:
        """Git Parameter Plugin 의 descriptor 엔드포인트로 실제 브랜치/태그 목록 조회.
        최신 버전은 JSON(ItemsErrorModel), 구버전은 HTML <select><option> 을 반환한다."""
        path = self._job_path_to_url(job_full_name)
        descriptor = "net.uaznia.lukanus.hudson.plugins.gitparameter.GitParameterDefinition"
        qs = urlencode({"param": param_name})
        url = f"{self.base_url}{path}/descriptorByName/{descriptor}/fillValueItems?{qs}"
        req = Request(url, method="GET")
        req.add_header("Authorization", self._auth_header)
        try:
            with urlopen(req, timeout=30) as resp:
                body = resp.read().decode("utf-8", errors="replace")
        except (HTTPError, URLError) as e:
            logger.info(
                f"Git Parameter descriptor fetch failed ({job_full_name}/{param_name}): {e}"
            )
            return []

        stripped = body.lstrip()
        if stripped.startswith("{") or stripped.startswith("["):
            try:
                data = json.loads(body)
            except ValueError as e:
                logger.debug(f"Git Parameter JSON parse failed: {e}")
                return []
            values = data.get("values") if isinstance(data, dict) else None
            if not isinstance(values, list):
                return []
            out = []
            for v in values:
                if isinstance(v, dict):
                    val = v.get("value") or v.get("name")
                    if val and val not in out:
                        out.append(val)
                elif v and v not in out:
                    out.append(str(v))
            return out

        parser = _GitParamOptionsParser()
        try:
            parser.feed(body)
        except Exception as e:
            logger.debug(f"Git Parameter HTML parse failed: {e}")
            return []
        return parser.options

    def _get_git_parameter_choices_via_script(self, job_full_name: str) -> dict:
        """Jenkins scriptText 로 GitParameterDefinition 의 실제 값 목록 조회.
        plugin 의 getAllValueItems(JobWrapper, ParametersDefinitionProperty) 를 호출."""
        safe_name = job_full_name.replace("\\", "\\\\").replace("'", "\\'")
        groovy = (
            "import jenkins.model.*\n"
            "import net.uaznia.lukanus.hudson.plugins.gitparameter.*\n"
            "import net.uaznia.lukanus.hudson.plugins.gitparameter.jobs.*\n"
            f"def job = Jenkins.instance.getItemByFullName('{safe_name}')\n"
            "if (job == null) { return }\n"
            "def prop = job.getProperty(hudson.model.ParametersDefinitionProperty)\n"
            "if (prop == null) { return }\n"
            "def jobWrapper = null\n"
            "try { jobWrapper = JobWrapperFactory.createJobWrapper(job) } catch (Throwable t) {}\n"
            "prop.parameterDefinitions.each { p ->\n"
            "  try {\n"
            "    if (!(p.class.name.endsWith('GitParameterDefinition'))) { return }\n"
            "    def items = null\n"
            "    try { items = p.getAllValueItems(jobWrapper, prop) } catch (Throwable t) {}\n"
            "    if (items == null) {\n"
            "      try { items = p.getAllValueItems() } catch (Throwable t) {}\n"
            "    }\n"
            "    def vals = null\n"
            "    if (items instanceof Map) { vals = items.keySet() as List }\n"
            "    else if (items instanceof Collection) { vals = items as List }\n"
            "    if (vals != null && !vals.isEmpty()) {\n"
            "      def joined = vals.collect { it.toString() }.join('|\\u0001|')\n"
            "      println '@@QGP@@' + p.name + '=' + joined\n"
            "    }\n"
            "  } catch (Throwable t) {}\n"
            "}\n"
        )
        headers = {"Content-Type": "application/x-www-form-urlencoded"}
        headers.update(self._get_csrf_crumb())

        url = f"{self.base_url}/scriptText"
        data = urlencode({"script": groovy}).encode()
        req = Request(url, method="POST", data=data)
        req.add_header("Authorization", self._auth_header)
        for k, v in headers.items():
            req.add_header(k, v)
        try:
            with urlopen(req, timeout=60) as resp:
                output = resp.read().decode("utf-8", errors="replace")
        except (HTTPError, URLError) as e:
            logger.info(
                f"Git Parameter via scriptText 조회 실패 ({job_full_name}): {e}. "
                "API 토큰에 Overall/RunScripts 권한이 필요합니다."
            )
            return {}

        result = {}
        for line in output.splitlines():
            if line.startswith("@@QGP@@") and "=" in line:
                rest = line[len("@@QGP@@"):]
                name, _, val = rest.partition("=")
                if name:
                    result[name] = [x for x in val.split("|\x01|") if x]
        return result

    def _scrape_build_form_choices(self, job_full_name: str) -> dict:
        """Jenkins 빌드 폼 HTML 을 파싱하여 {파라미터명: [옵션...]} 반환."""
        path = self._job_path_to_url(job_full_name)
        url = f"{self.base_url}{path}/build?delay=0sec"
        req = Request(url, method="GET")
        req.add_header("Authorization", self._auth_header)
        try:
            with urlopen(req, timeout=30) as resp:
                html = resp.read().decode("utf-8", errors="replace")
        except (HTTPError, URLError) as e:
            logger.debug(f"Build form fetch failed for {job_full_name}: {e}")
            return {}

        parser = _ParamChoicesParser()
        try:
            parser.feed(html)
        except Exception as e:
            logger.debug(f"Build form parse failed for {job_full_name}: {e}")
            return {}
        return parser.result

    def get_build_info(
        self,
        job_full_name: str,
        build_number: int,
        *,
        prefer_remote_urls: list | None = None,
        prefer_branches: list | None = None,
    ) -> dict:
        """Pipeline-from-SCM 빌드는 BuildData 가 여러 개(Jenkinsfile + source) — hint 로
        source 측의 BuildData 를 우선 선택할 수 있다."""
        path = self._job_path_to_url(job_full_name)
        data = self._request(f"{path}/{build_number}/api/json")
        git_branch, git_commit = _extract_git_info(
            data.get("actions") or [],
            prefer_remote_urls=prefer_remote_urls,
            prefer_branches=prefer_branches,
        )
        return {
            "number": data.get("number"),
            "result": data.get("result"),
            "building": data.get("building"),
            "duration": data.get("duration"),
            "url": data.get("url"),
            "timestamp": data.get("timestamp"),
            "git_branch": git_branch,
            "git_commit": git_commit,
        }

    def get_build_stages(self, job_full_name: str, build_number: int) -> list:
        """Pipeline 빌드의 스테이지 목록 조회 (wfapi)."""
        path = self._job_path_to_url(job_full_name)
        try:
            data = self._request(f"{path}/{build_number}/wfapi/describe")
            stages = []
            for s in data.get("stages", []):
                stages.append({
                    "name": s.get("name", ""),
                    "status": s.get("status", ""),  # SUCCESS, IN_PROGRESS, FAILED, NOT_EXECUTED
                    "duration_ms": s.get("durationMillis", 0),
                })
            return stages
        except JenkinsError:
            return []

    def is_job_building(self, job_full_name: str) -> bool:
        """Job이 현재 빌드 중인지 확인."""
        try:
            path = self._job_path_to_url(job_full_name)
            data = self._request(f"{path}/lastBuild/api/json?tree=building")
            return data.get("building", False)
        except JenkinsError:
            return False

    def get_console_output(self, job_full_name: str, build_number: int) -> str:
        """Jenkins 빌드의 콘솔 출력(텍스트) 조회."""
        path = self._job_path_to_url(job_full_name)
        url = f"{self.base_url}{path}/{build_number}/consoleText"
        req = Request(url, method="GET")
        req.add_header("Authorization", self._auth_header)
        try:
            with urlopen(req, timeout=60) as resp:
                return resp.read().decode("utf-8", errors="replace")
        except HTTPError as e:
            logger.error(f"Console output error: {e.code} {e.reason}")
            raise JenkinsError(f"HTTP {e.code}: {e.reason}", status=e.code) from e
        except URLError as e:
            logger.error(f"Console output connection error: {e.reason}")
            raise JenkinsError(f"Connection failed: {e.reason}") from e

    def stop_build(self, job_full_name: str, build_number: int) -> bool:
        """Jenkins 빌드를 강제 중단(abort)한다. 성공 시 True."""
        path = self._job_path_to_url(job_full_name)
        url = f"{self.base_url}{path}/{build_number}/stop"
        req = Request(url, method="POST", data=b"")
        req.add_header("Authorization", self._auth_header)
        try:
            with urlopen(req, timeout=30) as resp:
                return True
        except HTTPError as e:
            # Jenkins stop은 보통 302 redirect 로 응답
            if e.code in (200, 201, 302):
                return True
            logger.warning(f"stop_build {job_full_name} #{build_number}: {e.code} {e.reason}")
            return False
        except URLError as e:
            logger.warning(f"stop_build connection error: {e.reason}")
            return False

    def get_last_build_info(self, job_full_name: str) -> dict:
        path = self._job_path_to_url(job_full_name)
        data = self._request(f"{path}/lastBuild/api/json")
        return {
            "number": data.get("number"),
            "result": data.get("result"),
            "building": data.get("building"),
            "duration": data.get("duration"),
            "url": data.get("url"),
        }


class JenkinsError(Exception):
    def __init__(self, message, status=None):
        super().__init__(message)
        self.status = status


_GIT_PARAM_NAMES = ("GIT_BRANCH", "BRANCH", "BRANCH_NAME", "GIT_TAG", "TAG", "REVISION")


def _normalize_repo_url(url: str) -> str:
    """git repo URL 비교를 위한 정규화 — scheme/credential/`.git` suffix 제거 + 소문자."""
    if not isinstance(url, str):
        return ""
    s = url.strip().lower()
    # SSH style 먼저 (git@host:owner/repo) — `:` 를 `/` 로 변환해 일반 path 형태로.
    if s.startswith("git@"):
        s = s[len("git@"):].replace(":", "/", 1)
    else:
        for prefix in ("https://", "http://", "ssh://", "git://"):
            if s.startswith(prefix):
                s = s[len(prefix):]
                break
    if "@" in s and "/" in s:
        # user:token@host/... 의 credential 부분 제거
        head, _, tail = s.partition("/")
        if "@" in head:
            head = head.split("@", 1)[1]
            s = head + "/" + tail
    if s.endswith(".git"):
        s = s[:-len(".git")]
    return s.rstrip("/")


def _normalize_branch_name(name: str) -> str:
    """branch name 비교용 — origin/, refs/heads/, refs/remotes/origin/ prefix 제거."""
    if not isinstance(name, str):
        return ""
    s = name.strip()
    for prefix in ("refs/remotes/origin/", "refs/heads/", "origin/"):
        if s.startswith(prefix):
            s = s[len(prefix):]
            break
    return s


def _extract_git_info(
    actions: list,
    *,
    prefer_remote_urls: list | None = None,
    prefer_branches: list | None = None,
) -> tuple:
    """Jenkins build actions 배열에서 git branch / commit hash 를 추출한다.

    Pipeline-from-SCM 빌드는 actions 안에 BuildData 가 두 개 이상 들어오는 경우가 있다 —
    Jenkinsfile 저장소(보통 master 고정) 와 실제 source code 저장소. 호출 측이 hint 로
    `prefer_remote_urls` (예: image_params_json 의 SCM_URL) / `prefer_branches` (예:
    BRANCH_NAME) 를 넘기면 매칭되는 BuildData 를 우선 선택해 source 의 branch/commit 을
    돌려준다. hint 매칭이 없으면 기존 동작(첫 BuildData) 으로 fallback.

    우선순위:
      1) BuildData 중 hint 매칭 (remoteUrls 일치 → branches 일치 순)
      2) 매칭 없으면 첫 BuildData
      3) 그래도 못 잡으면 ParametersAction 의 BRANCH/GIT_BRANCH/TAG 등
    """
    build_datas: list[dict] = []
    for action in actions:
        if not isinstance(action, dict):
            continue
        cls = action.get("_class") or ""
        if "BuildData" in cls:
            build_datas.append(action)

    norm_pref_urls = [_normalize_repo_url(u) for u in (prefer_remote_urls or []) if u]
    norm_pref_branches = [_normalize_branch_name(b) for b in (prefer_branches or []) if b]

    chosen: dict | None = None
    # 1) remoteUrls 매칭
    if norm_pref_urls:
        for bd in build_datas:
            for u in (bd.get("remoteUrls") or []):
                if _normalize_repo_url(u) in norm_pref_urls:
                    chosen = bd
                    break
            if chosen:
                break
    # 2) branches 매칭
    if chosen is None and norm_pref_branches:
        for bd in build_datas:
            for b in ((bd.get("lastBuiltRevision") or {}).get("branch") or []):
                if not isinstance(b, dict):
                    continue
                if _normalize_branch_name(b.get("name") or "") in norm_pref_branches:
                    chosen = bd
                    break
            if chosen:
                break
    # 3) fallback — 첫 BuildData (기존 동작)
    if chosen is None and build_datas:
        chosen = build_datas[0]

    branch, commit = "", ""
    if chosen is not None:
        revision = chosen.get("lastBuiltRevision") or {}
        commit = revision.get("SHA1") or ""
        for b in (revision.get("branch") or []):
            if isinstance(b, dict):
                name = b.get("name") or ""
                if name:
                    branch = name
                    break

    # `origin/master`, `refs/remotes/origin/master`, `refs/heads/master` 등 prefix 정리.
    if branch:
        branch = _normalize_branch_name(branch)

    if branch and commit:
        return branch, commit

    for action in actions:
        if not isinstance(action, dict):
            continue
        cls = action.get("_class") or ""
        if "ParametersAction" not in cls:
            continue
        for p in action.get("parameters") or []:
            if not isinstance(p, dict):
                continue
            name = (p.get("name") or "").upper()
            value = p.get("value")
            if not isinstance(value, str) or not value:
                continue
            if not branch and name in _GIT_PARAM_NAMES:
                branch = value

    return branch, commit


def _strip_active_choice_marker(value: str) -> str:
    """Active Choices 의 :selected, :disabled, :selected:disabled 접미어 제거."""
    for marker in (":selected:disabled", ":disabled:selected", ":selected", ":disabled"):
        if value.endswith(marker):
            return value[: -len(marker)]
    return value


class _GitParamOptionsParser(HTMLParser):
    """Git Parameter descriptor 응답의 <select><option> 목록을 추출한다.
    응답은 <select>...</select> 또는 <j:forEach> 기반 일부 플러그인 버전에서 option 만 반환."""

    def __init__(self):
        super().__init__()
        self.options = []
        self._in_option = False
        self._option_text = []
        self._option_val = None

    def handle_starttag(self, tag, attrs):
        if tag == "option":
            self._in_option = True
            self._option_text = []
            self._option_val = dict(attrs).get("value")

    def handle_data(self, data):
        if self._in_option:
            self._option_text.append(data)

    def handle_endtag(self, tag):
        if tag == "option" and self._in_option:
            text = "".join(self._option_text).strip()
            val = self._option_val if self._option_val is not None else text
            if val and val not in self.options:
                self.options.append(val)
            self._in_option = False
            self._option_text = []
            self._option_val = None


class _ParamChoicesParser(HTMLParser):
    """Jenkins 빌드 폼에서 <input name="name" value="PARAM"> 직후의
    <select name="value">...</select> 옵션을 추출한다.
    Active Choices 등 서버사이드 렌더 플러그인 대응."""

    def __init__(self):
        super().__init__()
        self.result = {}
        self._cur_name = None
        self._options = []
        self._in_select = False
        self._in_option = False
        self._option_text = []
        self._option_val = None

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "input" and a.get("name") == "name" and a.get("value"):
            self._cur_name = a["value"]
            self._options = []
            self._in_select = False
        elif tag == "select" and a.get("name") == "value" and self._cur_name:
            self._in_select = True
        elif tag == "option" and self._in_select:
            self._in_option = True
            self._option_text = []
            self._option_val = a.get("value")

    def handle_data(self, data):
        if self._in_option:
            self._option_text.append(data)

    def handle_endtag(self, tag):
        if tag == "option" and self._in_option:
            text = "".join(self._option_text).strip()
            val = self._option_val if self._option_val is not None else text
            if val:
                self._options.append(val)
            self._in_option = False
            self._option_text = []
            self._option_val = None
        elif tag == "select" and self._in_select:
            self._in_select = False
            if self._cur_name and self._options:
                self.result[self._cur_name] = list(self._options)
            self._cur_name = None
            self._options = []
