/// <reference types="vite/client" />
import React, { useEffect, useState } from 'react'
import { createRoot } from 'react-dom/client'
import { AppstoreOutlined, CommentOutlined, HistoryOutlined, ProfileOutlined, CloudDownloadOutlined, UnorderedListOutlined, ProjectOutlined } from '@ant-design/icons'
import { Alert, Button, Card, ConfigProvider, Descriptions, Drawer, Empty, Form, Input, Layout, Modal, Select, Space, Table, Tag, Typography } from 'antd'
import zhCN from 'antd/locale/zh_CN'
import 'antd/dist/reset.css'
import { ReleasePage, SystemLogPage } from './OperationsPage'
import type { ReleaseHistory, SystemLogs } from './OperationsPage'
import { DevelopmentTasksPage } from './DevelopmentTasksPage'
import type { TaskSnapshot } from './DevelopmentTasksPage'
import { ProjectInfoPage } from './ProjectInfoPage'
import type { ProjectInfo } from './ProjectInfoPage'
import { WorkspaceShell } from './WorkspaceShell'
import { FeedbackDelivery } from './FeedbackDelivery'
import type { Delivery } from './FeedbackDelivery'

type DeployStatus = { state: string; event: {at: string; code: string; sha: string} | null; locked: boolean; age_seconds: number | null; requested_at: string | null; running_sha: string; rollback_failed: boolean }
const deployLabels: Record<string, string> = { request_waiting: "已登记请求，等待更新器领取", request_unacknowledged: "更新器未确认请求，请检查服务或锁", update_started: "更新器已开始检查", update_candidate: "已取得目标提交", update_dependencies: "检查依赖", update_test_build: "测试及构建中", update_restarting: "服务重启及健康验证中", update_ok: "本机部署成功", update_no_change: "已检查，无新提交", update_held: "目标提交被失败保护拦截", update_preflight_failed: "更新前置条件失败", update_fetch_failed: "拉取失败", update_dependencies_failed: "依赖检查失败", update_schema_failed: "数据库结构检查失败", update_test_build_failed: "测试或构建失败", update_restart_failed: "重启或健康检查失败", update_interrupted: "更新进程未完成", running_sha_mismatch: "运行版本与更新记录不一致", unknown: "尚无更新器回执" }

type AppBranding = { workspace_name: string; reference_name: string }
type UpdateCheck = { state: 'available' | 'current' | 'unavailable'; branch: string; running_sha?: string; remote_sha?: string; reason?: string }
type RunningVersion = { version: string; running_sha: string; manual_update_available: boolean }
type Session = { username: string; csrf_token: string }
type FeedbackInput = { category: string; message: string }
type Feedback = FeedbackInput & {
  id: number; status: string; github_issue_number: number | null
  created_at?: string | null; synced_at?: string | null
  claimed_at?: string | null; development_started_at?: string | null
  development_finished_at?: string | null; development_submitted_at?: string | null
  overall_status?: string; developer_reply?: string; delivered_sha?: string | null
  developer_pr?: number | null; progress_synced_at?: string | null; progress_stale?: boolean
  delivery?: Delivery
}
const feedbackStatus: Record<string, { label: string; color: string }> = {
  pending_sync: { label: '待同步', color: 'default' },
  awaiting_claim: { label: '待领取', color: 'default' },
  claimed: { label: '已领取', color: 'blue' },
  in_progress: { label: '开发中', color: 'processing' },
  development_finished: { label: '开发完成', color: 'cyan' },
  submitted: { label: '已提交', color: 'blue' },
  ready_for_review: { label: '待验收', color: 'warning' },
  accepted: { label: '已验收', color: 'success' },
  blocked: { label: '受阻', color: 'error' },
}
function FeedbackStatus({ row }: { row: Feedback }) {
  const state = row.overall_status ?? (row.status === 'synced' ? 'awaiting_claim' : 'pending_sync')
  const info = feedbackStatus[state] ?? { label: '状态未记录', color: 'default' }
  return <Space size={4} wrap><Tag color={info.color}>{info.label}</Tag>{row.progress_stale && <Tag>待刷新</Tag>}</Space>
}

