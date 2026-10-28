/* ============================================================
   Quality Highway / Live Race Control
   Test Suite run을 탑다운 레이싱 머신으로 표현하는 Canvas 엔진.
   PASS=완주, FAIL=사고/리커버리, RUNNING=라이브, SKIP=피트 이탈이라는 도메인 의미와
   기존 차량 물리·6초 증분 폴링은 유지한다. DOM HUD, 키보드/포인터 링크,
   DPR 캔버스, reduced-motion, ResizeObserver를 더한 몰입형 command-center 버전.
   ============================================================ */
(function () {
  "use strict";

  var NS = (window.QGHighway = {});

  // ---- 팔레트 ----------------------------------------------------------
  function palette() {
    var dark = document.documentElement.getAttribute("data-theme") === "dark";
    if (dark) {
      return {
        dark: true,
        groundTop: "#06101d", groundBottom: "#02060b",
        horizon: "rgba(44,178,238,0.19)",
        asphalt1: "#101923", asphalt2: "#080e16",
        shoulder: "#17232f", curb: "#283744", lane: "rgba(222,242,250,0.74)",
        cyan: "#35d8ff", magenta: "#b26cff", grid: "rgba(81,171,213,0.10)",
        edgeTop: "#35d8ff", edgeBot: "#687cff",
        carPass: "#35e6a1", carFail: "#ff607d", carRun: "#4da1ff", carSkip: "#ffbd4a", carCancel: "#8997aa",
        text: "#eef8ff", textDim: "rgba(218,235,246,0.56)", chipBg: "rgba(3,10,18,0.88)",
        scan: 0.025, vignette: 0.56,
      };
    }
    return {
      dark: false,
      groundTop: "#16283a", groundBottom: "#07111d",
      horizon: "rgba(60,185,235,0.20)",
      asphalt1: "#263541", asphalt2: "#17232e",
      shoulder: "#344755", curb: "#607481", lane: "rgba(239,249,251,0.86)",
      cyan: "#00b7e8", magenta: "#775af2", grid: "rgba(91,182,216,0.12)",
      edgeTop: "#21ccef", edgeBot: "#6c7cff",
      carPass: "#00bd7e", carFail: "#ef405f", carRun: "#1687ff", carSkip: "#e99a16", carCancel: "#7c8b9b",
      text: "#f4fbff", textDim: "rgba(224,239,247,0.66)", chipBg: "rgba(5,14,23,0.86)",
      scan: 0.014, vignette: 0.34,
    };
  }
  function P() { return palette(); }

  function statusColor(p, status) {
    return status === "pass" ? p.carPass
      : status === "fail" ? p.carFail
      : status === "running" ? p.carRun
      : status === "skip" ? p.carSkip
      : p.carCancel;
  }

  function runHref(rec) {
    if (rec && rec.url) return rec.url;
    if (!rec || !rec.id) return "";
    if (runUrlTemplate) {
      return runUrlTemplate.replace("00000000-0000-0000-0000-000000000000", encodeURIComponent(rec.id));
    }
    return rec.kind === "suite" ? "/suites/runs/" + encodeURIComponent(rec.id) + "/" : "";
  }

  function isRecordOnRoad(id) {
    for (var i = 0; i < cars.length; i++) {
      if (cars[i].id === id && !cars[i]._dead) return true;
    }
    return false;
  }

  // ---- 상태 ------------------------------------------------------------
  var canvas, ctx, tip, empty, stage;
  var W = 0, H = 0, DPR = 1;
  var cars = [], smoke = [], skids = [], pool = [], poolIdx = 0, uidSeq = 0;
  var seenIds = {};            // 풀에 이미 들어간 run id
  var pollCursor = 0;          // 마지막 성공 응답의 query-start server epoch
  var spawnTimer = 0, nextSpawn = 0.6;
  var mouseX = -1, mouseY = -1, hovered = null, keyboardCar = null;
  var dataUrl = null, dataPrime = null, runUrlTemplate = null;
  var background = null, backgroundReady = false;
  var running = true, userPaused = false, lastFrame = 0, dashOffset = 0, sceneTime = 0, signScroll = 0;
  var reducedMotion = false, resizeObserver = null, resizeRaf = 0, frameRaf = 0;
  var syncInFlight = false;
  var pauseBtn, linkStatus, linkLabel, lastSyncEl, motionModeEl, announcer, feedEl;
  var statEls = {}, networkOk = false, lastSyncAt = 0, lastPaint = 0, hasLoaded = false;
  var POLL_REPLAY_SECONDS = 10;

  // ---- 지오메트리 ------------------------------------------------------
  var LANES = 4;
  function roadTop() { return H * 0.275; }
  function roadH() { return H * 0.505; }
  function laneH() { return roadH() / LANES; }
  function laneY(i) { return roadTop() + laneH() * (i + 0.5); }
  // 사고 차가 정착할 '도로 한참 밖' y (위/아래 off-road 밴드 깊숙이, 캔버스 안으로 클램프)
  function offRoadY(c) {
    return c.crashTop ? Math.max(10, roadTop() - 44) : Math.min(H - 12, roadTop() + roadH() + 50);
  }
  // 렉카가 다니는 더 바깥 행 — 사고차 위를 밟고 지나가지 않도록 y 를 분리
  function towLaneY(c) {
    return c.crashTop ? Math.max(8, offRoadY(c) - 36) : Math.min(H - 10, offRoadY(c) + 36);
  }

  // ---- 차량 ------------------------------------------------------------
  function spawnCar(rec) {
    if (!rec || isRecordOnRoad(rec.id)) return false;
    var lane = (Math.random() * LANES) | 0;
    var len = W < 560 ? 43 : 50, wid = W < 560 ? 22 : 25;
    var fast = Math.random() < 0.26;                  // 일부는 과속 차량 → 추월/차선변경 유발
    var car = {
      id: rec.id, status: rec.status, label: rec.label, detail: rec.detail, href: runHref(rec),
      kind: rec.kind, ts: rec.ts, fresh: !!(rec.fresh || rec._fresh), fast: fast,
      x: -60 - Math.random() * 70, lane: lane, y: laneY(lane),
      len: len, wid: wid,
      speed: fast ? (150 + Math.random() * 75) : (56 + Math.random() * 50),  // px/s
      state: "drive", angle: 0, t: 0, life: 0, changeCool: Math.random() * 1.5,
      honk: 0, honkCool: 0, boost: 0, huh: 0,
      crashAt: W * (0.46 + Math.random() * 0.26),
      pitAt: W * (0.58 + Math.random() * 0.18),
      skidDone: false, wreckLife: 0, flash: rec.fresh ? 1.4 : 0,
    };
    if (car.status === "fail") car.speed *= 1.1;      // 사고 차는 좀 빠르게 진입
    if (car.status === "skip") car.speed *= 0.82;     // Skip은 피트 진입을 위해 차분하게 주행
    if (car.status === "cancelled") car.speed *= 0.7;
    car.baseSpeed = car.speed;                        // 선행차가 없을 때 복귀할 목표 속도
    car.lead = null; car.gap = Infinity;
    car.uid = ++uidSeq;                               // 인스턴스 고유 id(풀 재활용으로 id 중복 가능 → 물리 tiebreak 용)
    cars.push(car);
    return true;
  }

  // 차선별로 각 차의 '바로 앞 차'와 간격(gap)을 계산 — 차간 충돌/추종 판정용.
  function planTraffic() {
    var byLane = {};
    for (var i = 0; i < cars.length; i++) {
      var c = cars[i]; c.lead = null; c.gap = Infinity;
      (byLane[c.lane] = byLane[c.lane] || []).push(c);
    }
    for (var ln in byLane) {
      var arr = byLane[ln];
      arr.sort(function (a, b) { return a.x - b.x; });
      for (var j = 0; j < arr.length; j++) {
        var c2 = arr[j];
        for (var k = j + 1; k < arr.length; k++) {
          var a = arr[k];
          if (Math.abs(a.y - c2.y) > laneH() * 0.5) continue;  // 다른 차선/갓길 차는 장애물 아님
          c2.lead = a;
          c2.gap = a.x - c2.x - (a.len / 2 + c2.len / 2);
          break;
        }
      }
    }
  }

  // off-road 물리: 정차/회전 중 사고차들끼리 겹치지 않게 분리 + 렉카가 다른 사고차를 피해 우회
  function offRoadPhysics(dt) {
    var ws = [], tks = [];
    for (var i = 0; i < cars.length; i++) {
      var c = cars[i];
      if (c.state === "wreck" || (c.state === "crash" && c.t >= c.swerveDur)) ws.push(c);
      if (c.state === "tow" && c.truck) tks.push(c);
    }
    // ① 사고차–사고차 분리 — 렉카 길이만큼 충분히 벌려 트럭끼리도 안 겹치게
    for (var a = 0; a < ws.length; a++) {
      for (var b = a + 1; b < ws.length; b++) {
        var p = ws[a], q = ws[b];
        if (p.crashTop !== q.crashTop) continue;
        var dx = p.x - q.x, minX = (p.len + q.len) / 2 + 22;
        if (Math.abs(dx) < minX && Math.abs(p.y - q.y) < (p.wid + q.wid) / 2 + 4) {
          var ov = (minX - Math.abs(dx)) / 2;
          var dir = Math.abs(dx) < 0.5 ? (p.uid > q.uid ? 1 : -1) : (dx > 0 ? 1 : -1);
          p.x += dir * ov; q.x -= dir * ov;
        }
      }
    }
    // ② 렉카 행(row) 배정 — 근처 트럭끼리 서로 다른 행을 쓰게 해 꼬임/겹침 방지.
    //    row 0 = 갓길(towLaneY), 자리 없으면 1,2.. 로 도로 안쪽으로 넘쳐 운반(도로를 타서라도).
    for (a = 0; a < tks.length; a++) {
      var ta = tks[a], row = 0;
      for (b = 0; b < tks.length; b++) {
        if (b === a) continue;
        var tb = tks[b];
        if (tb.crashTop === ta.crashTop && tb.uid < ta.uid &&
            Math.abs(ta.truck.x - tb.truck.x) < 80) row++;
      }
      ta._row = row;
    }
    // ③ 견인 중 사고차를 트럭 뒤에 재동기화(분리 보정 반영)
    for (a = 0; a < tks.length; a++) {
      if (tks[a].towPhase === "haul") tks[a].x = tks[a].truck.x - tks[a].hookGap;
    }
  }

  // 목표 차선이 비었는지(앞뒤 여유) — 추월 차선 변경 가능 판정
  function laneClear(c, lane) {
    for (var i = 0; i < cars.length; i++) {
      var o = cars[i];
      if (o === c || o.lane !== lane) continue;
      var d = o.x - c.x;
      if (d > -c.len * 1.7 && d < c.len * 2.8) return false;
    }
    return true;
  }
  // 느린 앞차에 막히면 인접 차선으로 변경(추월) 시도
  function tryLaneChange(c) {
    var cands = [];
    if (c.lane > 0) cands.push(c.lane - 1);
    if (c.lane < LANES - 1) cands.push(c.lane + 1);
    if (Math.random() < 0.5) cands.reverse();
    for (var i = 0; i < cands.length; i++) {
      if (laneClear(c, cands[i])) { c.lane = cands[i]; c.changeCool = 2.2 + Math.random(); return true; }
    }
    return false;
  }

  function startCrash(c, rearEnd) {
    c.state = "crash"; c.t = 0;
    c.crashTop = c.lane < LANES / 2;        // 가까운 쪽 갓길로(도로 밖으로) 빠짐
    c.spinDir = Math.random() < 0.5 ? 1 : -1;
    c.speed = Math.max(c.speed, 145);       // 충분히 길게 미끄러지도록 최소 속도
    c.lastSkid = c.x - 100;
    c.swerveBaseY = c.y;                    // 우왕좌왕 기준 y(차선 중심)
    c.swerveDur = 0.6 + Math.random() * 0.3; // 돌기 전 좌우로 흔드는 시간
    c.rearEnd = !!rearEnd;
  }

  function updateCar(c, dt) {
    c.life += dt;
    if (c.flash > 0) c.flash = Math.max(0, c.flash - dt);
    if (c.honk > 0) c.honk -= dt;
    if (c.honkCool > 0) c.honkCool -= dt;
    if (c.huh > 0) c.huh -= dt;

    if (c.state === "drive") {
      c.changeCool -= dt;
      if (c.boost > 0) c.boost -= dt;
      var desired = c.baseSpeed * (c.boost > 0 ? 1.6 : 1); // 경적 받으면 잠깐 빨리, 끝나면 원속도 복귀
      var step = 70 * dt;
      // ── 차간 물리: 추종 / 추월 / 경적 ──
      if (c.lead) {
        var lead = c.lead;
        var leadSpd = (lead.state === "crash" || lead.state === "wreck" || lead.state === "tow")
          ? Math.max(0, lead.speed) : lead.speed;
        // 바짝 붙으면(정체) 무조건 경적, 또는 빠른데 느린 앞차에 막혀도 경적 → 앞차가 비켜주고 "??"
        var blocked = c.gap < 16 || (c.gap < 70 && leadSpd < c.baseSpeed - 25);
        if (blocked && (c.honkCool || 0) <= 0) {
          c.honk = 1.5; c.honkCool = 0.6 + Math.random() * 0.5;
          if (lead.state === "drive" && (lead.changeCool || 0) <= 0 && tryLaneChange(lead)) {
            lead.huh = 1.3;                  // 비켜주며 "??"
          } else {
            lead.boost = Math.max(lead.boost || 0, 1.0); // 못 비키면 잠깐 가속
          }
        }
        // 그래도 안 비켜주고 바짝 붙으면 본인이 직접 추월
        if (c.gap < 32 && leadSpd < c.speed - 10 && c.changeCool <= 0) {
          tryLaneChange(c);
        }
        if (c.gap < 6) {
          // 겹침 방지: 앞차 속도로 감속(추돌 사고 없음 — 사고는 FAIL 차만).
          c.speed = Math.max(0, leadSpd);
          if (c.gap < 0) c.x = lead.x - (lead.len / 2 + c.len / 2) - 0.5; // 하드 클램프
        } else if (c.gap < 46) {
          if (c.speed > leadSpd) c.speed = Math.max(leadSpd, c.speed - 160 * dt); // 안전거리 감속
        } else {
          c.speed += (c.speed < desired) ? Math.min(desired - c.speed, step) : -Math.min(c.speed - desired, step);
        }
      } else {
        c.speed += (c.speed < desired) ? Math.min(desired - c.speed, step) : -Math.min(c.speed - desired, step);
      }

      if (c.state === "drive") {                 // startCrash 로 전환되지 않았으면 주행
        c.x += c.speed * dt;
        var ty0 = laneY(c.lane) + Math.sin(c.life * 2 + c.lane) * 1.2;
        c.y += (ty0 - c.y) * Math.min(1, dt * 6); // 차선 변경/흔들림 부드럽게
        if (c.status === "fail" && c.x >= c.crashAt) {
          startCrash(c, false);
        } else if (c.status === "skip" && c.x >= c.pitAt) {
          c.state = "pit"; c.t = 0; c.pitTop = c.lane < LANES / 2;
          c.pitY = c.pitTop ? Math.max(18, roadTop() - 28) : Math.min(H - 24, roadTop() + roadH() + 30);
          c.angle = c.pitTop ? -0.08 : 0.08;
        } else if (c.x > W + 70) {
          c._dead = true;
        }
      }
    } else if (c.state === "pit") {
      // Skip은 장애가 아니다. 감속한 뒤 방향지시등을 켜고 피트레인으로 부드럽게 이탈한다.
      c.t += dt;
      var pitTargetSpeed = c.t < 1.4 ? 38 : 92;
      c.speed += (pitTargetSpeed - c.speed) * Math.min(1, dt * 2.3);
      c.x += c.speed * dt;
      c.y += (c.pitY - c.y) * Math.min(1, dt * 1.55);
      c.angle *= Math.max(0, 1 - dt * 1.35);
      if (c.x > W + 80) c._dead = true;
    } else if (c.state === "crash") {
      c.t += dt;
      if (c.lastSkid === undefined) c.lastSkid = c.x - 100;
      if (c.t < c.swerveDur) {
        // ① 우왕좌왕 — 좌우로 흔들며 미끄러짐(아직 안 돎)
        c.speed = Math.max(0, c.speed - 42 * dt);
        c.x += c.speed * dt;
        var prog = c.t / c.swerveDur;
        var amp = laneH() * 0.55 * prog;
        c.y = c.swerveBaseY + Math.sin(c.t * 21) * amp;     // 빠른 좌우 스워브
        c.angle = Math.sin(c.t * 21) * 0.4 * prog;          // 피시테일(살짝 흔들림)
        if (c.x - c.lastSkid > 11) {
          skids.push({ x: c.x - 10, y: c.y, angle: c.angle, w: 22, alpha: 0.7, life: 0 });
          c.lastSkid = c.x;
        }
        if (Math.random() < dt * 10) smoke.push(smokeP(c.x - 6, c.y));
      } else {
        // ② 스핀 — 여러 바퀴 돌며 아예 도로 밖으로
        c.speed = Math.max(0, c.speed - 78 * dt);
        c.x += c.speed * dt;
        c.y += (offRoadY(c) - c.y) * Math.min(1, dt * 2.2);
        c.angle += c.spinDir * (c.speed * 0.085 + 1.8) * dt;
        if (c.speed > 12 && c.x - c.lastSkid > 13) {
          skids.push({ x: c.x - 12, y: c.y, angle: c.angle, w: 26, alpha: 0.8, life: 0 });
          c.lastSkid = c.x;
        }
        if (c.t < 3.5 && Math.random() < dt * 20) {
          smoke.push(smokeP(c.x + (Math.random() - 0.5) * 18, c.y - 6 + (Math.random() - 0.5) * 14));
        }
        if (c.speed <= 8) { c.state = "wreck"; }
      }
    } else if (c.state === "wreck") {
      c.wreckLife += dt;
      c.y += (offRoadY(c) - c.y) * Math.min(1, dt * 1.6);  // 완전히 도로 밖으로 정착(분리는 offRoadPhysics)
      if (c.wreckLife < 4 && Math.random() < dt * 6) {
        smoke.push(smokeP(c.x + (Math.random() - 0.5) * 12, c.y - 8));
      }
      if (c.wreckLife > 2.8) { c.state = "tow"; c.t = 0; }   // 곧 렉카 도착
    } else if (c.state === "tow") {
      // 렉카(견인차)가 와서 체결 → 빠르게 끌고 감
      if (!c.truck) { c.truck = { x: W + 130, y: towLaneY(c) }; c.towPhase = "arrive"; c.t = 0; c.hookGap = 62; }
      c.t += dt;
      var tk = c.truck;
      // 배정된 행(row)으로 — 0=갓길, 자리 없으면 도로 안쪽으로 넘쳐 운반
      var roadward = c.crashTop ? 1 : -1;
      var desiredY = Math.max(roadTop() + 8, Math.min(H - 10, towLaneY(c) + roadward * (c._row || 0) * 24));
      tk.y += (desiredY - tk.y) * Math.min(1, dt * 2.5);
      if (c.towPhase === "arrive") {
        var tgt = c.x + c.hookGap;                       // 사고차 바로 앞에 정차
        tk.x += (tgt - tk.x) * Math.min(1, dt * 3.2);    // 빠르게 진입
        if (Math.abs(tk.x - tgt) < 5) { c.towPhase = "hook"; c.t = 0; }
      } else if (c.towPhase === "hook") {
        if (c.t > 0.4) { c.towPhase = "haul"; c.t = 0; }  // 체결 잠깐
      } else {                                           // haul — 점점 빨라지며 견인
        tk.x += (180 + c.t * 160) * dt;
        c.x = tk.x - c.hookGap;                          // 사고차는 렉카 뒤에 끌려감
        c.y += (tk.y - c.y) * Math.min(1, dt * 3);       // 사고차도 바깥 행으로 끌어올림
        c.angle *= Math.max(0, 1 - dt * 2.2);            // 견인되며 정렬
        if (tk.x > W + 170) c._dead = true;
      }
    }
  }

  function drawCar(c, p) {
    var col = statusColor(p, c.status);
    ctx.save();
    ctx.translate(c.x, c.y);

    // 바닥 반사 / 언더글로우. 차체보다 먼저 그려 지면에 붙어 보이게 한다.
    var ug = ctx.createRadialGradient(0, 3, 2, 0, 3, c.len * 0.62);
    ug.addColorStop(0, hexA(col, p.dark ? 0.42 : 0.30));
    ug.addColorStop(1, "rgba(0,0,0,0)");
    ctx.fillStyle = ug;
    ctx.fillRect(-c.len * 0.62, -c.wid * 0.9, c.len * 1.24, c.wid * 2.0);

    // 헤드라이트 빔(앞=+x). 도로 위의 얇은 부채꼴로 속도감을 준다.
    if (c.state === "drive") {
      var beam = ctx.createLinearGradient(c.len * 0.3, 0, c.len * 1.65, 0);
      beam.addColorStop(0, "rgba(214,246,255,0.18)");
      beam.addColorStop(1, "rgba(214,246,255,0)");
      ctx.fillStyle = beam;
      ctx.beginPath();
      ctx.moveTo(c.len * 0.28, -c.wid * 0.35);
      ctx.lineTo(c.len * 1.7, -c.wid * 0.72);
      ctx.lineTo(c.len * 1.7, c.wid * 0.72);
      ctx.lineTo(c.len * 0.28, c.wid * 0.35);
      ctx.closePath(); ctx.fill();
    }

    // 속도 트레일(주행 중) — 과속 차는 더 길고 진하게
    if (c.state === "drive" && c.speed > 40) {
      var tl = c.fast ? c.len * 2.7 : c.len * 1.6;
      var tg = ctx.createLinearGradient(-tl, 0, -c.len * 0.4, 0);
      tg.addColorStop(0, "rgba(0,0,0,0)");
      tg.addColorStop(1, hexA(col, c.fast ? 0.34 : 0.18));
      ctx.fillStyle = tg;
      ctx.fillRect(-tl, -c.wid * 0.4, tl - c.len * 0.4, c.wid * 0.8);
    }

    ctx.rotate(c.angle);
    ctx.globalAlpha = 1;

    // 고속 주행 섀도
    ctx.fillStyle = "rgba(0,0,0,0.38)";
    roundRect(-c.len / 2 + 2.5, -c.wid / 2 + 4.5, c.len, c.wid, 8);
    ctx.fill();

    // 노출된 타이어(슈퍼카 실루엣)
    ctx.fillStyle = "rgba(3,7,12,0.96)";
    roundRect(-c.len * 0.35, -c.wid / 2 - 2, c.len * 0.18, 5, 2); ctx.fill();
    roundRect(c.len * 0.17, -c.wid / 2 - 2, c.len * 0.18, 5, 2); ctx.fill();
    roundRect(-c.len * 0.35, c.wid / 2 - 3, c.len * 0.18, 5, 2); ctx.fill();
    roundRect(c.len * 0.17, c.wid / 2 - 3, c.len * 0.18, 5, 2); ctx.fill();

    // 쐐기형 바디. 둥근 캡슐 대신 앞 펜더가 좁아지는 레이싱 실루엣.
    var bg = ctx.createLinearGradient(0, -c.wid / 2, 0, c.wid / 2);
    bg.addColorStop(0, lighten(col, 0.24));
    bg.addColorStop(0.46, col);
    bg.addColorStop(1, darken(col, 0.24));
    ctx.fillStyle = bg;
    ctx.beginPath();
    ctx.moveTo(-c.len * 0.48, -c.wid * 0.3);
    ctx.quadraticCurveTo(-c.len * 0.4, -c.wid * 0.49, -c.len * 0.2, -c.wid * 0.48);
    ctx.lineTo(c.len * 0.23, -c.wid * 0.43);
    ctx.quadraticCurveTo(c.len * 0.45, -c.wid * 0.32, c.len * 0.5, -c.wid * 0.13);
    ctx.lineTo(c.len * 0.5, c.wid * 0.13);
    ctx.quadraticCurveTo(c.len * 0.45, c.wid * 0.32, c.len * 0.23, c.wid * 0.43);
    ctx.lineTo(-c.len * 0.2, c.wid * 0.48);
    ctx.quadraticCurveTo(-c.len * 0.4, c.wid * 0.49, -c.len * 0.48, c.wid * 0.3);
    ctx.closePath(); ctx.fill();
    ctx.strokeStyle = lighten(col, 0.48); ctx.lineWidth = 0.8; ctx.stroke();

    // 카본 사이드 인테이크
    ctx.fillStyle = "rgba(2,8,14,0.56)";
    ctx.beginPath(); ctx.moveTo(-c.len * 0.08, -c.wid * 0.45); ctx.lineTo(c.len * 0.17, -c.wid * 0.4);
    ctx.lineTo(c.len * 0.07, -c.wid * 0.26); ctx.lineTo(-c.len * 0.13, -c.wid * 0.31); ctx.closePath(); ctx.fill();
    ctx.beginPath(); ctx.moveTo(-c.len * 0.08, c.wid * 0.45); ctx.lineTo(c.len * 0.17, c.wid * 0.4);
    ctx.lineTo(c.len * 0.07, c.wid * 0.26); ctx.lineTo(-c.len * 0.13, c.wid * 0.31); ctx.closePath(); ctx.fill();

    // 글라스 캐노피 + 유리 하이라이트
    var glass = ctx.createLinearGradient(-c.len * 0.17, 0, c.len * 0.25, 0);
    glass.addColorStop(0, "rgba(3,10,18,0.92)");
    glass.addColorStop(0.6, "rgba(30,69,93,0.86)");
    glass.addColorStop(1, "rgba(151,221,245,0.72)");
    ctx.fillStyle = glass;
    ctx.beginPath();
    ctx.moveTo(-c.len * 0.2, -c.wid * 0.29); ctx.quadraticCurveTo(-c.len * 0.28, 0, -c.len * 0.2, c.wid * 0.29);
    ctx.lineTo(c.len * 0.25, c.wid * 0.24); ctx.quadraticCurveTo(c.len * 0.32, 0, c.len * 0.25, -c.wid * 0.24);
    ctx.closePath(); ctx.fill();
    ctx.strokeStyle = "rgba(218,245,255,0.4)"; ctx.lineWidth = 0.7; ctx.stroke();

    // 센터 스파인 / 팀 리버리
    ctx.fillStyle = "rgba(255,255,255,0.62)";
    ctx.fillRect(-c.len * 0.42, -0.65, c.len * 0.76, 1.3);
    ctx.fillStyle = hexA(col, 0.95);
    ctx.fillRect(-c.len * 0.43, -0.2, c.len * 0.78, 0.4);

    // 헤드라이트 / 테일 라이트 바
    ctx.shadowBlur = 8;
    ctx.shadowColor = "#dff8ff";
    ctx.strokeStyle = "rgba(224,250,255,0.98)"; ctx.lineWidth = 1.8;
    ctx.beginPath();
    ctx.moveTo(c.len * 0.39, -c.wid * 0.27); ctx.lineTo(c.len * 0.48, -c.wid * 0.13);
    ctx.moveTo(c.len * 0.39, c.wid * 0.27); ctx.lineTo(c.len * 0.48, c.wid * 0.13); ctx.stroke();
    ctx.shadowColor = "#ff294f";
    ctx.strokeStyle = "rgba(255,49,85,0.98)"; ctx.lineWidth = 1.7;
    ctx.beginPath();
    ctx.moveTo(-c.len * 0.47, -c.wid * 0.25); ctx.lineTo(-c.len * 0.43, -c.wid * 0.08);
    ctx.moveTo(-c.len * 0.47, c.wid * 0.25); ctx.lineTo(-c.len * 0.43, c.wid * 0.08); ctx.stroke();
    ctx.shadowBlur = 0;

    // RUNNING — 비상등 깜빡임
    if (c.status === "running") {
      var blink = (Math.sin(c.life * 9) > 0) ? 0.95 : 0.15;
      ctx.fillStyle = hexA("#fbbf24", blink);
      ctx.shadowColor = "#fbbf24"; ctx.shadowBlur = 10 * blink;
      ctx.beginPath(); ctx.arc(-c.len * 0.05, -c.wid * 0.43, 2.3, 0, 7); ctx.fill();
      ctx.beginPath(); ctx.arc(-c.len * 0.05, c.wid * 0.43, 2.3, 0, 7); ctx.fill();
      ctx.shadowBlur = 0;
    }
    // SKIP — 사고 경광등이 아니라 amber 방향지시등/피트 마커.
    if (c.status === "skip") {
      var pitBlink = reducedMotion ? 0.9 : (Math.sin(c.life * 7) > 0 ? 1 : 0.25);
      ctx.fillStyle = hexA(p.carSkip, pitBlink);
      ctx.shadowColor = p.carSkip; ctx.shadowBlur = 8 * pitBlink;
      ctx.beginPath(); ctx.arc(c.len * 0.08, c.wid * 0.43, 2.4, 0, 7); ctx.fill();
      ctx.shadowBlur = 0;
    }
    // 신규 run — 글로우 펄스
    if (c.flash > 0) {
      ctx.strokeStyle = hexA(col, Math.min(1, c.flash));
      ctx.lineWidth = 2.5;
      roundRect(-c.len / 2 - 3, -c.wid / 2 - 3, c.len + 6, c.wid + 6, 9);
      ctx.stroke();
    }
    // 사고 — 구겨짐 + 경고 삼각형
    if (c.state === "wreck" || (c.state === "crash" && c.t > 0.4)) {
      ctx.fillStyle = "rgba(20,20,25,0.45)";
      roundRect(c.len * 0.18, -c.wid * 0.4, c.len * 0.3, c.wid * 0.8, 3);
      ctx.fill();
    }
    if (c === hovered || c === keyboardCar) {
      ctx.strokeStyle = "rgba(210,248,255,0.9)";
      ctx.lineWidth = 1;
      ctx.setLineDash([3, 3]);
      roundRect(-c.len / 2 - 5, -c.wid / 2 - 5, c.len + 10, c.wid + 10, 5); ctx.stroke();
      ctx.setLineDash([]);
    }
    ctx.restore();

    // 라벨 칩 — 주행 중에만 머리 위
    if (c.state === "drive" || c.state === "pit") {
      drawChip(c, p, col);
    }
    if (c.state === "pit") {
      ctx.save();
      ctx.font = "900 9px -apple-system, 'Segoe UI', sans-serif";
      var pitText = "PIT →", pitW = ctx.measureText(pitText).width + 12;
      ctx.fillStyle = "rgba(5,12,18,0.88)"; roundRect(c.x - pitW / 2, c.y + c.wid + 6, pitW, 16, 2); ctx.fill();
      ctx.strokeStyle = p.carSkip; ctx.lineWidth = 1; roundRect(c.x - pitW / 2, c.y + c.wid + 6, pitW, 16, 2); ctx.stroke();
      ctx.fillStyle = p.carSkip; ctx.textAlign = "center"; ctx.textBaseline = "middle"; ctx.fillText(pitText, c.x, c.y + c.wid + 14);
      ctx.restore();
    }
    // 사고 중 패닉 말풍선 — 한 글자씩 급박하게: ! ? ! ? !!!!!!
    if (c.state === "crash") {
      var pseq = ["!", "?", "!", "?", "!", "?", "!!!!!!"];
      var pidx = Math.floor(c.t / 0.16);
      var pmsg = pidx >= pseq.length - 1 ? "!!!!!!" : pseq[pidx];
      ctx.save();
      ctx.font = "900 15px -apple-system, 'Segoe UI', sans-serif";
      var ptw = ctx.measureText(pmsg).width, pbw = ptw + 14, pbh = 21;
      var pbx = c.x, pby = c.y - c.wid - 20;
      ctx.fillStyle = "#fde047";
      roundRect(pbx - pbw / 2, pby - pbh / 2, pbw, pbh, 6); ctx.fill();
      ctx.beginPath();
      ctx.moveTo(pbx - 4, pby + pbh / 2 - 1);
      ctx.lineTo(pbx, pby + pbh / 2 + 6);
      ctx.lineTo(pbx + 4, pby + pbh / 2 - 1);
      ctx.closePath(); ctx.fill();
      ctx.fillStyle = "#7c2d12";
      ctx.textAlign = "center"; ctx.textBaseline = "middle";
      ctx.fillText(pmsg, pbx, pby + 0.5);
      ctx.restore();
    }
    // 경적 — 앞으로 퍼지는 음파 + "!!" 말풍선
    if (c.honk > 0) {
      var hx = c.x + c.len / 2 + 2, hy = c.y - c.wid * 0.15;
      ctx.save();
      var amp = Math.min(1, c.honk / 0.5);
      for (var hw = 0; hw < 3; hw++) {
        var hph = (sceneTime * 3.4 + hw * 0.33) % 1;
        ctx.globalAlpha = (1 - hph) * 0.9 * amp;
        ctx.strokeStyle = "#facc15"; ctx.lineWidth = 2.4;
        ctx.beginPath();
        ctx.arc(hx, hy, 4 + hph * 14, -0.7, 0.7);
        ctx.stroke();
      }
      ctx.globalAlpha = amp * (0.65 + 0.35 * Math.abs(Math.sin(sceneTime * 14)));
      var txt = "빵빵!!";
      ctx.font = "900 12px -apple-system, 'Segoe UI', sans-serif";
      var tw = ctx.measureText(txt).width, bw = tw + 14, bh = 19;
      var bx = hx + 8 + bw / 2, by = c.y - c.wid - 11;
      ctx.fillStyle = "#facc15";
      roundRect(bx - bw / 2, by - bh / 2, bw, bh, 6); ctx.fill();
      ctx.beginPath();                                   // 말풍선 꼬리
      ctx.moveTo(bx - bw * 0.20, by + bh / 2 - 1);
      ctx.lineTo(bx - bw * 0.32, by + bh / 2 + 6);
      ctx.lineTo(bx - bw * 0.02, by + bh / 2 - 1);
      ctx.closePath(); ctx.fill();
      ctx.fillStyle = "#3a2200";
      ctx.textAlign = "center"; ctx.textBaseline = "middle";
      ctx.fillText(txt, bx, by + 0.5);
      ctx.restore();
    }
    // "??" — 경적 받고 비켜주는 앞차 반응
    if (c.huh > 0) {
      ctx.save();
      ctx.globalAlpha = Math.min(1, c.huh / 0.9);
      ctx.font = "900 13px -apple-system, 'Segoe UI', sans-serif";
      var qt = "??", qtw = ctx.measureText(qt).width, qbw = qtw + 14, qbh = 19;
      var qbx = c.x, qby = c.y - c.wid - 24;
      ctx.fillStyle = "#e0f2fe";
      roundRect(qbx - qbw / 2, qby - qbh / 2, qbw, qbh, 6); ctx.fill();
      ctx.beginPath();
      ctx.moveTo(qbx - 4, qby + qbh / 2 - 1);
      ctx.lineTo(qbx, qby + qbh / 2 + 6);
      ctx.lineTo(qbx + 4, qby + qbh / 2 - 1);
      ctx.closePath(); ctx.fill();
      ctx.fillStyle = "#0c4a6e";
      ctx.textAlign = "center"; ctx.textBaseline = "middle";
      ctx.fillText(qt, qbx, qby + 0.5);
      ctx.restore();
    }
    // 사고 경고 아이콘
    if (c.state === "wreck") {
      ctx.save();
      ctx.translate(c.x, c.y - c.wid - 6);
      ctx.globalAlpha = 0.5 + 0.5 * Math.abs(Math.sin(c.wreckLife * 4));
      ctx.fillStyle = p.carFail;
      ctx.beginPath();
      ctx.moveTo(0, -7); ctx.lineTo(7, 6); ctx.lineTo(-7, 6); ctx.closePath();
      ctx.fill();
      ctx.fillStyle = "#fff"; ctx.font = "bold 9px sans-serif"; ctx.textAlign = "center";
      ctx.fillText("!", 0, 5);
      ctx.restore();
    }
    // 렉카(견인차)
    if (c.state === "tow") drawTow(c, p);
  }

  // 렉카(견인차) — 사고차 앞에 정차해 체결 후 끌고 간다. 우측에서 진입, 우측으로 견인.
  function drawTow(c, p) {
    var tk = c.truck; if (!tk) return;
    var L = 60, Wd = 26;
    if (c.towPhase !== "arrive") {                 // 견인 줄
      ctx.save();
      ctx.strokeStyle = "rgba(200,200,210,0.9)"; ctx.lineWidth = 2;
      ctx.beginPath(); ctx.moveTo(tk.x - L / 2, tk.y + 2); ctx.lineTo(c.x + c.len / 2 - 2, c.y); ctx.stroke();
      ctx.restore();
    }
    ctx.save();
    ctx.translate(tk.x, tk.y);
    // 언더글로우
    var ug = ctx.createRadialGradient(0, 3, 2, 0, 3, L * 0.55);
    ug.addColorStop(0, hexA("#f59e0b", p.dark ? 0.4 : 0.22)); ug.addColorStop(1, "rgba(0,0,0,0)");
    ctx.fillStyle = ug; ctx.fillRect(-L * 0.55, -Wd, L * 1.1, Wd * 2);
    ctx.fillStyle = "rgba(0,0,0,0.28)"; roundRect(-L / 2 + 2, -Wd / 2 + 4, L, Wd, 6); ctx.fill();
    var bg = ctx.createLinearGradient(0, -Wd / 2, 0, Wd / 2);
    bg.addColorStop(0, "#fb923c"); bg.addColorStop(1, "#ea580c");
    ctx.fillStyle = bg; roundRect(-L / 2, -Wd / 2, L, Wd, 6); ctx.fill();
    ctx.fillStyle = "rgba(0,0,0,0.18)"; roundRect(-L / 2 + 2, -Wd / 2 + 2, L * 0.42, Wd - 4, 4); ctx.fill();
    // 캡(앞=오른쪽)
    ctx.fillStyle = "#f1f5f9"; roundRect(L * 0.14, -Wd * 0.44, L * 0.32, Wd * 0.88, 4); ctx.fill();
    ctx.fillStyle = hexA("#bfdbfe", 0.75); roundRect(L * 0.30, -Wd * 0.34, L * 0.12, Wd * 0.68, 2); ctx.fill();
    // 크레인 붐 + 후크(뒤쪽)
    ctx.strokeStyle = "#475569"; ctx.lineWidth = 3; ctx.lineCap = "round";
    ctx.beginPath(); ctx.moveTo(-L * 0.34, -Wd * 0.05); ctx.lineTo(-L * 0.6, -Wd * 0.6);
    ctx.lineTo(-L * 0.66, -Wd * 0.15); ctx.stroke();
    ctx.fillStyle = "rgba(10,12,18,0.85)";
    ctx.fillRect(-L * 0.36, -Wd / 2 - 2, L * 0.16, 3); ctx.fillRect(L * 0.18, -Wd / 2 - 2, L * 0.16, 3);
    ctx.fillRect(-L * 0.36, Wd / 2 - 1, L * 0.16, 3); ctx.fillRect(L * 0.18, Wd / 2 - 1, L * 0.16, 3);
    // 회전 경고등(점멸 글로우)
    var bl = (Math.sin(sceneTime * 16) > 0) ? 1 : 0.25;
    ctx.shadowColor = "#f59e0b"; ctx.shadowBlur = 12 * bl;
    ctx.fillStyle = hexA("#fbbf24", bl);
    ctx.beginPath(); ctx.arc(L * 0.02, -Wd * 0.5 - 3, 3.4, 0, 7); ctx.fill();
    ctx.restore();
  }

  function drawChip(c, p, col) {
    var label = (c.label || "").toString();
    if (label.length > 18) label = label.slice(0, 17) + "…";
    ctx.save();
    ctx.font = "600 10px -apple-system, sans-serif";
    var tw = ctx.measureText(label).width;
    var pad = 6, w = tw + pad * 2 + 10, h = 16;
    var cx = c.x, cy = c.y - c.wid - 12;
    ctx.globalAlpha = 0.92;
    ctx.fillStyle = p.chipBg;
    roundRect(cx - w / 2, cy - h / 2, w, h, 8); ctx.fill();
    ctx.strokeStyle = hexA(col, 0.9); ctx.lineWidth = 1.2;
    roundRect(cx - w / 2, cy - h / 2, w, h, 8); ctx.stroke();
    ctx.fillStyle = col;
    ctx.beginPath(); ctx.arc(cx - w / 2 + pad + 1, cy, 3, 0, 7); ctx.fill();
    ctx.fillStyle = p.text; ctx.textAlign = "left"; ctx.textBaseline = "middle";
    ctx.fillText(label, cx - w / 2 + pad + 8, cy + 0.5);
    ctx.restore();
  }

  // ---- 연기 / 스키드 ---------------------------------------------------
  function smokeP(x, y) {
    return { x: x, y: y, vx: (Math.random() - 0.5) * 14, vy: -18 - Math.random() * 22,
             r: 4 + Math.random() * 5, alpha: 0.34, life: 0, max: 1.4 + Math.random() * 1.2 };
  }
  function updateSmoke(dt) {
    for (var i = smoke.length - 1; i >= 0; i--) {
      var s = smoke[i]; s.life += dt;
      s.x += s.vx * dt; s.y += s.vy * dt; s.vy += 6 * dt; s.r += 14 * dt;
      s.alpha = 0.34 * (1 - s.life / s.max);
      if (s.life >= s.max) smoke.splice(i, 1);
    }
  }
  function drawSmoke(p) {
    for (var i = 0; i < smoke.length; i++) {
      var s = smoke[i];
      var alpha = Math.max(0, s.alpha);
      var sg = ctx.createRadialGradient(s.x - s.r * 0.18, s.y - s.r * 0.18, 1, s.x, s.y, s.r);
      var rgb = p.dark ? "166,180,194" : "70,78,88";
      sg.addColorStop(0, "rgba(" + rgb + "," + (alpha * 0.72) + ")");
      sg.addColorStop(0.55, "rgba(" + rgb + "," + (alpha * 0.36) + ")");
      sg.addColorStop(1, "rgba(" + rgb + ",0)");
      ctx.fillStyle = sg; ctx.beginPath(); ctx.arc(s.x, s.y, s.r, 0, 7); ctx.fill();
    }
  }
  function updateSkids(dt) {
    for (var i = skids.length - 1; i >= 0; i--) {
      var k = skids[i]; k.life += dt; k.alpha -= dt * 0.06;
      if (k.alpha <= 0) skids.splice(i, 1);
    }
  }
  function drawSkids() {
    for (var i = 0; i < skids.length; i++) {
      var k = skids[i];
      ctx.save(); ctx.translate(k.x, k.y); ctx.rotate(k.angle);
      ctx.strokeStyle = "rgba(15,18,24," + k.alpha + ")"; ctx.lineWidth = 3;
      ctx.beginPath(); ctx.moveTo(-k.w / 2, -4); ctx.lineTo(k.w / 2, -2);
      ctx.moveTo(-k.w / 2, 4); ctx.lineTo(k.w / 2, 2); ctx.stroke();
      ctx.restore();
    }
  }

  // ---- 네온 / 표지판 ---------------------------------------------------
  function flicker(t, seed) {
    var b = 0.8 + 0.2 * Math.sin(t * 3 + seed);
    if (Math.sin(t * 27 + seed * 1.7) > 0.92) b *= 0.45; // 가끔 깜빡
    return b;
  }
  function neonText(text, x, y, col, size, glow, align) {
    ctx.save();
    ctx.font = "800 " + size + "px -apple-system, 'Segoe UI', sans-serif";
    ctx.textAlign = align || "center";
    ctx.textBaseline = "middle";
    ctx.shadowColor = col; ctx.shadowBlur = glow;
    ctx.fillStyle = col; ctx.fillText(text, x, y);
    ctx.shadowBlur = glow * 0.4;
    ctx.fillStyle = "rgba(255,255,255,0.9)";
    ctx.font = "800 " + (size - 1) + "px -apple-system, 'Segoe UI', sans-serif";
    ctx.fillText(text, x, y);
    ctx.restore();
  }
  function neonFrame(x, y, w, h, col, glow, fillBg) {
    ctx.save();
    if (fillBg) {
      ctx.fillStyle = fillBg;
      roundRect(x, y, w, h, 9); ctx.fill();
    }
    ctx.shadowColor = col; ctx.shadowBlur = glow;
    ctx.strokeStyle = col; ctx.lineWidth = 2.4;
    roundRect(x + 1.5, y + 1.5, w - 3, h - 3, 8); ctx.stroke();
    ctx.restore();
  }

  // 상단 배경의 네온 빌보드 2개 + 중앙 LIVE 사인
  function drawBillboards(p, t) {
    var bandH = roadTop();
    var panelH = Math.max(46, bandH * 0.56);
    var py = bandH * 0.14;
    var pw = Math.min(210, W * 0.24);
    var bg = p.dark ? "rgba(7,11,20,0.92)" : "rgba(17,22,36,0.90)";

    // 좌측: QUALITY HIGHWAY (시안)
    var cyan = "#22d3ee", g1 = 16 * flicker(t, 0);
    var x1 = Math.max(12, W * 0.03);
    // 지지대
    ctx.fillStyle = "rgba(0,0,0,0.3)";
    ctx.fillRect(x1 + pw * 0.5 - 3, py + panelH, 6, bandH - (py + panelH));
    neonFrame(x1, py, pw, panelH, cyan, g1, bg);
    neonText("QUALITY", x1 + pw / 2, py + panelH * 0.36, cyan, Math.max(13, panelH * 0.30), g1);
    neonText("HIGHWAY", x1 + pw / 2, py + panelH * 0.72, "#a78bfa", Math.max(13, panelH * 0.30), 14 * flicker(t, 2));

    // 우측: ✓ QA ZONE (핑크/라임)
    var pink = "#f472b6", g2 = 16 * flicker(t, 5);
    var x2 = W - pw - Math.max(12, W * 0.03);
    ctx.fillStyle = "rgba(0,0,0,0.3)";
    ctx.fillRect(x2 + pw * 0.5 - 3, py + panelH, 6, bandH - (py + panelH));
    neonFrame(x2, py, pw, panelH, pink, g2, bg);
    // 네온 체크마크
    ctx.save();
    ctx.shadowColor = "#a3e635"; ctx.shadowBlur = 14 * flicker(t, 7);
    ctx.strokeStyle = "#a3e635"; ctx.lineWidth = 4; ctx.lineCap = "round";
    var cxk = x2 + pw * 0.22, cyk = py + panelH * 0.5, s = panelH * 0.22;
    ctx.beginPath(); ctx.moveTo(cxk - s, cyk); ctx.lineTo(cxk - s * 0.2, cyk + s * 0.8);
    ctx.lineTo(cxk + s, cyk - s * 0.8); ctx.stroke();
    ctx.restore();
    neonText("QA ZONE", x2 + pw * 0.62, py + panelH * 0.5, pink, Math.max(12, panelH * 0.26), g2);

    // 중앙: ◄ LIVE ► (작게, 빨강 점멸)
    var live = "#fb7185", gl = 12 * (0.5 + 0.5 * Math.abs(Math.sin(t * 2.4)));
    neonText("◄ LIVE ►", W / 2, py + panelH * 0.34, live, Math.max(12, panelH * 0.22), gl);
  }

  // 갓길의 교통 표지판들 (경고/속도제한/방향) — 일부는 흐름 위해 스크롤
  function drawRoadsideSigns(p, t) {
    var topY = roadTop() - 16, botY = roadTop() + roadH() + 18;
    // 스크롤되는 방향 화살표 보드(하단 갓길) — 흐름 강조 (signScroll 은 frame 에서 증가)
    var spacing = 240, n = Math.ceil(W / spacing) + 2;
    for (var i = 0; i < n; i++) {
      var x = ((i * spacing - (signScroll % spacing)) + W) % (W + spacing) - spacing / 2;
      drawChevronSign(x, botY, "#fbbf24", t);
    }
    // 고정 표지판들(상단 갓길)
    drawWarnSign(W * 0.30, topY, t);
    drawSpeedSign(W * 0.62, topY, "100");
    drawWarnSign(W * 0.86, topY, t + 1.3);
  }
  function drawChevronSign(x, y, col, t) {
    ctx.save();
    ctx.translate(x, y);
    ctx.shadowColor = col; ctx.shadowBlur = 8;
    ctx.strokeStyle = col; ctx.lineWidth = 3; ctx.lineCap = "round";
    for (var k = -1; k <= 1; k++) {
      ctx.beginPath();
      ctx.moveTo(k * 7 - 4, -5); ctx.lineTo(k * 7 + 2, 0); ctx.lineTo(k * 7 - 4, 5);
      ctx.stroke();
    }
    ctx.restore();
  }
  function drawWarnSign(x, y, t) {
    ctx.save();
    ctx.translate(x, y);
    ctx.fillStyle = "rgba(0,0,0,0.3)"; ctx.fillRect(-1.5, 8, 3, 12); // 기둥
    ctx.shadowColor = "#f59e0b"; ctx.shadowBlur = 9 * flicker(t, 3);
    ctx.fillStyle = "#f59e0b";
    ctx.beginPath(); ctx.moveTo(0, -11); ctx.lineTo(11, 8); ctx.lineTo(-11, 8); ctx.closePath(); ctx.fill();
    ctx.shadowBlur = 0;
    ctx.fillStyle = "#1a1206"; ctx.font = "900 12px sans-serif"; ctx.textAlign = "center"; ctx.textBaseline = "middle";
    ctx.fillText("!", 0, 2);
    ctx.restore();
  }
  function drawSpeedSign(x, y, txt) {
    ctx.save();
    ctx.translate(x, y);
    ctx.fillStyle = "rgba(0,0,0,0.3)"; ctx.fillRect(-1.5, 11, 3, 11);
    ctx.fillStyle = "#fff";
    ctx.beginPath(); ctx.arc(0, 0, 12, 0, 7); ctx.fill();
    ctx.shadowColor = "#ef4444"; ctx.shadowBlur = 8;
    ctx.strokeStyle = "#ef4444"; ctx.lineWidth = 3;
    ctx.beginPath(); ctx.arc(0, 0, 11, 0, 7); ctx.stroke();
    ctx.shadowBlur = 0;
    ctx.fillStyle = "#111"; ctx.font = "900 10px sans-serif"; ctx.textAlign = "center"; ctx.textBaseline = "middle";
    ctx.fillText(txt, 0, 1);
    ctx.restore();
  }

  // ---- 도로 ------------------------------------------------------------
  function drawScene(p, dt) {
    var rt = roadTop(), rh = roadH();
    // 1) 환경 plate. 생성 이미지가 있으면 cover 합성하고, 없으면 procedural pit-city로 폴백.
    var g = ctx.createLinearGradient(0, 0, 0, H);
    g.addColorStop(0, p.groundTop); g.addColorStop(1, p.groundBottom);
    ctx.fillStyle = g; ctx.fillRect(0, 0, W, H);

    if (backgroundReady && background && background.naturalWidth) {
      var ir = background.naturalWidth / background.naturalHeight;
      var cr = W / H;
      var sw, sh, sx, sy;
      if (ir > cr) {
        sh = background.naturalHeight; sw = sh * cr;
        sx = (background.naturalWidth - sw) / 2; sy = 0;
      } else {
        sw = background.naturalWidth; sh = sw / cr;
        sx = 0; sy = (background.naturalHeight - sh) / 2;
      }
      ctx.save();
      ctx.globalAlpha = p.dark ? 0.76 : 0.68;
      ctx.drawImage(background, sx, sy, sw, sh, 0, 0, W, H);
      ctx.restore();
      var plateShade = ctx.createLinearGradient(0, 0, 0, H);
      plateShade.addColorStop(0, p.dark ? "rgba(0,5,11,0.18)" : "rgba(4,12,20,0.08)");
      plateShade.addColorStop(0.28, "rgba(2,8,14,0.33)");
      plateShade.addColorStop(0.78, "rgba(2,7,13,0.42)");
      plateShade.addColorStop(1, "rgba(0,3,8,0.58)");
      ctx.fillStyle = plateShade; ctx.fillRect(0, 0, W, H);
    } else {
      // 절차적 폴백: 관중석/피트월/도시 조명을 낮은 대비로 쌓는다.
      var sun = ctx.createRadialGradient(W * 0.5, rt * 0.56, 8, W * 0.5, rt * 0.56, W * 0.62);
      sun.addColorStop(0, p.horizon); sun.addColorStop(1, "rgba(0,0,0,0)");
      ctx.fillStyle = sun; ctx.fillRect(0, 0, W, rt + 30);
      ctx.fillStyle = "rgba(3,9,16,0.68)";
      for (var b = 0; b < Math.ceil(W / 92) + 1; b++) {
        var bw = 54 + ((b * 37) % 44), bh = 25 + ((b * 53) % Math.max(30, rt * 0.42));
        ctx.fillRect(b * 92 - 10, rt - bh - 16, bw, bh);
        ctx.fillStyle = b % 3 === 0 ? "rgba(53,216,255,0.14)" : "rgba(100,130,162,0.10)";
        for (var wx = b * 92; wx < b * 92 + bw - 8; wx += 15) ctx.fillRect(wx, rt - bh - 8, 7, 2);
        ctx.fillStyle = "rgba(3,9,16,0.68)";
      }
      ctx.fillStyle = "rgba(11,21,31,0.9)"; ctx.fillRect(0, rt - 48, W, 38);
      ctx.strokeStyle = "rgba(117,174,204,0.2)"; ctx.lineWidth = 1;
      for (var px = -(signScroll * 0.18 % 88); px < W; px += 88) {
        ctx.strokeRect(px, rt - 44, 70, 28);
        ctx.fillStyle = px % 176 ? "rgba(53,216,255,0.14)" : "rgba(104,124,255,0.14)";
        ctx.fillRect(px + 7, rt - 37, 29, 3);
      }
    }

    // 2) 피트월/세이프티 존.
    ctx.fillStyle = p.shoulder; ctx.fillRect(0, rt - 13, W, 13); ctx.fillRect(0, rt + rh, W, 13);
    ctx.fillStyle = "rgba(225,239,246,0.15)";
    for (var cs = -(signScroll % 64); cs < W + 64; cs += 64) {
      ctx.fillRect(cs, rt - 12, 32, 4); ctx.fillRect(cs + 32, rt + rh + 8, 32, 4);
    }

    // 3) 아스팔트. 중앙부를 가장 어둡게 하고 얇은 러버 라인을 더한다.
    var ag = ctx.createLinearGradient(0, rt, 0, rt + rh);
    ag.addColorStop(0, p.asphalt1); ag.addColorStop(0.14, p.asphalt2);
    ag.addColorStop(0.5, darken(p.asphalt2, 0.17)); ag.addColorStop(0.86, p.asphalt2); ag.addColorStop(1, p.asphalt1);
    ctx.fillStyle = ag; ctx.fillRect(0, rt, W, rh);

    // 아스팔트 미세 텍스처: 고정 패턴이라 프레임 플리커가 없다.
    ctx.save();
    ctx.fillStyle = "rgba(192,218,230,0.035)";
    for (var tx = 0; tx < W; tx += 37) {
      for (var ty = rt + ((tx * 7) % 19); ty < rt + rh; ty += 31) ctx.fillRect(tx, ty, 9, 0.8);
    }
    ctx.strokeStyle = "rgba(0,0,0,0.16)"; ctx.lineWidth = 4;
    for (var rr = 0; rr < LANES; rr++) {
      var ry = laneY(rr);
      ctx.beginPath(); ctx.moveTo(0, ry - 7); ctx.lineTo(W, ry - 7); ctx.stroke();
      ctx.beginPath(); ctx.moveTo(0, ry + 7); ctx.lineTo(W, ry + 7); ctx.stroke();
    }
    ctx.restore();

    // 4) 레일/차선. 과도한 Tron 글로우 대신 실제 서킷 조명 톤.
    ctx.save();
    ctx.lineWidth = 2; ctx.setLineDash([]);
    var pulse = reducedMotion ? 4 : 5 + 2 * Math.sin(sceneTime * 1.8);
    ctx.shadowBlur = pulse; ctx.shadowColor = p.edgeTop; ctx.strokeStyle = hexA(p.edgeTop, 0.86);
    ctx.beginPath(); ctx.moveTo(0, rt + 1.5); ctx.lineTo(W, rt + 1.5); ctx.stroke();
    ctx.shadowColor = p.edgeBot; ctx.strokeStyle = hexA(p.edgeBot, 0.82);
    ctx.beginPath(); ctx.moveTo(0, rt + rh - 1.5); ctx.lineTo(W, rt + rh - 1.5); ctx.stroke();
    ctx.restore();

    dashOffset = (dashOffset + dt * 142) % 76;
    ctx.save();
    ctx.shadowBlur = p.dark ? 3 : 1; ctx.shadowColor = p.cyan;
    ctx.strokeStyle = p.lane; ctx.lineWidth = 2;
    ctx.setLineDash([38, 38]); ctx.lineDashOffset = -dashOffset;
    for (var i = 1; i < LANES; i++) {
      var y = rt + laneH() * i;
      ctx.beginPath(); ctx.moveTo(0, y); ctx.lineTo(W, y); ctx.stroke();
    }
    ctx.restore();
    ctx.setLineDash([]);

    // 스타트/피니시 타이밍 라인과 갓길 사인.
    var timingX = W * 0.18;
    var sq = Math.max(5, Math.min(9, laneH() * 0.12));
    for (var cy = rt; cy < rt + rh; cy += sq) {
      ctx.fillStyle = ((Math.floor((cy - rt) / sq) % 2) ? "rgba(238,246,249,0.72)" : "rgba(8,14,20,0.72)");
      ctx.fillRect(timingX, cy, 5, sq);
    }
    drawRoadsideSigns(p, sceneTime);
  }

  // 스캔라인 + 비네트 오버레이 (CRT/사이버 질감)
  function drawOverlay(p) {
    ctx.save();
    ctx.globalAlpha = p.scan;
    ctx.fillStyle = "#000";
    if (!reducedMotion) for (var y = 0; y < H; y += 4) ctx.fillRect(0, y, W, 1);
    ctx.globalAlpha = 1;
    var vg = ctx.createRadialGradient(W / 2, H / 2, Math.min(W, H) * 0.32, W / 2, H / 2, Math.max(W, H) * 0.72);
    vg.addColorStop(0, "rgba(0,0,0,0)");
    vg.addColorStop(1, "rgba(0,0,0," + p.vignette + ")");
    ctx.fillStyle = vg; ctx.fillRect(0, 0, W, H);
    ctx.restore();
  }

  // ---- HUD -------------------------------------------------------------
  function drawHUD(p) {
    var pad = W < 560 ? 11 : 16;
    var y = H - 17;
    ctx.save();
    ctx.font = "800 " + (W < 560 ? 8 : 9) + "px -apple-system, 'Segoe UI', sans-serif";
    ctx.textBaseline = "middle"; ctx.textAlign = "left";
    ctx.fillStyle = "rgba(3,10,18,0.7)"; roundRect(pad, y - 9, W < 560 ? 116 : 148, 18, 2); ctx.fill();
    ctx.fillStyle = p.cyan; ctx.fillRect(pad, y - 9, 2, 18);
    ctx.fillStyle = p.textDim;
    ctx.fillText("ACTIVE MACHINES  " + String(cars.length).padStart(2, "0"), pad + 9, y + 0.5);
    if (keyboardCar && cars.indexOf(keyboardCar) >= 0) {
      var label = String(keyboardCar.label || "SELECTED");
      if (label.length > 28) label = label.slice(0, 27) + "…";
      ctx.textAlign = "right"; ctx.fillStyle = statusColor(p, keyboardCar.status);
      ctx.fillText("TARGET / " + label.toUpperCase(), W - pad, y + 0.5);
    }
    ctx.restore();
  }

  // ---- 메인 루프 -------------------------------------------------------
  function requestFrame() {
    if (!running || frameRaf) return;
    frameRaf = requestAnimationFrame(frame);
  }

  function frame(now) {
    frameRaf = 0;
    if (!running) return;
    // 빈 grid/reduced-motion/paused 상태는 저주사율로 낮춰 장시간 열린 관제 화면의 CPU를 절약한다.
    var interval = userPaused ? 250 : reducedMotion ? 84 : pool.length ? 22 : 125;
    if (lastPaint && now - lastPaint < interval) { requestFrame(); return; }
    lastPaint = now;

    // dt는 rAF 타임스탬프만 사용한다. 탭 복귀 시 급격한 점프가 없도록 clamp.
    var dt = lastFrame ? (now - lastFrame) / 1000 : 0;
    lastFrame = now;
    dt = Math.max(0, Math.min(0.08, dt));
    var simDt = userPaused ? 0 : dt * (reducedMotion ? 0.18 : 1);
    sceneTime += simDt;
    signScroll += simDt * 82;
    var p = P();

    // 스폰
    if (!userPaused && pool.length) {
      spawnTimer += reducedMotion ? dt : simDt;
      if (spawnTimer >= nextSpawn) {
        spawnTimer = 0;
        nextSpawn = (reducedMotion ? 2.2 : 0.72) + Math.random() * (reducedMotion ? 1.4 : 1.15);
        // 동일 run을 동시에 여러 차량으로 복제하지 않는다. pool은 순환하되 active id는 건너뛴다.
        for (var attempt = 0; attempt < pool.length; attempt++) {
          var rec = pool[poolIdx % pool.length]; poolIdx++;
          if (spawnCar(rec)) {
            if (rec._fresh) rec._fresh = false;
            break;
          }
        }
      }
    }

    // 차간 간격 계산(추종/추돌) 후 업데이트
    if (!userPaused) planTraffic();
    for (var i = cars.length - 1; i >= 0; i--) {
      if (!userPaused) updateCar(cars[i], simDt);
      if (cars[i]._dead) {
        if (keyboardCar === cars[i]) keyboardCar = null;
        cars.splice(i, 1);
      }
    }
    if (!userPaused) {
      offRoadPhysics(simDt);
      updateSmoke(simDt); updateSkids(simDt);
    }

    // 드로우
    ctx.clearRect(0, 0, W, H);
    drawScene(p, simDt);
    drawSkids();
    // 차량은 y 순으로 정렬해 겹침 자연스럽게
    cars.sort(function (a, b) { return a.y - b.y; });
    for (var j = 0; j < cars.length; j++) drawCar(cars[j], p);
    drawSmoke(p);
    drawOverlay(p);
    drawHUD(p);

    // 호버 처리
    hitTest();

    requestFrame();
  }

  // ---- 호버 툴팁 -------------------------------------------------------
  function pickCar(x, y) {
    var picked = null;
    var best = 1e9;
    for (var i = cars.length - 1; i >= 0; i--) {
      var c = cars[i];
      var dx = Math.abs(c.x - x), dy = Math.abs(c.y - y);
      var touchPad = W < 560 ? 14 : 8;
      if (dx < c.len / 2 + touchPad && dy < c.wid / 2 + touchPad) {
        var d = dx + dy; if (d < best) { best = d; picked = c; }
      }
    }
    return picked;
  }

  function positionTip(c) {
    if (!tip || !c) return;
    var r = canvas.getBoundingClientRect();
    var left = r.left + window.scrollX + c.x + 15;
    var top = r.top + window.scrollY + c.y - 12;
    var minLeft = r.left + window.scrollX + 6;
    var maxLeft = r.right + window.scrollX - tip.offsetWidth - 6;
    tip.style.left = Math.max(minLeft, Math.min(left, maxLeft)) + "px";
    var minTop = Math.max(r.top + window.scrollY + 6, window.scrollY + 6);
    var maxTop = window.scrollY + window.innerHeight - tip.offsetHeight - 8;
    tip.style.top = Math.max(minTop, Math.min(top, maxTop)) + "px";
  }

  function showTip(c) {
    if (!tip || !c) return;
    var st = String(c.status || "").toUpperCase();
    var col = statusColor(P(), c.status);
    var tipKey = [c.uid, c.label, c.detail, c.status, rel(c.ts), col].join("|");
    if (tip.dataset.key !== tipKey) {
      tip.innerHTML =
        '<div class="hw-tip-head"><span class="hw-dot" style="background:' + col + ';color:' + col + '"></span>' +
        esc(c.label) + '</div>' +
        '<div class="hw-tip-row">' + esc(c.detail || "") + '</div>' +
        '<div class="hw-tip-row hw-tip-dim">' + (c.kind === "qg" ? "Quality Gate" : "Test Suite") +
        ' · ' + esc(st) + ' · ' + esc(rel(c.ts)) + '</div>' +
        (c.href ? '<div class="hw-tip-cta">SELECT TO OPEN RUN ↗</div>' : '');
      tip.style.setProperty("--tip-color", col);
      tip.dataset.key = tipKey;
    }
    tip.style.display = "block";
    tip.setAttribute("aria-hidden", "false");
    positionTip(c);
  }

  function hideTip() {
    if (!tip) return;
    tip.style.display = "none";
    tip.setAttribute("aria-hidden", "true");
  }

  function hitTest() {
    hovered = mouseX < 0 ? null : pickCar(mouseX, mouseY);
    canvas.classList.toggle("is-targeting", !!hovered);
    if (hovered) showTip(hovered);
    else if (keyboardCar && document.activeElement === canvas) showTip(keyboardCar);
    else hideTip();
  }

  function openCar(c) {
    if (!c || !c.href) return;
    window.location.assign(c.href);
  }

  function selectKeyboardCar(direction) {
    var selectable = cars.filter(function (c) { return c.href && !c._dead; });
    selectable.sort(function (a, b) { return a.x - b.x; });
    if (!selectable.length) return;
    var idx = keyboardCar ? selectable.indexOf(keyboardCar) : -1;
    idx = idx < 0 ? (direction < 0 ? selectable.length - 1 : 0) : (idx + direction + selectable.length) % selectable.length;
    keyboardCar = selectable[idx];
    showTip(keyboardCar);
    if (announcer) announcer.textContent = keyboardCar.label + ", " + keyboardCar.status + ", Enter 키로 상세 열기";
  }

  // ---- 데이터 ----------------------------------------------------------
  function setNetworkState(state) {
    if (!linkStatus || !linkLabel) return;
    linkStatus.classList.toggle("is-offline", state === "offline");
    linkStatus.classList.toggle("is-connecting", state === "connecting" || state === "stale");
    linkLabel.textContent = state === "online" ? "LIVE FEED" : state === "offline" ? "LINK LOST" : state === "stale" ? "STALE LINK" : "CONNECTING";
  }

  function setEmptyState(state) {
    if (!empty) return;
    var title = empty.querySelector("strong");
    var copy = empty.querySelector("span:not(.hw-empty-mark)");
    empty.style.display = state === "hidden" ? "none" : "flex";
    if (state === "loading") {
      if (title) title.textContent = "SYNCHRONIZING GRID";
      if (copy) copy.textContent = "실시간 Test Suite 텔레메트리를 연결하고 있습니다.";
    } else if (state === "error") {
      if (title) title.textContent = "TELEMETRY LINK LOST";
      if (copy) copy.textContent = "연결을 복구하는 중입니다. 잠시 후 자동으로 재시도합니다.";
    } else {
      if (title) title.textContent = "THE GRID IS CLEAR";
      if (copy) copy.textContent = "Test Suite를 실행하면 첫 번째 머신이 출발합니다.";
    }
  }

  function updateCommandHUD() {
    var counts = { pass: 0, fail: 0, running: 0, skip: 0 };
    for (var i = 0; i < pool.length; i++) {
      if (counts[pool[i].status] !== undefined) counts[pool[i].status]++;
    }
    Object.keys(counts).forEach(function (key) {
      if (statEls[key] && statEls[key].textContent !== String(counts[key])) statEls[key].textContent = counts[key];
    });
    if (statEls.total) {
      var total = String(pool.length).padStart(2, "0");
      if (statEls.total.textContent !== total) statEls.total.textContent = total;
    }

    if (!feedEl) return;
    var latest = pool.slice().sort(function (a, b) { return (b.ts || 0) - (a.ts || 0); }).slice(0, 4);
    feedEl.textContent = "";
    if (!latest.length) {
      var placeholder = document.createElement("li");
      placeholder.className = "hw-feed-placeholder";
      placeholder.textContent = hasLoaded ? "No session entries yet." : "Awaiting circuit data…";
      feedEl.appendChild(placeholder);
      return;
    }
    latest.forEach(function (rec) {
      var li = document.createElement("li");
      var a = document.createElement("a");
      a.href = runHref(rec) || "#";
      if (!runHref(rec)) a.setAttribute("aria-disabled", "true");
      var dot = document.createElement("i"); dot.className = "hw-feed-dot hw-feed-" + (rec.status || "cancelled");
      var copy = document.createElement("span"); copy.className = "hw-feed-copy";
      var label = document.createElement("b"); label.textContent = rec.label || "Unnamed run";
      var meta = document.createElement("small"); meta.textContent = (rec.detail || "Test Suite") + " · " + rel(rec.ts);
      var status = document.createElement("span"); status.className = "hw-feed-status hw-feed-" + (rec.status || "cancelled"); status.textContent = String(rec.status || "unknown").toUpperCase();
      copy.appendChild(label); copy.appendChild(meta);
      a.appendChild(dot); a.appendChild(copy); a.appendChild(status);
      li.appendChild(a); feedEl.appendChild(li);
    });
  }

  function addToPool(runs, asFresh) {
    var added = 0;
    for (var i = 0; i < runs.length; i++) {
      var r = runs[i];
      if (!r || !r.id) continue;
      var existing = seenIds[r.id];
      if (existing) {
        // 이미 아는 run 이 새 상태로 재방출됨(running→pass/fail 등) → 풀 레코드 갱신
        var oldStatus = existing.status;
        existing.status = r.status; existing.detail = r.detail; existing.ts = r.ts;
        existing.label = r.label || existing.label; existing.kind = r.kind || existing.kind;
        existing.url = r.url || existing.url;
        // 화면 위 live 머신도 즉시 동기화한다. FAIL 전환이면 현재 위치 이후에 사고가 시작된다.
        for (var ci = 0; ci < cars.length; ci++) {
          if (cars[ci].id !== r.id) continue;
          cars[ci].status = existing.status; cars[ci].detail = existing.detail; cars[ci].label = existing.label;
          cars[ci].ts = existing.ts; cars[ci].kind = existing.kind; cars[ci].href = runHref(existing);
          if (oldStatus !== "fail" && existing.status === "fail") {
            if (cars[ci].state === "pit") startCrash(cars[ci], false);
            else if (cars[ci].state === "drive") {
              cars[ci].crashAt = Math.min(W * 0.82, Math.max(cars[ci].x + W * 0.12, W * 0.46));
            }
          }
          if (oldStatus !== "skip" && existing.status === "skip" && cars[ci].state === "drive") {
            cars[ci].pitAt = Math.min(W * 0.84, Math.max(cars[ci].x + W * 0.1, W * 0.54));
          }
          if (oldStatus === "fail" && existing.status === "skip" && cars[ci].state !== "drive") {
            cars[ci].state = "pit"; cars[ci].t = 0; cars[ci].truck = null; cars[ci].towPhase = null;
            cars[ci].speed = 42; cars[ci].angle = 0;
            cars[ci].pitTop = cars[ci].y < H / 2;
            cars[ci].pitY = cars[ci].pitTop ? Math.max(18, roadTop() - 28) : Math.min(H - 24, roadTop() + roadH() + 30);
          }
        }
        continue;
      }
      r._fresh = !!asFresh;
      seenIds[r.id] = r;            // boolean 대신 레코드 참조 저장(재방출 시 갱신용)
      pool.push(r);
      added++;
    }
    // 장기 상시 화면에서 무한히 증가하지 않도록 최신 80개만 유지.
    if (pool.length > 80) {
      pool.sort(function (a, b) { return (a.ts || 0) - (b.ts || 0); });
      while (pool.length > 80) {
        var dropIndex = -1;
        for (var pi = 0; pi < pool.length; pi++) {
          if (!isRecordOnRoad(pool[pi].id)) { dropIndex = pi; break; }
        }
        // 화면 위 차량의 record 참조는 상태 재전송에 필요하다. 보통 active 차량은
        // 80대보다 훨씬 적지만, 전부 active라면 다음 프레임까지 cap을 잠시 초과한다.
        if (dropIndex < 0) break;
        var dropped = pool.splice(dropIndex, 1)[0];
        if (dropped) delete seenIds[dropped.id];
      }
      poolIdx = poolIdx % Math.max(1, pool.length);
    }
    updateCommandHUD();
    return added;
  }

  function replacePool(runs) {
    // stale cursor 재시드는 이전 세션과 최신 snapshot을 섞지 않는다. 화면 위 차량도
    // 함께 비워 같은 id가 old/new record로 동시에 주행하는 상황을 막는다.
    cars = []; smoke = []; skids = []; pool = []; seenIds = {}; poolIdx = 0;
    hovered = null; keyboardCar = null; hideTip();
    return addToPool(runs, false);
  }

  function fetchJSON(url) {
    return fetch(url, {
      headers: { "X-Requested-With": "fetch", "Accept": "application/json" },
      credentials: "same-origin",
    }).then(function (response) {
      var type = response.headers.get("content-type") || "";
      if (!response.ok) throw new Error("HTTP " + response.status);
      if (response.redirected && response.url.indexOf("/visualization/data") === -1) throw new Error("unexpected redirect");
      if (type.indexOf("application/json") === -1) throw new Error("invalid content type");
      return response.json();
    });
  }

  function syncSucceeded(d, announceNew) {
    networkOk = true; hasLoaded = true; lastSyncAt = Date.now();
    setNetworkState("online");
    if (lastSyncEl) lastSyncEl.textContent = new Date(lastSyncAt).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" });
    if (d.server_ts) pollCursor = Number(d.server_ts) || pollCursor;
    updateCommandHUD();
    setEmptyState(pool.length ? "hidden" : "empty");
    if (announceNew > 0 && announcer) announcer.textContent = "새 Test Suite 실행 " + announceNew + "건이 서킷에 합류했습니다.";
  }

  function syncFailed() {
    networkOk = false; hasLoaded = true;
    setNetworkState("offline");
    if (lastSyncEl) lastSyncEl.textContent = lastSyncAt ? "LAST " + new Date(lastSyncAt).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" }) : "RETRYING…";
    if (!pool.length) setEmptyState("error");
    updateCommandHUD();
  }

  function prime() {
    if (syncInFlight) return;
    syncInFlight = true;
    setNetworkState("connecting"); setEmptyState("loading");
    fetchJSON(dataPrime)
      .then(function (d) {
        addToPool((d.runs || []).slice().reverse(), false); // 오래된→새 순으로 풀에 적재
        syncSucceeded(d, 0);
      })
      .catch(syncFailed)
      .finally(function () { syncInFlight = false; });
  }

  function poll() {
    if (document.hidden || syncInFlight) return;   // 탭 비가시/이전 요청 중이면 중복 폴링 생략
    syncInFlight = true;
    // 서버 query snapshot과 commit 사이 경계 이벤트를 놓치지 않도록 짧게 replay한다.
    // addToPool은 id 기준 멱등 갱신이라 중복 응답이 차량/통계를 늘리지 않는다.
    var after = Math.max(1, (pollCursor || 0) - POLL_REPLAY_SECONDS);
    var url = dataUrl + (dataUrl.indexOf("?") >= 0 ? "&" : "?") + "after=" + after;
    fetchJSON(url)
      .then(function (d) {
        var n;
        if (d.reset) {
          n = replacePool((d.runs || []).slice().reverse()); // seed는 newest-first
        } else {
          n = addToPool(d.runs || [], true);                 // incremental은 oldest-first
        }
        syncSucceeded(d, d.reset ? 0 : n);
        // 새 run 은 곧바로 등장하도록 스폰 큐 앞쪽 효과(다음 스폰 가속)
        if (n > 0) nextSpawn = Math.min(nextSpawn, 0.25);
      })
      .catch(syncFailed)
      .finally(function () { syncInFlight = false; });
  }

  // ---- 리사이즈 / 헬퍼 -------------------------------------------------
  function resize() {
    var rect = canvas.getBoundingClientRect();
    if (!rect.width || !rect.height) return;
    var oldW = W, oldH = H;
    W = Math.round(rect.width * 100) / 100; H = Math.round(rect.height * 100) / 100;
    DPR = Math.min(2.5, Math.max(1, window.devicePixelRatio || 1));
    canvas.width = Math.round(W * DPR); canvas.height = Math.round(H * DPR);
    ctx.setTransform(DPR, 0, 0, DPR, 0, 0);
    LANES = W < 560 ? 3 : 4;
    var scaleX = oldW ? W / oldW : 1;
    var scaleY = oldH ? H / oldH : 1;
    // 진행률과 오프로드 위치를 새 viewport에 맞춰 보존한다.
    for (var i = 0; i < cars.length; i++) {
      cars[i].x *= scaleX;
      cars[i].crashAt *= scaleX;
      cars[i].pitAt *= scaleX;
      if (cars[i].lane >= LANES) cars[i].lane = LANES - 1;
      cars[i].crashAt = Math.min(cars[i].crashAt, W * 0.72);
      if (cars[i].state === "drive") cars[i].y = laneY(cars[i].lane);
      else cars[i].y *= scaleY;
      if (cars[i].state === "pit") cars[i].pitY = cars[i].pitTop ? Math.max(18, roadTop() - 28) : Math.min(H - 24, roadTop() + roadH() + 30);
      if (cars[i].truck) { cars[i].truck.x *= scaleX; cars[i].truck.y *= scaleY; }
    }
    lastPaint = 0;
  }

  function scheduleResize() {
    if (resizeRaf) cancelAnimationFrame(resizeRaf);
    resizeRaf = requestAnimationFrame(function () { resizeRaf = 0; resize(); });
  }

  function roundRect(x, y, w, h, r) {
    r = Math.min(r, w / 2, h / 2);
    ctx.beginPath();
    ctx.moveTo(x + r, y);
    ctx.arcTo(x + w, y, x + w, y + h, r);
    ctx.arcTo(x + w, y + h, x, y + h, r);
    ctx.arcTo(x, y + h, x, y, r);
    ctx.arcTo(x, y, x + w, y, r);
    ctx.closePath();
  }
  function hexA(hex, a) {
    var c = hexToRgb(hex); return "rgba(" + c.r + "," + c.g + "," + c.b + "," + a + ")";
  }
  function lighten(hex, f) { return mix(hex, "#ffffff", f); }
  function darken(hex, f) { return mix(hex, "#000000", f); }
  function mix(a, b, f) {
    var x = hexToRgb(a), y = hexToRgb(b);
    return "rgb(" + Math.round(x.r + (y.r - x.r) * f) + "," + Math.round(x.g + (y.g - x.g) * f) + "," + Math.round(x.b + (y.b - x.b) * f) + ")";
  }
  function hexToRgb(h) {
    h = (h || "#888888").replace("#", "");
    if (h.length === 3) h = h[0] + h[0] + h[1] + h[1] + h[2] + h[2];
    var n = parseInt(h, 16);
    if (isNaN(n)) return { r: 136, g: 136, b: 136 };
    return { r: (n >> 16) & 255, g: (n >> 8) & 255, b: n & 255 };
  }
  function esc(s) { return (s == null ? "" : String(s)).replace(/[&<>"]/g, function (m) { return ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" })[m]; }); }
  function rel(ts) {
    if (!ts) return "";
    var s = Math.max(0, Math.floor(Date.now() / 1000 - ts));
    if (s < 60) return s + "초 전";
    if (s < 3600) return Math.floor(s / 60) + "분 전";
    if (s < 86400) return Math.floor(s / 3600) + "시간 전";
    return Math.floor(s / 86400) + "일 전";
  }

  // ---- init ------------------------------------------------------------
  NS.init = function (opts) {
    opts = opts || {};
    canvas = document.getElementById(opts.canvasId);
    if (!canvas || canvas.dataset.hwReady === "1") return;
    ctx = canvas.getContext("2d");
    if (!ctx) return;
    canvas.dataset.hwReady = "1";
    tip = document.getElementById(opts.tipId);
    empty = document.getElementById(opts.emptyId);
    stage = document.getElementById(opts.stageId) || canvas.parentElement;
    dataUrl = opts.dataUrl;
    dataPrime = opts.dataUrl;
    runUrlTemplate = opts.runUrlTemplate || null;

    pauseBtn = document.getElementById("hw-pause");
    linkStatus = document.getElementById("hw-link-status");
    linkLabel = document.getElementById("hw-link-label");
    lastSyncEl = document.getElementById("hw-last-sync");
    motionModeEl = document.getElementById("hw-motion-mode");
    announcer = document.getElementById("hw-announcer");
    feedEl = document.getElementById("hw-feed");
    statEls = {
      pass: document.getElementById("hw-stat-pass"),
      fail: document.getElementById("hw-stat-fail"),
      running: document.getElementById("hw-stat-running"),
      skip: document.getElementById("hw-stat-skip"),
      total: document.getElementById("hw-stat-total"),
    };

    // OS 설정을 즉시 반영하고 런타임 변경도 추적한다.
    var motionQuery = window.matchMedia ? window.matchMedia("(prefers-reduced-motion: reduce)") : null;
    function syncMotionPreference() {
      reducedMotion = !!(motionQuery && motionQuery.matches);
      if (motionModeEl) motionModeEl.textContent = reducedMotion ? "REDUCED MOTION" : userPaused ? "PAUSED" : "FULL MOTION";
      lastFrame = 0; lastPaint = 0;
    }
    syncMotionPreference();
    if (motionQuery) {
      if (motionQuery.addEventListener) motionQuery.addEventListener("change", syncMotionPreference);
      else if (motionQuery.addListener) motionQuery.addListener(syncMotionPreference);
    }

    if (pauseBtn) {
      pauseBtn.addEventListener("click", function () {
        userPaused = !userPaused;
        pauseBtn.setAttribute("aria-pressed", userPaused ? "true" : "false");
        pauseBtn.setAttribute("aria-label", userPaused ? "애니메이션 재생" : "애니메이션 일시정지");
        pauseBtn.title = userPaused ? "애니메이션 재생" : "애니메이션 일시정지";
        var icon = pauseBtn.querySelector("i");
        if (icon) icon.className = userPaused ? "fas fa-play" : "fas fa-pause";
        if (motionModeEl) motionModeEl.textContent = userPaused ? "PAUSED" : reducedMotion ? "REDUCED MOTION" : "FULL MOTION";
        if (announcer) announcer.textContent = userPaused ? "Quality Highway 애니메이션이 일시정지되었습니다." : "Quality Highway 애니메이션이 재생됩니다.";
        lastFrame = 0; lastPaint = 0;
      });
    }

    // 선택적 art plate. 실패해도 procedural scene이 동일한 기능을 제공한다.
    if (opts.backgroundUrl) {
      background = new Image();
      background.decoding = "async";
      background.onload = function () { backgroundReady = true; lastPaint = 0; };
      background.onerror = function () { backgroundReady = false; background = null; };
      background.src = opts.backgroundUrl;
    }

    resize();
    window.addEventListener("resize", scheduleResize, { passive: true });
    if (window.ResizeObserver && stage) {
      resizeObserver = new ResizeObserver(scheduleResize);
      resizeObserver.observe(stage);
    }

    canvas.addEventListener("pointermove", function (e) {
      var r = canvas.getBoundingClientRect();
      mouseX = e.clientX - r.left; mouseY = e.clientY - r.top;
      if (userPaused) hitTest();
    });
    canvas.addEventListener("pointerleave", function () {
      mouseX = mouseY = -1; hovered = null; canvas.classList.remove("is-targeting");
      if (!keyboardCar || document.activeElement !== canvas) hideTip();
    });
    canvas.addEventListener("click", function (e) {
      var r = canvas.getBoundingClientRect();
      var target = pickCar(e.clientX - r.left, e.clientY - r.top);
      if (target) { keyboardCar = target; openCar(target); }
    });
    canvas.addEventListener("keydown", function (e) {
      if (e.key === "ArrowRight" || e.key === "ArrowDown") {
        e.preventDefault(); selectKeyboardCar(1);
      } else if (e.key === "ArrowLeft" || e.key === "ArrowUp") {
        e.preventDefault(); selectKeyboardCar(-1);
      } else if (e.key === "Enter" || e.key === " ") {
        if (keyboardCar) { e.preventDefault(); openCar(keyboardCar); }
      } else if (e.key === "Escape") {
        keyboardCar = null; hideTip();
      }
    });
    canvas.addEventListener("focus", function () { if (!keyboardCar) selectKeyboardCar(1); });
    canvas.addEventListener("blur", function () { if (!hovered) hideTip(); });

    // 테마를 pause 상태에서도 즉시 다시 그릴 수 있도록 paint를 invalidate한다.
    if (window.MutationObserver) {
      new MutationObserver(function () { lastPaint = 0; }).observe(document.documentElement, { attributes: true, attributeFilter: ["data-theme"] });
    }

    // 탭 비가시 시 일시정지(자원 절약)
    document.addEventListener("visibilitychange", function () {
      if (document.hidden) {
        running = false;
        if (frameRaf) { cancelAnimationFrame(frameRaf); frameRaf = 0; }
      }
      else if (!running) {
        running = true; lastFrame = 0; lastPaint = 0; requestFrame(); poll();
      }
    });

    // 초기 데이터: 서버가 seed 를 임베드했으면 그걸 쓰고(첫 fetch 생략), 없으면 prime 으로 가져온다.
    if (opts.seed && opts.seed.length) {
      addToPool(opts.seed.slice().reverse(), false); // newest-first → oldest-first 로 적재
      syncSucceeded({}, 0);
    } else {
      prime();
    }

    var pollMs = opts.pollMs || 6000;
    setInterval(poll, pollMs);
    setInterval(function () {
      if (networkOk && lastSyncAt && Date.now() - lastSyncAt > pollMs * 2.5) setNetworkState("stale");
    }, Math.max(4000, pollMs));
    lastFrame = 0;
    lastPaint = 0;
    requestFrame();
  };
})();
