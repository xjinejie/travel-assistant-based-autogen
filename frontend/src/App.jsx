import { startTransition, useEffect, useRef, useState } from 'react';
import './App.css';

const TRAVEL_STYLE_OPTIONS = [
  '文化历史',
  '自然风景',
  '美食探索',
  '购物',
  '休闲度假',
  '冒险运动',
];

const BUDGET_OPTIONS = ['经济', '中等', '高端', '豪华'];
const ACCOMMODATION_OPTIONS = ['青旅', '民宿', '酒店', '高端酒店'];
const TRANSPORT_OPTIONS = ['公共交通', '自驾', '打车', '混合'];

function formatDate(date) {
  return date.toISOString().slice(0, 10);
}

function shiftDate(date, days) {
  const nextDate = new Date(date);
  nextDate.setDate(nextDate.getDate() + days);
  return nextDate;
}

function buildDefaultForm() {
  const today = new Date();
  return {
    destination: '北京',
    departure_city: '上海',
    start_date: formatDate(today),
    end_date: formatDate(shiftDate(today, 2)),
    flexible_dates: false,
    travelers_count: '2',
    has_children: false,
    has_elderly: false,
    budget_level: '中等',
    travel_styles: ['文化历史', '美食探索'],
    accommodation_preference: '酒店',
    transport_preference: '公共交通',
    dietary_restrictions: '',
    additional_notes: '希望节奏适中，少走回头路，优先安排经典景点和当地特色餐厅。',
  };
}

function generateSessionId() {
  if (window.crypto && typeof window.crypto.randomUUID === 'function') {
    return window.crypto.randomUUID();
  }

  return `session-${Date.now()}-${Math.random().toString(16).slice(2)}`;
}

function createEntryId(prefix) {
  return `${prefix}-${Date.now()}-${Math.random().toString(16).slice(2)}`;
}

function buildBackendOrigin() {
  const configuredOrigin = import.meta.env.VITE_BACKEND_ORIGIN?.trim();
  if (configuredOrigin) {
    return configuredOrigin.replace(/\/$/, '');
  }

  const { protocol, hostname, host, port } = window.location;
  if (import.meta.env.DEV || port === '4173') {
    return `${protocol}//${hostname}:8000`;
  }

  return `${protocol}//${host}`;
}

function buildWebSocketUrl(pathname) {
  const httpUrl = new URL(pathname, `${buildBackendOrigin()}/`);
  httpUrl.protocol = httpUrl.protocol === 'https:' ? 'wss:' : 'ws:';
  return httpUrl.toString();
}

function releaseSessionSilently(sessionId) {
  const trimmedSessionId = (sessionId || '').trim();
  if (!trimmedSessionId) {
    return;
  }

  const payload = JSON.stringify({ session_id: trimmedSessionId });
  const releaseUrl = new URL('/api/session/release', `${buildBackendOrigin()}/`).toString();

  if (navigator.sendBeacon) {
    const blob = new Blob([payload], { type: 'application/json' });
    navigator.sendBeacon(releaseUrl, blob);
    return;
  }

  fetch(releaseUrl, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: payload,
    keepalive: true,
  }).catch(() => {});
}

function stripReviewerTerminate(content = '') {
  return content
    .split('\n')
    .filter((line) => line.trim() !== 'TERMINATE')
    .join('\n')
    .trim();
}

function buildLogEntry(type, source, content) {
  return {
    id: createEntryId('log'),
    type,
    source,
    content,
    timestamp: new Date().toLocaleTimeString('zh-CN', {
      hour: '2-digit',
      minute: '2-digit',
      second: '2-digit',
    }),
  };
}

function buildThreadEntry(role, meta, content, tone = 'plan') {
  return {
    id: createEntryId('thread'),
    role,
    meta,
    content,
    tone,
  };
}

