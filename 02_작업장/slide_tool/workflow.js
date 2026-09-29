(function () {
  'use strict';

  var TOKEN = null;
  var POLL_MS = 700;
  var COLLAPSE_KEY = 'wfPanelCollapsed_v1';
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
    collapsed: false,
    collapseReady: false,
    expandedStep: null,
    manualStep: false,
    exportMode: 'per-folder',
    exportOrder: [],
    draggedGroup: null,
    disabled: false,
    offline: false,
    newEventRunning: false,
    localBandChecked: false,
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
        text: '≡',
        draggable: 'true',
        title: '드래그해 순서 변경',
        'aria-label': name + ' 순서 변경 핸들'
      });
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

  function attachCorrectionEditor() {
    var content = ui.accordions && ui.accordions[2] && ui.accordions[2].content;
    if (!content) return;
    if (ui.correctionEmpty && !content.contains(ui.correctionEmpty)) content.appendChild(ui.correctionEmpty);
    if (ui.correctionEditor && !content.contains(ui.correctionEditor)) content.appendChild(ui.correctionEditor);
  }

  function restoreCorrectionEditor() {
    if (ui.emptyAnchor && ui.emptyAnchor.parentNode && ui.correctionEmpty &&
        ui.correctionEmpty.parentNode !== ui.emptyAnchor.parentNode) {
      ui.emptyAnchor.parentNode.insertBefore(ui.correctionEmpty, ui.emptyAnchor.nextSibling);
    }
    if (ui.editorAnchor && ui.editorAnchor.parentNode && ui.correctionEditor &&
        ui.correctionEditor.parentNode !== ui.editorAnchor.parentNode) {
      ui.editorAnchor.parentNode.insertBefore(ui.correctionEditor, ui.editorAnchor.nextSibling);
    }
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
      '.wfAccordion{border-bottom:1px solid var(--line)}',
      '.wfAccordionHead{display:flex;align-items:center;justify-content:space-between;gap:12px;padding:10px 0}',
      '.wfAccordionToggle{min-width:62px}',
      '.wfAccordionContent[hidden]{display:none}',
      '.wfRow{display:grid;grid-template-columns:minmax(120px,.28fr) minmax(280px,1fr);gap:14px;padding:3px 0 13px}',
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
      '.wfExportModes{display:grid;gap:7px;margin-top:4px}',
      '.wfExportModes[hidden],.wfOrderBox[hidden]{display:none}',
      '.wfExportOption{display:flex;align-items:flex-start;gap:7px;padding:8px 10px;border:1px solid var(--line);border-radius:7px;background:#f8fafc;font-size:12px;color:var(--ink);cursor:pointer}',
      '.wfExportOption:has(input:checked){border-color:var(--acc);background:#eff6ff}',
      '.wfExportOption input{margin-top:3px}',
      '.wfExportOption strong{display:block}',
      '.wfExportOption span{display:block;color:var(--mut);font-size:11px}',
      '.wfOrderBox{margin:8px 0 0;padding:9px 10px;border:1px solid var(--line);border-radius:7px}',
      '.wfOrderTitle{font-size:12px;font-weight:800;margin-bottom:6px}',
      '.wfOrderList{list-style:none;margin:0;padding:0;display:grid;gap:5px}',
      '.wfOrderItem{display:flex;align-items:center;gap:8px;padding:6px 8px;border:1px solid var(--line);border-radius:5px;background:#fff;font-size:12px}',
      '.wfOrderItem.dragging{opacity:.45}',
      '.wfOrderHandle{font-size:17px;line-height:1;color:var(--mut);cursor:grab;user-select:none}',
      '.wfOrderHandle:active{cursor:grabbing}',
      '.wfBanner{display:none;margin-top:11px;padding:9px 11px;border-radius:6px;font-size:12px;white-space:pre-wrap}',
      '.wfBanner.show{display:block}',
      '.wfBanner.info{background:#eff6ff;color:var(--acc2);border:1px solid #bfdbfe}',
      '.wfBanner.ok{background:#ecfdf3;color:var(--ok);border:1px solid #bbf7d0}',
      '.wfBanner.warn{background:#fffbeb;color:var(--warn);border:1px solid #fde68a}',
      '.wfBanner.error{background:#fef2f2;color:var(--bad);border:1px solid #fecaca}',
      '.wfNoteActions{display:flex;align-items:center;gap:8px;flex-wrap:wrap;margin-top:7px}',
      '.wfNoteActions[hidden]{display:none}',
      '.wfHeadActions{display:flex;align-items:center;gap:8px;flex-wrap:wrap}',
      '.wfWhy{font-size:11px;color:var(--warn)}',
      '.wfWhy:empty{display:none}',
      '#wfLocalBand{margin:0 0 10px}',
      '.wfNeList{margin:7px 0;padding-left:18px;font-size:13px}',
      '.wfNeList li{margin:2px 0}',
      '.wfNeOption{display:flex;align-items:flex-start;gap:7px;margin-top:10px;font-size:13px}',
      '.wfNeOption input{margin-top:3px}',
      '.wfNeError{color:var(--bad);font-size:12px;margin-top:8px;white-space:pre-wrap}',
      '.wfNeError:empty{display:none}',
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
      'body.wfCorrectionCollapsed #editor,body.wfCorrectionCollapsed #empty{display:none!important}',
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
    headLeft.appendChild(ui.summary);
    ui.collapseBtn = el('button', {type: 'button', className: 'btn sm', text: '접기'});
    ui.collapseBtn.addEventListener('click', function () {
      setCollapsed(!state.collapsed, true);
    });
    ui.newEventBtn = el('button', {
      type: 'button',
      className: 'btn sm warn',
      text: '새 행사 시작…',
      title: '지금 작업을 보관 폴더로 옮기고 빈 작업장에서 새로 시작합니다 (삭제하지 않습니다)'
    });
    ui.newEventBtn.hidden = true;
    ui.newEventBtn.addEventListener('click', confirmNewEvent);
    head.appendChild(headLeft);
    head.appendChild(el('div', {className: 'wfHeadActions'}, [ui.newEventBtn, ui.collapseBtn]));
    ui.panel.appendChild(head);

    // 알림 띠 두 개: 배너(작업 결과·오류, 필요하면 버튼 하나)와 알림(상태에서 계산되는 안내 — 원본 변경 등).
    var banner = buildNote('wfBanner');
    ui.banner = banner.root;
    ui.bannerText = banner.text;
    ui.bannerActions = banner.actions;
    ui.panel.appendChild(ui.banner);
    var notice = buildNote('wfBanner');
    ui.notice = notice.root;
    ui.noticeText = notice.text;
    ui.noticeActions = notice.actions;
    ui.panel.appendChild(ui.notice);

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
    var fileRow = el('div', {className: 'wfRow'}, [fileInfo, fileWork]);

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
    ui.prepareWhy = el('span', {className: 'wfWhy'});
    prepareWork.appendChild(el('div', {className: 'wfControls'}, [
      el('label', {}, [document.createTextNode('발표 간격(분)'), ui.gap]),
      ui.prepareBtn,
      ui.regroupBtn,
      ui.prepareWhy
    ]));
    ui.groupInfo = el('div', {className: 'wfCounts', text: '그룹 계획 없음'});
    ui.envInfo = el('div', {className: 'wfEnv', text: '환경 상태 확인 중'});
    prepareWork.appendChild(ui.groupInfo);
    prepareWork.appendChild(ui.envInfo);
    var prepareRow = el('div', {className: 'wfRow'}, [prepareInfo, prepareWork]);

    var exportInfo = el('div', {}, [
      el('h3', {text: '④ PDF 만들기'}),
      el('div', {className: 'wfRowInfo', text: '현재 모서리·색 보정값을 원본 사진에 적용해 고해상도 PDF를 만듭니다.'})
    ]);
    var exportWork = el('div');
    ui.onlyDone = el('input', {type: 'checkbox'});
    ui.exportModeRadios = {};
    ui.exportModes = el('div', {className: 'wfExportModes'});
    ui.exportModes.appendChild(exportModeOption(
      'per-folder',
      'A. 폴더별로 내보내기',
      '그룹마다 PDF를 1개씩 만듭니다.'
    ));
    ui.multiExportModes = el('div', {className: 'wfExportModes'});
    ui.multiExportModes.appendChild(exportModeOption(
      'merged',
      'B. 한꺼번에 합쳐서 내보내기',
      '그룹별 PDF와 모든 그룹을 합친 통합본을 함께 만듭니다.'
    ));
    ui.multiExportModes.appendChild(exportModeOption(
      'ordered',
      'C. 순서 변경 후 1개로 합치기',
      '아래 그룹 순서대로 통합 PDF 1개만 만듭니다.'
    ));
    ui.exportModes.appendChild(ui.multiExportModes);
    ui.orderBox = el('div', {className: 'wfOrderBox'});
    ui.orderBox.appendChild(el('div', {className: 'wfOrderTitle', text: '≡ 핸들을 끌어 PDF 페이지 묶음 순서를 바꾸세요.'}));
    ui.orderList = el('ol', {className: 'wfOrderList'});
    ui.orderBox.appendChild(ui.orderList);
    ui.exportBtn = el('button', {type: 'button', className: 'btn on', text: '선택한 방식으로 PDF 만들기'});
    ui.openOutBtn = el('button', {type: 'button', className: 'btn', text: '결과 폴더 열기'});
    ui.exportBtn.addEventListener('click', runExportPdf);
    ui.openOutBtn.addEventListener('click', function () { openFolder('out'); });
    exportWork.appendChild(ui.exportModes);
    exportWork.appendChild(ui.orderBox);
    ui.exportWhy = el('span', {className: 'wfWhy'});
    exportWork.appendChild(el('div', {className: 'wfControls'}, [
      ui.exportBtn,
      el('label', {}, [ui.onlyDone, document.createTextNode('완료본만')]),
      ui.openOutBtn,
      ui.exportWhy
    ]));
    ui.resultInfo = el('div', {className: 'wfCounts', text: '현재 결과 PDF 0권'});
    exportWork.appendChild(ui.resultInfo);
    exportWork.appendChild(el('div', {
      className: 'wfEnv',
      text: '기존 “백업 내보내기” 다운로드도 오프라인 백업용으로 계속 사용할 수 있습니다.'
    }));
    var exportRow = el('div', {className: 'wfRow'}, [exportInfo, exportWork]);

    var correctionEmpty = document.getElementById('empty');
    var correctionEditor = document.getElementById('editor');
    var correctionSlot = document.createComment('correction-editor-slot');
    if (correctionEmpty && correctionEmpty.parentNode) {
      ui.emptyAnchor = document.createComment('correction-empty-anchor');
      correctionEmpty.parentNode.insertBefore(ui.emptyAnchor, correctionEmpty);
    }
    if (correctionEditor && correctionEditor.parentNode) {
      ui.editorAnchor = document.createComment('correction-editor-anchor');
      correctionEditor.parentNode.insertBefore(ui.editorAnchor, correctionEditor);
    }
    ui.correctionEmpty = correctionEmpty;
    ui.correctionEditor = correctionEditor;

    ui.accordions = [];
    [fileRow, prepareRow, correctionSlot, exportRow].forEach(function (content, index) {
      var section = el('section', {className: 'wfAccordion', 'data-step': String(index + 1)});
      var toggle = el('button', {
        type: 'button',
        className: 'btn sm wfAccordionToggle',
        text: '펼치기',
        'aria-expanded': 'false'
      });
      toggle.addEventListener('click', function () { setExpandedStep(index, true); });
      section.appendChild(el('div', {className: 'wfAccordionHead'}, [ui.steps[index], toggle]));
      var contentBox = null;
      if (content) {
        contentBox = el('div', {className: 'wfAccordionContent'}, [content]);
        contentBox.hidden = true;
        section.appendChild(contentBox);
      }
      ui.accordions.push({section: section, content: contentBox, toggle: toggle});
      ui.body.appendChild(section);
    });

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
    buildLocalBand(main);
    buildModal();
    buildNewEventModal();
    return true;
  }

  // 글 한 덩어리 + 버튼 줄로 된 알림 띠 뼈대.
  function buildNote(className) {
    var text = el('div');
    var actions = el('div', {className: 'wfNoteActions'});
    actions.hidden = true;
    var root = el('div', {className: className, role: 'status'}, [text, actions]);
    return {root: root, text: text, actions: actions};
  }

  function setNoteActions(box, actions) {
    box.textContent = '';
    (actions || []).forEach(function (action) {
      var button = el('button', {
        type: 'button',
        className: 'btn sm' + (action.className ? ' ' + action.className : ''),
        text: action.label
      });
      if (action.disabled) button.disabled = true;
      button.addEventListener('click', action.onClick);
      box.appendChild(button);
    });
    box.hidden = !(actions && actions.length);
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

  function renameAvailability() {
    if (state.disabled) {
      return {enabled: false, reason: '시작 파일로 연 워크플로 서버에서만 이름을 바꿀 수 있습니다.'};
    }
    if (!TOKEN || !state.status) {
      return {enabled: false, reason: '워크플로 서버 연결을 확인하는 중입니다.'};
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

  // 보정 화면의 '자동 찾기' — 서버가 사진 안의 슬라이드 경계를 OpenCV 로 찾아 준다(동기 응답).
  // 서버 없이 파일로 연 경우(state.disabled)에는 쓸 수 없고, 준비·PDF 잡이 도는 동안에도 잠근다.
  function autoDetectAvailability() {
    if (state.disabled) {
      return {enabled: false, reason: '시작 파일로 연 워크플로 서버에서만 자동으로 찾을 수 있습니다.'};
    }
    if (!TOKEN || !state.status) {
      return {enabled: false, reason: '워크플로 서버 연결을 확인하는 중입니다.'};
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
        restoreCorrectionEditor();
        document.body.classList.remove('wfCorrectionCollapsed');
        notifyState();
        return null;
      }
      state.status = payload;
      state.disabled = false;
      state.offline = false;
      attachCorrectionEditor();
      ui.panel.hidden = false;
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
        ui.panel.hidden = true;
        restoreCorrectionEditor();
        document.body.classList.remove('wfCorrectionCollapsed');
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
    var running = isBusy() || state.uploading || !!(status.job && status.job.state === 'running');
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
    syncExportOrder(groups.map(function (group) { return String(group.name); }));
    syncExportOptions();
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
    ui.newEventBtn.hidden = !hasArchivableWork(status);

    if (!state.collapseReady) {
      var saved = null;
      try { saved = localStorage.getItem(COLLAPSE_KEY); } catch (_error) { saved = null; }
      if (saved === null) {
        state.collapsed = false;
      }
      else state.collapsed = saved === '1';
      state.collapseReady = true;
      setCollapsed(state.collapsed, false);
    }
    if (!state.manualStep) setExpandedStep(automaticStep(status), false);
    setControlsDisabled(running || state.disabled);
    renderNotice(status);
    maybeShowLocalBand(status);
    renderProgress();
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
        '(이전 작업 그룹은 그대로 남아 있어 보정 화면에서 계속 편집할 수 있습니다.)';
      actions = [{label: '새 행사 시작…', className: 'warn', disabled: running, onClick: confirmNewEvent}];
    } else if (diff) {
      var parts = [];
      if (diff.added) parts.push('새 사진 ' + diff.added + '장');
      if (diff.missing) parts.push('없어진 사진 ' + diff.missing + '장');
      text = '원본이 바뀌었습니다(' + parts.join(', ') + '). ' +
        '지금 작업을 보관하고 새로 시작하거나, 원본 전체로 그룹을 다시 나누세요.';
      actions = [
        {label: '새 행사로 시작', className: 'warn', disabled: running, onClick: confirmNewEvent},
        {label: '다시 나누기', disabled: running, onClick: confirmRegroup}
      ];
    }
    if (!text) {
      ui.notice.className = 'wfBanner';
      setText(ui.noticeText, '');
      setNoteActions(ui.noticeActions, null);
      return;
    }
    setText(ui.noticeText, text);
    setNoteActions(ui.noticeActions, actions);
    ui.notice.className = 'wfBanner show warn';
  }

  // 버튼이 꺼진 이유 한 줄. 켜져 있으면 ''.
  function whyDisabled(kind, force) {
    var status = state.status || {};
    var hasPhotos = Number(status.srcCount || 0) > 0;
    var envOk = !!(status.env && status.env.ok);
    var groups = Array.isArray(status.groups) ? status.groups : [];
    if (state.offline) return '서버에 연결되지 않았습니다 — [다시 연결]을 누르세요.';
    if (force) return state.uploading ? '사진을 올리는 중입니다 — 끝난 뒤 누르세요.' : '작업이 실행 중입니다 — 끝난 뒤 누르세요.';
    if (kind === 'export') {
      if (!status.dataJs || !groups.length) return '준비된 사진이 없습니다 — ② 사진 준비를 먼저 하세요.';
      if (!hasPhotos) return '원본 사진이 없어 PDF를 만들 수 없습니다 — ① 에서 원본을 다시 넣으세요.';
    } else if (!hasPhotos) {
      return '원본 사진이 없습니다 — ① 에서 사진을 넣으세요.';
    }
    if (!envOk) return '환경이 준비되지 않았습니다 — 시작 파일(시작하기)을 다시 실행하세요.';
    if (kind === 'prepare' && planDiff(status)) return '원본이 계획과 달라 그대로 준비할 수 없습니다 — 위 안내에서 고르세요.';
    return '';
  }

  function setControlsDisabled(force) {
    var prepareWhy = whyDisabled('prepare', !!force);
    var regroupWhy = whyDisabled('regroup', !!force);
    var exportWhy = whyDisabled('export', !!force);
    ui.openSrcBtn.disabled = !!force;
    ui.drop.setAttribute('aria-disabled', force ? 'true' : 'false');
    ui.fileInput.disabled = !!force;
    ui.gap.disabled = !!force;
    ui.prepareBtn.disabled = !!prepareWhy;
    ui.regroupBtn.disabled = !!regroupWhy;
    ui.exportBtn.disabled = !!exportWhy;
    ui.newEventBtn.disabled = !!force;
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
  }

  function setCollapsed(value, persist) {
    state.collapsed = !!value;
    ui.panel.classList.toggle('wfCollapsed', state.collapsed);
    setText(ui.collapseBtn, state.collapsed ? '펼치기' : '접기');
    ui.collapseBtn.setAttribute('aria-expanded', state.collapsed ? 'false' : 'true');
    syncCorrectionVisibility();
    if (persist) {
      try { localStorage.setItem(COLLAPSE_KEY, state.collapsed ? '1' : '0'); } catch (_error) {}
    }
  }

  function automaticStep(status) {
    var job = status.job && status.job.state === 'running' ? status.job : state.job;
    if (state.uploading) return 0;
    if (job && job.state === 'running') return job.kind === 'export' ? 3 : 1;
    if (Number(status.srcCount || 0) === 0) return 0;
    if (!Array.isArray(status.groups) || status.groups.length === 0) return 1;
    return 2;
  }

  function setExpandedStep(index, manual) {
    if (manual) {
      state.manualStep = true;
      state.expandedStep = state.expandedStep === index ? null : index;
    } else {
      state.expandedStep = index;
    }
    (ui.accordions || []).forEach(function (item, itemIndex) {
      var open = state.expandedStep === itemIndex;
      if (item.content) item.content.hidden = !open;
      item.section.classList.toggle('open', open);
      setText(item.toggle, open ? '접기' : '펼치기');
      item.toggle.setAttribute('aria-expanded', open ? 'true' : 'false');
    });
    syncCorrectionVisibility();
  }

  function syncCorrectionVisibility() {
    var hideCorrection = !!ui.panel && !ui.panel.hidden && !state.collapsed && state.expandedStep !== 2;
    document.body.classList.toggle('wfCorrectionCollapsed', hideCorrection);
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
    if (!state.manualStep) setExpandedStep(0, false);
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
    if (!state.manualStep) setExpandedStep(kind === 'export' ? 3 : 1, false);
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
        var message = 'PDF ' + count + '권이 결과 폴더에 있습니다.';
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
    showBanner('작업이 실패했습니다. 전체 로그에서 마지막 오류를 확인하세요.', 'error');
    refreshStatus();
  }

  // export 잡 결과(job.result)의 그룹별 쪽 수 한 줄. 한 쪽도 못 만든 그룹은 따로 알린다.
  function groupPageSummary(result) {
    if (!result || !Array.isArray(result.groups) || !result.groups.length) return '';
    var parts = result.groups.map(function (group) {
      return String(group.name) + ' ' + Number(group.pages || 0) + '쪽';
    });
    var line = '그룹별 쪽 수: ' + parts.join(' · ');
    var empty = Array.isArray(result.emptyGroups) ? result.emptyGroups : [];
    if (empty.length) line += '\n쪽이 없어 빠진 그룹: ' + empty.map(String).join(', ');
    return line;
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

  function showBanner(message, level, actions) {
    if (!ui.banner) return;
    setText(ui.bannerText, message);
    setNoteActions(ui.bannerActions, actions);
    ui.banner.className = 'wfBanner show ' + (level || 'info');
  }

  function clearBanner() {
    if (!ui.banner) return;
    setText(ui.bannerText, '');
    setNoteActions(ui.bannerActions, null);
    ui.banner.className = 'wfBanner';
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
    showBanner('도구 서버가 꺼졌습니다. 시작 파일(시작하기)을 다시 실행하세요.', 'error', [
      {label: '다시 연결', className: 'on', onClick: reconnect}
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
        ui.panel.hidden = true;
        restoreCorrectionEditor();
        document.body.classList.remove('wfCorrectionCollapsed');
        return;
      }
      ui.panel.hidden = false;
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

  function buildNewEventModal() {
    ui.neBack = el('div', {className: 'wfModalBack', role: 'dialog', 'aria-modal': 'true'});
    var modal = el('div', {className: 'wfModal'});
    modal.appendChild(el('h2', {text: '새 행사 시작'}));
    modal.appendChild(el('p', {text: '지금 작업을 통째로 보관하고 빈 작업장에서 시작합니다. 삭제하지 않고 작업장 안의 보관 폴더로 옮깁니다.'}));
    ui.neList = el('ul', {className: 'wfNeList'});
    modal.appendChild(ui.neList);
    ui.neOriginals = el('input', {type: 'checkbox'});
    ui.neOriginalsText = el('span', {text: '원본 사진도 함께 보관'});
    modal.appendChild(el('label', {className: 'wfNeOption'}, [ui.neOriginals, ui.neOriginalsText]));
    modal.appendChild(el('p', {
      className: 'wfRowInfo',
      text: '체크하지 않으면 원본 폴더의 사진은 그대로 남아, 다음 행사 사진과 섞일 수 있습니다.'
    }));
    ui.neError = el('div', {className: 'wfNeError', role: 'alert'});
    modal.appendChild(ui.neError);
    ui.neCancel = el('button', {type: 'button', className: 'btn', text: '취소'});
    ui.neOk = el('button', {type: 'button', className: 'btn on', text: '백업 받고 시작'});
    ui.neCancel.addEventListener('click', closeNewEvent);
    ui.neOk.addEventListener('click', runNewEvent);
    modal.appendChild(el('div', {className: 'wfModalActions'}, [ui.neCancel, ui.neOk]));
    ui.neBack.appendChild(modal);
    ui.neBack.addEventListener('click', function (event) {
      if (event.target === ui.neBack && !state.newEventRunning) closeNewEvent();
    });
    document.addEventListener('keydown', function (event) {
      if (event.key === 'Escape' && ui.neBack.classList.contains('show') && !state.newEventRunning) closeNewEvent();
    });
    document.body.appendChild(ui.neBack);
  }

  function confirmNewEvent() {
    if (state.disabled || isBusy() || state.uploading || !state.status) return;
    var status = state.status;
    var groups = Array.isArray(status.groups) ? status.groups : [];
    var stored = storedWorkSummary();
    var srcCount = Number(status.srcCount || 0);
    ui.neList.textContent = '';
    ui.neList.appendChild(el('li', {
      text: '작업 그룹 ' + groups.length + '개 · 작업용 사진 ' + photoTotal(status) + '장 (그룹 폴더·그룹 계획·목록)'
    }));
    ui.neList.appendChild(el('li', {
      text: stored.any
        ? '저장된 보정값 — 모서리 ' + stored.corners + '장 · 완료/제외 ' + stored.status + '장 · 색보정 ' + stored.color +
          '장 (백업 파일로 저장한 뒤 이 브라우저에서 지웁니다)'
        : '저장된 보정값 — 없음'
    }));
    ui.neList.appendChild(el('li', {text: '결과 PDF(결과물 폴더)는 그대로 둡니다.'}));
    setText(ui.neOriginalsText, '원본 사진도 함께 보관 (원본 폴더의 ' + srcCount + '장을 보관 폴더로 옮김)');
    ui.neOriginals.checked = false;
    ui.neOriginals.disabled = srcCount === 0;
    setText(ui.neError, '');
    ui.neOk.disabled = false;
    ui.neCancel.disabled = false;
    setText(ui.neOk, '백업 받고 시작');
    ui.neBack.classList.add('show');
    ui.neOk.focus();
  }

  function closeNewEvent() {
    ui.neBack.classList.remove('show');
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

  function runNewEvent() {
    if (state.newEventRunning) return;
    if (typeof collectBackup !== 'function') {
      setText(ui.neError, '현재 보정값을 수집하지 못했습니다. 페이지를 새로고침한 뒤 다시 시도하세요.');
      return;
    }
    var backup;
    try {
      backup = collectBackup();
    } catch (error) {
      setText(ui.neError, '현재 보정값을 읽지 못했습니다.\n' + error.message);
      return;
    }
    state.newEventRunning = true;
    ui.neOk.disabled = true;
    ui.neCancel.disabled = true;
    setText(ui.neOk, '보관하는 중…');
    setText(ui.neError, '');
    api('/api/new-event', {
      method: 'POST',
      json: {backup: backup, moveOriginals: !!ui.neOriginals.checked}
    }).then(function (payload) {
      clearToolStorageAndReload('이전 작업을 ' + payload.archive + ' 에 보관했습니다');
    }).catch(function (error) {
      state.newEventRunning = false;
      ui.neOk.disabled = false;
      ui.neCancel.disabled = false;
      setText(ui.neOk, '백업 받고 시작');
      setText(ui.neError, isNetworkError(error)
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
  function buildLocalBand(main) {
    var note = buildNote('wfBanner warn');
    note.root.id = 'wfLocalBand';
    ui.localBand = note.root;
    ui.localBandText = note.text;
    ui.localBandActions = note.actions;
    main.prepend(ui.localBand);
  }

  function formatBackupTime(seconds) {
    var date = new Date(Number(seconds) * 1000);
    if (isNaN(date.getTime())) return '';
    function two(n) { return (n < 10 ? '0' : '') + n; }
    return two(date.getMonth() + 1) + '-' + two(date.getDate()) + ' ' + two(date.getHours()) + ':' + two(date.getMinutes());
  }

  function hideLocalBand() {
    ui.localBand.className = 'wfBanner warn';
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
        {label: '최근 백업 불러오기', className: 'on', onClick: function () { loadLatestBackup(latest.name); }},
        {label: '닫기', onClick: dismissLocalBand}
      ]);
      ui.localBand.className = 'wfBanner warn show';
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
      {label: '최근 백업 불러오기', className: 'on', onClick: function () { loadLatestBackup(name); }},
      {label: '닫기', onClick: dismissLocalBand}
    ]);
  }

  function init() {
    injectStyle();
    if (!buildPanel()) return;
    showPendingToast();
    connect();
  }

  window.__slideWorkflow = {
    refreshStatus: refreshStatus,
    renameAvailability: renameAvailability,
    renameGroup: renameGroup,
    autoDetectAvailability: autoDetectAvailability,
    autoDetect: autoDetect
  };
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init);
  else init();
})();
