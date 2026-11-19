# 개발

코드, `spec/`, 인수 테스트는 검증 결과에 맞춰 필요한 순서로 수정할 수 있습니다. 구현보다 먼저 spec을 커밋할 필요는 없습니다.

## 제출 전 검증

커밋 또는 푸시 전에 다음 명령을 실행합니다.

```bash
uv run python tools/harness.py verify
```

`verify`는 MRL 오류 코드와 표준 라이브러리 함수, factory profile, `spec/system` 장 구조, 7장과 `tests/acceptance/`의 양방향 매핑을 검사합니다. 이어서 연결된 인수 테스트, MRL conformance, MRL coverage, 코드 정책 테스트를 실행합니다. 연결된 인수 테스트에는 실패, 오류, skip이 없어야 합니다.

## 편집 흐름

코드, spec, 테스트가 일치하지 않으면 같은 변경에서 수정하고 `verify`를 다시 실행합니다. 현재 저장소에는 작업 할당이나 시작, stage gate, 작업 상태, 보호 파일 lock, review artifact, PreToolUse hook, Git hook이 필요하지 않습니다. `plan/tasks/`, `plan/journal/`, 마일스톤 자료는 과거 기록으로 남아 있지만 현재 구현 진입점이 아니며 `verify`도 읽지 않습니다.

## 기술 규칙

의존성을 추가하지 않고, control plane에서 LLM API를 호출하지 않으며, `shell=True`를 사용하지 않고, 검증을 위해 테스트를 약화하지 않습니다. 자격 증명을 로그, 커밋, 답변에 남기지 않습니다. 무관한 변경은 보존하고 커밋과 푸시는 명시적으로 요청받은 경우에만 합니다.
