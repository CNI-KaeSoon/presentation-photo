(function () {
  'use strict';

  // 화면 배치: 헤더 단계·다음 할 일·⋯ 메뉴, 보정 화면, 시작 화면은 index.html 이 그린다.
  // 이 파일은 서버 연결(사진 넣기·준비·PDF·새 행사)과 그 작업을 보여 주는 단계 패널(오른쪽 서랍)·알림 띠를 맡는다.
  var TOKEN = null;
  var POLL_MS = 700;
  var COLLAPSE_KEY = 'wfPanelCollapsed_v1';   // 옛 버전이 쓰던 키 — 새 행사 시작이 지운다
  // 이 출처(주소·포트)가 이미 이 작업을 본 적이 있다는 표시 — 있으면 "저장된 보정값 없음" 안내 띠를 다시 띄우지 않는다.
  var ORIGIN_SEEN_KEY = 'wfOriginSeen_v1';
  var NEW_EVENT_TOAST_KEY = 'wfNewEventToast_v1';
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
    drawerStep: null,     // 열려 있는 단계 패널: 0(① 사진 넣기) · 1(② 준비) · 3(④ PDF) · null(닫힘)
    exportMode: 'per-folder',
    exportOrder: [],
    draggedGroup: null,
    disabled: false,
    offline: false,
    newEventRunning: false,
    deckBusy: false,      // 발표자료 PDF 를 확인·올리는 중(서버 잡이 시작되기 전)
    deckGroup: null,
    localBandChecked: false,
    dndBound: false
  };
  var ui = {};
  var STEP_TITLES = {0: '① 사진 넣기', 1: '② 준비', 3: '④ PDF'};
  var DECK_PDF_MAX_BYTES = 200 * 1024 * 1024;   // 서버 상한(MAX_DECK_PDF_BYTES)과 같다
  var DECK_DROP_GUIDE = '발표자료는 발표 ⋯ 메뉴의 [발표자료 PDF 넣기]로 넣으세요. (PDF는 사진으로 올리지 않았습니다.)';

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

  // index.html 의 <symbol> 스프라이트를 쓰는 선 아이콘.
  function icon(name) {
    var ns = 'http://www.w3.org/2000/svg';
    var svg = document.createElementNS(ns, 'svg');
    svg.setAttribute('class', 'ic');
    svg.setAttribute('viewBox', '0 0 24 24');
    svg.setAttribute('aria-hidden', 'true');
    var use = document.createElementNS(ns, 'use');
    use.setAttribute('href', '#i-' + name);
    svg.appendChild(use);
    return svg;
  }

  function exportModeOption(mode, title, description) {
    var radio = el('input', {
      type: 'radio',
      name: 'wfExportMode',
      value: mode,
      checked: mode === state.exportMode
    });
    radio.addEventListener('change', function () {
      if (!radio.checked) return;
      state.exportMode = mode;
      syncExportOptions();
    });
    ui.exportModeRadios[mode] = radio;
    return el('label', {className: 'wfExportOption'}, [
      radio,
      el('span', {}, [el('strong', {text: title}), el('span', {text: description})])
    ]);
  }

  function groupNames() {
    var groups = state.status && Array.isArray(state.status.groups) ? state.status.groups : [];
    return groups.map(function (group) { return String(group.name); });
  }

  function syncExportOrder(names) {
    var available = names || groupNames();
    var kept = state.exportOrder.filter(function (name) {
      return available.indexOf(name) >= 0;
    });
    available.forEach(function (name) {
      if (kept.indexOf(name) < 0) kept.push(name);
    });
    state.exportOrder = kept;
  }

  function moveOrderedGroup(dragged, target, after) {
    var next = state.exportOrder.filter(function (name) { return name !== dragged; });
    var targetIndex = next.indexOf(target);
    if (targetIndex < 0) return;
    next.splice(targetIndex + (after ? 1 : 0), 0, dragged);
    state.exportOrder = next;
    renderOrderList();
  }

  function renderOrderList() {
    if (!ui.orderList) return;
    syncExportOrder();
    ui.orderList.textContent = '';
    state.exportOrder.forEach(function (name) {
      var item = el('li', {className: 'wfOrderItem', 'data-group': name});
      var handle = el('span', {
        className: 'wfOrderHandle',
        draggable: 'true',
        title: '끌어서 순서 변경',
        'aria-label': name + ' 순서 변경 손잡이'
      }, [icon('grip')]);
      handle.addEventListener('dragstart', function (event) {
        if (state.disabled || isBusy() || state.uploading) {
          event.preventDefault();
          return;
        }
        state.draggedGroup = name;
        item.classList.add('dragging');
        event.dataTransfer.effectAllowed = 'move';
        event.dataTransfer.setData('text/plain', name);
      });
      handle.addEventListener('dragend', function () {
        state.draggedGroup = null;
        item.classList.remove('dragging');
      });
      item.addEventListener('dragover', function (event) {
        if (!state.draggedGroup || state.draggedGroup === name) return;
        event.preventDefault();
        event.dataTransfer.dropEffect = 'move';
      });
      item.addEventListener('drop', function (event) {
        if (!state.draggedGroup || state.draggedGroup === name) return;
        event.preventDefault();
        var rect = item.getBoundingClientRect();
        moveOrderedGroup(state.draggedGroup, name, event.clientY > rect.top + rect.height / 2);
        state.draggedGroup = null;
      });
      item.appendChild(handle);
      item.appendChild(el('span', {text: name}));
      ui.orderList.appendChild(item);
    });
  }

  function syncExportOptions() {
    if (!ui.multiExportModes) return;
    var multiple = groupNames().length >= 2;
    ui.multiExportModes.hidden = !multiple;
    if (!multiple && state.exportMode !== 'per-folder') {
      state.exportMode = 'per-folder';
      ui.exportModeRadios['per-folder'].checked = true;
    }
    ui.orderBox.hidden = !multiple || state.exportMode !== 'ordered';
    if (!ui.orderBox.hidden) renderOrderList();
  }

  function injectStyle() {
    if (document.getElementById('wfStyle')) return;
    var style = el('style', {id: 'wfStyle'});
    style.textContent = [
      /* 단계 패널(오른쪽 서랍): 헤더의 ①②④ 를 누르면 열린다. 보정 화면 위에 겹쳐 뜨므로 보정 화면 배치는 그대로다. */
      '#wfDrawer{position:fixed;top:var(--hdr-h);right:0;bottom:0;z-index:70;width:min(var(--drawer-w),100vw);display:flex;flex-direction:column;background:var(--surface);border-left:1px solid var(--border);box-shadow:var(--shadow-modal);transform:translateX(104%);visibility:hidden;transition:transform .2s,visibility 0s .2s}',
      '#wfDrawer.open{transform:none;visibility:visible;transition:transform .2s,visibility 0s}',
      '.wfDrawerHead{flex:none;display:flex;align-items:flex-start;justify-content:space-between;gap:var(--s2);padding:var(--s3) var(--s3) var(--s2)}',
      '.wfDrawerHead h2{font-size:var(--f3);font-weight:800}',
      '.wfSummary{font-size:var(--f1);color:var(--muted);margin-top:2px}',
      '.wfDrawerTabs{flex:none;padding:0 var(--s3) var(--s2)}',
      '.wfDrawerBody{flex:1;min-height:0;overflow:auto;padding:0 var(--s3) var(--s3);border-top:1px solid var(--border)}',
      '.wfStepInfo{font-size:var(--f1);color:var(--muted);margin:var(--s2) 0}',
      '.wfDrop{display:flex;flex-direction:column;align-items:center;gap:var(--s1);border:2px dashed var(--border-strong);border-radius:var(--r3);background:var(--surface);padding:var(--s3);text-align:center;cursor:pointer;transition:.15s}',
      '.wfDrop:hover,.wfDrop.dragover{border-color:var(--primary);background:var(--primary-soft)}',
      '.wfDrop[aria-disabled=true]{opacity:.5;cursor:not-allowed}',
      '.wfDropIcon{width:48px;height:48px;border-radius:50%;display:grid;place-items:center;background:var(--primary-soft);color:var(--primary-text)}',
      '.wfDropIcon .ic{width:24px;height:24px}',
      '.wfDrop strong{display:block;font-size:var(--f3);font-weight:800}',
      '.wfDropSub{display:block;color:var(--muted);font-size:var(--f1)}',
      '.wfControls{display:flex;align-items:center;gap:var(--s1);flex-wrap:wrap;margin-top:var(--s1)}',
      '.wfControls label{font-size:var(--f1);color:var(--text-2);display:flex;align-items:center;gap:var(--s1)}',
      '.wfControls input[type=number]{width:72px;border:1px solid var(--border-strong);border-radius:var(--r1);padding:var(--s1);background:var(--surface)}',
      '.wfCounts{font-size:var(--f1);margin-top:var(--s1);color:var(--text)}',
      '.wfEnv{font-size:var(--f1);color:var(--muted);margin-top:var(--s1)}',
      '.wfExportModes{display:grid;gap:var(--s1);margin-top:4px}',
      '.wfExportOption{display:flex;align-items:flex-start;gap:var(--s1);padding:var(--s1) var(--s2);border:1px solid var(--border);border-radius:var(--r2);background:var(--surface-2);font-size:var(--f1);color:var(--text);cursor:pointer}',
      '.wfExportOption:has(input:checked){border-color:var(--primary);background:var(--primary-soft)}',
      '.wfExportOption input{margin-top:3px}',
      '.wfExportOption strong{display:block;font-size:var(--f2);color:var(--text)}',
      '.wfExportOption span{display:block;color:var(--muted);font-size:var(--f1)}',
      '.wfOrderBox{margin:var(--s1) 0 0;padding:var(--s2);border:1px solid var(--border);border-radius:var(--r2)}',
      '.wfOrderTitle{font-size:var(--f1);font-weight:800;margin-bottom:var(--s1)}',
      '.wfOrderList{list-style:none;margin:0;padding:0;display:grid;gap:var(--s1)}',
      '.wfOrderItem{display:flex;align-items:center;gap:var(--s1);padding:var(--s1) var(--s2);border:1px solid var(--border);border-radius:var(--r1);background:var(--surface);font-size:var(--f2)}',
      '.wfOrderItem.dragging{opacity:.45}',
      '.wfOrderHandle{display:inline-grid;place-items:center;color:var(--muted);cursor:grab;user-select:none}',
      '.wfOrderHandle:active{cursor:grabbing}',
      '.wfNoteText{white-space:pre-wrap}',
      '.wfNoteActions{display:flex;align-items:center;gap:var(--s1);flex-wrap:wrap}',
      '.wfWhy{font-size:var(--f1);color:var(--warn)}',
      '.wfWhy:empty{display:none}',
      '.wfNeOption{display:flex;align-items:flex-start;gap:var(--s1);margin-top:var(--s2)}',
      '.wfNeOption input{margin-top:3px}',
      '.wfRowInfo{font-size:var(--f1);color:var(--muted)}',
      '.wfProgress{display:none;margin:var(--s2) 0 0;padding:var(--s2);border:1px solid var(--border);border-radius:var(--r2);background:var(--surface-2)}',
      '.wfProgress.show{display:block}',
      '.wfProgressHead{display:flex;align-items:center;justify-content:space-between;gap:var(--s1);flex-wrap:wrap}',
      '.wfProgressNote{font-size:var(--f1);color:var(--muted)}',
      '.wfProgressNote:empty{display:none}',
      '.wfBar{height:6px;background:var(--surface-3);border-radius:var(--r-pill);overflow:hidden;margin:var(--s1) 0}',
      '.wfBarFill{height:100%;width:0;background:var(--primary);border-radius:var(--r-pill);transition:width .2s}',
      '.wfLogAll{margin:var(--s1) 0 0;white-space:pre-wrap;overflow-wrap:anywhere;font-family:var(--font);font-size:var(--f1);color:var(--text-2);max-height:260px;overflow:auto}',
      '.wfDetails summary{cursor:pointer;color:var(--primary-text);font-size:var(--f1);margin-top:var(--s1)}',
      '.wfUploadResults{margin-top:var(--s1);white-space:pre-wrap;font-size:var(--f1);color:var(--muted)}',
      '@media(max-width:1024px){#wfDrawer{top:var(--hdr-h)}}'
    ].join('\n');
    document.head.appendChild(style);
  }

  // 진행 상자(업로드·준비·PDF 진행과 자세한 기록). 단계 패널과 시작 화면이 각각 하나씩 갖는다.
  function makeProgress() {
    var box = el('div', {className: 'wfProgress'});
    var label = el('strong', {text: '대기'});
    var cancel = el('button', {type: 'button', className: 'btn sm danger', text: '작업 취소'});
    cancel.addEventListener('click', cancelJob);
    box.appendChild(el('div', {className: 'wfProgressHead'}, [label, cancel]));
    var fill = el('div', {className: 'wfBarFill'});
    box.appendChild(el('div', {className: 'wfBar'}, [fill]));
    // 명령·절대경로가 섞인 기록은 기본 접힘 — 진행 상황은 위 막대와 이름으로 충분하다. 실패하면 펼친다.
    var note = el('div', {className: 'wfProgressNote'});
    box.appendChild(note);
    var log = el('pre', {className: 'wfLogAll'});
    var details = el('details', {className: 'wfDetails'}, [el('summary', {text: '자세한 기록'}), log]);
    box.appendChild(details);
    return {root: box, label: label, cancel: cancel, fill: fill, note: note, log: log, details: details};
  }

  // 사진 넣기 드롭 영역(단계 패널용). 시작 화면의 큰 드롭 영역은 index.html 에 있고 아래 bindStartView 가 연결한다.
  function buildDropZone() {
    var drop = el('div', {className: 'wfDrop', role: 'button', tabindex: '0'}, [
      el('span', {className: 'wfDropIcon'}, [icon('upload')]),
      el('strong', {text: '사진을 여기로 끌어 놓으세요'}),
      el('span', {className: 'wfDropSub', text: 'JPG · PNG · HEIC 등 파일 단위로 올립니다. 사진은 이 컴퓨터 밖으로 나가지 않습니다.'})
    ]);
    drop.addEventListener('click', pickFiles);
    drop.addEventListener('keydown', function (event) {
      if (event.key === 'Enter' || event.key === ' ') {
        event.preventDefault();
        pickFiles();
      }
    });
    return drop;
  }

  function buildUi() {
    var bands = document.getElementById('bands');
    if (!bands || !document.getElementById('main')) return false;

    // ---- 알림 띠(헤더 바로 아래): 작업 결과·오류(닫을 수 있음) / 상태에서 계산되는 안내 / 이 주소에 저장된 작업 없음 ----
    var banner = buildNote('banner', true);
    ui.banner = banner.root;
    ui.bannerText = banner.text;
    ui.bannerActions = banner.actions;
    bands.appendChild(ui.banner);
    var notice = buildNote('banner');
    ui.notice = notice.root;
    ui.noticeText = notice.text;
    ui.noticeActions = notice.actions;
    bands.appendChild(ui.notice);
    buildLocalBand(bands);

    // ---- 파일 입력 하나(드롭 영역이 둘이어도 이것을 함께 쓴다) ----
    ui.fileInput = el('input', {
      type: 'file',
      multiple: 'multiple',
      accept: '.jpg,.jpeg,.png,.heic,.heif,.tif,.tiff,.bmp,.webp'
    });
    ui.fileInput.hidden = true;
    ui.fileInput.addEventListener('change', function () {
      uploadFiles(ui.fileInput.files);
      ui.fileInput.value = '';
    });
    document.body.appendChild(ui.fileInput);

    // 발표자료 PDF 고르기(발표 ⋯ 메뉴에서 연다) — 사진 입력과 따로 둔다: PDF 한 개, 확장자 .pdf.
    ui.deckInput = el('input', {type: 'file', accept: '.pdf,application/pdf'});
    ui.deckInput.hidden = true;
    ui.deckInput.addEventListener('change', function () {
      var file = ui.deckInput.files && ui.deckInput.files[0];
      ui.deckInput.value = '';
      if (file) inspectDeck(state.deckGroup, file);
    });
    document.body.appendChild(ui.deckInput);

    // ---- 단계 패널(오른쪽 서랍) ----
    ui.drawer = el('aside', {id: 'wfDrawer', 'aria-label': '작업 단계 패널'});
    ui.drawerTitle = el('h2', {text: ''});
    ui.summary = el('div', {className: 'wfSummary', text: '서버 연결을 확인하는 중입니다.'});
    ui.drawerClose = el('button', {type: 'button', className: 'btn ghost sm icon', 'aria-label': '패널 닫기', title: '패널 닫기 (Esc)'}, [icon('x')]);
    ui.drawerClose.addEventListener('click', closeDrawer);
    ui.drawer.appendChild(el('div', {className: 'wfDrawerHead'}, [el('div', {}, [ui.drawerTitle, ui.summary]), ui.drawerClose]));
    ui.tabs = {};
    var tabSeg = el('div', {className: 'seg sm', role: 'tablist', 'aria-label': '단계'});
    [0, 1, 3].forEach(function (step) {
      var tab = el('button', {type: 'button', role: 'tab', 'aria-selected': 'false', text: STEP_TITLES[step]});
      tab.addEventListener('click', function () { openStep(step); });
      ui.tabs[step] = tab;
      tabSeg.appendChild(tab);
    });
    ui.drawer.appendChild(el('div', {className: 'wfDrawerTabs'}, [tabSeg]));
    ui.drawerBody = el('div', {className: 'wfDrawerBody'});
    ui.drawer.appendChild(ui.drawerBody);

    ui.progresses = [makeProgress()];
    ui.drawerBody.appendChild(ui.progresses[0].root);

    // ① 사진 넣기
    var filePane = el('div', {'data-step': '0'});
    filePane.appendChild(el('div', {className: 'wfStepInfo', text: '원본은 보존되며 작업용 사진은 다음 단계에서 만듭니다.'}));
    ui.drop = buildDropZone();
    ui.openSrcBtn = el('button', {type: 'button', className: 'btn'}, [icon('folder'), '사진 폴더 열기']);
    ui.openSrcBtn.addEventListener('click', function () { openFolder('src'); });
    ui.srcCount = el('span', {className: 'wfCounts', text: '현재 원본 0장'});
    filePane.appendChild(ui.drop);
    filePane.appendChild(el('div', {className: 'wfControls'}, [
      ui.openSrcBtn,
      el('span', {className: 'muted', text: '대용량·폴더 단위 복사는 이 버튼으로 폴더를 연 뒤 넣으세요.'})
    ]));
    filePane.appendChild(ui.srcCount);
    ui.uploadResults = el('div', {className: 'wfUploadResults'});
    filePane.appendChild(ui.uploadResults);

    // ② 준비
    var preparePane = el('div', {'data-step': '1'});
    preparePane.appendChild(el('div', {className: 'wfStepInfo', text: '촬영 간격이 “발표 간격”보다 크게 벌어진 곳에서 발표를 나누고, 이어서 작업용 사진과 목록을 만듭니다.'}));
    ui.gap = el('input', {type: 'number', min: '1', max: '600', value: '20', inputmode: 'numeric'});
    ui.prepareBtn = el('button', {type: 'button', className: 'btn primary', text: '사진 준비 실행'});
    ui.regroupBtn = el('button', {type: 'button', className: 'btn danger', text: '다시 나누기…'});
    ui.prepareBtn.addEventListener('click', function () { runPrepare(false); });
    ui.regroupBtn.addEventListener('click', confirmRegroup);
    ui.prepareWhy = el('span', {className: 'wfWhy'});
    preparePane.appendChild(el('div', {className: 'wfControls'}, [
      el('label', {}, [document.createTextNode('발표 간격(분)'), ui.gap]),
      ui.prepareBtn,
      ui.regroupBtn
    ]));
    preparePane.appendChild(ui.prepareWhy);
    ui.groupInfo = el('div', {className: 'wfCounts', text: '나눈 발표 없음'});
    ui.envInfo = el('div', {className: 'wfEnv', text: '환경 상태 확인 중'});
    preparePane.appendChild(ui.groupInfo);
    preparePane.appendChild(ui.envInfo);

    // ④ PDF
    var exportPane = el('div', {'data-step': '3'});
    exportPane.appendChild(el('div', {className: 'wfStepInfo', text: '현재 경계·색보정 값을 원본 사진에 적용해 고해상도 PDF를 만듭니다.'}));
    ui.onlyDone = el('input', {type: 'checkbox'});
    ui.exportModeRadios = {};
    ui.exportModes = el('div', {className: 'wfExportModes'});
    ui.exportModes.appendChild(exportModeOption(
      'per-folder',
      'A. 발표별로 PDF 만들기',
      '발표마다 PDF를 1개씩 만듭니다.'
    ));
    ui.multiExportModes = el('div', {className: 'wfExportModes'});
    ui.multiExportModes.appendChild(exportModeOption(
      'merged',
      'B. 발표별 PDF + 전체 합본',
      '발표별 PDF와 모든 발표를 합친 PDF를 함께 만듭니다.'
    ));
    ui.multiExportModes.appendChild(exportModeOption(
      'ordered',
      'C. 순서를 바꿔 PDF 1개로 합치기',
      '아래 발표 순서대로 합친 PDF 1개만 만듭니다.'
    ));
    ui.exportModes.appendChild(ui.multiExportModes);
    ui.orderBox = el('div', {className: 'wfOrderBox'});
    ui.orderBox.appendChild(el('div', {className: 'wfOrderTitle', text: '손잡이를 끌어 발표 순서를 바꾸세요.'}));
    ui.orderList = el('ol', {className: 'wfOrderList'});
    ui.orderBox.appendChild(ui.orderList);
    ui.exportBtn = el('button', {type: 'button', className: 'btn primary', text: '선택한 방식으로 PDF 만들기'});
    ui.openOutBtn = el('button', {type: 'button', className: 'btn'}, [icon('folder'), '결과 폴더 열기']);
    ui.exportBtn.addEventListener('click', runExportPdf);
    ui.openOutBtn.addEventListener('click', function () { openFolder('out'); });
    exportPane.appendChild(ui.exportModes);
    exportPane.appendChild(ui.orderBox);
    ui.exportWhy = el('span', {className: 'wfWhy'});
    exportPane.appendChild(el('div', {className: 'wfControls'}, [
      ui.exportBtn,
      el('label', {}, [ui.onlyDone, document.createTextNode('완료본만')]),
      ui.openOutBtn
    ]));
    exportPane.appendChild(ui.exportWhy);
    ui.resultInfo = el('div', {className: 'wfCounts', text: '현재 PDF 0개'});
    exportPane.appendChild(ui.resultInfo);
    exportPane.appendChild(el('div', {
      className: 'wfEnv',
      text: '보정값은 헤더의 ⋯ 메뉴 “백업 내보내기”로 파일에 따로 보관할 수도 있습니다.'
    }));

    ui.panes = {0: filePane, 1: preparePane, 3: exportPane};
    [0, 1, 3].forEach(function (step) {
      ui.panes[step].hidden = true;
      ui.drawerBody.appendChild(ui.panes[step]);
    });
    document.body.appendChild(ui.drawer);

    bindStartView();
    document.addEventListener('keydown', function (event) {
      if (event.key !== 'Escape' || state.drawerStep === null) return;
      if (typeof modalOpen === 'function' && modalOpen()) return;
      if (document.getElementById('popMenu')) return;
      closeDrawer();
    });
    return true;
  }

  // 시작 화면(index.html 의 #startView)의 요소를 서버 동작에 연결한다.
  function bindStartView() {
    ui.startDrop = document.getElementById('startDrop');
    ui.startOpenSrc = document.getElementById('startOpenSrcBtn');
    ui.startWhy = document.getElementById('startWhy');
    ui.startResume = document.getElementById('startResume');
    ui.startResumeMeta = document.getElementById('startResumeMeta');
    ui.startResumeBtn = document.getElementById('startResumeBtn');
    ui.startNewEventBtn = document.getElementById('startNewEventBtn');
    ui.startUploadResults = document.getElementById('startUploadResults');
    ui.startProgress = makeProgress();
    var slot = document.getElementById('startProgressSlot');
    if (slot) slot.appendChild(ui.startProgress.root);
    ui.progresses.push(ui.startProgress);
    ui.drops = [ui.drop];
    if (ui.startDrop) {
      ui.drops.push(ui.startDrop);
      ui.startDrop.addEventListener('click', pickFiles);
      ui.startDrop.addEventListener('keydown', function (event) {
        if (event.target !== ui.startDrop) return;
        if (event.key === 'Enter' || event.key === ' ') {
          event.preventDefault();
          pickFiles();
        }
      });
    }
    if (ui.startOpenSrc) {
      ui.startOpenSrc.addEventListener('click', function (event) {
        event.stopPropagation();
        openFolder('src');
      });
    }
    if (ui.startResumeBtn) ui.startResumeBtn.addEventListener('click', function () { runPrepare(false); });
    if (ui.startNewEventBtn) ui.startNewEventBtn.addEventListener('click', confirmNewEvent);
  }

  function setUploadResults(text) {
    setText(ui.uploadResults, text);
    setText(ui.startUploadResults, text);
  }

  // 글 한 덩어리 + 버튼 줄로 된 알림 띠 뼈대. dismissible 이면 오른쪽에 닫기(✕)가 붙는다.
  function buildNote(className, dismissible) {
    var text = el('div', {className: 'wfNoteText'});
    var actions = el('div', {className: 'wfNoteActions'});
    actions.hidden = true;
    var children = [text, actions];
    var root = el('div', {className: className, role: 'status'}, children);
    if (dismissible) {
      var close = el('button', {type: 'button', className: 'btn ghost sm icon', 'aria-label': '알림 닫기', title: '알림 닫기'}, [icon('x')]);
      close.addEventListener('click', function () { root.hidden = true; });
      root.appendChild(close);
    }
    root.hidden = true;
    return {root: root, text: text, actions: actions};
  }

  function setNoteActions(box, actions) {
    box.textContent = '';
    (actions || []).forEach(function (action) {
      var button = el('button', {
        type: 'button',
        className: 'btn sm' + (action.className ? ' ' + action.className : ''),   // 'primary' | 'danger'
        text: action.label
      });
      if (action.disabled) button.disabled = true;
      button.addEventListener('click', action.onClick);
      box.appendChild(button);
    });
    box.hidden = !(actions && actions.length);
  }

  // 확인 대화는 index.html 의 openModal 하나만 쓴다(같은 마크업·같은 버튼 순서 [취소][확인/위험]).
  function confirmRegroup() {
    if (state.disabled || isBusy()) return;
    if (typeof openModal !== 'function') return;
    openModal({
      title: '발표 다시 나누기',
      node: el('div', {}, [
        el('p', {text: '다시 나누면 발표 이름이 바뀔 수 있습니다.'}),
        el('p', {}, [
          el('strong', {text: '이미 보정한 경계·색보정이 새 발표와 어긋날 수 있습니다.'}),
          document.createTextNode(' 필요한 백업(“백업 내보내기”)을 먼저 받았는지 확인하세요.')
        ])
      ]),
      ok: '다시 나누기',
      okKind: 'danger',
      onOk: function () { runPrepare(true); }
    });
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

  function renameAvailability() {
    if (state.disabled) {
      return {enabled: false, reason: '시작 파일로 연 도구 서버에서만 이름을 바꿀 수 있습니다.'};
    }
    if (!TOKEN || !state.status) {
      return {enabled: false, reason: '도구 서버 연결을 확인하는 중입니다.'};
    }
    if (isBusy() || state.uploading) {
      return {enabled: false, reason: '실행 중인 작업이 끝난 뒤 이름을 바꾸세요.'};
    }
    return {enabled: true, reason: ''};
  }

  function renameGroup(from, to) {
    var availability = renameAvailability();
    if (!availability.enabled) return Promise.reject(new Error(availability.reason));
    return api('/api/rename-group', {
      method: 'POST',
      json: {from: from, to: to}
    });
  }

  // 보정 화면의 '경계 자동 찾기' — 서버가 사진 안의 슬라이드 경계를 OpenCV 로 찾아 준다(동기 응답).
  // 서버 없이 파일로 연 경우(state.disabled)에는 쓸 수 없고, 준비·PDF 잡이 도는 동안에도 잠근다.
  function autoDetectAvailability() {
    if (state.disabled) {
      return {enabled: false, reason: '시작 파일로 연 도구 서버에서만 자동으로 찾을 수 있습니다.'};
    }
    if (!TOKEN || !state.status) {
      return {enabled: false, reason: '도구 서버 연결을 확인하는 중입니다.'};
    }
    if (isBusy() || state.uploading) {
      return {enabled: false, reason: '실행 중인 작업이 끝난 뒤 쓰세요.'};
    }
    return {enabled: true, reason: ''};
  }

  function autoDetect(keys, rotations) {
    var availability = autoDetectAvailability();
    if (!availability.enabled) return Promise.reject(new Error(availability.reason));
    return api('/api/auto-detect', {
      method: 'POST',
      json: {keys: keys, rotations: rotations || {}}
    });
  }

  // index.html 의 버튼 활성 상태가 서버 연결·잡 진행을 따라가도록 알린다.
  function notifyState() {
    try {
      document.dispatchEvent(new CustomEvent('slideworkflow:state'));
    } catch (_error) { /* 구형 브라우저 — 버튼은 다음 화면 갱신 때 맞춰진다 */ }
  }

  function fetchToken() {
    return api('/api/token').then(function (payload) {
      if (!payload || payload.ok !== true || typeof payload.token !== 'string') {
        throw new Error('도구 서버 응답이 올바르지 않습니다.');
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
        closeDrawer();
        notifyState();
        return null;
      }
      state.status = payload;
      state.disabled = false;
      state.offline = false;
      renderPanel(payload);
      notifyState();
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
        closeDrawer();
        notifyState();
        return null;
      }
      setControlsDisabled(true);
      if (isNetworkError(error)) {
        showServerDown();
        return null;
      }
      showBanner('서버 상태를 읽지 못했습니다 — 시작 파일로 도구를 다시 여세요.\n' + (error.detail || error.message), 'error');
      return null;
    });
  }

  // 헤더(단계 표시·다음 할 일)가 읽어 가는 서버 쪽 상태 요약. 서버 상태를 아직 모르면 null.
  function summary() {
    var status = state.status;
    if (!status && !state.disabled && !state.offline) return null;
    status = status || {};
    var groups = Array.isArray(status.groups) ? status.groups : [];
    var result = {
      disabled: state.disabled,
      offline: state.offline,
      busy: false,
      busyText: '',
      busyStep: null,
      src: Number(status.srcCount || 0),
      groups: groups.length,
      prepared: !!status.dataJs && groups.length > 0,
      worktree: !!status.worktree,
      results: Number(status.resultCount || 0),
      envOk: !!(status.env && status.env.ok),
      whyPrepare: state.status ? whyDisabled('prepare', false) : '',
      whyExport: state.status ? whyDisabled('export', false) : '',
      openStep: state.drawerStep
    };
    var job = state.job;
    if (state.uploading) {
      result.busy = true;
      result.busyStep = 0;
      result.busyText = '사진을 올리는 중 · ' + (state.uploadLabel || '').replace(/^업로드 /, '');
    } else if (job && job.state === 'running') {
      var total = Number(job.phaseTotal || 0);
      result.busy = true;
      result.busyStep = job.kind === 'export' ? 3 : 1;
      result.busyText = (job.kind === 'export' ? 'PDF 만드는 중' : job.kind === 'deck-import' ? '발표자료 넣는 중' : '사진 준비 중') +
        (total ? ' · 단계 ' + Number(job.phase || 0) + '/' + total : '');
    }
    return result;
  }

  function renderPanel(status) {
    var running = isBusy() || state.uploading || state.deckBusy || !!(status.job && status.job.state === 'running');
    setText(ui.summary,
      '원본 ' + Number(status.srcCount || 0) + '장 · 발표 ' +
      (Array.isArray(status.groups) ? status.groups.length : 0) + '개 · PDF ' +
      Number(status.resultCount || 0) + '개');
    setText(ui.srcCount, '현재 원본 ' + Number(status.srcCount || 0) + '장');

    var groups = Array.isArray(status.groups) ? status.groups : [];
    syncExportOrder(groups.map(function (group) { return String(group.name); }));
    syncExportOptions();
    if (groups.length) {
      setText(ui.groupInfo, groups.map(function (group) {
        return String(group.name) + ' ' + Number(group.count || 0) + '장';
      }).join(' · '));
    } else {
      setText(ui.groupInfo, status.worktree ? '발표 계획은 있으나 준비된 사진이 없습니다.' : '나눈 발표 없음');
    }

    var env = status.env || {};
    setText(ui.envInfo,
      '환경 ' + (env.ok ? '준비됨' : '준비 필요') +
      ' · 동시 처리 ' + Number(env.workers || 0) + '개' +
      ' · HEIC ' + (env.heic ? '지원' : '미지원'));
    setText(ui.resultInfo, '현재 PDF ' + Number(status.resultCount || 0) + '개');
    setText(ui.prepareBtn, status.worktree ? '그대로 준비' : '사진 준비 실행');
    ui.regroupBtn.hidden = !status.worktree;

    // 시작 화면: 원본을 넣었거나 발표 계획이 있으면 "이어서 하기" 카드를 보여 준다
    var src = Number(status.srcCount || 0);
    if (ui.startResume) {
      ui.startResume.hidden = !(src > 0 || status.worktree);
      setText(ui.startResumeMeta,
        '원본 사진 ' + src + '장이 들어 있습니다' + (status.worktree ? ' · 발표 나누기 계획이 있습니다' : ''));
      setText(ui.startResumeBtn && ui.startResumeBtn.firstChild, status.worktree ? '그대로 준비 ' : '사진 준비 ');
    }

    setControlsDisabled(running || state.disabled);
    renderNotice(status);
    maybeShowLocalBand(status);
    renderProgress();
  }

  function newEventAvailability() {
    if (state.disabled) return {enabled: false, reason: '시작 파일로 연 도구 서버에서만 쓸 수 있습니다.'};
    if (!state.status) return {enabled: false, reason: '도구 서버 연결을 확인하는 중입니다.'};
    if (isBusy() || state.uploading) return {enabled: false, reason: '실행 중인 작업이 끝난 뒤 쓰세요.'};
    if (!hasArchivableWork(state.status)) return {enabled: false, reason: '보관할 작업이 없습니다.'};
    return {enabled: true, reason: ''};
  }

  function photoTotal(status) {
    var groups = Array.isArray(status.groups) ? status.groups : [];
    return groups.reduce(function (sum, group) { return sum + Number(group.count || 0); }, 0);
  }

  // 새 행사 시작으로 보관할 것이 있는가 — 그룹·계획·목록·원본 중 하나라도.
  function hasArchivableWork(status) {
    var groups = Array.isArray(status.groups) ? status.groups : [];
    return groups.length > 0 || !!status.worktree || !!status.dataJs || Number(status.srcCount || 0) > 0;
  }

  // 원본 폴더가 작업 계획과 어긋난 정도. 계획이 없으면 null.
  function planDiff(status) {
    var mismatch = status.planMismatch;
    if (!status.worktree || !mismatch || typeof mismatch !== 'object') return null;
    var missing = Number(mismatch.missing || 0);
    var added = Number(mismatch.added || 0);
    return missing + added > 0 ? {missing: missing, added: added} : null;
  }

  // 상태에서 계산되는 안내: 원본이 비었는데 작업장이 남았을 때 / 원본이 계획과 달라졌을 때.
  function renderNotice(status) {
    var groups = Array.isArray(status.groups) ? status.groups : [];
    var hasWork = groups.length > 0 || !!status.worktree;
    var running = isBusy() || state.uploading;
    var diff = planDiff(status);
    var text = '';
    var actions = [];
    if (hasWork && Number(status.srcCount || 0) === 0) {
      text = '원본이 비어 있습니다 — 새 행사를 시작하려면 [새 행사 시작…]을 누르세요. ' +
        '(이전 작업의 발표는 그대로 남아 있어 보정 화면에서 계속 편집할 수 있습니다.)';
      actions = [{label: '새 행사 시작…', className: 'danger', disabled: running, onClick: confirmNewEvent}];
    } else if (diff) {
      var parts = [];
      if (diff.added) parts.push('새 사진 ' + diff.added + '장');
      if (diff.missing) parts.push('없어진 사진 ' + diff.missing + '장');
      text = '원본이 바뀌었습니다(' + parts.join(', ') + '). ' +
        '지금 작업을 보관하고 새로 시작하거나, 원본 전체로 발표를 다시 나누세요.';
      actions = [
        {label: '새 행사 시작…', className: 'danger', disabled: running, onClick: confirmNewEvent},
        {label: '다시 나누기', disabled: running, onClick: confirmRegroup}
      ];
    }
    if (!text) {
      ui.notice.hidden = true;
      setText(ui.noticeText, '');
      setNoteActions(ui.noticeActions, null);
      return;
    }
    setText(ui.noticeText, text);
    setNoteActions(ui.noticeActions, actions);
    ui.notice.className = 'banner warn';
    ui.notice.hidden = false;
  }

  // 버튼이 꺼진 이유 한 줄. 켜져 있으면 ''.
  function whyDisabled(kind, force) {
    var status = state.status || {};
    var hasPhotos = Number(status.srcCount || 0) > 0;
    var envOk = !!(status.env && status.env.ok);
    var groups = Array.isArray(status.groups) ? status.groups : [];
    if (state.offline) return '서버에 연결되지 않았습니다 — [다시 연결]을 누르세요.';
    if (state.disabled) return '시작 파일로 연 도구 서버에서만 쓸 수 있습니다.';
    if (force) return state.uploading ? '사진을 올리는 중입니다 — 끝난 뒤 누르세요.' : '작업이 실행 중입니다 — 끝난 뒤 누르세요.';
    if (kind === 'export') {
      if (!status.dataJs || !groups.length) return '준비된 사진이 없습니다 — ② 준비를 먼저 하세요.';
      if (!hasPhotos) return '원본 사진이 없어 PDF를 만들 수 없습니다 — ① 사진 넣기에서 원본을 다시 넣으세요.';
    } else if (!hasPhotos) {
      return '원본 사진이 없습니다 — ① 사진 넣기에서 사진을 넣으세요.';
    }
    if (!envOk) return '환경이 준비되지 않았습니다 — 시작 파일(시작하기)을 다시 실행하세요.';
    if (kind === 'prepare' && planDiff(status)) return '원본이 계획과 달라 그대로 준비할 수 없습니다 — 화면 위쪽 안내에서 [새 행사 시작…]이나 [다시 나누기]를 고르세요.';
    return '';
  }

  function setControlsDisabled(force) {
    var prepareWhy = whyDisabled('prepare', !!force);
    var regroupWhy = whyDisabled('regroup', !!force);
    var exportWhy = whyDisabled('export', !!force);
    ui.openSrcBtn.disabled = !!force;
    (ui.drops || [ui.drop]).forEach(function (drop) {
      drop.setAttribute('aria-disabled', force ? 'true' : 'false');
    });
    ui.fileInput.disabled = !!force;
    ui.gap.disabled = !!force;
    ui.prepareBtn.disabled = !!prepareWhy;
    ui.regroupBtn.disabled = !!regroupWhy;
    ui.exportBtn.disabled = !!exportWhy;
    // 사진 준비 줄: 두 버튼 이유가 같으면 한 번만, 다르면 버튼 이름을 붙여 둘 다 적는다.
    var prepareLine = '';
    if (prepareWhy && regroupWhy === prepareWhy) prepareLine = prepareWhy;
    else {
      var pieces = [];
      if (prepareWhy) pieces.push(ui.prepareBtn.textContent + ': ' + prepareWhy);
      if (regroupWhy && !ui.regroupBtn.hidden) pieces.push('다시 나누기: ' + regroupWhy);
      prepareLine = pieces.join(' · ');
    }
    setText(ui.prepareWhy, prepareLine);
    setText(ui.exportWhy, exportWhy);
    ui.onlyDone.disabled = !!force;
    Object.keys(ui.exportModeRadios || {}).forEach(function (mode) {
      ui.exportModeRadios[mode].disabled = !!force;
    });
    if (ui.orderList) {
      Array.prototype.forEach.call(ui.orderList.querySelectorAll('.wfOrderHandle'), function (handle) {
        handle.setAttribute('draggable', force ? 'false' : 'true');
      });
    }
    ui.openOutBtn.disabled = !!force;

    // 시작 화면: 버튼이 꺼진 이유를 드롭 영역 안에 한 줄로 적는다.
    if (ui.startOpenSrc) {
      ui.startOpenSrc.disabled = !!force;
      if (ui.startResumeBtn) ui.startResumeBtn.disabled = !!prepareWhy;
      if (ui.startNewEventBtn) ui.startNewEventBtn.disabled = !newEventAvailability().enabled;
      var startReason = '';
      if (state.disabled) startReason = '시작 파일로 연 도구 서버에서만 사진을 넣을 수 있습니다.';
      else if (force) startReason = whyDisabled('prepare', true);
      else if (ui.startResume && !ui.startResume.hidden && prepareWhy) startReason = prepareWhy;
      setText(ui.startWhy, startReason);
    }
  }

  // ---- 단계 패널(오른쪽 서랍) 열고 닫기 ----
  function renderDrawer() {
    var step = state.drawerStep;
    var open = step !== null;
    ui.drawer.classList.toggle('open', open);
    ui.drawer.setAttribute('aria-hidden', open ? 'false' : 'true');
    [0, 1, 3].forEach(function (index) {
      ui.panes[index].hidden = step !== index;
      ui.tabs[index].setAttribute('aria-selected', step === index ? 'true' : 'false');
    });
    setText(ui.drawerTitle, open ? STEP_TITLES[step] : '');
    if (step === 3) syncExportOptions();
    notifyState();
  }

  function openStep(step) {
    if (!ui.drawer || !STEP_TITLES[step]) return;
    if (state.disabled) {
      notify('시작 파일로 연 도구 서버에서만 쓸 수 있습니다.', 5000);
      return;
    }
    state.drawerStep = step;
    renderDrawer();
  }

  // 헤더 단계 버튼: 같은 단계를 다시 누르면 닫는다.
  function toggleStep(step) {
    if (state.drawerStep === step) closeDrawer();
    else openStep(step);
  }

  function closeDrawer() {
    if (!ui.drawer || state.drawerStep === null) return;
    state.drawerStep = null;
    renderDrawer();
  }

  function pickFiles() {
    if (state.disabled || state.uploading || isBusy()) {
      notify(whyDisabled('upload', true) || '지금은 사진을 넣을 수 없습니다.', 4000);
      return;
    }
    ui.fileInput.click();
  }

  function isBusy() {
    return !!(state.job && state.job.state === 'running');
  }

  function setDropHighlight(on) {
    (ui.drops || []).forEach(function (drop) { drop.classList.toggle('dragover', on); });
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
      setDropHighlight(true);
    }, true);
    window.addEventListener('dragleave', function (event) {
      if (!hasFileTransfer(event)) return;
      if (!event.relatedTarget) setDropHighlight(false);
    }, true);
    window.addEventListener('drop', function (event) {
      if (!hasFileTransfer(event)) return;
      event.preventDefault();
      setDropHighlight(false);
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
      var dropped = Array.from(event.dataTransfer.files || []);
      var pdfs = dropped.filter(isPdfFile);
      if (pdfs.length) {
        // 발표자료 PDF 는 사진 원본 폴더로 받지 않는다 — 넣는 방법만 알려 준다(다른 사진은 그대로 올린다).
        notify(DECK_DROP_GUIDE, 8000);
        dropped = dropped.filter(function (file) { return !isPdfFile(file); });
        if (!dropped.length) {
          showBanner(DECK_DROP_GUIDE, 'info');
          return;
        }
      }
      uploadFiles(dropped);
    }, true);
  }

  async function uploadFiles(fileList) {
    var files = Array.from(fileList || []);
    if (!files.length || state.uploading || state.disabled) return;
    state.uploading = true;
    state.uploadPercent = 0;
    state.logs = [];
    setUploadResults('');
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
        var uploadHeaders = {'X-Filename': encodeURIComponent(file.name)};
        // EXIF 없는 사진은 수정 시각으로 촬영순을 정한다 — 원래 수정 시각을 서버에 알려 준다.
        if (Number.isFinite(file.lastModified) && file.lastModified > 0) {
          uploadHeaders['X-Last-Modified'] = String(Math.floor(file.lastModified));
        }
        var payload = await api('/api/upload', {
          method: 'POST',
          headers: uploadHeaders,
          body: file
        });
        var note = payload.dedup ? '같은 파일이라 건너뜀' :
          (payload.renamed ? '이름을 바꿔 저장: ' + payload.saved : '저장: ' + payload.saved);
        results.push(file.name + ' — ' + note);
      } catch (error) {
        failed += 1;
        results.push(file.name + ' — 실패: ' + (error.detail || error.message));
      }
      setUploadResults(results.join('\n'));
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

  // 화면에 보이는 그룹별 최종 사진 순서(촬영순 + 사용자 지정 순서 + 이동 반영, 발표자료 포함).
  // PDF 쪽 순서를 화면과 똑같이 맞추려고 그대로 서버에 보낸다. 못 읽으면 null(서버가 대체 순서 사용).
  function currentPhotoOrder() {
    try {
      if (typeof DATA === 'undefined' || !DATA || typeof DATA !== 'object') return null;
      var order = {};
      Object.keys(DATA).forEach(function (group) {
        order[group] = (DATA[group] || []).map(function (slide) { return String(slide.file); });
      });
      return order;
    } catch (error) {
      return null;
    }
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
    var mode = state.exportMode;
    startJob('/api/export-pdf', {
      backup: backup,
      photoOrder: currentPhotoOrder(),
      onlyDone: !!ui.onlyDone.checked,
      merge: mode === 'merged',
      mode: mode,
      order: mode === 'ordered' ? state.exportOrder.slice() : []
    }, 'export');
  }

  function startJob(path, payload, kind) {
    state.uploadPercent = 0;
    state.uploadLabel = '';
    if (kind === 'export') openStep(3);
    else if (!document.body.classList.contains('nodata')) openStep(1);
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
      showApiError(error);
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
      if (isNetworkError(error)) {
        showServerDown();
        return;
      }
      showBanner('서버에서 작업 상태를 읽지 못했습니다. 시작 파일로 도구를 다시 여세요.\n' + error.message, 'error');
    });
  }

  function finishJob(job) {
    if (state.pollTimer) clearTimeout(state.pollTimer);
    state.pollTimer = null;
    setControlsDisabled(false);
    if (job.kind === 'deck-import' && job.state !== 'cancelled') {
      var deckResult = job.result || {};
      if (job.state === 'done') {
        var deckMessage = '발표자료 ' + Number(deckResult.pages || 0) + '쪽을 넣었습니다';
        try { sessionStorage.setItem(NEW_EVENT_TOAST_KEY, deckMessage); } catch (_error) { /* 알림만 못 띄울 뿐 */ }
        showBanner(deckMessage + '. 새 목록을 불러옵니다.', 'ok');
        setTimeout(function () { location.reload(); }, 250);
        return;
      }
      (ui.progresses || []).forEach(function (box) { box.details.open = true; });
      showBanner((deckResult.detail || '발표자료를 넣지 못했습니다.') + '\n“자세한 기록”에서 마지막 오류를 확인하세요.', 'error', [
        {label: '자세한 기록 보기', onClick: function () {
          if (!document.body.classList.contains('nodata')) openStep(1);
        }}
      ]);
      refreshStatus();
      return;
    }
    if (job.state === 'done' && job.kind === 'prepare') {
      markOriginSeen();
      showBanner('사진 준비가 끝났습니다. 새 목록을 불러옵니다.', 'ok');
      setTimeout(function () { location.reload(); }, 250);
      return;
    }
    if (job.state === 'done' && job.kind === 'export') {
      if (typeof markBackedUp === 'function') markBackedUp();
      refreshStatus().then(function (status) {
        var count = status ? Number(status.resultCount || 0) : 0;
        var message = 'PDF ' + count + '개가 결과 폴더에 있습니다.';
        var pages = groupPageSummary(job.result);
        if (pages) message += '\n' + pages;
        showBanner(message, job.result && job.result.emptyGroups && job.result.emptyGroups.length ? 'warn' : 'ok');
      });
      return;
    }
    if (job.state === 'cancelled') {
      showBanner('작업을 취소했습니다. 일부 생성물은 남아 있을 수 있습니다.', 'warn');
      refreshStatus();
      return;
    }
    (ui.progresses || []).forEach(function (box) { box.details.open = true; });
    showBanner('작업이 실패했습니다. “자세한 기록”에서 마지막 오류를 확인하세요.', 'error', [
      {label: '자세한 기록 보기', onClick: function () {
        if (!document.body.classList.contains('nodata')) openStep(job.kind === 'export' ? 3 : 1);
      }}
    ]);
    refreshStatus();
  }

  // export 잡 결과(job.result)의 그룹별 쪽 수 한 줄. 한 쪽도 못 만든 그룹은 따로 알린다.
  function groupPageSummary(result) {
    if (!result || !Array.isArray(result.groups) || !result.groups.length) return '';
    var parts = result.groups.map(function (group) {
      return String(group.name) + ' ' + Number(group.pages || 0) + '쪽';
    });
    var line = '발표별 쪽 수: ' + parts.join(' · ');
    var empty = Array.isArray(result.emptyGroups) ? result.emptyGroups : [];
    if (empty.length) line += '\n쪽이 없어 빠진 발표: ' + empty.map(String).join(', ');
    return line;
  }

  function cancelJob() {
    if (!isBusy() || state.cancelling) return;
    state.cancelling = true;
    if (state.pollTimer) clearTimeout(state.pollTimer);
    state.pollTimer = null;
    (ui.progresses || []).forEach(function (box) { box.cancel.disabled = true; });
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
      (ui.progresses || []).forEach(function (box) { box.cancel.disabled = false; });
      showApiError(error);
      watchJob(state.activeKind);
    });
  }

  function openFolder(target) {
    if (state.disabled || isBusy() || state.uploading) return;
    api('/api/open-folder', {method: 'POST', json: {target: target}}).catch(showApiError);
  }

  function renderProgress() {
    notifyState();
    if (!ui.progresses) return;
    var boxes = ui.progresses;
    if (state.uploading || state.uploadPercent === 100) {
      boxes.forEach(function (box) {
        box.root.classList.add('show');
        setText(box.label, state.uploadLabel || '사진 업로드');
        box.fill.style.width = state.uploadPercent + '%';
        box.cancel.hidden = true;
        setText(box.note, '파일별 결과는 아래에 표시됩니다.');
        setText(box.log, '');
      });
      return;
    }
    var job = state.job;
    if (!job && !state.logs.length) {
      boxes.forEach(function (box) { box.root.classList.remove('show'); });
      return;
    }
    var phase = Number(job && job.phase || 0);
    var total = Number(job && job.phaseTotal || 0);
    var percent = total > 0 ? Math.round((phase / total) * 100) : 4;
    var pageNote = '';
    if (job && job.kind === 'deck-import') {
      // 쪽 그림을 만드는 동안에는 "쪽 3/12" 기록으로 진행률을 낸다(쪽이 많을 수 있다).
      var pages = deckPageProgress();
      if (phase === 1 && pages) {
        percent = Math.round((pages.done / pages.total) * 90);
        pageNote = ' · ' + pages.done + '/' + pages.total + '쪽';
      } else if (phase >= 2) {
        percent = 95;
      }
      if (job.state === 'running') showBanner('발표자료를 넣는 중입니다' + pageNote + ' — 끝날 때까지 창을 닫지 마세요.', 'info');
    }
    if (job && job.state === 'done') percent = 100;
    boxes.forEach(function (box) {
      box.root.classList.add('show');
      setText(box.label,
        job ? ((job.phaseName || '작업 중') + pageNote + (total ? ' · 단계 ' + phase + '/' + total : '')) : '작업 로그');
      box.fill.style.width = Math.max(0, Math.min(100, percent)) + '%';
      box.cancel.hidden = !(job && job.state === 'running');
      box.cancel.disabled = state.cancelling;
      setText(box.note, '');
      setText(box.log, state.logs.join('\n'));
    });
  }

  // deck_to_pages.py 가 쪽마다 찍는 "쪽 3/12" 중 마지막 것.
  function deckPageProgress() {
    for (var i = state.logs.length - 1; i >= 0; i -= 1) {
      var m = /^쪽 (\d+)\/(\d+)$/.exec(state.logs[i]);
      if (m) return {done: Number(m[1]), total: Number(m[2])};
    }
    return null;
  }

  function showBanner(message, level, actions) {
    if (!ui.banner) return;
    setText(ui.bannerText, message);
    setNoteActions(ui.bannerActions, actions);
    ui.banner.className = 'banner ' + (level || 'info');
    ui.banner.hidden = false;
  }

  function clearBanner() {
    if (!ui.banner) return;
    setText(ui.bannerText, '');
    setNoteActions(ui.bannerActions, null);
    ui.banner.className = 'banner';
    ui.banner.hidden = true;
  }

  // 서버 연결 자체가 끊긴 경우(fetch 가 응답 없이 실패)와 서버가 거절한 경우를 구분한다.
  function showApiError(error) {
    if (isNetworkError(error)) showServerDown();
    else showBanner((error && (error.detail || error.message)) || '요청에 실패했습니다.', 'error');
  }

  function isNetworkError(error) {
    return !!error && error.status === undefined && error instanceof TypeError;
  }

  function showServerDown() {
    state.offline = true;
    setControlsDisabled(true);
    notifyState();
    showBanner('도구 서버가 꺼졌습니다. 시작 파일(시작하기)을 다시 실행하세요.', 'error', [
      {label: '다시 연결', className: 'primary', onClick: reconnect}
    ]);
  }

  function reconnect() {
    clearBanner();
    TOKEN = null;
    return connect();
  }

  function connect() {
    return fetchToken().then(function () {
      return refreshStatus();
    }).then(function (status) {
      if (!status) return;
      bindDnD();
    }).catch(function (error) {
      state.disabled = true;
      notifyState();
      if (unavailable(error)) {
        closeDrawer();
        return;
      }
      setControlsDisabled(true);
      if (isNetworkError(error)) {
        showServerDown();
        return;
      }
      showBanner(
        '보안 확인 실패 — 이 서버는 localhost 주소로 연 페이지에서만 쓸 수 있습니다. ' +
        'http://localhost:' + (location.port || '포트') + '/slide_tool/ 주소로 여세요.\n' +
        '현재 주소: ' + location.origin + '\n' +
        (error.detail || error.message),
        'error',
        [{label: '다시 연결', onClick: reconnect}]
      );
    });
  }

  // ===================== 발표자료 PDF 넣기 =====================
  // 발표 ⋯ 메뉴 → PDF 고르기 → 서버가 쪽 수를 알려 줌(inspect) → 확인 대화 → 잡(쪽마다 그림 만들기 → 목록 만들기).
  function deckAvailability() {
    if (state.offline) return {enabled: false, reason: '서버에 연결되지 않았습니다 — [다시 연결]을 누르세요.'};
    if (state.disabled) return {enabled: false, reason: '시작 파일로 연 도구 서버에서만 쓸 수 있습니다.'};
    if (!TOKEN || !state.status) return {enabled: false, reason: '도구 서버 연결을 확인하는 중입니다.'};
    if (isBusy() || state.uploading || state.deckBusy) return {enabled: false, reason: '실행 중인 작업이 끝난 뒤 쓰세요.'};
    if (state.status.env && state.status.env.pdfium === false) {
      return {
        enabled: false,
        reason: '발표자료 PDF 기능(pypdfium2)이 설치되어 있지 않습니다. 시작 파일을 다시 실행하거나 터미널에서 pip install pypdfium2 를 실행하세요.'
      };
    }
    return {enabled: true, reason: ''};
  }

  function isPdfFile(file) {
    return !!file && (/\.pdf$/i.test(String(file.name || '')) || file.type === 'application/pdf');
  }

  function deckHeaders(group, file, mode, replace) {
    // 서버는 파일명을 검사만 한다(#·?·% 등은 거부) — PDF 이름은 쓰이지 않으므로 안전한 글자로 바꿔 보낸다.
    var name = String(file.name || 'deck.pdf').replace(/[#?%<>:"|*\\\/\u0000-\u001f]/g, '_').replace(/^\.+/, '_').replace(/[ .]+$/, '');
    if (!/\.pdf$/i.test(name)) name = 'deck.pdf';
    return {
      'X-Filename': encodeURIComponent(name),
      'X-Group': encodeURIComponent(group),
      'X-Deck-Mode': mode,
      'X-Replace': replace ? '1' : '0'
    };
  }

  function pickDeckPdf(group) {
    var availability = deckAvailability();
    if (!availability.enabled) {
      notify(availability.reason, 6000);
      return;
    }
    state.deckGroup = group;
    ui.deckInput.value = '';
    ui.deckInput.click();
  }

  function inspectDeck(group, file) {
    if (!group) return;
    if (!isPdfFile(file)) {
      notify('PDF 파일만 넣을 수 있습니다.', 5000);
      return;
    }
    if (file.size > DECK_PDF_MAX_BYTES) {
      notify('PDF가 너무 큽니다(200MB까지 넣을 수 있습니다).', 6000);
      return;
    }
    clearBanner();
    state.deckBusy = true;
    setControlsDisabled(true);
    showBanner('PDF를 읽는 중입니다… ' + file.name, 'info');
    api('/api/deck-import', {method: 'POST', headers: deckHeaders(group, file, 'inspect', false), body: file}).then(function (info) {
      state.deckBusy = false;
      clearBanner();
      setControlsDisabled(false);
      confirmDeck(group, file, info);
    }).catch(function (error) {
      state.deckBusy = false;
      setControlsDisabled(false);
      showApiError(error);
    });
  }

  function confirmDeck(group, file, info) {
    if (typeof openModal !== 'function') return;
    var pages = Number(info.pages || 0);
    var existing = Number(info.existing || 0);
    var deckNo = Number(info.deckNo);
    var label = 'DECK' + (deckNo < 10 ? '0' : '') + deckNo;
    var body = el('div', {}, [
      el('p', {}, [
        el('strong', {text: pages + '쪽'}),
        document.createTextNode('을 «' + group + '» 발표에 자료로 넣습니다.')
      ]),
      el('p', {
        className: 'wfRowInfo',
        text: '«' + file.name + '» 를 쪽마다 그림으로 바꿔 넣습니다(' + label + '_p001 …). ' +
          '자료 쪽은 경계·색보정 없이 원본 그대로 PDF에 들어가고, 위치는 고정됩니다.'
      })
    ]);
    if (existing > 0) {
      body.appendChild(el('p', {}, [
        el('strong', {text: '이 발표에는 같은 번호(' + label + ')의 발표자료가 이미 ' + existing + '쪽 있습니다.'}),
        document.createTextNode(' 교체하면 기존 쪽은 삭제하지 않고 이 발표 폴더 안의 보관 폴더로 옮깁니다.')
      ]));
    }
    openModal({
      title: existing > 0 ? '발표자료 교체' : '발표자료 PDF 넣기',
      node: body,
      ok: existing > 0 ? '교체해서 넣기' : '넣기',
      okKind: existing > 0 ? 'danger' : 'primary',
      onOk: function () { startDeckImport(group, file, existing > 0); }
    });
  }

  function startDeckImport(group, file, replace) {
    if (state.disabled || isBusy() || state.uploading || state.deckBusy) return;
    clearBanner();
    state.uploadPercent = 0;
    state.uploadLabel = '';
    state.deckBusy = true;
    if (!document.body.classList.contains('nodata')) openStep(1);
    setControlsDisabled(true);
    showBanner('발표자료 PDF를 서버로 보내는 중입니다… ' + file.name, 'info');
    api('/api/deck-import', {method: 'POST', headers: deckHeaders(group, file, 'import', replace), body: file}).then(function (response) {
      state.deckBusy = false;
      state.job = response.job || {kind: 'deck-import', state: 'running'};
      state.job.state = state.job.state || 'running';
      state.activeKind = 'deck-import';
      state.logs = [];
      state.after = 0;
      renderProgress();
      watchJob('deck-import');
    }).catch(function (error) {
      state.deckBusy = false;
      state.job = null;
      setControlsDisabled(false);
      showApiError(error);
      renderProgress();
    });
  }

  // ===================== 새 행사 시작 =====================
  function readJsonKey(key) {
    try {
      var raw = localStorage.getItem(key);
      var value = raw ? JSON.parse(raw) : null;
      return value && typeof value === 'object' ? value : null;
    } catch (_error) {
      return null;
    }
  }

  // 이 브라우저에 저장된 보정값 요약(모서리·완료/제외·색보정 장수).
  function storedWorkSummary() {
    var corners = readJsonKey('slideCorners_v1');
    var status = readJsonKey('slideStatus_v1');
    var color = readJsonKey('slideColor_v1');
    var counts = {
      corners: corners ? Object.keys(corners).length : 0,
      status: status ? Object.keys(status).length : 0,
      color: color ? Object.keys(color).length : 0
    };
    counts.any = counts.corners + counts.status + counts.color > 0;
    return counts;
  }

  function markOriginSeen() {
    try { localStorage.setItem(ORIGIN_SEEN_KEY, '1'); } catch (_error) { /* 저장 못 해도 안내만 다시 뜰 뿐 */ }
  }

  function originSeen() {
    try { return localStorage.getItem(ORIGIN_SEEN_KEY) === '1'; } catch (_error) { return false; }
  }

  // 새 행사 시작 확인 — index.html 의 openModal 을 쓴다. 서버 요청이 끝날 때까지 닫히지 않는다(keepOpen).
  function confirmNewEvent() {
    if (state.disabled || isBusy() || state.uploading || !state.status) return;
    if (typeof openModal !== 'function') return;
    var status = state.status;
    var groups = Array.isArray(status.groups) ? status.groups : [];
    var stored = storedWorkSummary();
    var srcCount = Number(status.srcCount || 0);
    var list = el('ul');
    list.appendChild(el('li', {
      text: '발표 ' + groups.length + '개 · 작업용 사진 ' + photoTotal(status) + '장 (발표 폴더·발표 나누기 계획·목록)'
    }));
    list.appendChild(el('li', {
      text: stored.any
        ? '저장된 보정값 — 경계 ' + stored.corners + '장 · 완료/제외 ' + stored.status + '장 · 색보정 ' + stored.color +
          '장 (백업 파일로 저장한 뒤 이 브라우저에서 지웁니다)'
        : '저장된 보정값 — 없음'
    }));
    list.appendChild(el('li', {text: 'PDF(결과물 폴더)는 그대로 둡니다.'}));
    var originals = el('input', {type: 'checkbox'});
    originals.disabled = srcCount === 0;
    var body = el('div', {}, [
      el('p', {text: '지금 작업을 통째로 보관하고 빈 작업장에서 시작합니다. 삭제하지 않고 작업장 안의 보관 폴더로 옮깁니다.'}),
      list,
      el('label', {className: 'wfNeOption'}, [
        originals,
        el('span', {text: '원본 사진도 함께 보관 (원본 폴더의 ' + srcCount + '장을 보관 폴더로 옮김)'})
      ]),
      el('p', {
        className: 'wfRowInfo',
        text: '체크하지 않으면 원본 폴더의 사진은 그대로 남아, 다음 행사 사진과 섞일 수 있습니다.'
      })
    ]);
    ui.neModal = openModal({
      title: '새 행사 시작',
      node: body,
      ok: '백업 받고 시작',
      okKind: 'danger',
      keepOpen: true,
      canClose: function () { return !state.newEventRunning; },
      onOk: function () { runNewEvent(!!originals.checked); }
    });
  }

  // 이 도구가 쓰는 localStorage 키를 모두 지우고(index.html 의 목록 + 이 패널의 접기 상태) 새로고침한다.
  function clearToolStorageAndReload(toastMessage) {
    try {
      if (typeof clearToolStorage === 'function') clearToolStorage();
      localStorage.removeItem(COLLAPSE_KEY);
      localStorage.removeItem(ORIGIN_SEEN_KEY);
    } catch (_error) { /* 접근이 막혀 있으면 지울 것도 없다 */ }
    markOriginSeen();
    try { sessionStorage.setItem(NEW_EVENT_TOAST_KEY, toastMessage); } catch (_error) {}
    location.reload();
  }

  function runNewEvent(moveOriginals) {
    var modal = ui.neModal;
    if (state.newEventRunning || !modal) return;
    if (typeof collectBackup !== 'function') {
      modal.setError('현재 보정값을 수집하지 못했습니다. 페이지를 새로고침한 뒤 다시 시도하세요.');
      return;
    }
    var backup;
    try {
      backup = collectBackup();
    } catch (error) {
      modal.setError('현재 보정값을 읽지 못했습니다.\n' + error.message);
      return;
    }
    state.newEventRunning = true;
    modal.setBusy(true, '보관하는 중…');
    modal.setError('');
    api('/api/new-event', {
      method: 'POST',
      json: {backup: backup, moveOriginals: !!moveOriginals}
    }).then(function (payload) {
      clearToolStorageAndReload('이전 작업을 ' + payload.archive + ' 에 보관했습니다');
    }).catch(function (error) {
      state.newEventRunning = false;
      modal.setBusy(false, '백업 받고 시작');
      modal.setError(isNetworkError(error)
        ? '도구 서버가 꺼졌습니다. 시작 파일(시작하기)을 다시 실행하세요.'
        : ((error && (error.detail || error.message)) || '요청에 실패했습니다.'));
    });
  }

  function showPendingToast() {
    var message = null;
    try {
      message = sessionStorage.getItem(NEW_EVENT_TOAST_KEY);
      if (message) sessionStorage.removeItem(NEW_EVENT_TOAST_KEY);
    } catch (_error) { message = null; }
    if (!message) return;
    if (typeof toast === 'function') toast(message, 9000);
    showBanner(message, 'ok');
  }

  // ===================== 이 주소에 저장된 작업 없음 안내 =====================
  function buildLocalBand(host) {
    var note = buildNote('banner warn');
    note.root.id = 'wfLocalBand';
    ui.localBand = note.root;
    ui.localBandText = note.text;
    ui.localBandActions = note.actions;
    host.prepend(ui.localBand);
  }

  function formatBackupTime(seconds) {
    var date = new Date(Number(seconds) * 1000);
    if (isNaN(date.getTime())) return '';
    function two(n) { return (n < 10 ? '0' : '') + n; }
    return two(date.getMonth() + 1) + '-' + two(date.getDate()) + ' ' + two(date.getHours()) + ':' + two(date.getMinutes());
  }

  function hideLocalBand() {
    ui.localBand.hidden = true;
    setText(ui.localBandText, '');
    setNoteActions(ui.localBandActions, null);
  }

  function dismissLocalBand() {
    markOriginSeen();
    hideLocalBand();
  }

  // 작업장에 그룹·사진이 있는데 이 출처에는 도구 저장값이 하나도 없고, 서버에 백업 파일이 있을 때만 띄운다.
  // (포트·주소가 바뀌면 브라우저가 다른 사이트로 봐서 저장값이 비어 보인다.)
  function maybeShowLocalBand(status) {
    if (state.localBandChecked || state.disabled) return;
    var groups = Array.isArray(status.groups) ? status.groups : [];
    if (!groups.length || photoTotal(status) === 0) return;
    if (typeof hasStoredWork !== 'function') return;
    state.localBandChecked = true;
    if (hasStoredWork() || originSeen()) return;
    api('/api/backups').then(function (payload) {
      var backups = payload && Array.isArray(payload.backups) ? payload.backups : [];
      if (!backups.length) return;
      var latest = backups[0];
      var when = formatBackupTime(latest.modified);
      setText(ui.localBandText,
        '이 주소(' + location.host + ')에는 저장된 보정값이 없습니다. 다른 주소(포트)에서 작업했다면 ' +
        '최근 백업' + (when ? '(' + when + ')' : '') + '을 불러올 수 있습니다.');
      setNoteActions(ui.localBandActions, [
        {label: '최근 백업 불러오기', className: 'primary', onClick: function () { loadLatestBackup(latest.name); }},
        {label: '닫기', onClick: dismissLocalBand}
      ]);
      ui.localBand.hidden = false;
    }).catch(function () { /* 안내 띠는 부가 기능 — 목록을 못 받으면 조용히 넘어간다 */ });
  }

  function notify(message, ms) {
    if (typeof toast === 'function') toast(message, ms);
    else showBanner(message, 'info');
  }

  function loadLatestBackup(name) {
    if (state.disabled || isBusy() || state.uploading) return;
    setNoteActions(ui.localBandActions, null);
    api('/api/backup?name=' + encodeURIComponent(name)).then(function (payload) {
      if (typeof applyBackup !== 'function') throw new Error('백업 복원 기능을 찾지 못했습니다.');
      var count = applyBackup(payload.backup);
      if (!count) {
        notify('백업에 복원할 항목이 없습니다.', 6000);
        return;
      }
      if (typeof markBackedUp === 'function') markBackedUp();
      markOriginSeen();
      notify('백업을 불러왔습니다 (' + count + '개 항목). 새로고침합니다.', 4000);
      setTimeout(function () { location.reload(); }, 900);
    }).catch(function (error) {
      notify('백업을 불러오지 못했습니다: ' + ((error && (error.detail || error.message)) || error), 7000);
      maybeRestoreBandButtons(name);
    });
  }

  function maybeRestoreBandButtons(name) {
    setNoteActions(ui.localBandActions, [
      {label: '최근 백업 불러오기', className: 'primary', onClick: function () { loadLatestBackup(name); }},
      {label: '닫기', onClick: dismissLocalBand}
    ]);
  }

  function init() {
    injectStyle();
    if (!buildUi()) return;
    showPendingToast();
    connect();
  }

  window.__slideWorkflow = {
    refreshStatus: refreshStatus,
    renameAvailability: renameAvailability,
    renameGroup: renameGroup,
    autoDetectAvailability: autoDetectAvailability,
    autoDetect: autoDetect,
    // 헤더(index.html)가 쓰는 것: 서버 상태 요약 · 단계 패널 · 사진 넣기/준비/폴더 열기 · 다시 연결 · 새 행사
    summary: summary,
    openStep: openStep,
    toggleStep: toggleStep,
    closeDrawer: closeDrawer,
    pickFiles: pickFiles,
    prepare: function () { runPrepare(false); },
    openFolder: openFolder,
    reconnect: reconnect,
    confirmNewEvent: confirmNewEvent,
    newEventAvailability: newEventAvailability,
    // 발표 ⋯ 메뉴의 [발표자료 PDF 넣기…]
    deckAvailability: deckAvailability,
    pickDeckPdf: pickDeckPdf
  };
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init);
  else init();
})();
