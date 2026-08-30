# hidden/ — 헤이딜러 숨은이력 스크린샷

1. `python merge.py --list` 로 상위 매물 차량번호 확인
2. 헤이딜러 앱 '숨은이력찾기'에서 각 차량번호 조회
3. 결과 화면 스크린샷을 이 폴더에 저장 (파일명에 차량번호를 넣으면 자동 매칭됨)
4. `python merge.py --show` 출력을 클로드 코드에 전달
5. 클로드가 이미지를 읽어 `extracted.json` 생성
6. `python merge.py --apply` 로 병합 및 최종 리포트 생성

`extracted.template.json` 이 입력 형식 예시입니다.
