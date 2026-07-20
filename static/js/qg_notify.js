/* QG Portal 공통 알림 유틸
 * 토스트(권한 불필요) + 삐 + 브라우저 데스크톱 알림(권한 있을 때)
 *
 * 공통 인프라 (base.html 에 배치):
 *   - #notify-toggle-btn (내부에 #notify-toggle-label) — 사이드바의 ON/OFF 토글
 *   - #qg-toast-container — 토스트가 렌더될 컨테이너
 *
 * API:
 *   - QGNotify.toggleNotify() — 버튼 onclick 핸들러
 *   - QGNotify.fireBoth(title, body, type, onclickUrl, tagScope) — 알림 발사
 *   - QGNotify.pollStatus(url, initialStatus, onChange, intervalMs) — 상태 폴링
 */
(function (global) {
    'use strict';

    function escapeHtml(s) {
        return String(s).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
    }

    function ensureToastStyle() {
        if (document.getElementById('qg-toast-keyframes')) return;
        const st = document.createElement('style');
        st.id = 'qg-toast-keyframes';
        st.textContent = '@keyframes qgToastIn { from { transform: translateX(100%); opacity: 0; } to { transform: translateX(0); opacity: 1; } }';
        document.head.appendChild(st);
    }

    function showToast(title, body, type) {
        ensureToastStyle();
        const container = document.getElementById('qg-toast-container');
        if (!container) return;
        const colors = {
            info:    { bg: '#dbeafe', border: '#2563eb', icon: 'fa-info-circle', fg: '#1e3a8a' },
            success: { bg: '#d1fae5', border: '#059669', icon: 'fa-check-circle', fg: '#064e3b' },
            error:   { bg: '#fee2e2', border: '#dc2626', icon: 'fa-exclamation-circle', fg: '#7f1d1d' },
        };
        const c = colors[type] || colors.info;
        const el = document.createElement('div');
        el.style.cssText = 'background:' + c.bg + ';border-left:4px solid ' + c.border + ';color:' + c.fg +
            ';padding:12px 14px;border-radius:6px;box-shadow:0 4px 12px rgba(0,0,0,0.15);' +
            'font-size:0.9rem;cursor:pointer;animation:qgToastIn 0.25s ease-out';
        el.innerHTML = '<div style="display:flex;align-items:flex-start;gap:8px">' +
            '<i class="fas ' + c.icon + '" style="margin-top:2px"></i>' +
            '<div style="flex:1">' +
                '<div style="font-weight:700;margin-bottom:2px">' + escapeHtml(title) + '</div>' +
                '<div style="font-size:0.85rem;opacity:0.9">' + escapeHtml(body) + '</div>' +
            '</div>' +
            '<span style="opacity:0.6;padding-left:4px">&times;</span>' +
        '</div>';
        el.onclick = () => { el.remove(); };
        container.appendChild(el);
        setTimeout(() => {
            el.style.transition = 'opacity 0.3s';
            el.style.opacity = '0';
            setTimeout(() => el.remove(), 300);
        }, 10000);
    }

    function beep() {
        try {
            const Ctx = window.AudioContext || window.webkitAudioContext;
            if (!Ctx) return;
            const ctx = new Ctx();
            const o = ctx.createOscillator();
            const g = ctx.createGain();
            o.connect(g); g.connect(ctx.destination);
            o.type = 'sine'; o.frequency.value = 880;
            g.gain.setValueAtTime(0.0001, ctx.currentTime);
            g.gain.exponentialRampToValueAtTime(0.3, ctx.currentTime + 0.01);
            g.gain.exponentialRampToValueAtTime(0.0001, ctx.currentTime + 0.4);
            o.start(); o.stop(ctx.currentTime + 0.4);
        } catch (e) { /* ignore */ }
    }

    const MUTE_KEY = 'qg_notify_muted';
    function isMuted() {
        try { return localStorage.getItem(MUTE_KEY) === '1'; } catch (e) { return false; }
    }
    function setMuted(v) {
        try {
            if (v) localStorage.setItem(MUTE_KEY, '1');
            else localStorage.removeItem(MUTE_KEY);
        } catch (e) { /* ignore */ }
    }

    function canNotify() {
        return ('Notification' in window) && Notification.permission === 'granted' && !isMuted();
    }

    function fireViaConstructor(title, body, onclickUrl, tag) {
        try {
            const n = new Notification(title, { body: body, tag: tag });
            n.onclick = () => {
                window.focus();
                if (onclickUrl) { location.href = onclickUrl; }
                n.close();
            };
            n.onerror = (e) => { console.error('[QGNotify] Notification.onerror:', e); };
            n.onshow = () => { console.log('[QGNotify] Notification.onshow fired:', title); };
            console.log('[QGNotify] OS 알림 발사 (직접):', title);
            return true;
        } catch (e) { console.error('[QGNotify] OS 알림 실패 (직접):', e); return false; }
    }

    /* SW 경로 우선 → 실패 시 new Notification() 폴백.
       macOS Sequoia / Windows 11 Chrome 에서는 SW 경로가 더 안정적으로 OS 트레이까지 전달됨. */
    async function fireBrowserNotification(title, body, onclickUrl, tagScope) {
        if (!canNotify()) {
            console.warn('[QGNotify] OS 알림 스킵 — 권한:',
                ('Notification' in window) ? Notification.permission : 'unsupported',
                'muted:', isMuted());
            return false;
        }
        const tag = (tagScope || 'qg') + '-' + Date.now();
        const viaSW = await fireViaServiceWorker(title, body, onclickUrl, tag);
        if (viaSW) return true;
        return fireViaConstructor(title, body, onclickUrl, tag);
    }

    function fireBoth(title, body, type, onclickUrl, tagScope) {
        showToast(title, body, type);
        beep();
        // fire-and-forget — 호출부가 await 할 필요 없음
        fireBrowserNotification(title, body, onclickUrl, tagScope);
    }

    function updateBanner() {
        const banner = document.getElementById('qg-notify-banner');
        const text = document.getElementById('qg-notify-banner-text');
        if (!banner) return;
        if (!('Notification' in window)) {
            banner.style.display = 'none';
            return;
        }
        const p = Notification.permission;
        if (p === 'granted') {
            banner.style.display = 'none';
        } else if (p === 'denied') {
            banner.style.display = 'block';
            banner.style.background = '#fee2e2';
            banner.style.borderColor = '#dc2626';
            banner.style.color = '#7f1d1d';
            if (text) text.textContent = 'OS 알림이 차단되어 있습니다. 주소창 왼쪽 🔒 아이콘 → 사이트 설정 → 알림 → 허용으로 변경 후 새로고침하세요.';
        } else {
            banner.style.display = 'block';
            banner.style.background = '#fef3c7';
            banner.style.borderColor = '#f59e0b';
            banner.style.color = '#78350f';
            if (text) text.textContent = 'OS 알림 권한이 필요합니다. 이 배너를 클릭해서 허용하세요.';
        }
    }

    function updateToggleBtn() {
        updateBanner();
        const btn = document.getElementById('notify-toggle-btn');
        const label = document.getElementById('notify-toggle-label');
        if (!btn || !label) return;
        if (!('Notification' in window)) {
            label.textContent = '알림 미지원';
            btn.disabled = true;
            return;
        }
        const p = Notification.permission;
        const icon = btn.querySelector('i');
        btn.classList.remove('btn-primary', 'btn-secondary');
        if (p === 'granted' && !isMuted()) {
            label.textContent = '알림 ON';
            if (icon) { icon.className = 'fas fa-bell'; }
            btn.classList.add('btn-primary');
            btn.title = '클릭하면 OS 알림을 끕니다. (페이지 내 토스트는 계속 표시)';
        } else if (p === 'denied') {
            label.textContent = '알림 차단됨';
            if (icon) { icon.className = 'fas fa-bell-slash'; }
            btn.classList.add('btn-secondary');
            btn.title = '브라우저 설정에서 이 사이트의 알림을 허용으로 바꿔야 합니다 (주소창 왼쪽 자물쇠 아이콘 → 사이트 설정 → 알림).';
        } else {
            label.textContent = '알림 OFF';
            if (icon) { icon.className = 'fas fa-bell-slash'; }
            btn.classList.add('btn-secondary');
            btn.title = (p === 'granted')
                ? '클릭하면 OS 알림을 다시 켭니다.'
                : '클릭하면 OS 알림 권한을 요청합니다. (탭이 뒤에 있어도 OS 알림 수신)';
        }
    }

    function requestPermission() {
        if (!('Notification' in window)) {
            showToast('알림 미지원', '이 브라우저는 데스크톱 알림을 지원하지 않습니다.', 'error');
            return;
        }
        if (Notification.permission === 'denied') {
            showToast('알림 차단됨', '브라우저 설정에서 이 사이트의 알림을 허용으로 바꿔주세요 (주소창 왼쪽 자물쇠 → 사이트 설정 → 알림).', 'error');
            return;
        }
        Notification.requestPermission().then((p) => {
            if (p === 'granted') setMuted(false);
            updateToggleBtn();
            if (p === 'granted') {
                fireBoth('알림 활성화 완료', '이제 작업 완료 시 데스크톱 알림으로도 알려드립니다.', 'success');
            } else {
                showToast('알림 권한 미허용', '페이지 내 토스트 알림은 계속 표시됩니다.', 'info');
            }
        }).catch(() => {});
    }

    /* 알림 버튼 토글 — 상태별로 다르게 동작.
     *  - permission=default: 권한 요청
     *  - permission=denied: 브라우저 설정 안내
     *  - permission=granted + 켜진 상태: 로컬 mute 플래그로 끔
     *  - permission=granted + 꺼진 상태: 로컬 mute 해제하여 다시 켬
     * (브라우저 권한은 JS 로 revoke 불가 → 로컬 mute 방식으로 OFF 구현)
     */
    function toggleNotify() {
        if (!('Notification' in window)) {
            showToast('알림 미지원', '이 브라우저는 데스크톱 알림을 지원하지 않습니다.', 'error');
            return;
        }
        const p = Notification.permission;
        if (p === 'denied') {
            showToast('알림 차단됨', '브라우저 설정에서 이 사이트의 알림을 허용으로 바꿔주세요 (주소창 왼쪽 자물쇠 → 사이트 설정 → 알림).', 'error');
            return;
        }
        if (p !== 'granted') {
            requestPermission();
            return;
        }
        if (isMuted()) {
            setMuted(false);
            updateToggleBtn();
            showToast('알림 ON', 'OS 알림을 다시 켰습니다.', 'success');
        } else {
            setMuted(true);
            updateToggleBtn();
            showToast('알림 OFF', 'OS 알림을 껐습니다. (페이지 내 토스트는 계속 표시)', 'info');
        }
    }

    async function ensurePermission() {
        if (!('Notification' in window)) {
            showToast('알림 미지원', '이 브라우저는 OS 알림을 지원하지 않습니다.', 'error');
            return false;
        }
        if (Notification.permission === 'granted') return true;
        if (Notification.permission === 'denied') {
            showToast('알림 차단됨', '주소창 🔒 → 사이트 설정 → 알림 → 허용으로 바꾸고 새로고침하세요.', 'error');
            return false;
        }
        // default: 권한 요청
        try {
            const p = await Notification.requestPermission();
            updateToggleBtn();
            if (p === 'granted') return true;
            showToast('알림 권한 미허용', '권한을 허용해야 OS 알림이 표시됩니다.', 'error');
            return false;
        } catch (e) {
            console.error('[QGNotify] requestPermission 실패:', e);
            return false;
        }
    }

    /* Service Worker 등록 */
    let swRegPromise = null;
    function registerSW() {
        if (!('serviceWorker' in navigator)) return Promise.resolve(null);
        if (swRegPromise) return swRegPromise;
        swRegPromise = navigator.serviceWorker.register('/sw.js', { scope: '/' })
            .then((reg) => { console.log('[QGNotify] SW 등록 성공:', reg.scope); return reg; })
            .catch((e) => { console.error('[QGNotify] SW 등록 실패:', e); return null; });
        return swRegPromise;
    }

    async function waitForSWActive(reg, timeoutMs) {
        if (reg.active) return true;
        return new Promise((resolve) => {
            const deadline = Date.now() + (timeoutMs || 2500);
            const tick = () => {
                if (reg.active) return resolve(true);
                if (Date.now() >= deadline) return resolve(false);
                setTimeout(tick, 100);
            };
            tick();
        });
    }

    async function fireViaServiceWorker(title, body, url, tag) {
        const reg = await registerSW();
        if (!reg) return false;
        const active = await waitForSWActive(reg, 2500);
        if (!active) {
            console.warn('[QGNotify] SW activate 타임아웃 — 직접 경로로 폴백');
            return false;
        }
        try {
            await reg.showNotification(title, {
                body: body,
                tag: tag || ('qg-sw-' + Date.now()),
                requireInteraction: true,
                data: { url: url || location.href },
            });
            console.log('[QGNotify] SW showNotification 성공:', title);
            return true;
        } catch (e) {
            console.error('[QGNotify] SW showNotification 실패:', e);
            return false;
        }
    }

    /* 브라우저/OS 환경 정보 수집 */
    function getEnvInfo() {
        const ua = navigator.userAgent;
        let browser = 'Unknown';
        let version = '';
        if (/Edg\//.test(ua)) { browser = 'Edge'; version = (ua.match(/Edg\/([\d.]+)/) || [])[1] || ''; }
        else if (/Chrome\//.test(ua)) { browser = 'Chrome'; version = (ua.match(/Chrome\/([\d.]+)/) || [])[1] || ''; }
        else if (/Firefox\//.test(ua)) { browser = 'Firefox'; version = (ua.match(/Firefox\/([\d.]+)/) || [])[1] || ''; }
        else if (/Safari\//.test(ua)) { browser = 'Safari'; version = (ua.match(/Version\/([\d.]+)/) || [])[1] || ''; }
        let os = 'Unknown';
        if (/Windows NT 10/.test(ua)) os = 'Windows 10/11';
        else if (/Windows/.test(ua)) os = 'Windows';
        else if (/Mac OS X/.test(ua)) os = 'macOS';
        else if (/Linux/.test(ua)) os = 'Linux';
        return { browser, version, os, ua };
    }

    /* 고정 버튼용 OS 알림 종합 진단 */
    async function diagnoseOS() {
        const box = document.getElementById('qg-diag-result');
        const lines = [];
        function log(msg) { lines.push(msg); if (box) { box.textContent = lines.join('\n'); box.style.display = 'block'; box.scrollTop = box.scrollHeight; } }

        const env = getEnvInfo();
        log('== OS 알림 종합 진단 ==');
        log('브라우저: ' + env.browser + ' ' + env.version);
        log('OS: ' + env.os);
        log('Notification API: ' + (('Notification' in window) ? '지원 ✓' : '미지원 ✗'));
        log('Service Worker API: ' + (('serviceWorker' in navigator) ? '지원 ✓' : '미지원 ✗'));
        if (!('Notification' in window)) return;
        log('현재 권한: ' + Notification.permission);
        log('프로토콜: ' + location.protocol + ' / host: ' + location.host);
        log('visibility: ' + document.visibilityState + (document.hasFocus() ? ' (포커스됨)' : ' (포커스 없음)'));
        log('');

        const ok = await ensurePermission();
        if (!ok) { log('권한 미확보 — 중단.'); return; }

        /* === 경로 A: new Notification() === */
        log('── [경로 A] new Notification() ──');
        let aShown = false, aErrored = false;
        try {
            const n = new Notification('[A] 직접 Notification', {
                body: '직접 호출 방식입니다. (우측 하단 또는 알림센터 확인)',
                tag: 'qg-diag-A-' + Date.now(),
            });
            n.onshow = () => { aShown = true; log('  onshow ✓'); };
            n.onerror = () => { aErrored = true; log('  onerror ✗'); };
            n.onclick = () => { window.focus(); n.close(); };
            log('  생성 ✓');
        } catch (e) {
            log('  예외: ' + e.message);
            aErrored = true;
        }
        await new Promise(r => setTimeout(r, 1500));
        if (!aShown && !aErrored) log('  ⚠ onshow 이벤트 없음 — OS 전달 실패 의심');

        log('');

        /* === 경로 B: Service Worker showNotification() === */
        log('── [경로 B] Service Worker ──');
        if (!('serviceWorker' in navigator)) {
            log('  SW 미지원 — 스킵');
        } else {
            try {
                const reg = await navigator.serviceWorker.register('/sw.js', { scope: '/' });
                log('  SW 등록 ✓ (scope: ' + reg.scope + ')');
                // activate 대기
                if (!reg.active) {
                    log('  SW activate 대기...');
                    await new Promise((resolve) => {
                        if (reg.active) return resolve();
                        const checkReg = () => {
                            if (reg.active) { resolve(); }
                            else { setTimeout(checkReg, 200); }
                        };
                        checkReg();
                    });
                }
                log('  SW active ✓');
                await reg.showNotification('[B] Service Worker 알림', {
                    body: 'SW 경로 테스트입니다. 이 알림이 뜨면 Run 완료 알림도 동일 경로로 받을 수 있습니다.',
                    tag: 'qg-diag-B-' + Date.now(),
                    requireInteraction: true,
                    data: { url: location.href },
                });
                log('  showNotification() 호출 ✓');
                // 실제로 뜬 지 확인
                await new Promise(r => setTimeout(r, 800));
                const shown = await reg.getNotifications();
                log('  현재 active 알림 수: ' + shown.length);
            } catch (e) {
                log('  SW 경로 실패: ' + e.message);
            }
        }

        log('');
        log('── 결과 해석 ──');
        log('• [A] 또는 [B] 알림 중 하나라도 OS 트레이에 보였다면: 그 방식으로 사용 가능.');
        log('• 둘 다 안 보이는데 onshow 가 떴다면:');
        log('  1) Windows 작업표시줄 우측 날짜/시간 클릭 → 알림센터에 쌓여있는지 확인');
        log('  2) 쌓여있음 = 배너 표시만 꺼진 상태. 설정→시스템→알림→Chrome→"알림 배너 표시" ON');
        log('  3) 안 쌓여있음 = Chrome 앱 자체 차단. 설정→시스템→알림 목록에 Chrome 이 있는지 확인');
        log('  4) chrome://flags/#enable-system-notifications 를 Enabled 로 바꾸고 브라우저 완전 종료 후 재시작');
    }

    async function testNotify(prefix) {
        const ok = await ensurePermission();
        const title = (prefix || '') + '테스트 알림';
        const body = ok ? 'OS 알림이 정상 작동합니다. (다른 창으로 전환해도 유지됨)' : 'OS 알림 권한이 없어 페이지 내 토스트만 표시됩니다.';
        showToast(title, body, ok ? 'success' : 'info');
        beep();
        if (ok) {
            const fired = fireBrowserNotification(title, 'OS 알림이 성공적으로 발사되었습니다.', null, 'qg-test');
            if (!fired) {
                showToast('OS 알림 실패', '권한은 허용이지만 알림이 발사되지 않았습니다. OS/Chrome 알림 설정을 확인하세요 (Windows: 설정 → 시스템 → 알림 → Chrome).', 'error');
            }
        }
    }

    // 페이지 로드 시 배너 상태 갱신 (권한 default → 배너 표시, granted → 배너 숨김)
    // NOTE: Chrome 은 user gesture 없는 requestPermission() 을 무시하므로 자동 요청은 불가능.
    //       대신 배너를 크게 띄워서 사용자가 클릭으로 권한 요청하도록 유도.
    function initOnLoad() {
        updateBanner();
        updateToggleBtn();
        // 권한이 이미 granted 라면 SW 를 미리 등록/활성화해둔다 — 첫 알림에서 지연 없이 뜨도록.
        if (('Notification' in window) && Notification.permission === 'granted') {
            registerSW();
        }
        // 이전 페이지에서 등록된 실행 중 Run 들을 계속 추적
        startAllWatchers();
    }

    /**
     * statusUrl 을 intervalMs 주기로 GET해서 응답 HTML 내
     * data-run-status="..." 값을 추출하고, 이전과 달라지면 onChange(newStatus, prevStatus) 호출.
     * onChange 가 true 를 반환하면 폴링 중단.
     * @returns {Function} stop()
     */
    function pollStatus(statusUrl, initialStatus, onChange, intervalMs) {
        let prev = initialStatus;
        let timer = null;
        async function tick() {
            try {
                const r = await fetch(statusUrl, {credentials: 'same-origin', headers: {'X-Requested-With': 'XMLHttpRequest'}});
                if (!r.ok) return;
                const html = await r.text();
                const m = html.match(/data-run-status="([^"]+)"/);
                if (!m) return;
                const newStatus = m[1];
                if (newStatus !== prev) {
                    const done = onChange(newStatus, prev);
                    prev = newStatus;
                    if (done && timer) { clearInterval(timer); timer = null; }
                }
            } catch (e) { /* transient */ }
        }
        timer = setInterval(tick, intervalMs || 5000);
        return function stop() { if (timer) { clearInterval(timer); timer = null; } };
    }

    /* ==================================================================
     * 글로벌 Watcher — 페이지 이동/새로고침해도 실행 중인 Run 을 계속 추적
     * 하여 터미널 상태 도달 시 OS/토스트 알림을 발사.
     *
     * 동작: QGNotify.watch(key, {...}) 호출 시 localStorage 레지스트리에
     * 등록. 모든 페이지에서 base.html 을 통해 qg_notify.js 가 로드되면
     * initOnLoad() 가 레지스트리를 읽어 각 watcher 에 대해 독립 폴링을
     * 시작. 터미널 상태 도달하면 알림 발사 후 자동 등록 해제.
     *
     * 다중 탭: notifiedKeys 를 localStorage 에 저장하고, 알림 발사 직전
     * 재조회하여 중복 발사 가능성을 최소화. OS 레벨에서는 stable tag
     * (qg-watch-<key>-<notifKey>) 로 dedupe.
     * ================================================================== */
    const WATCH_KEY = 'qg_watchers_v1';
    const activePollers = new Map(); // key -> stop fn

    function loadWatchers() {
        try { return JSON.parse(localStorage.getItem(WATCH_KEY) || '{}') || {}; }
        catch (e) { return {}; }
    }
    function saveWatchers(map) {
        try { localStorage.setItem(WATCH_KEY, JSON.stringify(map)); } catch (e) {}
    }

    const QG_RUN_NOTIFY_MAP = {
        'waiting_deploy':      { title: '이미지 빌드 완료', body: '검수대상 환경 배포 대기 중 — 배포 실행이 필요합니다.', type: 'info' },
        'image_build_failed':  { title: '이미지 빌드 실패', body: '파이프라인이 중단되었습니다.', type: 'error' },
        'deploy_failed':       { title: '검수대상 환경 배포 실패', body: '파이프라인이 중단되었습니다.', type: 'error' },
        'qg_passed':           { title: '검수 통과!', body: '검수통과 환경 배포 대기 중 — 배포 실행이 필요합니다.', type: 'success' },
        'failed':              { title: '테스트 실패', body: '테스트만 재실행하거나 처음부터 다시 실행할 수 있습니다.', type: 'error' },
        'prod_deploy_failed':  { title: '검수통과 환경 배포 실패', body: '파이프라인이 중단되었습니다.', type: 'error' },
        'completed':           { title: '검수 완료', body: '모든 단계가 통과했습니다.', type: 'success' },
    };
    const QG_RUN_TERMINAL = new Set(['completed', 'failed', 'image_build_failed', 'deploy_failed', 'prod_deploy_failed', 'cancelled']);

    const SUITE_RUN_TERMINAL = new Set(['completed', 'failed', 'cancelled']);

    function qgRunHandle(w, newStatus, prevStatus) {
        const prefix = w.label ? '[' + w.label + '] ' : '';
        const notifs = [];
        if (prevStatus === 'deploying' && (newStatus === 'waiting_test' || newStatus === 'testing')) {
            notifs.push({ nkey: 'deploy_done_start_test', title: prefix + '검수대상 환경 배포 완료', body: '테스트를 시작합니다.', type: 'info' });
        }
        const entry = QG_RUN_NOTIFY_MAP[newStatus];
        if (entry) {
            notifs.push({ nkey: newStatus, title: prefix + entry.title, body: entry.body, type: entry.type });
        }
        return { notifs: notifs, terminal: QG_RUN_TERMINAL.has(newStatus) };
    }

    function suiteRunHandle(w, newStatus, prevStatus) {
        const prefix = w.label ? '[' + w.label + '] ' : '';
        const notifs = [];
        if (newStatus === 'completed') {
            notifs.push({ nkey: 'completed', title: prefix + 'Suite 완료', body: 'Jobs 실행이 모두 끝났습니다.', type: 'success' });
        }
        return { notifs: notifs, terminal: SUITE_RUN_TERMINAL.has(newStatus) };
    }

    const WATCH_HANDLERS = { qg_run: qgRunHandle, suite_run: suiteRunHandle };

    function startWatcherPoller(key) {
        if (activePollers.has(key)) return;
        const snapshot = loadWatchers()[key];
        if (!snapshot) return;
        const handler = WATCH_HANDLERS[snapshot.kind];
        if (!handler) return;

        const stop = pollStatus(snapshot.statusUrl, snapshot.prevStatus || '', (newStatus, prevStatus) => {
            // 항상 최신 상태를 localStorage 에서 재조회 (다른 탭이 업데이트했을 수 있음)
            const all = loadWatchers();
            const cur = all[key];
            if (!cur) return true; // 외부에서 unwatch 됨

            const result = handler(cur, newStatus, prevStatus);
            cur.notifiedKeys = cur.notifiedKeys || [];
            for (const n of result.notifs) {
                if (cur.notifiedKeys.indexOf(n.nkey) !== -1) continue;
                // stable tag 로 OS 레벨 dedupe
                const tag = 'qg-watch-' + key + '-' + n.nkey;
                fireBrowserNotification(n.title, n.body, cur.runUrl, tag);
                showToast(n.title, n.body, n.type);
                beep();
                cur.notifiedKeys.push(n.nkey);
            }
            cur.prevStatus = newStatus;
            all[key] = cur;
            saveWatchers(all);

            if (result.terminal) {
                const a = loadWatchers();
                delete a[key];
                saveWatchers(a);
                activePollers.delete(key);
                return true;
            }
            return false;
        }, 5000);

        activePollers.set(key, stop);
    }

    /**
     * 실행 중인 Run 을 글로벌 watcher 에 등록. 페이지 이동해도 추적 유지.
     * @param {string} key   고유 키 (예: 'qg-run-42')
     * @param {object} opts  { kind: 'qg_run'|'suite_run', statusUrl, runUrl, label, initialStatus }
     */
    function watch(key, opts) {
        if (!key || !opts || !opts.kind || !opts.statusUrl) return;
        // 이미 터미널이면 등록 불필요 — 즉시 unwatch 처리
        const handler = WATCH_HANDLERS[opts.kind];
        if (!handler) return;
        const terminalSet = (opts.kind === 'qg_run') ? QG_RUN_TERMINAL : SUITE_RUN_TERMINAL;
        if (opts.initialStatus && terminalSet.has(opts.initialStatus)) {
            unwatch(key);
            return;
        }

        const all = loadWatchers();
        const existing = all[key] || {};
        all[key] = {
            kind: opts.kind,
            statusUrl: opts.statusUrl,
            runUrl: opts.runUrl || null,
            label: opts.label || '',
            prevStatus: existing.prevStatus || opts.initialStatus || '',
            notifiedKeys: existing.notifiedKeys || [],
            startedAt: existing.startedAt || Date.now(),
        };
        saveWatchers(all);
        startWatcherPoller(key);
    }

    function unwatch(key) {
        const all = loadWatchers();
        if (all[key]) { delete all[key]; saveWatchers(all); }
        const stop = activePollers.get(key);
        if (stop) { stop(); activePollers.delete(key); }
    }

    function startAllWatchers() {
        const all = loadWatchers();
        // 24 시간 이상 묵은 항목은 stale 로 정리 (Jenkins/서버 문제로 터미널 도달 못한 경우 방지)
        const now = Date.now();
        let changed = false;
        for (const key of Object.keys(all)) {
            if (all[key].startedAt && (now - all[key].startedAt) > 24 * 60 * 60 * 1000) {
                delete all[key];
                changed = true;
            }
        }
        if (changed) saveWatchers(all);

        for (const key of Object.keys(loadWatchers())) {
            startWatcherPoller(key);
        }
    }

    // 다른 탭에서 unwatch 발생 시 이 탭의 폴러도 정리
    window.addEventListener('storage', (e) => {
        if (e.key !== WATCH_KEY) return;
        const all = loadWatchers();
        for (const key of Array.from(activePollers.keys())) {
            if (!all[key]) {
                const stop = activePollers.get(key);
                if (stop) stop();
                activePollers.delete(key);
            }
        }
        // 새 watcher 가 추가됐다면 폴링 시작
        for (const key of Object.keys(all)) {
            if (!activePollers.has(key)) startWatcherPoller(key);
        }
    });

    global.QGNotify = {
        showToast: showToast,
        beep: beep,
        fireBoth: fireBoth,
        fireBrowserNotification: fireBrowserNotification,
        fireViaServiceWorker: fireViaServiceWorker,
        registerSW: registerSW,
        updateToggleBtn: updateToggleBtn,
        requestPermission: requestPermission,
        toggleNotify: toggleNotify,
        isMuted: isMuted,
        ensurePermission: ensurePermission,
        testNotify: testNotify,
        diagnoseOS: diagnoseOS,
        pollStatus: pollStatus,
        canNotify: canNotify,
        watch: watch,
        unwatch: unwatch,
    };

    // 페이지 로드 시 배너/버튼 상태 갱신
    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', initOnLoad);
    } else {
        initOnLoad();
    }
})(window);
