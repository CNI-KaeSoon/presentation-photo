(function () {
  'use strict';

  var TOKEN = null;
  var POLL_MS = 700;
  var COLLAPSE_KEY = 'wfPanelCollapsed_v1';
  var state = {
    status: null,
    job: null,
    uploading: false,
    uploadPercent: 0,
    uploadLabel: '',
    logs: [],
    after: 0,
    pollTimer: null,
    activeKind: null,
    cancelling: false,
    collapsed: false,
    collapseReady: false,
    disabled: false,
    dndBound: false
  };
  var ui = {};

  function el(tag, attrs, children) {
    var node = document.createElement(tag);
    var key;
    attrs = attrs || {};
    for (key in attrs) {
      if (!Object.prototype.hasOwnProperty.call(attrs, key)) continue;
      if (key === 'className') node.className = attrs[key];
      else if (key === 'text') node.textContent = attrs[key];
      else if (key === 'checked') node.checked = !!attrs[key];
      else if (key === 'disabled') node.disabled = !!attrs[key];
      else node.setAttribute(key, attrs[key]);
    }
    (children || []).forEach(function (child) {
      if (child == null) return;
      node.appendChild(typeof child === 'string' ? document.createTextNode(child) : child);
    });
    return node;
  }

  function setText(node, value) {
    if (node) node.textContent = value == null ? '' : String(value);
  }

  function injectStyle() {
    if (document.getElementById('wfStyle')) return;
    var style = el('style', {id: 'wfStyle'});
    style.textContent = [
      '#wfPanel{margin-top:0;border-color:#bfd0e8}',
      '#wfPanel[hidden]{display:none}',
      '.wfHead{display:flex;align-items:flex-start;justify-content:space-between;gap:12px;flex-wrap:wrap}',
      '.wfTitle{display:flex;align-items:center;gap:9px;flex-wrap:wrap}',
      '.wfTitle h2{font-size:17px;margin:0}',
      '.wfSteps{display:flex;align-items:center;gap:5px;flex-wrap:wrap;margin-top:9px}',
      '.wfStep{border:1px solid var(--line);border-radius:18px;padding:3px 9px;font-size:11px;color:var(--mut);background:var(--panel2)}',
      '.wfStep.active{border-color:var(--acc);color:var(--acc2);background:#eff6ff;font-weight:800}',
      '.wfStep.done{border-color:#bbf7d0;color:var(--ok);background:#ecfdf3}',
      '.wfSummary{font-size:12px;color:var(--mut);margin-top:5px}',
      '.wfBody{margin-top:12px;border-top:1px solid var(--line)}',
      '#wfPanel.wfCollapsed .wfBody{display:none}',
      '.wfRow{display:grid;grid-template-columns:minmax(120px,.28fr) minmax(280px,1fr);gap:14px;padding:13px 0;border-bottom:1px solid var(--line)}',
      '.wfRow h3{font-size:13px;margin:1px 0 3px}',
      '.wfRowInfo{font-size:12px;color:var(--mut)}',
      '.wfDrop{border:2px dashed #9bb4d5;border-radius:8px;background:#f8fafc;padding:18px;text-align:center;cursor:pointer;transition:.15s}',
      '.wfDrop:hover,.wfDrop.dragover{border-color:var(--acc);background:#eff6ff}',
      '.wfDrop strong{display:block;color:var(--acc2);font-size:13px}',
      '.wfDrop span{display:block;color:var(--mut);font-size:11px;margin-top:3px}',
      '.wfControls{display:flex;align-items:center;gap:8px;flex-wrap:wrap;margin-top:8px}',
      '.wfControls label{font-size:12px;color:var(--mut);display:flex;align-items:center;gap:5px}',
      '.wfControls input[type=number]{width:72px;border:1px solid var(--line);border-radius:5px;padding:5px 7px}',
      '.wfCounts{font-size:12px;margin-top:7px;color:var(--ink)}',
      '.wfEnv{font-size:11px;color:var(--mut);margin-top:5px}',
      '.wfBanner{display:none;margin-top:11px;padding:9px 11px;border-radius:6px;font-size:12px;white-space:pre-wrap}',
      '.wfBanner.show{display:block}',
      '.wfBanner.info{background:#eff6ff;color:var(--acc2);border:1px solid #bfdbfe}',
      '.wfBanner.ok{background:#ecfdf3;color:var(--ok);border:1px solid #bbf7d0}',
      '.wfBanner.warn{background:#fffbeb;color:var(--warn);border:1px solid #fde68a}',
      '.wfBanner.error{background:#fef2f2;color:var(--bad);border:1px solid #fecaca}',
      '.wfProgress{display:none;margin-top:11px;padding:10px;border:1px solid var(--line);border-radius:7px;background:var(--panel2)}',
      '.wfProgress.show{display:block}',
      '.wfProgressHead{display:flex;align-items:center;justify-content:space-between;gap:8px;flex-wrap:wrap}',
      '.wfBar{height:7px;background:#dbe4f0;border-radius:8px;overflow:hidden;margin:8px 0}',
      '.wfBarFill{height:100%;width:0;background:var(--acc);transition:width .2s}',
      '.wfLogTail,.wfLogAll{margin:7px 0 0;white-space:pre-wrap;overflow-wrap:anywhere;font:11px ui-monospace,SFMono-Regular,Menlo,monospace;color:#334155}',
      '.wfLogAll{max-height:260px;overflow:auto}',
      '.wfDetails summary{cursor:pointer;color:var(--acc2);font-size:11px;margin-top:6px}',
      '.wfUploadResults{margin-top:7px;white-space:pre-wrap;font-size:11px;color:var(--mut)}',
      '.wfModalBack{display:none;position:fixed;inset:0;z-index:120;background:rgba(15,23,42,.48);align-items:center;justify-content:center;padding:20px}',
      '.wfModalBack.show{display:flex}',
      '.wfModal{max-width:520px;width:100%;background:var(--panel);border-radius:10px;border:1px solid var(--line);box-shadow:0 18px 50px rgba(15,23,42,.28);padding:18px}',
      '.wfModal h2{font-size:17px;margin:0 0 9px}',
      '.wfModal p{font-size:13px;margin:7px 0}',
      '.wfModalActions{display:flex;justify-content:flex-end;gap:8px;margin-top:15px}',
      '@media(max-width:760px){.wfRow{grid-template-columns:1fr}.wfDrop{padding:13px}}'
    ].join('\n');
    document.head.appendChild(style);
  }

  function buildPanel() {
    var main = document.getElementById('main');
    if (!main) return false;

    ui.panel = el('section', {id: 'wfPanel', className: 'panel'});
    ui.panel.hidden = true;
    var head = el('div', {className: 'wfHead'});
    var headLeft = el('div');
    var title = el('div', {className: 'wfTitle'}, [
      el('h2', {text: '📋 작업 순서'})
    ]);
    ui.summary = el('div', {className: 'wfSummary', text: '서버 연결을 확인하는 중입니다.'});
    ui.steps = [
      el('span', {className: 'wfStep', text: '① 사진 넣기'}),
      el('span', {className: 'wfStep', text: '② 사진 준비'}),
      el('span', {className: 'wfStep', text: '③ 모서리·색 보정'}),
      el('span', {className: 'wfStep', text: '④ PDF'})
    ];
    headLeft.appendChild(title);
    headLeft.appendChild(el('div', {className: 'wfSteps'}, ui.steps));
    headLeft.appendChild(ui.summary);
    ui.collapseBtn = el('button', {type: 'button', className: 'btn sm', text: '접기'});
    ui.collapseBtn.addEventListener('click', function () {
      setCollapsed(!state.collapsed, true);
    });
    head.appendChild(headLeft);
    head.appendChild(ui.collapseBtn);
    ui.panel.appendChild(head);

    ui.banner = el('div', {className: 'wfBanner', role: 'status'});
    ui.panel.appendChild(ui.banner);

    ui.body = el('div', {className: 'wfBody'});
    ui.panel.appendChild(ui.body);

    var fileInfo = el('div', {}, [
      el('h3', {text: '① 사진 넣기'}),
      el('div', {className: 'wfRowInfo', text: '원본은 보존되며 작업용 사진은 다음 단계에서 만듭니다.'})
    ]);
    var fileWork = el('div');
    ui.drop = el('div', {className: 'wfDrop', role: 'button', tabindex: '0'}, [
      el('strong', {text: '여기에 사진을 끌어다 놓으세요'}),
      el('span', {text: 'JPG · PNG · HEIC 등 파일 단위로 업로드합니다.'})
    ]);
    ui.fileInput = el('input', {
      type: 'file',
      multiple: 'multiple',
      accept: '.jpg,.jpeg,.png,.heic,.heif,.tif,.tiff,.bmp,.webp'
    });
    ui.fileInput.hidden = true;
    ui.drop.addEventListener('click', function () { ui.fileInput.click(); });
    ui.drop.addEventListener('keydown', function (event) {
      if (event.key === 'Enter' || event.key === ' ') {
        event.preventDefault();
        ui.fileInput.click();
      }
    });
    ui.fileInput.addEventListener('change', function () {
      uploadFiles(ui.fileInput.files);
      ui.fileInput.value = '';
    });
    ui.openSrcBtn = el('button', {type: 'button', className: 'btn', text: '사진 폴더 열기'});
    ui.openSrcBtn.addEventListener('click', function () { openFolder('src'); });
    ui.srcCount = el('span', {className: 'wfCounts', text: '현재 원본 0장'});
    fileWork.appendChild(ui.drop);
    fileWork.appendChild(ui.fileInput);
    fileWork.appendChild(el('div', {className: 'wfControls'}, [
      ui.openSrcBtn,
      el('span', {className: 'muted', text: '대용량·폴더 단위 복사는 이 버튼으로 폴더를 연 뒤 넣으세요.'})
    ]));
    fileWork.appendChild(ui.srcCount);
    ui.uploadResults = el('div', {className: 'wfUploadResults'});
    fileWork.appendChild(ui.uploadResults);
    ui.body.appendChild(el('div', {className: 'wfRow'}, [fileInfo, fileWork]));

    var prepareInfo = el('div', {}, [
      el('h3', {text: '② 사진 준비'}),
      el('div', {className: 'wfRowInfo', text: '첫 사진의 촬영 시각을 기준으로, 촬영 간격이 이 값보다 벌어지는 지점을 발표의 경계로 보고 그룹을 나눕니다. 이어서 작업용 축소본과 목록을 만듭니다.'})
    ]);
    var prepareWork = el('div');
    ui.gap = el('input', {type: 'number', min: '1', max: '600', value: '20', inputmode: 'numeric'});
    ui.prepareBtn = el('button', {type: 'button', className: 'btn on', text: '사진 준비 실행'});
    ui.regroupBtn = el('button', {type: 'button', className: 'btn warn', text: '다시 나누기…'});
    ui.prepareBtn.addEventListener('click', function () { runPrepare(false); });
    ui.regroupBtn.addEventListener('click', confirmRegroup);
    prepareWork.appendChild(el('div', {className: 'wfControls'}, [
      el('label', {}, [document.createTextNode('발표 간격(분)'), ui.gap]),
      ui.prepareBtn,
      ui.regroupBtn
    ]));
    ui.groupInfo = el('div', {className: 'wfCounts', text: '그룹 계획 없음'});
    ui.envInfo = el('div', {className: 'wfEnv', text: '환경 상태 확인 중'});
    prepareWork.appendChild(ui.groupInfo);
    prepareWork.appendChild(ui.envInfo);
    ui.body.appendChild(el('div', {className: 'wfRow'}, [prepareInfo, prepareWork]));

    var exportInfo = el('div', {}, [
      el('h3', {text: '④ PDF 만들기'}),
      el('div', {className: 'wfRowInfo', text: '현재 모서리·색 보정값을 원본 사진에 적용해 고해상도 PDF를 만듭니다.'})
    ]);
    var exportWork = el('div');
    ui.onlyDone = el('input', {type: 'checkbox'});
    ui.exportBtn = el('button', {type: 'button', className: 'btn on', text: 'PDF 만들기(고해상도)'});
    ui.openOutBtn = el('button', {type: 'button', className: 'btn', text: '결과 폴더 열기'});
    ui.exportBtn.addEventListener('click', runExportPdf);
    ui.openOutBtn.addEventListener('click', function () { openFolder('out'); });
    exportWork.appendChild(el('div', {className: 'wfControls'}, [
      ui.exportBtn,
      el('label', {}, [ui.onlyDone, document.createTextNode('완료본만')]),
      ui.openOutBtn
    ]));
    ui.resultInfo = el('div', {className: 'wfCounts', text: '현재 결과 PDF 0권'});
    exportWork.appendChild(ui.resultInfo);
    exportWork.appendChild(el('div', {
      className: 'wfEnv',
      text: '기존 “백업 내보내기” 다운로드도 오프라인 백업용으로 계속 사용할 수 있습니다.'
    }));
    ui.body.appendChild(el('div', {className: 'wfRow'}, [exportInfo, exportWork]));

    ui.progress = el('div', {className: 'wfProgress'});
    ui.progressLabel = el('strong', {text: '대기'});
    ui.cancelBtn = el('button', {type: 'button', className: 'btn sm warn', text: '작업 취소'});
    ui.cancelBtn.addEventListener('click', cancelJob);
    ui.progress.appendChild(el('div', {className: 'wfProgressHead'}, [ui.progressLabel, ui.cancelBtn]));
    ui.barFill = el('div', {className: 'wfBarFill'});
    ui.progress.appendChild(el('div', {className: 'wfBar'}, [ui.barFill]));
    ui.logTail = el('pre', {className: 'wfLogTail'});
    ui.progress.appendChild(ui.logTail);
    ui.logAll = el('pre', {className: 'wfLogAll'});
    var details = el('details', {className: 'wfDetails'}, [
      el('summary', {text: '전체 로그 펼치기'}),
      ui.logAll
    ]);
    ui.progress.appendChild(details);
    ui.body.appendChild(ui.progress);

    main.prepend(ui.panel);
    buildModal();
    return true;
  }

  function buildModal() {
    ui.modalBack = el('div', {className: 'wfModalBack', role: 'dialog', 'aria-modal': 'true'});
    var modal = el('div', {className: 'wfModal'});
    modal.appendChild(el('h2', {text: '그룹 다시 나누기'}));
    modal.appendChild(el('p', {text: '다시 나누면 그룹 이름이 바뀔 수 있습니다.'}));
    modal.appendChild(el('p', {}, [
      el('strong', {text: '이미 보정한 작업의 저장 키가 어긋날 수 있습니다.'}),
      document.createTextNode(' 필요한 백업을 먼저 내려받았는지 확인하세요.')
    ]));
    var cancel = el('button', {type: 'button', className: 'btn', text: '취소'});
    ui.modalOk = el('button', {type: 'button', className: 'btn warn on', text: '다시 나누기'});
    cancel.addEventListener('click', closeModal);
    ui.modalOk.addEventListener('click', function () {
      closeModal();
      runPrepare(true);
    });
    modal.appendChild(el('div', {className: 'wfModalActions'}, [cancel, ui.modalOk]));
    ui.modalBack.appendChild(modal);
    ui.modalBack.addEventListener('click', function (event) {
      if (event.target === ui.modalBack) closeModal();
    });
    document.addEventListener('keydown', function (event) {
      if (event.key === 'Escape' && ui.modalBack.classList.contains('show')) closeModal();
    });
    document.body.appendChild(ui.modalBack);
  }

  function confirmRegroup() {
    if (state.disabled || isBusy()) return;
    ui.modalBack.classList.add('show');
    ui.modalOk.focus();
  }

  function closeModal() {
    ui.modalBack.classList.remove('show');
  }

  function api(path, opts) {
    opts = opts || {};
    var headers = new Headers(opts.headers || {});
    var init = {
      method: opts.method || 'GET',
      headers: headers,
      cache: 'no-store'
    };
    if (TOKEN && path !== '/api/token') headers.set('X-Workflow-Token', TOKEN);
    if (Object.prototype.hasOwnProperty.call(opts, 'json')) {
      headers.set('Content-Type', 'application/json');
      init.body = JSON.stringify(opts.json);
    } else if (Object.prototype.hasOwnProperty.call(opts, 'body')) {
      init.body = opts.body;
    }
    return fetch(path, init).then(function (response) {
      return response.text().then(function (text) {
        var payload = null;
        try {
          payload = text ? JSON.parse(text) : {};
        } catch (_error) {
          payload = {};
        }
        if (!response.ok) {
          var failure = new Error(payload.detail || ('요청 실패 (HTTP ' + response.status + ')'));
          failure.status = response.status;
          failure.code = payload.error || 'request_failed';
          failure.detail = payload.detail || failure.message;
          throw failure;
        }
        return payload;
      });
    });
  }

  function unavailable(error) {
    return error && (error.status === 404 || error.status === 503);
  }

  function fetchToken() {
    return api('/api/token').then(function (payload) {
      if (!payload || payload.ok !== true || typeof payload.token !== 'string') {
        throw new Error('워크플로 토큰 응답이 올바르지 않습니다.');
      }
      TOKEN = payload.token;
      return payload;
    });
  }

  function refreshStatus() {
    if (!TOKEN) return Promise.resolve(null);
    return api('/api/status').then(function (payload) {
      if (payload.workflow === false) {
        state.disabled = true;
        ui.panel.hidden = true;
        return null;
      }
      state.status = payload;
      state.disabled = false;
      ui.panel.hidden = false;
      renderPanel(payload);
      if (payload.job && payload.job.state === 'running' && !state.pollTimer) {
        state.job = payload.job;
        state.activeKind = payload.job.kind;
        state.after = Number(payload.job.nextAfter || 0);
        addLines(payload.job.lines);
        renderProgress();
        watchJob(payload.job.kind);
      }
      return payload;
    }).catch(function (error) {
      if (unavailable(error)) {
        state.disabled = true;
        ui.panel.hidden = true;
        return null;
      }
      showBanner('서버 상태를 읽지 못했습니다 — 시작 파일로 도구를 다시 여세요.\n' + error.message, 'error');
      setControlsDisabled(true);
      return null;
    });
  }

  function stepClasses(status) {
    var hasPhotos = Number(status.srcCount || 0) > 0;
    var prepared = !!status.dataJs && Array.isArray(status.groups) && status.groups.length > 0;
    var hasResults = Number(status.resultCount || 0) > 0;
    return [
      hasPhotos ? 'done' : 'active',
      prepared ? 'done' : (hasPhotos ? 'active' : ''),
      prepared ? (hasResults ? 'done' : 'active') : '',
      hasResults ? 'done' : (prepared ? 'active' : '')
    ];
  }

  function renderPanel(status) {
    var running = isBusy() || state.uploading;
    var classes = stepClasses(status);
    ui.steps.forEach(function (node, index) {
      node.className = 'wfStep' + (classes[index] ? ' ' + classes[index] : '');
    });
    setText(ui.summary,
      '원본 ' + Number(status.srcCount || 0) + '장 · 그룹 ' +
      (Array.isArray(status.groups) ? status.groups.length : 0) + '개 · 결과 ' +
      Number(status.resultCount || 0) + '권');
    setText(ui.srcCount, '현재 원본 ' + Number(status.srcCount || 0) + '장');

    var groups = Array.isArray(status.groups) ? status.groups : [];
    if (groups.length) {
      setText(ui.groupInfo, groups.map(function (group) {
        return String(group.name) + ' ' + Number(group.count || 0) + '장';
      }).join(' · '));
    } else {
      setText(ui.groupInfo, status.worktree ? '그룹 계획은 있으나 준비된 사진이 없습니다.' : '그룹 계획 없음');
    }

    var env = status.env || {};
    setText(ui.envInfo,
      '환경 ' + (env.ok ? '준비됨' : '준비 필요') +
      ' · 작업 워커 ' + Number(env.workers || 0) + '개' +
      ' · HEIC ' + (env.heic ? '지원' : '미지원'));
    setText(ui.resultInfo, '현재 결과 PDF ' + Number(status.resultCount || 0) + '권');
    setText(ui.prepareBtn, status.worktree ? '그대로 준비' : '사진 준비 실행');
    ui.regroupBtn.hidden = !status.worktree;

    if (!state.collapseReady) {
      var saved = null;
      try { saved = localStorage.getItem(COLLAPSE_KEY); } catch (_error) { saved = null; }
      if (saved === null) {
        state.collapsed = !!status.dataJs && !(status.job && status.job.state === 'running');
      }
      else state.collapsed = saved === '1';
      state.collapseReady = true;
      setCollapsed(state.collapsed, false);
    }
    setControlsDisabled(running || state.disabled);
    renderProgress();
  }

  function setControlsDisabled(force) {
    var status = state.status || {};
    var envOk = !!(status.env && status.env.ok);
    var hasPhotos = Number(status.srcCount || 0) > 0;
    var prepared = !!status.dataJs;
    ui.openSrcBtn.disabled = !!force;
    ui.drop.setAttribute('aria-disabled', force ? 'true' : 'false');
    ui.fileInput.disabled = !!force;
    ui.gap.disabled = !!force;
    ui.prepareBtn.disabled = !!force || !hasPhotos || !envOk;
    ui.regroupBtn.disabled = !!force || !hasPhotos || !envOk;
    ui.exportBtn.disabled = !!force || !prepared || !envOk;
    ui.onlyDone.disabled = !!force;
    ui.openOutBtn.disabled = !!force;
  }

  function setCollapsed(value, persist) {
    state.collapsed = !!value;
    ui.panel.classList.toggle('wfCollapsed', state.collapsed);
    setText(ui.collapseBtn, state.collapsed ? '펼치기' : '접기');
    ui.collapseBtn.setAttribute('aria-expanded', state.collapsed ? 'false' : 'true');
    if (persist) {
      try { localStorage.setItem(COLLAPSE_KEY, state.collapsed ? '1' : '0'); } catch (_error) {}
    }
  }

  function isBusy() {
    return !!(state.job && state.job.state === 'running');
  }

  function hasFileTransfer(event) {
    if (!event.dataTransfer || !event.dataTransfer.types) return false;
    return Array.prototype.indexOf.call(event.dataTransfer.types, 'Files') >= 0;
  }

  function bindDnD() {
    if (state.dndBound) return;
    state.dndBound = true;
    window.addEventListener('dragover', function (event) {
      if (!hasFileTransfer(event) || state.disabled || state.uploading) return;
      event.preventDefault();
      ui.drop.classList.add('dragover');
    }, true);
    window.addEventListener('dragleave', function (event) {
      if (!hasFileTransfer(event)) return;
      if (!event.relatedTarget) ui.drop.classList.remove('dragover');
    }, true);
    window.addEventListener('drop', function (event) {
      if (!hasFileTransfer(event)) return;
      event.preventDefault();
      ui.drop.classList.remove('dragover');
      if (state.disabled || state.uploading) return;
      var items = Array.from(event.dataTransfer.items || []);
      var hasFolder = items.some(function (item) {
        var entry = typeof item.webkitGetAsEntry === 'function' ? item.webkitGetAsEntry() : null;
        return !!(entry && entry.isDirectory);
      });
      if (hasFolder) {
        showBanner('폴더는 “사진 폴더 열기”로 넣어주세요. 이 화면에서는 파일만 끌어놓을 수 있습니다.', 'warn');
        return;
      }
      uploadFiles(event.dataTransfer.files);
    }, true);
  }

  async function uploadFiles(fileList) {
    var files = Array.from(fileList || []);
    if (!files.length || state.uploading || state.disabled) return;
    state.uploading = true;
    state.uploadPercent = 0;
    state.logs = [];
    setText(ui.uploadResults, '');
    clearBanner();
    setControlsDisabled(true);
    var results = [];
    var failed = 0;
    for (var index = 0; index < files.length; index += 1) {
      var file = files[index];
      state.uploadPercent = Math.round((index / files.length) * 100);
      state.uploadLabel = '업로드 ' + (index + 1) + '/' + files.length + ' · ' + file.name;
      renderProgress();
      try {
        var payload = await api('/api/upload', {
          method: 'POST',
          headers: {'X-Filename': encodeURIComponent(file.name)},
          body: file
        });
        var note = payload.dedup ? '같은 파일이라 건너뜀' :
          (payload.renamed ? '이름을 바꿔 저장: ' + payload.saved : '저장: ' + payload.saved);
        results.push(file.name + ' — ' + note);
      } catch (error) {
        failed += 1;
        results.push(file.name + ' — 실패: ' + (error.detail || error.message));
      }
      setText(ui.uploadResults, results.join('\n'));
    }
    state.uploadPercent = 100;
    state.uploadLabel = '업로드 완료 ' + (files.length - failed) + '/' + files.length;
    state.uploading = false;
    renderProgress();
    await refreshStatus();
    showBanner(
      failed ? '일부 사진을 올리지 못했습니다. 아래 파일별 사유를 확인하세요.' :
        '사진 업로드가 끝났습니다. 이제 “사진 준비”를 실행하세요.',
      failed ? 'warn' : 'ok'
    );
  }

  function validatedGap() {
    var gap = Number(ui.gap.value);
    if (!Number.isInteger(gap) || gap < 1 || gap > 600) {
      showBanner('발표 간격은 1~600 사이의 정수로 입력하세요.', 'warn');
      ui.gap.focus();
      return null;
    }
    return gap;
  }

  function runPrepare(regroup) {
    if (state.disabled || isBusy() || state.uploading) return;
    var gap = validatedGap();
    if (gap === null) return;
    clearBanner();
    startJob('/api/prepare', {regroup: !!regroup, gapMinutes: gap}, 'prepare');
  }

  function runExportPdf() {
    if (state.disabled || isBusy() || state.uploading) return;
    if (typeof collectBackup !== 'function') {
      showBanner('현재 보정값을 수집하지 못했습니다. 페이지를 새로고침한 뒤 다시 시도하세요.', 'error');
      return;
    }
    var backup;
    try {
      backup = collectBackup();
    } catch (error) {
      showBanner('현재 보정값을 읽지 못했습니다.\n' + error.message, 'error');
      return;
    }
    clearBanner();
    showBanner('PDF 생성 중에는 창을 닫지 않는 것이 좋습니다. 닫아도 서버가 작업을 마친 뒤 종료합니다.', 'info');
    startJob('/api/export-pdf', {
      backup: backup,
      onlyDone: !!ui.onlyDone.checked,
      merge: false
    }, 'export');
  }

  function startJob(path, payload, kind) {
    state.uploadPercent = 0;
    state.uploadLabel = '';
    setControlsDisabled(true);
    api(path, {method: 'POST', json: payload}).then(function (response) {
      state.job = response.job || {kind: kind, state: 'running'};
      state.job.state = state.job.state || 'running';
      state.activeKind = kind;
      state.logs = [];
      state.after = 0;
      renderProgress();
      watchJob(kind);
    }).catch(function (error) {
      state.job = null;
      setControlsDisabled(false);
      showBanner(error.detail || error.message, 'error');
      renderProgress();
    });
  }

  function addLines(lines) {
    if (!Array.isArray(lines)) return;
    lines.forEach(function (entry) {
      if (!Array.isArray(entry) || entry.length < 2) return;
      state.logs.push(String(entry[1]));
    });
    if (state.logs.length > 2000) state.logs = state.logs.slice(-2000);
  }

  function watchJob(kind) {
    state.activeKind = kind || state.activeKind;
    if (state.pollTimer) clearTimeout(state.pollTimer);
    state.pollTimer = setTimeout(pollJob, 0);
  }

  function pollJob() {
    state.pollTimer = null;
    api('/api/job?after=' + state.after).then(function (payload) {
      var job = payload.job;
      if (!job) {
        state.job = null;
        renderProgress();
        showBanner('실행 중인 작업 상태를 찾지 못했습니다. 상태를 새로 확인하세요.', 'warn');
        refreshStatus();
        return;
      }
      addLines(job.lines);
      state.after = Number(job.nextAfter || state.after || 0);
      state.job = job;
      renderProgress();
      if (job.state === 'running') {
        state.pollTimer = setTimeout(pollJob, POLL_MS);
      } else {
        finishJob(job);
      }
    }).catch(function (error) {
      state.job = null;
      renderProgress();
      setControlsDisabled(true);
      showBanner('서버에서 작업 상태를 읽지 못했습니다. 시작 파일로 도구를 다시 여세요.\n' + error.message, 'error');
    });
  }

  function finishJob(job) {
    if (state.pollTimer) clearTimeout(state.pollTimer);
    state.pollTimer = null;
    setControlsDisabled(false);
    if (job.state === 'done' && job.kind === 'prepare') {
      showBanner('사진 준비가 끝났습니다. 새 목록을 불러옵니다.', 'ok');
      setTimeout(function () { location.reload(); }, 250);
      return;
    }
    if (job.state === 'done' && job.kind === 'export') {
      if (typeof markBackedUp === 'function') markBackedUp();
      refreshStatus().then(function (status) {
        var count = status ? Number(status.resultCount || 0) : 0;
        showBanner('PDF ' + count + '권이 결과 폴더에 있습니다.', 'ok');
      });
      return;
    }
    if (job.state === 'cancelled') {
      showBanner('작업을 취소했습니다. 일부 생성물은 남아 있을 수 있습니다.', 'warn');
      refreshStatus();
      return;
    }
    showBanner('작업이 실패했습니다. 전체 로그에서 마지막 오류를 확인하세요.', 'error');
    refreshStatus();
  }

  function cancelJob() {
    if (!isBusy() || state.cancelling) return;
    state.cancelling = true;
    if (state.pollTimer) clearTimeout(state.pollTimer);
    state.pollTimer = null;
    ui.cancelBtn.disabled = true;
    api('/api/job/cancel', {method: 'POST', json: {}}).then(function (payload) {
      state.cancelling = false;
      state.job = payload.job || state.job;
      state.logs = [];
      state.after = Number(state.job && state.job.nextAfter || 0);
      addLines(state.job && state.job.lines);
      renderProgress();
      finishJob(state.job || {state: 'cancelled', kind: state.activeKind});
    }).catch(function (error) {
      state.cancelling = false;
      ui.cancelBtn.disabled = false;
      showBanner(error.detail || error.message, 'error');
      watchJob(state.activeKind);
    });
  }

  function openFolder(target) {
    if (state.disabled || isBusy() || state.uploading) return;
    api('/api/open-folder', {method: 'POST', json: {target: target}}).catch(function (error) {
      showBanner(error.detail || error.message, 'error');
    });
  }

  function renderProgress() {
    if (!ui.progress) return;
    if (state.uploading || state.uploadPercent === 100) {
      ui.progress.classList.add('show');
      setText(ui.progressLabel, state.uploadLabel || '사진 업로드');
      ui.barFill.style.width = state.uploadPercent + '%';
      ui.cancelBtn.hidden = true;
      setText(ui.logTail, '파일별 결과는 사진 넣기 단계 아래에 표시됩니다.');
      setText(ui.logAll, '');
      return;
    }
    var job = state.job;
    if (!job && !state.logs.length) {
      ui.progress.classList.remove('show');
      return;
    }
    ui.progress.classList.add('show');
    var phase = Number(job && job.phase || 0);
    var total = Number(job && job.phaseTotal || 0);
    var percent = total > 0 ? Math.round((phase / total) * 100) : 4;
    if (job && job.state === 'done') percent = 100;
    setText(ui.progressLabel,
      job ? ((job.phaseName || '작업 중') + (total ? ' · 단계 ' + phase + '/' + total : '')) : '작업 로그');
    ui.barFill.style.width = Math.max(0, Math.min(100, percent)) + '%';
    ui.cancelBtn.hidden = !(job && job.state === 'running');
    ui.cancelBtn.disabled = state.cancelling;
    setText(ui.logTail, state.logs.slice(-3).join('\n'));
    setText(ui.logAll, state.logs.join('\n'));
  }

  function showBanner(message, level) {
    if (!ui.banner) return;
    setText(ui.banner, message);
    ui.banner.className = 'wfBanner show ' + (level || 'info');
  }

  function clearBanner() {
    if (!ui.banner) return;
    setText(ui.banner, '');
    ui.banner.className = 'wfBanner';
  }

  function init() {
    injectStyle();
    if (!buildPanel()) return;
    fetchToken().then(function () {
      return refreshStatus();
    }).then(function (status) {
      if (!status) return;
      bindDnD();
    }).catch(function (error) {
      state.disabled = true;
      if (unavailable(error)) {
        ui.panel.hidden = true;
        return;
      }
      ui.panel.hidden = false;
      setControlsDisabled(true);
      showBanner(
        '보안 확인 실패 — 페이지를 http://localhost:8770/slide_tool/ 주소로 여세요.\n' +
        (error.detail || error.message),
        'error'
      );
    });
  }

  window.__slideWorkflow = {refreshStatus: refreshStatus};
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init);
  else init();
})();