function buildBootstrapLogs(sessionId, notice) {
  const entries = [
    buildLogEntry('info', 'System', `当前会话 session_id: ${sessionId}`),
    buildLogEntry('info', 'System', `前端连接目标: ${buildBackendOrigin()}`),
  ];

  if (notice) {
    entries.push(buildLogEntry('info', 'System', notice));
  }

  return entries;
}

function buildStatusView(tone, badge, title, detail) {
  return { tone, badge, title, detail };
}

function StructuredText({ content }) {
  const lines = content.split('\n');

  return (
    <div className="structured-text">
      {lines.map((line, index) => {
        const trimmed = line.trim();
        const key = `${index}-${trimmed}`;

        if (!trimmed) {
          return <div key={key} className="structured-spacer" aria-hidden="true" />;
        }

        if (/^【.+】$/.test(trimmed)) {
          return (
            <p key={key} className="structured-heading">
              {trimmed}
            </p>
          );
        }

        if (/^Day\s*\d+/i.test(trimmed)) {
          return (
            <p key={key} className="structured-day">
              {trimmed}
            </p>
          );
        }

        if (/^[-•]\s*/.test(trimmed)) {
          return (
            <p key={key} className="structured-bullet">
              <span className="structured-bullet-mark">•</span>
              <span>{trimmed.replace(/^[-•]\s*/, '')}</span>
            </p>
          );
        }

        return (
          <p key={key} className="structured-line">
            {line}
          </p>
        );
      })}
    </div>
  );
}

