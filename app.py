import streamlit as st
import requests
import urllib.parse
import gspread
from oauth2client.service_account import ServiceAccountCredentials
from datetime import datetime

# ==========================================
# 👇 [설정] 클라우드 & 로컬 호환 설정
# ==========================================
SHEET_NAME = "멸망전_신청자_명단"

# 1. API 키 가져오기 (우선순위: Streamlit Secrets -> 로컬 변수)
if "riot_api_key" in st.secrets:
    API_KEY = st.secrets["riot_api_key"]
else:
    # 로컬에서 테스트할 때만 여기를 수정해서 쓰세요.
    API_KEY = "RGAPI-12ee7d29-2733-4421-a122-ef12bf9539b0" 

# 2. 룰 설정 (판수 패널티 등)
MIN_GAMES = 40
PENALTY_SCORE = 20
# ==========================================

# --- [기능 1] 구글 시트 인증 (클라우드 호환) ---
def get_google_creds():
    scope = ['https://spreadsheets.google.com/feeds', 'https://www.googleapis.com/auth/drive']
    
    # 1. 서버(Streamlit Cloud)에 올렸을 때
    if "gcp_service_account" in st.secrets:
        return ServiceAccountCredentials.from_json_keyfile_dict(dict(st.secrets["gcp_service_account"]), scope)
    
    # 2. 내 컴퓨터(Local)에서 돌릴 때
    else:
        try:
            return ServiceAccountCredentials.from_json_keyfile_name("secrets.json", scope)
        except FileNotFoundError:
            return None

def save_to_google_sheet(data):
    try:
        creds = get_google_creds()
        if not creds:
            return False, "인증 파일을 찾을 수 없습니다. (secrets.json 또는 Secrets 설정 확인)"
            
        client = gspread.authorize(creds)
        sh = client.open(SHEET_NAME)
        worksheet = sh.sheet1 
        worksheet.append_row(data)
        return True, "저장 성공"
    except Exception as e:
        return False, f"구글 시트 저장 실패: {e}"

# --- [기능 2] 점수 산정 로직 ---
FIXED_SCORES = {
    "DIAMOND":     {"I": 95, "II": 90, "III": 85, "IV": 80},
    "EMERALD":     {"I": 75, "II": 70, "III": 65, "IV": 60},
    "PLATINUM":    {"I": 55, "II": 50, "III": 45, "IV": 40},
    "GOLD":        {"I": 35, "II": 30, "III": 25, "IV": 20},
    "SILVER":      {"I": 15, "II": 12, "III": 9,  "IV": 6},
    "BRONZE":      {"I": 4,  "II": 3,  "III": 2,  "IV": 1},
    "IRON":        {"I": 0,  "II": 0,  "III": 0,  "IV": 0},
    "UNRANKED":    {"": 0}
}
HIGH_TIER_BASE = {"CHALLENGER": 160, "GRANDMASTER": 140, "MASTER": 120}

def get_raw_score(tier, rank, lp):
    if tier in HIGH_TIER_BASE:
        return HIGH_TIER_BASE[tier] + int(lp / 10)
    if tier in FIXED_SCORES:
        # 랭크 정보가 없으면 IV로 간주
        rank_key = rank if rank in FIXED_SCORES[tier] else "IV"
        return FIXED_SCORES[tier][rank_key]
    return 0

def calculate_final_score(current_info, prev_tier, peak_tier, games_played):
    # 1. 현재 점수
    score_current = get_raw_score(current_info['tier'], current_info['rank'], current_info['lp'])
    # 2. 전시즌 점수 (랭크 IV 기준)
    score_prev = get_raw_score(prev_tier, "IV", 0)
    # 3. 최고 티어 점수 (랭크 IV 기준)
    score_peak = get_raw_score(peak_tier, "IV", 0)
    
    # 셋 중 가장 높은 점수 채택
    final_score = max(score_current, score_prev, score_peak)
    
    # 판수 패널티 적용
    is_penalty = False
    if games_played < MIN_GAMES and games_played > 0:
        final_score += PENALTY_SCORE
        is_penalty = True
        
    return final_score, is_penalty, score_current, score_prev, score_peak

