# bio-news-bot

해외 바이오 뉴스를 2시간마다 검색해 텔레그램으로 링크를 보내주는 봇입니다.
GitHub Actions로 돌아가므로 별도 서버가 필요 없습니다.

## 동작 방식

- **대상 기업**: 글로벌 빅파마·대형 바이오텍 100곳 + 국내 제약·바이오 200곳 (`companies.yaml`)
- **수집 경로**
  1. 구글 뉴스 영문판 검색: 기업명을 묶어 최근 24시간 기사 검색
  2. 해외 바이오 전문지 RSS: Fierce Biotech, Fierce Pharma, Endpoints News, BioPharma Dive, STAT, GEN (`sources.yaml`)
- **필터**: 기사 제목(전문지는 요약 앞부분 포함)에 대상 기업명이 있어야 알림.
  Bayer(축구팀), Lilly(인명)처럼 오탐이 잦은 이름은 바이오 관련 단어가 함께 있을 때만 알림
- **중복 제거**: 보낸 기사는 `sent.json`에 기록해 다시 보내지 않음 (14일 보관)
- **알림 형식**: 🇰🇷 국내 기업 해외 보도 / 🌍 글로벌 제약·바이오로 나눠 제목·링크·매체·시각(KST)
- **실행 주기**: 2시간마다 (한국시간 01:07, 03:07 … 23:07). GitHub 사정으로 수십 분 늦어질 수 있음

## 처음 설정 (한 번만)

### 1. 텔레그램 봇 만들기
1. 텔레그램에서 `@BotFather` 검색 → `/newbot` 입력 → 봇 이름과 아이디(끝이 `bot`) 정하기
2. 받은 **토큰**(`123456789:ABC...` 형태)을 복사

### 2. 받을 채팅 ID 확인
- **개인 채팅으로 받기**: 방금 만든 봇에게 아무 메시지나 보낸 뒤, 브라우저에서
  `https://api.telegram.org/bot<토큰>/getUpdates` 를 열면 `"chat":{"id": 숫자` 부분이 채팅 ID
- **채널로 받기**: 채널에 봇을 관리자로 추가하고, 채널 ID(`-100`으로 시작) 또는 `@채널아이디` 사용
- 여러 곳에 보내려면 쉼표로 구분: `12345678,-1001234567890`

### 3. GitHub Secrets 등록
저장소 → **Settings** → **Secrets and variables** → **Actions** → **New repository secret**

| 이름 | 값 |
|---|---|
| `TELEGRAM_BOT_TOKEN` | 1번의 토큰 |
| `TELEGRAM_CHAT_ID` | 2번의 채팅 ID |

### 4. 첫 실행
저장소 → **Actions** 탭 → 왼쪽 **Bio News Alert** → **Run workflow**.
이후에는 2시간마다 자동으로 실행됩니다.

## 자주 바꾸는 설정

| 하고 싶은 것 | 고칠 곳 |
|---|---|
| 기업 추가·삭제 | `companies.yaml` |
| 전문지 RSS 추가 | `sources.yaml`의 `feeds` |
| 실행 간격 변경 | `.github/workflows/news.yml`의 `cron` |
| 새 기사 없을 때도 알림 받기 | `sources.yaml`의 `send_when_empty: true` |
| 오탐 단어 기준 조정 | `sources.yaml`의 `context_words` |

## 로컬에서 시험하기

```bash
pip install -r requirements.txt
python main.py --dry-run   # 텔레그램으로 보내지 않고 화면에 출력
```

## 참고
- 구글 뉴스 링크는 `news.google.com` 경유 주소라 누르면 원문으로 이동합니다.
  같은 기사가 전문지 RSS에도 있으면 전문지 원문 링크를 우선 사용합니다.
- 텔레그램 전송이 실패하면 기록을 남기지 않아 다음 실행 때 다시 보냅니다.