function App() {
  const [formData, setFormData] = useState(buildDefaultForm);
  const [feedbackInput, setFeedbackInput] = useState('');
  const [sessionId, setSessionId] = useState(() => generateSessionId());
  const [currentRound, setCurrentRound] = useState(0);
  const [conversationStatus, setConversationStatus] = useState('idle');
  const [statusView, setStatusView] = useState(
    buildStatusView(
      'idle',
      '未开始',
      '等待一次新的旅行规划',
      '先填写左侧结构化需求。Planner 会生成方案，Reviewer 审核通过后才能继续反馈。',
    ),
  );
  const [logs, setLogs] = useState(() =>
    buildBootstrapLogs(
      sessionId,
      '当前页面已经准备好，可以直接提交示例需求或按需修改表单。',
    ),
  );
  const [threadEntries, setThreadEntries] = useState([]);
  const [isBusy, setIsBusy] = useState(false);

  const socketRef = useRef(null);
  const threadViewportRef = useRef(null);
  const logViewportRef = useRef(null);
  const latestPlannerReplyRef = useRef('');
  const latestReviewerReplyRef = useRef('');
  const sessionIdRef = useRef(sessionId);
  const conversationStatusRef = useRef(conversationStatus);

  useEffect(() => {
    sessionIdRef.current = sessionId;
  }, [sessionId]);

  useEffect(() => {
    conversationStatusRef.current = conversationStatus;
  }, [conversationStatus]);

  useEffect(() => {
    const viewport = threadViewportRef.current;
    if (viewport) {
      viewport.scrollTop = viewport.scrollHeight;
    }
  }, [threadEntries]);

  useEffect(() => {
    const viewport = logViewportRef.current;
    if (viewport) {
      viewport.scrollTop = viewport.scrollHeight;
    }
  }, [logs]);

  useEffect(() => {
    const handlePageHide = () => {
      releaseSessionSilently(sessionIdRef.current);
    };

    window.addEventListener('pagehide', handlePageHide);

    return () => {
      const socket = socketRef.current;
      if (socket && socket.readyState < WebSocket.CLOSING) {
        socket.close();
      }
      releaseSessionSilently(sessionIdRef.current);
      window.removeEventListener('pagehide', handlePageHide);
    };
  }, []);

  function appendLog(type, source, content) {
    startTransition(() => {
      setLogs((previousLogs) => [...previousLogs, buildLogEntry(type, source, content)]);
    });
  }

  function appendThreadEntry(role, meta, content, tone) {
    startTransition(() => {
      setThreadEntries((previousEntries) => [
        ...previousEntries,
        buildThreadEntry(role, meta, content, tone),
      ]);
    });
  }

  function closeActiveSocket() {
    const socket = socketRef.current;
    if (socket && socket.readyState < WebSocket.CLOSING) {
      socket.close();
    }
    socketRef.current = null;
  }

  function updateField(field, value) {
    setFormData((previousData) => ({
      ...previousData,
      [field]: value,
    }));
  }

  function toggleTravelStyle(option) {
    setFormData((previousData) => {
      const exists = previousData.travel_styles.includes(option);
      return {
        ...previousData,
        travel_styles: exists
          ? previousData.travel_styles.filter((item) => item !== option)
          : [...previousData.travel_styles, option],
      };
    });
  }

  function resetRunBuffers() {
    latestPlannerReplyRef.current = '';
    latestReviewerReplyRef.current = '';
  }

  function handleNewSession() {
    closeActiveSocket();
    releaseSessionSilently(sessionIdRef.current);

    const nextSessionId = generateSessionId();
    sessionIdRef.current = nextSessionId;
    setSessionId(nextSessionId);
    setCurrentRound(0);
    setConversationStatus('idle');
    setStatusView(
      buildStatusView(
        'idle',
        '未开始',
        '已创建新的独立会话',
        '旧日志和线程已经清空。你可以保留当前表单，也可以继续调整后重新规划。',
      ),
    );
    setFeedbackInput('');
    setThreadEntries([]);
    setLogs(
      buildBootstrapLogs(
        nextSessionId,
        '已新建会话，旧的聊天记录与执行日志已经清空。',
      ),
    );
    setIsBusy(false);
    resetRunBuffers();
  }

  function buildTravelRequestPayload() {
    const destination = formData.destination.trim();
    if (!destination) {
      window.alert('请输入目的地。');
      return null;
    }

    const travelersCount = Math.min(
      20,
      Math.max(1, Number.parseInt(formData.travelers_count, 10) || 1),
    );

    return {
      session_id: sessionIdRef.current,
      destination,
      departure_city: formData.departure_city.trim() || null,
      start_date: formData.start_date || null,
      end_date: formData.end_date || null,
      flexible_dates: formData.flexible_dates,
      travelers_count: travelersCount,
      has_children: formData.has_children,
      has_elderly: formData.has_elderly,
      budget_level: formData.budget_level,
      travel_styles: formData.travel_styles,
      accommodation_preference: formData.accommodation_preference,
      transport_preference: formData.transport_preference,
      dietary_restrictions: formData.dietary_restrictions.trim() || null,
      additional_notes: formData.additional_notes.trim() || null,
    };
  }

  function finishSocketRun(nextStatus) {
    setIsBusy(false);
    if (nextStatus) {
      setConversationStatus(nextStatus);
    }
  }

  function runSocket(pathname, payload, options) {
    closeActiveSocket();
    setIsBusy(true);
    resetRunBuffers();

    const socket = new WebSocket(buildWebSocketUrl(pathname));
    socketRef.current = socket;

    socket.onopen = () => {
      socket.send(JSON.stringify(payload));
      appendLog('info', 'System', options.openMessage);

      if (options.payloadLog) {
        appendLog('info', 'System', options.payloadLog);
      }
    };

    socket.onmessage = (event) => {
      const data = JSON.parse(event.data);

      if (data.session_id) {
        sessionIdRef.current = data.session_id;
        setSessionId(data.session_id);
      }

      if (data.type === 'done') {
        const finalStatus = data.conversation_status || 'terminated';
        appendLog('message', 'System', data.content || '本轮对话结束');

        if (finalStatus === 'awaiting_feedback') {
          if (latestPlannerReplyRef.current.trim()) {
            appendThreadEntry(
              'agent',
              `Travel Agent · 第 ${options.round} 轮方案`,
              latestPlannerReplyRef.current,
              'plan',
            );
          }

          setStatusView(
            buildStatusView(
              'awaiting_feedback',
              '等待反馈',
              '当前方案已经通过审核',
              '你可以继续补充修改意见，也可以直接确认方案，让 Reviewer 结束本轮对话。',
            ),
          );
        } else {
          const reviewerDisplay = stripReviewerTerminate(latestReviewerReplyRef.current);

          if (reviewerDisplay) {
            appendThreadEntry(
              'agent',
              'Reviewer · 对话结束',
              reviewerDisplay,
              'review',
            );
          } else if (latestPlannerReplyRef.current.trim()) {
            appendThreadEntry(
              'agent',
              `Travel Agent · 第 ${options.round} 轮结果`,
              latestPlannerReplyRef.current,
              'plan',
            );
          }

          setStatusView(
            buildStatusView(
              'terminated',
              '已结束',
              '本轮对话已经完成',
              'Reviewer 已输出终止信号。如需重新规划，可直接再次提交左侧表单，或先新建一个会话。',
            ),
          );
        }

        finishSocketRun(finalStatus);
        socket.close();
        return;
      }

      if (data.type === 'error') {
        appendLog('error', 'System', data.content || '发生未知错误。');
        setStatusView(
          buildStatusView(
            'error',
            '发生错误',
            '本轮运行未正常完成',
            data.content || '请检查输入字段、后端日志或模型环境配置。',
          ),
        );

        if (options.mode === 'plan') {
          finishSocketRun('idle');
        } else {
          finishSocketRun(conversationStatusRef.current);
        }

        socket.close();
        return;
      }

      appendLog(data.type || 'info', data.source || 'System', data.content || '');

      if (data.type === 'message' && data.source === 'Agent_Planner') {
        latestPlannerReplyRef.current = data.content || '';
      }

      if (data.type === 'message' && data.source === 'Agent_Reviewer') {
        latestReviewerReplyRef.current = data.content || '';
      }
    };

    socket.onerror = () => {
      appendLog('error', 'System', 'WebSocket 连接失败。请确认后端服务已经启动，并且 8000 端口可访问。');
      setStatusView(
        buildStatusView(
          'error',
          '连接失败',
          '无法连接到后端',
          `当前尝试连接: ${buildBackendOrigin()}。如果前端跑在 Vite 开发服务器，请先启动 FastAPI。`,
        ),
      );

      if (options.mode === 'plan') {
        finishSocketRun('idle');
      } else {
        finishSocketRun(conversationStatusRef.current);
      }
    };

    socket.onclose = () => {
      if (socketRef.current === socket) {
        socketRef.current = null;
      }
    };
  }

  function handleStartPlan(event) {
    event.preventDefault();

    const payload = buildTravelRequestPayload();
    if (!payload) {
      return;
    }

    setCurrentRound(1);
    setConversationStatus('planning');
    setStatusView(
      buildStatusView(
        'planning',
        '规划中',
        'Planner 正在生成首版方案',
        '当前轮会经过 Planner 生成和 Reviewer 审核。通过后，线程里会沉淀可继续反馈的最终方案。',
      ),
    );
    setFeedbackInput('');
    setThreadEntries([]);
    setLogs([
      ...buildBootstrapLogs(sessionIdRef.current),
      buildLogEntry('info', 'System', '第 1 轮：生成首版旅行方案'),
    ]);

    runSocket('/ws/plan', payload, {
      mode: 'plan',
      round: 1,
      openMessage: `已连接系统，正在发送结构化旅行需求。session_id=${sessionIdRef.current}`,
      payloadLog: `提交内容：\n${JSON.stringify(payload, null, 2)}`,
    });
  }

  function handleSubmitFeedback(content = feedbackInput) {
    const trimmedFeedback = content.trim();
    if (!trimmedFeedback) {
      window.alert('请输入反馈内容。');
      return;
    }

    if (conversationStatusRef.current !== 'awaiting_feedback') {
      window.alert('当前还不能提交反馈，请先完成一轮审核通过的旅行方案。');
      return;
    }

    const nextRound = currentRound + 1;
    setCurrentRound(nextRound);
    setFeedbackInput('');
    setStatusView(
      buildStatusView(
        'planning',
        '处理中',
        '反馈已交给 Reviewer',
        'Reviewer 会先判断反馈是否需要改稿；如果需要，再移交 Planner 修改当前方案。',
      ),
    );

    appendLog('info', 'System', `第 ${nextRound} 轮：提交用户反馈并继续改稿`);
    appendLog('message', 'user', trimmedFeedback);
    appendThreadEntry('user', `Traveler · 第 ${nextRound} 轮反馈`, trimmedFeedback, 'user');

    runSocket(
      '/ws/plan/feedback',
      {
        session_id: sessionIdRef.current,
        feedback: trimmedFeedback,
      },
      {
        mode: 'feedback',
        round: nextRound,
        openMessage: `已发送用户反馈，正在进入 Reviewer 判断流程。session_id=${sessionIdRef.current}`,
      },
    );
  }

  function handleAcceptPlan() {
    const acceptanceMessage = '我同意当前方案，无需修改，请结束本轮。';
    setFeedbackInput(acceptanceMessage);
    handleSubmitFeedback(acceptanceMessage);
  }

  const feedbackEnabled = conversationStatus === 'awaiting_feedback' && !isBusy;
  const canStartPlan = !isBusy && formData.destination.trim().length > 0;
  const canSubmitFeedback = feedbackEnabled && feedbackInput.trim().length > 0;
  const canAcceptPlan = feedbackEnabled;
  const shortSessionId = sessionId.length > 16 ? `${sessionId.slice(0, 8)}...${sessionId.slice(-6)}` : sessionId;

  return (
    <div className="travel-app">
      <aside className="control-panel shell-card">
        <section className="brand-card">
          <div className="brand-copy">
            <p className="eyebrow">Travel Assistant</p>
            <h1>旅行需求与会话面板</h1>
            <p>
              左侧整理本轮旅行约束，右侧查看规划结果、继续反馈和确认方案。
              前端已接入后端的 <code>session_id</code> 与 WebSocket 多轮流程。
            </p>
          </div>
        </section>

        <form className="planner-form" onSubmit={handleStartPlan}>
          <div className="section-heading">
            <p className="eyebrow">Trip Setup</p>
            <h2>详细旅行需求</h2>
            <p className="section-description">
              先填写目的地、时间、人数和偏好，再发起首版规划。
            </p>
          </div>

          <section className="form-group-card">
            <div className="mini-heading">
              <h3>基础信息</h3>
              <p>先确定目的地、出发地和时间范围。</p>
            </div>

            <label className="field-block field-block-wide">
              <span className="field-label">目的地</span>
              <input
                type="text"
                value={formData.destination}
                onChange={(event) => updateField('destination', event.target.value)}
                placeholder="如：北京、日本东京、云南大理"
              />
            </label>

            <div className="field-grid">
              <label className="field-block">
                <span className="field-label">出发城市</span>
                <input
                  type="text"
                  value={formData.departure_city}
                  onChange={(event) => updateField('departure_city', event.target.value)}
                  placeholder="如：上海、广州"
                />
              </label>

              <label className="field-block">
                <span className="field-label">总人数</span>
                <input
                  type="number"
                  min="1"
                  max="20"
                  value={formData.travelers_count}
                  onChange={(event) => updateField('travelers_count', event.target.value)}
                />
              </label>
            </div>

            <div className="field-grid stacked-date-grid">
              <label className="field-block">
                <span className="field-label">出发日期</span>
                <input
                  type="date"
                  value={formData.start_date}
                  onChange={(event) => updateField('start_date', event.target.value)}
                />
              </label>

              <label className="field-block">
                <span className="field-label">返回日期</span>
                <input
                  type="date"
                  value={formData.end_date}
                  onChange={(event) => updateField('end_date', event.target.value)}
                />
              </label>
            </div>
          </section>

          <section className="form-group-card">
            <div className="mini-heading">
              <h3>同行与节奏</h3>
              <p>这些条件会直接影响节奏、安全性和路线安排。</p>
            </div>

            <div className="toggle-grid">
              <label className="toggle-pill">
                <input
                  type="checkbox"
                  checked={formData.flexible_dates}
                  onChange={(event) => updateField('flexible_dates', event.target.checked)}
                />
                <span>日期灵活可调</span>
              </label>

              <label className="toggle-pill">
                <input
                  type="checkbox"
                  checked={formData.has_children}
                  onChange={(event) => updateField('has_children', event.target.checked)}
                />
                <span>有儿童同行</span>
              </label>

              <label className="toggle-pill">
                <input
                  type="checkbox"
                  checked={formData.has_elderly}
                  onChange={(event) => updateField('has_elderly', event.target.checked)}
                />
                <span>有老人同行</span>
              </label>
            </div>
          </section>

          <section className="form-group-card">
            <div className="mini-heading">
              <h3>偏好设置</h3>
              <p>预算、住宿、交通和旅行风格会影响方案取向。</p>
            </div>

            <div className="field-grid">
              <label className="field-block">
                <span className="field-label">预算级别</span>
                <select
                  value={formData.budget_level}
                  onChange={(event) => updateField('budget_level', event.target.value)}
                >
                  {BUDGET_OPTIONS.map((option) => (
                    <option key={option} value={option}>
                      {option}
                    </option>
                  ))}
                </select>
              </label>

              <label className="field-block">
                <span className="field-label">住宿偏好</span>
                <select
                  value={formData.accommodation_preference}
                  onChange={(event) => updateField('accommodation_preference', event.target.value)}
                >
                  {ACCOMMODATION_OPTIONS.map((option) => (
                    <option key={option} value={option}>
                      {option}
                    </option>
                  ))}
                </select>
              </label>

              <label className="field-block field-block-wide">
                <span className="field-label">交通偏好</span>
                <select
                  value={formData.transport_preference}
                  onChange={(event) => updateField('transport_preference', event.target.value)}
                >
                  {TRANSPORT_OPTIONS.map((option) => (
                    <option key={option} value={option}>
                      {option}
                    </option>
                  ))}
                </select>
              </label>
            </div>

            <div className="field-block field-block-wide">
              <span className="field-label">旅行风格</span>
              <div className="style-chip-grid">
                {TRAVEL_STYLE_OPTIONS.map((option) => {
                  const selected = formData.travel_styles.includes(option);
                  return (
                    <button
                      key={option}
                      type="button"
                      className={`style-chip ${selected ? 'selected' : ''}`}
                      onClick={() => toggleTravelStyle(option)}
                    >
                      {option}
                    </button>
                  );
                })}
              </div>
            </div>
          </section>

          <section className="form-group-card">
            <div className="mini-heading">
              <h3>补充限制</h3>
              <p>把特殊饮食、行动需求和期待体验补充完整。</p>
            </div>

            <label className="field-block field-block-wide">
              <span className="field-label">饮食限制</span>
              <input
                type="text"
                value={formData.dietary_restrictions}
                onChange={(event) => updateField('dietary_restrictions', event.target.value)}
                placeholder="如：素食、清真、海鲜过敏"
              />
            </label>

            <label className="field-block field-block-wide">
              <span className="field-label">补充说明</span>
              <textarea
                rows="4"
                value={formData.additional_notes}
                onChange={(event) => updateField('additional_notes', event.target.value)}
                placeholder="如：想看日出、需要无障碍设施、尽量少走路"
              />
            </label>
          </section>

          <button type="submit" className="primary-button form-submit" disabled={!canStartPlan}>
            {isBusy ? '处理中…' : '开始生成首版方案'}
          </button>
        </form>

        <section className="status-card">
          <div className="section-heading">
            <p className="eyebrow">Session</p>
            <h2>会话状态</h2>
          </div>
          <div className={`status-surface ${statusView.tone}`}>
            <div className="status-surface-title">{statusView.title}</div>
            <p>{statusView.detail}</p>
          </div>
          <dl className="status-metrics">
            <div>
              <dt>session_id</dt>
              <dd>{sessionId}</dd>
            </div>
            <div>
              <dt>当前轮次</dt>
              <dd>{currentRound > 0 ? `第 ${currentRound} 轮` : '未开始'}</dd>
            </div>
            <div>
              <dt>连接后端</dt>
              <dd>{buildBackendOrigin()}</dd>
            </div>
          </dl>
        </section>

        <section className="log-card">
          <div className="section-heading section-heading-inline">
            <div>
              <p className="eyebrow">Agent Log</p>
              <h2>执行轨迹</h2>
            </div>
            <span className="log-count">{logs.length}</span>
          </div>
          <div className="log-viewport" ref={logViewportRef}>
            {logs.map((entry) => (
              <article key={entry.id} className={`log-entry ${entry.type}`}>
                <div className="log-entry-meta">
                  <span>{entry.source}</span>
                  <time>{entry.timestamp}</time>
                </div>
                <pre>{entry.content}</pre>
              </article>
            ))}
          </div>
        </section>
      </aside>

      <main className="conversation-panel shell-card">
        <header className="conversation-header">
          <div className="conversation-identity">
            <div className="conversation-copy">
              <h2>聊天区</h2>
            </div>
          </div>

          <div className="conversation-actions">
            <div className={`status-pill ${statusView.tone}`}>{statusView.badge}</div>
            <button type="button" className="secondary-button" onClick={handleNewSession} disabled={isBusy}>
              新建会话
            </button>
          </div>
        </header>

        <section className="thread-stage">
          <div className="thread-banner">
            <div className="thread-banner-main">
              <p className="thread-banner-kicker">当前会话</p>
              <h3>{currentRound > 0 ? `第 ${currentRound} 轮` : '等待发起'}</h3>
            </div>
            <div className="thread-banner-meta">
              <span>{shortSessionId}</span>
              <span>{statusView.badge}</span>
            </div>
          </div>

          <div className="thread-viewport" ref={threadViewportRef}>
            {threadEntries.length === 0 ? (
              <div className="thread-empty">
                <p>首版旅行方案会显示在这里。</p>
                <span>提交左侧需求后，审核通过的方案和后续反馈都会按轮次沉淀到当前线程。</span>
              </div>
            ) : (
              threadEntries.map((entry) => (
                <article key={entry.id} className={`thread-row ${entry.role}`}>
                  <div className={`thread-avatar ${entry.role}`}>
                    {entry.role === 'user' ? 'YOU' : 'AI'}
                  </div>
                  <div className={`thread-bubble ${entry.tone}`}>
                    <div className="thread-bubble-meta">{entry.meta}</div>
                    <StructuredText content={entry.content} />
                  </div>
                </article>
              ))
            )}
          </div>

          <footer className="composer-panel">
            <textarea
              rows="5"
              value={feedbackInput}
              onChange={(event) => setFeedbackInput(event.target.value)}
              placeholder="例如：保留 Day 1，但住宿改成民宿；或者：我同意当前方案，无需修改。"
              disabled={!feedbackEnabled}
            />

            <div className="composer-actions">
              <button
                type="button"
                className="secondary-button secondary-button-soft"
                onClick={handleAcceptPlan}
                disabled={!canAcceptPlan}
              >
                直接确认当前方案
              </button>
              <button
                type="button"
                className="primary-button"
                onClick={() => handleSubmitFeedback()}
                disabled={!canSubmitFeedback}
              >
                提交反馈并继续改稿
              </button>
            </div>
          </footer>
        </section>
      </main>
    </div>
  );
}

export default App;