# --- [기능 3] API 데이터 조회 (헤더 방식 적용) ---
def get_player_info(name, tag):
    # 헤더 방식으로 요청 (403 에러 최소화)
    headers = {
        "X-Riot-Token": API_KEY.strip(),
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/90.0.4430.212 Safari/537.36"
    }

    try:
        # 1. PUUID 조회
        name_enc = urllib.parse.quote(name)
        tag_enc = urllib.parse.quote(tag)
        url_acc = f"https://asia.api.riotgames.com/riot/account/v1/accounts/by-riot-id/{name_enc}/{tag_enc}"
        
        resp = requests.get(url_acc, headers=headers)
        if resp.status_code != 200: return None, f"계정 조회 실패({resp.status_code})"
        puuid = resp.json()['puuid']

        # 2. 최근 전적 ID
        url_match = f"https://asia.api.riotgames.com/lol/match/v5/matches/by-puuid/{puuid}/ids?start=0&count=1"
        match_ids = requests.get(url_match, headers=headers).json()
        if not match_ids: return None, "휴면 계정 (최근 전적 없음)"
        
        # 3. 소환사 ID 추출
        match_data = requests.get(f"https://asia.api.riotgames.com/lol/match/v5/matches/{match_ids[0]}", headers=headers).json()
        summoner_id = next((p['summonerId'] for p in match_data['info']['participants'] if p['puuid'] == puuid), None)
        
        # 4. 랭크 조회 (KR)
        url_rank = f"https://kr.api.riotgames.com/lol/league/v4/entries/by-summoner/{summoner_id}"
        resp_rank = requests.get(url_rank, headers=headers)
        
        tier_info = {"tier": "UNRANKED", "rank": "", "lp": 0, "wins": 0, "losses": 0}
        
        # 정상 응답
        if resp_rank.status_code == 200:
            for item in resp_rank.json():
                if item['queueType'] == 'RANKED_SOLO_5x5':
                    tier_info = {
                        "tier": item['tier'], "rank": item['rank'], "lp": item['leaguePoints'],
                        "wins": item['wins'], "losses": item['losses']
                    }
                    break
        # 키 등록 지연 (403)
        elif resp_rank.status_code == 403:
            return tier_info, "API_DELAY"
            
        return tier_info, None

    except Exception as e:
        return None, f"시스템 에러: {e}"

# --- [UI 화면 구성] ---
st.set_page_config(page_title="2025 롤 멸망전", page_icon="🏆")
st.title("🏆 2025 롤 멸망전 참가 신청")
st.markdown("---")

if 'result' not in st.session_state: st.session_state.result = None

# [Tab 1] 자동 조회
with st.container():
    c1, c2 = st.columns([2, 1])
    input_name = c1.text_input("닉네임", placeholder="Hide on bush")
    input_tag = c2.text_input("태그", placeholder="KR1")

    # 추가 정보 (전시즌/최고티어)
    st.info("👇 정확한 산정을 위해 아래 정보도 선택해주세요.")
    tier_options = ["UNRANKED"] + list(reversed(list(FIXED_SCORES.keys())))[:-1] + list(HIGH_TIER_BASE.keys())
    col_a, col_b = st.columns(2)
    prev_tier = col_a.selectbox("전시즌 최고 티어", tier_options, index=0)
    peak_tier = col_b.selectbox("현시즌 최고 티어 (현재 포함)", tier_options, index=0)

    if st.button("내 점수 조회", type="primary"):
        with st.spinner("조회 중..."):
            info, err = get_player_info(input_name, input_tag)
            
            # 에러 처리
            if err == "API_DELAY":
                st.warning("⚠️ 라이엇 서버 지연으로 랭크 정보를 가져오지 못했습니다. (수동 입력값이 적용됩니다)")
                # 지연 시 기본값
                info = {"tier": "UNRANKED", "rank": "", "lp": 0, "wins": 0, "losses": 0}
            elif err:
                st.error(f"오류: {err}")
                info = {"tier": "UNRANKED", "rank": "", "lp": 0, "wins": 0, "losses": 0}
            
            # 점수 계산
            games = info['wins'] + info['losses']
            final, is_pen, s_cur, s_prev, s_peak = calculate_final_score(info, prev_tier, peak_tier, games)
            
            st.session_state.result = {
                "name": input_name, "tag": input_tag, "info": info, "games": games,
                "final_score": final, "is_penalty": is_pen,
                "scores": (s_cur, s_prev, s_peak), "inputs": (prev_tier, peak_tier)
            }

# [결과 화면]
if st.session_state.result:
    res = st.session_state.result
    st.divider()
    st.subheader(f"📊 최종 확정 점수: {res['final_score']}점")
    
    with st.expander("상세 내역 확인 (클릭)", expanded=True):
        c1, c2, c3, c4 = st.columns(4)
        c1.metric("현재 티어 점수", f"{res['scores'][0]}점", f"{res['info']['tier']}")
        c2.metric("전시즌 반영", f"{res['scores'][1]}점", res['inputs'][0])
        c3.metric("최고티어 반영", f"{res['scores'][2]}점", res['inputs'][1])
        c4.metric("총 판수", f"{res['games']}판", "패널티 적용" if res['is_penalty'] else "정상")
        
    with st.form("sub"):
        discord_id = st.text_input("디스코드 ID (필수)")
        m_pos = st.selectbox("주포지션", ["TOP", "JUNGLE", "MID", "ADC", "SUP"])
        s_pos = st.selectbox("부포지션", ["TOP", "JUNGLE", "MID", "ADC", "SUP"])
        
        if st.form_submit_button("🚀 참가 신청"):
            if not discord_id:
                st.error("디스코드 ID를 입력해주세요!")
            else:
                timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                # 엑셀에 저장될 데이터
                note = f"전:{res['inputs'][0]}/최:{res['inputs'][1]}"
                save_data = [
                    discord_id, res['name'], res['tag'], 
                    f"{res['info']['tier']} {res['info']['rank']}", 
                    res['final_score'], m_pos, s_pos, res['games'], note, timestamp
                ]
                
                with st.spinner("저장 중..."):
                    success, msg = save_to_google_sheet(save_data)
                    
                if success:
                    st.success("🎉 신청 완료! (구글 시트 저장 성공)")
                    st.balloons()
                else:
                    st.error(msg)