const basePreview = import.meta.env.VITE_BASE_PREVIEW === "1"
const appName = basePreview ? "基座验收中心" : "Web App Foundation"
const navigation = [
  { key: 'workspace', label: '工作台', icon: <AppstoreOutlined /> },
  { key: 'project', label: '项目信息', icon: <ProjectOutlined /> },
  { key: 'tasks', label: '开发任务', icon: <UnorderedListOutlined /> },
  { key: 'releases', label: '更新记录', icon: <HistoryOutlined /> },
  { key: 'logs', label: '系统日志', icon: <ProfileOutlined /> },
  { key: 'feedback', label: '意见反馈', icon: <CommentOutlined /> },
] as const

type Page = (typeof navigation)[number]['key']
const githubIssues = basePreview ? 'https://github.com/layho/web-app-init-base-env/issues' : 'https://github.com/example/web-app/issues'

class ApiError extends Error {
  constructor(public status: number, message: string) {
    super(message)
  }
}

async function api<T>(path: string, init: RequestInit = {}, csrf?: string): Promise<T> {
  const headers = new Headers(init.headers)
  if (init.body) headers.set('Content-Type', 'application/json')
  if (csrf) headers.set('X-CSRF-Token', csrf)
  const response = await fetch(path, { ...init, headers, credentials: 'same-origin', cache: 'no-store' })
  const data = await response.json().catch(() => null)
  if (!response.ok) {
    const message = typeof data?.detail === 'string' ? data.detail : `请求失败（${response.status}）`
    throw new ApiError(response.status, message)
  }
  return data as T
}

