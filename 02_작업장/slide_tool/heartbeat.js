// 브라우저 창의 생존 여부만 서버에 알린다. 도구 데이터에는 접근하지 않는다.
(function () {
  'use strict';

  var active = true;
  var timer = null;
  var INTERVAL = 4000;

  function disable() {
    active = false;
    if (timer !== null) {
      window.clearInterval(timer);
      timer = null;
    }
  }

  function ping() {
    if (!active) return;
    fetch('/heartbeat', {
      method: 'POST',
      cache: 'no-store',
      keepalive: true
    }).then(function (response) {
      // heartbeat 엔드포인트가 없는 서버에서는 조용히 멈춘다.
      if (!response.ok) disable();
    }).catch(function () {
      disable();
    });
  }

  function goodbye() {
    if (!active || !navigator.sendBeacon) return;
    navigator.sendBeacon('/goodbye');
  }

  // 첫 실패 뒤에는 반복 요청과 콘솔 오류를 만들지 않는다.
  ping();
  timer = window.setInterval(ping, INTERVAL);
  window.addEventListener('beforeunload', goodbye);
  window.addEventListener('pagehide', goodbye);

  // visibilitychange는 다른 탭을 보는 정상 작업이므로 종료 신호로 쓰지 않는다.
})();
