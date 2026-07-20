# LLM Browser Automation Policy

이 문서는 chrome-devtools-mcp 기반 LLM 브라우저 테스트 러너의 system/developer context에
포함할 운영 정책이다. Quality Gate Portal은 Jenkins 실행 결과를 수집하지만, 실제 LLM
프롬프트 생성과 액션 수행은 외부 테스트 러너에서 처리한다. 러너는 아래 규칙을 우선 적용한다.

## 공통 원칙

- UID는 현재 snapshot 안에서만 신뢰한다. 페이지 이동, 팝업 열림/닫힘, 동적 렌더링 이후에는 같은 UID를 재사용하지 말고 최신 snapshot 또는 DOM query로 대상을 다시 찾는다.
- 같은 UID 클릭이 2회 이상 timeout이면 즉시 전략을 바꾼다. 반복 클릭으로 진행하지 않는다.
- 버튼/입력/체크박스는 페이지 전체에서 텍스트만으로 고르지 말고, 가능한 한 섹션 제목이나 form 영역으로 scope를 제한한다.
- click/fill 뒤에는 기대 상태를 즉시 검증한다. 체크박스는 checked, select는 선택값, 팝업은 닫힘, 본문은 선택 결과 반영, 최종 저장은 URL 또는 완료 페이지 고유 문구를 확인한다.
- toast나 alert의 "저장 완료" 문구만으로 최종 pass를 선언하지 않는다. 저장과 작성 완료는 별도 상태일 수 있다.
- 필수 동작이 검증되지 않으면 다음 업무 단계로 넘어가지 말고 복구 전략을 수행한다.

## Styled/Hidden Control 처리

체크박스/라디오가 styled input이거나 label 뒤에 숨겨져 있으면 일반 click이 timeout될 수 있다.
다음 순서로 처리한다.

1. label 텍스트로 가장 가까운 input을 찾는다.
2. input이 보이면 input 또는 label을 클릭한다.
3. 실패하거나 timeout이면 JS fallback으로 상태를 설정하고 input/change 이벤트를 dispatch한다.
4. checked 상태가 true인지 확인한다.

예시 fallback:

```js
() => {
  const labels = [...document.querySelectorAll("label")];
  const label = labels.find((node) => node.innerText.includes("고등학교 미만 졸업"));
  const input =
    label?.querySelector("input[type='checkbox']") ||
    document.getElementById(label?.getAttribute("for")) ||
    [...document.querySelectorAll("input[type='checkbox']")]
      .find((node) => node.closest("label")?.innerText.includes("고등학교 미만 졸업"));

  if (!input) return { ok: false, reason: "checkbox not found" };
  input.checked = true;
  input.dispatchEvent(new Event("input", { bubbles: true }));
  input.dispatchEvent(new Event("change", { bubbles: true }));
  return { ok: input.checked };
}
```

## 팝업/모달 선택 규칙

- "추가하기"처럼 여러 섹션에 반복되는 버튼은 섹션 제목을 기준으로 찾는다.
- 팝업이 열리면 팝업/모달/root layer 안에서만 검색한다.
- 지역, 직무처럼 tree 형태의 선택은 부모 노드 선택 후 자식 노드가 렌더링될 때까지 기다린다.
- 선택 후 닫기/확인 버튼을 누르고, 본문 필드에 선택값이 반영됐는지 검증한다.

## C17508: 이력서 등록 기능 확인 보강 컨텍스트

이 케이스에서 특히 주의할 필드와 기대 결과:

- 학력: "고등학교 미만 졸업" 체크박스는 클릭 성공이 아니라 checked 상태를 pass 조건으로 삼는다.
- 희망직무: 직무 선택 팝업과 희망근무지 팝업을 혼동하지 않는다.
- 희망근무지: "희망근무지" 섹션의 추가하기 버튼을 눌러 "서울 > 서초구"를 선택하고 닫는다. 본문에 "서울" 또는 "서초구"가 반영됐는지 확인한다.
- 제목: "JK 리그레션 테스트" 입력 후 값이 유지되는지 확인한다.
- 최종 기대결과: 이력서 작성 완료 페이지로 이동해야 한다. URL이 `/User/Resume/Write`에 남아 있으면 실패다.
- "저장 완료" 메시지는 중간 저장 신호일 수 있으므로 최종 성공 조건으로 사용하지 않는다.

## 실패 판정 기준

- 필수 checkbox/radio/select/input이 기대 상태가 아니면 실패로 보고 final result에 누락 필드를 명시한다.
- 완료 페이지 이동이 기대결과인 케이스에서 작성 URL에 남아 있으면 실패다.
- 같은 UI 대상에서 반복 timeout이 발생하면 "selector/snapshot instability"로 분류하고, 최종 메시지에 UID와 step 목록을 포함한다.