function App() {
  const [session, setSession] = useState<Session | null>(null)
  const [branding, setBranding] = useState<AppBranding>({ workspace_name: appName, reference_name: appName })
  const [settingsOpen, setSettingsOpen] = useState(false)
  const [settingsTab, setSettingsTab] = useState<'branding' | 'password'>('branding')
  const [settingsSaving, setSettingsSaving] = useState(false)
  const [brandingForm] = Form.useForm<AppBranding>()
  const [passwordForm] = Form.useForm<{current_password: string; new_password: string; confirm_password: string}>()
  const [loading, setLoading] = useState(true)
  const [busy, setBusy] = useState(false)
  const [runningVersion, setRunningVersion] = useState<RunningVersion | null>(null)
  const [versionUnavailable, setVersionUnavailable] = useState(false)
  const [requestingUpdate, setRequestingUpdate] = useState(false)
  const [checkingUpdate, setCheckingUpdate] = useState(false)
  const [updateCheck, setUpdateCheck] = useState<UpdateCheck | null>(null)
  const [deployConfirm, setDeployConfirm] = useState(false)
  const [deployStatus, setDeployStatus] = useState<DeployStatus | null>(null)
  const [page, setPage] = useState<Page>('workspace')
  const [records, setRecords] = useState<Feedback[]>([])
  const [feedbackOpen, setFeedbackOpen] = useState(false)
  const [selectedFeedbackId, setSelectedFeedbackId] = useState<number | null>(null)
  const [feedbackLoading, setFeedbackLoading] = useState(false)
  const selectedFeedback = records.find(row => row.id === selectedFeedbackId)
  const feedbackLabels: Record<string, string> = { suggestion: '需求 / 建议', bug: '问题反馈', acceptance: '验收意见' }
  const feedbackDate = (value?: string | null) => value ? new Date(value).toLocaleString('zh-CN', { hour12: false }) : '未记录'
  const [releaseHistory, setReleaseHistory] = useState<ReleaseHistory | null>(null)
  const [projectInfo, setProjectInfo] = useState<ProjectInfo | null>(null)
  const [developmentTasks, setDevelopmentTasks] = useState<TaskSnapshot | null>(null)
  const [systemLogs, setSystemLogs] = useState<SystemLogs | null>(null)
  const [loadingOperations, setLoadingOperations] = useState<'releases' | 'logs' | 'tasks' | 'project' | null>(null)
  const [notice, setNotice] = useState<{ type: 'success' | 'error'; text: string } | null>(null)
  const [form] = Form.useForm<FeedbackInput>()

  useEffect(() => {
    api<Session>('/api/session')
      .then(setSession)
      .catch(error => {
        if (error instanceof ApiError && error.status === 401) return
        setNotice({ type: 'error', text: '暂时无法验证登录状态，请刷新重试。' })
      })
      .finally(() => setLoading(false))
  }, [])

  const signedIn = session !== null
  useEffect(() => {
    let active = true
    api<AppBranding>('/api/branding').then(data => { if(active) setBranding(data) }).catch(() => {})
    return () => { active = false }
  }, [])
  useEffect(() => {
    document.title = `${navigation.find(item => item.key === page)?.label ?? '工作台'} · ${branding.reference_name}`
  }, [branding.reference_name, page])
  useEffect(() => {
    if (!signedIn) return
    let active = true
    async function readVersion() {
      try {
        const data = await api<RunningVersion>('/api/version')
        if (active) {
          setRunningVersion(data)
          setVersionUnavailable(false)
        }
      } catch {
        if (active) setVersionUnavailable(true)
      }
    }

    void readVersion()
    const timer = window.setInterval(() => void readVersion(), 15_000)
    return () => {
      active = false
      window.clearInterval(timer)
    }
  }, [signedIn])

  useEffect(() => {
    if (!signedIn) { setDeployStatus(null); return }
    let active = true
    async function readDeployStatus() {
      try {
        const result = await api<DeployStatus>('/api/v1/deploy/status')
        if (active) setDeployStatus(result)
      } catch {
        if (active) setDeployStatus(null)
      }
    }
    void readDeployStatus()
    const timer = window.setInterval(() => void readDeployStatus(), 5000)
    return () => { active = false; window.clearInterval(timer) }
  }, [signedIn])

  async function checkUpdate() {
    if (!session || checkingUpdate) return
    setCheckingUpdate(true)
    setUpdateCheck(null)
    try {
      const result = await api<UpdateCheck>('/api/v1/deploy/check')
      setUpdateCheck(result)
      setNotice(result.state === 'unavailable'
        ? { type: 'error', text: result.reason || '无法检查远程版本' }
        : { type: 'success', text: result.state === 'available'
          ? `检测到 ${result.branch} 新提交：${result.remote_sha?.slice(0, 12)}，可选择立即部署。`
          : `当前已是 ${result.branch} 最新提交。` })
    } catch (error) {
      showError(error)
    } finally {
      setCheckingUpdate(false)
    }
  }

  async function requestUpdate() {
    if (!session || requestingUpdate) return
    if (updateCheck?.state !== 'available' || updateCheck.running_sha !== runningVersion?.running_sha) {
      setDeployConfirm(false)
      setNotice({type:'error',text:'检查结果已失效，请重新检查版本'})
      return
    }
    setDeployConfirm(false)
    setRequestingUpdate(true)
    setNotice(null)
    try {
      await api('/api/v1/deploy/update', { method: 'POST' }, session.csrf_token)
      setUpdateCheck(null)
      setNotice({ type: 'success', text: '部署请求已登记，等待本机更新器回执。' })
      setDeployStatus(await api<DeployStatus>('/api/v1/deploy/status'))
    } catch (error) {
      showError(error)
    } finally {
      setRequestingUpdate(false)
    }
  }

  function showError(error: unknown) {
    if (error instanceof ApiError && error.status === 401 && session) {
      setSession(null)
      setRunningVersion(null)
      setVersionUnavailable(false)
      setRecords([])
      setSelectedFeedbackId(null)
      setFeedbackOpen(false)
      setReleaseHistory(null)
      setSystemLogs(null)
      setDevelopmentTasks(null)
      setProjectInfo(null)
    }
    setNotice({ type: 'error', text: error instanceof Error ? error.message : '请求失败，请重试。' })
  }

  async function login(values: { username: string; password: string }) {
    setBusy(true)
    setNotice(null)
    try {
      setSession(await api<Session>('/api/login', { method: 'POST', body: JSON.stringify(values) }))
    } catch (error) {
      showError(error)
    } finally {
      setBusy(false)
    }
  }

  async function saveBranding(values: AppBranding) {
    if (!session || settingsSaving) return
    setSettingsSaving(true)
    try {
      const result = await api<AppBranding>('/api/v1/account/settings',
        {method:'POST', body:JSON.stringify(values)}, session.csrf_token)
      setBranding(result)
      setSettingsOpen(false)
      setNotice({type:'success',text:'工作台和引用名称已保存'})
    } catch(error) { showError(error) }
    finally { setSettingsSaving(false) }
  }

  async function changePassword(values: {current_password:string;new_password:string;confirm_password:string}) {
    if (!session || settingsSaving) return
    setSettingsSaving(true)
    try {
      await api('/api/v1/account/password',{method:'POST',body:JSON.stringify({
        current_password:values.current_password,new_password:values.new_password
      })},session.csrf_token)
      passwordForm.resetFields()
      setSettingsOpen(false)
      setSession(null)
      setNotice({type:'success',text:'密码已修改，所有会话均已失效，请用新密码重新登录'})
    } catch(error) { showError(error) }
    finally { setSettingsSaving(false) }
  }

  async function logout() {
    setBusy(true)
    setNotice(null)
    try {
      await api('/api/logout', { method: 'POST' }, session?.csrf_token)
      setSession(null)
      setRunningVersion(null)
      setVersionUnavailable(false)
      setRecords([])
      setSelectedFeedbackId(null)
      setFeedbackOpen(false)
      setReleaseHistory(null)
      setSystemLogs(null)
      setDevelopmentTasks(null)
      setProjectInfo(null)
      setPage('workspace')
    } catch (error) {
      showError(error)
    } finally {
      setBusy(false)
    }
  }

  async function loadFeedback() {
    setFeedbackLoading(true)
    try {
      setRecords(await api<Feedback[]>('/api/v1/feedback'))
    } catch (error) {
      showError(error)
    } finally {
      setFeedbackLoading(false)
    }
  }

  async function submitFeedback(values: FeedbackInput) {
    setBusy(true)
    setNotice(null)
    try {
      const result = await api<Pick<Feedback, 'id' | 'status'>>(
        '/api/v1/feedback', { method: 'POST', body: JSON.stringify(values) }, session?.csrf_token,
      )
      form.resetFields()
      setFeedbackOpen(false)
      setNotice({ type: 'success', text: `反馈 #${result.id} 已保存，等待同步到 GitHub。` })
      await loadFeedback()
    } catch (error) {
      showError(error)
    } finally {
      setBusy(false)
    }
  }

  async function loadOperations(key: 'releases' | 'logs' | 'tasks' | 'project', refresh = false) {
    setLoadingOperations(key)
    setNotice(null)
    try {
      if (key === 'releases') setReleaseHistory(await api<ReleaseHistory>('/api/release-notes'))
      else if (key === 'logs') setSystemLogs(await api<SystemLogs>('/api/v1/system-logs'))
      else if (key === 'tasks') setDevelopmentTasks(await api<TaskSnapshot>('/api/v1/development-tasks'))
      else setProjectInfo(await api<ProjectInfo>(refresh ? '/api/v1/project-info?refresh=true' : '/api/v1/project-info'))
    } catch (error) {
      showError(error)
    } finally {
      setLoadingOperations(current => current === key ? null : current)
    }
  }

  function navigate(key: Page) {
    setPage(key)
    setFeedbackOpen(false)
    setSelectedFeedbackId(null)
    setNotice(null)
    if (key === 'feedback') void loadFeedback()
    if (key === 'releases' || key === 'logs' || key === 'tasks' || key === 'project') void loadOperations(key)
  }

  if (loading) return <div style={{ minHeight: '100vh', display: 'grid', placeItems: 'center' }}>正在验证登录状态…</div>
  if (!session) return (
    <Layout style={{ minHeight: '100vh', display: 'grid', placeItems: 'center', background: '#f3f7fa', padding: 20 }}>
      <Card style={{ width: '100%', maxWidth: 390 }}>
        <Typography.Title level={3}>{branding.reference_name}</Typography.Title>
        {notice && <Alert type={notice.type} showIcon message={notice.text} style={{ marginBottom: 16 }} />}
        <Form layout="vertical" initialValues={{ username: 'admin' }} onFinish={login}>
          <Form.Item name="username" label="管理员账号" rules={[{ required: true }]}><Input autoComplete="username" size="large" /></Form.Item>
          <Form.Item name="password" label="密码" rules={[{ required: true }]}><Input.Password autoComplete="current-password" size="large" /></Form.Item>
          <Button type="primary" size="large" htmlType="submit" block loading={busy}>登录系统</Button>
        </Form>
      </Card>
    </Layout>
  )

  return (
    <WorkspaceShell
      appName={branding.workspace_name}
      brandMark="应用"
      username={session.username}
      activePage={page}
      navigation={navigation}
      onNavigate={navigate}
      onLogout={logout}
      logoutDisabled={busy}
      headerActions={<>
        <Button size="small" onClick={() => { brandingForm.setFieldsValue(branding); setSettingsTab('branding'); setSettingsOpen(true) }}>系统设置</Button>
        {runningVersion && !versionUnavailable ? (
          <Tag color="blue" title={runningVersion.running_sha}>
            v{runningVersion.version}{runningVersion.running_sha !== 'unknown' && ` · ${runningVersion.running_sha.slice(0, 8)}`}
          </Tag>
        ) : <Tag>{versionUnavailable ? '版本暂不可用' : '读取版本中…'}</Tag>}
        <Button onClick={() => void checkUpdate()} loading={checkingUpdate}
          disabled={busy || versionUnavailable || !runningVersion}>检查更新</Button>
        <Button type="primary" icon={<CloudDownloadOutlined />} onClick={() => setDeployConfirm(true)}
          loading={requestingUpdate}
          disabled={busy || versionUnavailable || !runningVersion?.manual_update_available ||
            updateCheck?.state !== 'available' || updateCheck.running_sha !== runningVersion?.running_sha ||
            Boolean(deployStatus?.locked)}>立即部署</Button>
      </>}
    >
      {basePreview && <Alert type="warning" showIcon message="基座候选版本验收环境；反馈将同步到公开 GitHub 仓库，请勿填写敏感信息。" style={{ marginBottom: 16 }} />}
      {notice && <Alert type={notice.type} showIcon message={notice.text} style={{ marginBottom: 16 }} />}
      {deployStatus && <Alert showIcon
        style={{ marginBottom: 12 }}
        type={deployStatus.rollback_failed || /failed|held|unacknowledged|interrupted|mismatch/.test(deployStatus.state) ? 'warning' : deployStatus.state === 'update_ok' || deployStatus.state === 'update_no_change' ? 'success' : 'info'}
        message={deployStatus.rollback_failed ? '回滚后服务恢复未确认，请查看系统日志' : (deployLabels[deployStatus.state] || '更新状态待核实')}
        description={deployStatus.event ? (new Date(deployStatus.event.at).toLocaleString() + ' · ' + deployStatus.event.sha.slice(0, 8) + (deployStatus.locked ? ' · 更新锁占用' : '')) : '等待本机更新器回执'} />}
      <Modal title="确认立即部署" open={deployConfirm} onCancel={() => setDeployConfirm(false)}
        onOk={() => void requestUpdate()} okText="确认部署" confirmLoading={requestingUpdate}
        okButtonProps={{ disabled: requestingUpdate || updateCheck?.state !== 'available' ||
          updateCheck.running_sha !== runningVersion?.running_sha }}>
        <Typography.Paragraph>即将请求 Mac mini 更新器部署当前应用最新 {updateCheck?.branch} 提交，可能执行测试、构建和重启。五分钟自动更新机制不受影响。</Typography.Paragraph>
        <Typography.Paragraph>目标 SHA：<Typography.Text code>{updateCheck?.remote_sha}</Typography.Text></Typography.Paragraph>
      </Modal>
      <Modal title="系统设置" open={settingsOpen} footer={null} onCancel={() => setSettingsOpen(false)} destroyOnHidden>
        <Space style={{marginBottom:16}}>
          <Button type={settingsTab === 'branding' ? 'primary' : 'default'} onClick={() => setSettingsTab('branding')}>名称设置</Button>
          <Button type={settingsTab === 'password' ? 'primary' : 'default'} onClick={() => setSettingsTab('password')}>修改密码</Button>
        </Space>
        {settingsTab === 'branding' ? <Form form={brandingForm} layout="vertical" onFinish={saveBranding}>
          <Form.Item name="workspace_name" label="工作台名称" rules={[{required:true,whitespace:true,max:80}]}><Input maxLength={80}/></Form.Item>
          <Form.Item name="reference_name" label="引用名称（浏览器 Title）" rules={[{required:true,whitespace:true,max:80}]}><Input maxLength={80}/></Form.Item>
          <Button type="primary" htmlType="submit" loading={settingsSaving}>保存</Button>
        </Form> : <Form form={passwordForm} layout="vertical" onFinish={changePassword}>
          <Form.Item name="current_password" label="当前密码" rules={[{required:true}]}><Input.Password autoComplete="current-password"/></Form.Item>
          <Form.Item name="new_password" label="新密码（至少12位）" rules={[{required:true,min:12,max:256}]}><Input.Password autoComplete="new-password"/></Form.Item>
          <Form.Item name="confirm_password" label="确认新密码" dependencies={['new_password']}
            rules={[{required:true},({getFieldValue})=>({validator(_,value) {
              return value === getFieldValue('new_password') ? Promise.resolve() : Promise.reject(new Error('两次密码不一致'))
            }})]}><Input.Password autoComplete="new-password"/></Form.Item>
          <Alert type="warning" showIcon message="修改成功后，所有设备需要重新登录" style={{marginBottom:12}}/>
          <Button type="primary" htmlType="submit" loading={settingsSaving}>确认修改密码</Button>
        </Form>}
      </Modal>
      {page === 'workspace' ? (
        <Card>
          <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="空应用，暂无业务模块">
            <Button type="primary" onClick={() => navigate('feedback')}>提交需求</Button>
          </Empty>
        </Card>
      ) : page === 'project' ? (
        <ProjectInfoPage data={projectInfo} loading={loadingOperations === 'project'} issueBase={githubIssues} onOpenTasks={() => navigate('tasks')} onRefresh={() => void loadOperations('project', true)} />
      ) : page === 'tasks' ? (
        <DevelopmentTasksPage data={developmentTasks} loading={loadingOperations === 'tasks'}
          onRefresh={() => void loadOperations('tasks')}
          onReleaseDraft={async (number, head) => {
            if (!session) throw new Error('请先登录')
            await api('/api/v1/pr-drafts/release', { method: 'POST', body: JSON.stringify({ number, head }) }, session.csrf_token)
            setNotice({ type: 'success', text: `PR #${number} 已解除 Draft，等待当前基线 MacCI 验证及自动合并。` })
            await loadOperations('tasks')
          }} />
      ) : page === 'releases' ? (
        <ReleasePage data={releaseHistory} loading={loadingOperations === 'releases'} onRefresh={() => void loadOperations('releases')}
          onAccept={async (sha, version) => {
            await api('/api/v1/releases/accept', { method: 'POST', body: JSON.stringify({ sha, version }) }, session.csrf_token)
            await loadOperations('releases')
          }} />
      ) : page === 'logs' ? (
        <SystemLogPage data={systemLogs} loading={loadingOperations === 'logs'} onRefresh={() => void loadOperations('logs')} />
      ) : (
        <Space direction="vertical" size="middle" style={{ width: '100%' }}>
          <Card title="意见反馈" extra={<Space wrap>
            <Button onClick={() => void loadFeedback()} loading={feedbackLoading}>刷新状态</Button>
            <Button type="primary" onClick={() => setFeedbackOpen(true)}>提交反馈</Button>
          </Space>}>
            <Typography.Paragraph type="secondary">一条反馈、一条交付链。点击详情查看提交 → 领取 → 开发 → 测试 → 合并 → 部署 → 验收的真实证据和任务执行状态。</Typography.Paragraph>
            <Table<Feedback> rowKey="id" size="small" dataSource={records} loading={feedbackLoading}
              scroll={{ x: 1660 }} pagination={{ pageSize: 15, showSizeChanger: true }} columns={[
                { title: '编号', dataIndex: 'id', width: 80, render: (id: number) => '#' + id },
                { title: '反馈内容', dataIndex: 'message', ellipsis: true, render: (message: string, row) =>
                  <Button type="link" style={{ padding: 0, maxWidth: '100%', overflow: 'hidden', textOverflow: 'ellipsis', textAlign: 'left', display: 'block' }} onClick={() => setSelectedFeedbackId(row.id)}>{message}</Button> },
                { title: '类别', dataIndex: 'category', width: 110, render: (category: string) => feedbackLabels[category] ?? category },
                { title: '提交时间', dataIndex: 'created_at', width: 170, render: feedbackDate },
                { title: '同步时间', dataIndex: 'synced_at', width: 170, render: feedbackDate },
                { title: '领取任务时间', dataIndex: 'claimed_at', width: 170, render: feedbackDate },
                { title: '开发开始时间', dataIndex: 'development_started_at', width: 170, render: feedbackDate },
                { title: '开发结束时间', dataIndex: 'development_finished_at', width: 170, render: feedbackDate },
                { title: '开发提交时间', dataIndex: 'development_submitted_at', width: 170, render: feedbackDate },
                { title: '整体状态', key: 'overall_status', width: 145, render: (_, row) => <FeedbackStatus row={row} /> },
                { title: '同步状态', key: 'status', width: 130, render: (_, row) =>
                  row.status === 'synced' && row.github_issue_number ? <Tag color="success">GitHub #{row.github_issue_number}</Tag> : <Tag>待同步</Tag> },
                { title: '操作', key: 'action', width: 75, render: (_, row) => <Button type="link" onClick={() => setSelectedFeedbackId(row.id)}>详情</Button> },
              ]} />
          </Card>
          <Modal title="提交意见反馈" open={feedbackOpen} onCancel={() => { if (!busy) setFeedbackOpen(false) }} footer={null} width={640}>
            <Form form={form} layout="vertical" onFinish={submitFeedback} initialValues={{ category: 'suggestion' }}>
              <Form.Item name="category" label="类型" rules={[{ required: true }]}>
                <Select options={[
                  { label: '需求 / 建议', value: 'suggestion' },
                  { label: '问题反馈', value: 'bug' },
                  { label: '验收意见', value: 'acceptance' },
                ]} />
              </Form.Item>
              <Form.Item name="message" label="详细说明" rules={[{ required: true, whitespace: true, min: 5, max: 2000 }]}>
                <Input.TextArea rows={6} maxLength={2000} showCount placeholder="描述操作步骤、实际结果与预期效果，请勿填写密码或密钥" />
              </Form.Item>
              <Space style={{ display: 'flex', justifyContent: 'flex-end' }}>
                <Button disabled={busy} onClick={() => setFeedbackOpen(false)}>取消</Button>
                <Button type="primary" htmlType="submit" loading={busy}>提交意见</Button>
              </Space>
            </Form>
          </Modal>
          <Drawer width={620} title={selectedFeedback ? '反馈 #' + selectedFeedback.id : '反馈详情'}
            open={selectedFeedbackId !== null} onClose={() => setSelectedFeedbackId(null)}
            extra={<Button loading={feedbackLoading} onClick={() => void loadFeedback()}>刷新状态</Button>}>
            {selectedFeedback ? <Space direction="vertical" size="middle" style={{ width: '100%' }}>
              <FeedbackDelivery delivery={selectedFeedback.delivery} issueBase={githubIssues} />
              <Descriptions bordered size="small" column={1} items={[
                { key: 'category', label: '反馈类别', children: feedbackLabels[selectedFeedback.category] ?? selectedFeedback.category },
                { key: 'created', label: '提交时间', children: feedbackDate(selectedFeedback.created_at) },
                { key: 'synced', label: '同步时间', children: feedbackDate(selectedFeedback.synced_at) },
                { key: 'claimed', label: '领取任务时间', children: feedbackDate(selectedFeedback.claimed_at) },
                { key: 'started', label: '开发开始时间', children: feedbackDate(selectedFeedback.development_started_at) },
                { key: 'finished', label: '开发结束时间', children: feedbackDate(selectedFeedback.development_finished_at) },
                { key: 'submitted', label: '开发提交时间', children: feedbackDate(selectedFeedback.development_submitted_at) },
                { key: 'overall', label: '整体状态', children: <FeedbackStatus row={selectedFeedback} /> },
                { key: 'refreshed', label: '开发状态最近同步', children: feedbackDate(selectedFeedback.progress_synced_at) },
                { key: 'sha', label: '交付提交 SHA', children: selectedFeedback.delivered_sha ? <Typography.Text copyable style={{ overflowWrap: 'anywhere' }}>{selectedFeedback.delivered_sha}</Typography.Text> : '未记录' },
                { key: 'pr', label: '关联 PR', children: selectedFeedback.developer_pr ? <a href={githubIssues.replace(/\/issues$/, '/pull/') + selectedFeedback.developer_pr} target="_blank" rel="noopener noreferrer">PR #{selectedFeedback.developer_pr}</a> : '未记录' },
                { key: 'status', label: '同步状态', children: selectedFeedback.status === 'synced' && selectedFeedback.github_issue_number ? <Tag color="success">已同步</Tag> : <Tag>待同步</Tag> },
                { key: 'issue', label: 'GitHub Issue', children: selectedFeedback.github_issue_number
                  ? <a href={githubIssues + '/' + selectedFeedback.github_issue_number} target="_blank" rel="noopener noreferrer">查看 Issue #{selectedFeedback.github_issue_number}</a>
                  : '尚未同步' },
              ]} />
              <Card size="small" title="你的意见"><Typography.Paragraph style={{ whiteSpace: 'pre-wrap', overflowWrap: 'anywhere', marginBottom: 0 }}>{selectedFeedback.message}</Typography.Paragraph></Card>
              <Card size="small" title="开发处理结果">
                {selectedFeedback.developer_reply ? <Typography.Paragraph style={{ whiteSpace: 'pre-wrap', overflowWrap: 'anywhere', marginBottom: 0 }}>{selectedFeedback.developer_reply}</Typography.Paragraph> : <Typography.Text type="secondary">尚无已同步的开发回复。已同步不代表已完成。</Typography.Text>}
                {selectedFeedback.progress_stale && <Alert type="warning" showIcon message="开发状态尚未刷新，请稍后重试" style={{ marginTop: 12 }} />}
              </Card>
            </Space> : <Empty description="该反馈暂不在当前列表中，请刷新状态" />}
          </Drawer>
        </Space>
      )}
    </WorkspaceShell>
  )
}

createRoot(document.getElementById('root')!).render(
  <React.StrictMode>
    <ConfigProvider locale={zhCN} theme={{
      token: {
        colorPrimary: '#3979ec',
        borderRadius: 9,
        fontFamily: '-apple-system, BlinkMacSystemFont, PingFang SC, Microsoft YaHei, sans-serif',
      },
      components: {
        Layout: { siderBg: '#fff', headerBg: '#fff', bodyBg: '#f6f8fc' },
        Menu: { itemSelectedBg: '#eaf2ff' },
      },
    }}>
      <App />
    </ConfigProvider>
  </React.StrictMode>,
)
