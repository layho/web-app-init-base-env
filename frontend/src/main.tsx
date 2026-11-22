/// <reference types="vite/client" />
import React, { useEffect, useState } from 'react'
import { createRoot } from 'react-dom/client'
import { AppstoreOutlined, CommentOutlined, HistoryOutlined, ProfileOutlined, CloudDownloadOutlined, UnorderedListOutlined, ProjectOutlined } from '@ant-design/icons'
import { Alert, Button, Card, ConfigProvider, Empty, Form, Input, Layout, Select, Space, Table, Tag, Typography } from 'antd'
import zhCN from 'antd/locale/zh_CN'
import 'antd/dist/reset.css'
import { ReleasePage, SystemLogPage } from './OperationsPage'
import type { ReleaseHistory, SystemLogs } from './OperationsPage'
import { DevelopmentTasksPage } from './DevelopmentTasksPage'
import type { TaskSnapshot } from './DevelopmentTasksPage'
import { ProjectInfoPage } from './ProjectInfoPage'
import type { ProjectInfo } from './ProjectInfoPage'
import { WorkspaceShell } from './WorkspaceShell'

type RunningVersion = { version: string; running_sha: string; manual_update_available: boolean }
type Session = { username: string; csrf_token: string }
type FeedbackInput = { category: string; message: string }
type Feedback = FeedbackInput & { id: number; status: string; github_issue_number: number | null }

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
  const [loading, setLoading] = useState(true)
  const [busy, setBusy] = useState(false)
  const [runningVersion, setRunningVersion] = useState<RunningVersion | null>(null)
  const [versionUnavailable, setVersionUnavailable] = useState(false)
  const [requestingUpdate, setRequestingUpdate] = useState(false)
  const [page, setPage] = useState<Page>('workspace')
  const [records, setRecords] = useState<Feedback[]>([])
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

  async function requestUpdate() {
    if (!session || requestingUpdate) return
    setRequestingUpdate(true)
    setNotice(null)
    try {
      await api('/api/v1/deploy/update', { method: 'POST' }, session.csrf_token)
      setNotice({ type: 'success', text: '已请求检查更新。如有新版本，可在「更新记录」或「系统日志」查看发布结果。' })
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

  async function logout() {
    setBusy(true)
    setNotice(null)
    try {
      await api('/api/logout', { method: 'POST' }, session?.csrf_token)
      setSession(null)
      setRunningVersion(null)
      setVersionUnavailable(false)
      setRecords([])
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
    try {
      setRecords(await api<Feedback[]>('/api/v1/feedback'))
    } catch (error) {
      showError(error)
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
      setNotice({ type: 'success', text: `反馈 #${result.id} 已保存，等待同步到 GitHub。` })
      await loadFeedback()
    } catch (error) {
      showError(error)
    } finally {
      setBusy(false)
    }
  }

  async function loadOperations(key: 'releases' | 'logs' | 'tasks' | 'project') {
    setLoadingOperations(key)
    setNotice(null)
    try {
      if (key === 'releases') setReleaseHistory(await api<ReleaseHistory>('/api/release-notes'))
      else if (key === 'logs') setSystemLogs(await api<SystemLogs>('/api/v1/system-logs'))
      else if (key === 'tasks') setDevelopmentTasks(await api<TaskSnapshot>('/api/v1/development-tasks'))
      else setProjectInfo(await api<ProjectInfo>('/api/v1/project-info'))
    } catch (error) {
      showError(error)
    } finally {
      setLoadingOperations(current => current === key ? null : current)
    }
  }

  function navigate(key: Page) {
    setPage(key)
    setNotice(null)
    if (key === 'feedback') void loadFeedback()
    if (key === 'releases' || key === 'logs' || key === 'tasks' || key === 'project') void loadOperations(key)
  }

  if (loading) return <div style={{ minHeight: '100vh', display: 'grid', placeItems: 'center' }}>正在验证登录状态…</div>
  if (!session) return (
    <Layout style={{ minHeight: '100vh', display: 'grid', placeItems: 'center', background: '#f3f7fa', padding: 20 }}>
      <Card style={{ width: '100%', maxWidth: 390 }}>
        <Typography.Title level={3}>{appName}</Typography.Title>
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
      appName={appName}
      brandMark="应用"
      username={session.username}
      activePage={page}
      navigation={navigation}
      onNavigate={navigate}
      onLogout={logout}
      logoutDisabled={busy}
      headerActions={<>
        {runningVersion && !versionUnavailable ? (
          <Tag color="blue" title={runningVersion.running_sha}>
            v{runningVersion.version}{runningVersion.running_sha !== 'unknown' && ` · ${runningVersion.running_sha.slice(0, 8)}`}
          </Tag>
        ) : <Tag>{versionUnavailable ? '版本暂不可用' : '读取版本中…'}</Tag>}
        <Button
          icon={<CloudDownloadOutlined />}
          onClick={() => void requestUpdate()}
          loading={requestingUpdate}
          disabled={busy || versionUnavailable || !runningVersion?.manual_update_available}
        >
          检查并更新
        </Button>
      </>}
    >
      {basePreview && <Alert type="warning" showIcon message="基座候选版本验收环境；反馈将同步到公开 GitHub 仓库，请勿填写敏感信息。" style={{ marginBottom: 16 }} />}
      {notice && <Alert type={notice.type} showIcon message={notice.text} style={{ marginBottom: 16 }} />}
      {page === 'workspace' ? (
        <Card>
          <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="空应用，暂无业务模块">
            <Button type="primary" onClick={() => navigate('feedback')}>提交需求</Button>
          </Empty>
        </Card>
      ) : page === 'project' ? (
        <ProjectInfoPage data={projectInfo} loading={loadingOperations === 'project'} issueBase={githubIssues} onOpenTasks={() => navigate('tasks')} />
      ) : page === 'tasks' ? (
        <DevelopmentTasksPage data={developmentTasks} loading={loadingOperations === 'tasks'} onRefresh={() => void loadOperations('tasks')} />
      ) : page === 'releases' ? (
        <ReleasePage data={releaseHistory} loading={loadingOperations === 'releases'} onRefresh={() => void loadOperations('releases')} />
      ) : page === 'logs' ? (
        <SystemLogPage data={systemLogs} loading={loadingOperations === 'logs'} onRefresh={() => void loadOperations('logs')} />
      ) : (
        <Space direction="vertical" size="middle" style={{ width: '100%' }}>
          <Card>
            <Form form={form} layout="vertical" onFinish={submitFeedback} initialValues={{ category: 'suggestion' }}>
              <Form.Item name="category" label="类型" rules={[{ required: true }]}>
                <Select options={[
                  { label: '需求 / 建议', value: 'suggestion' },
                  { label: '问题', value: 'bug' },
                  { label: '验收意见', value: 'acceptance' },
                ]} />
              </Form.Item>
              <Form.Item name="message" label="内容" rules={[{ required: true, whitespace: true, min: 5, max: 2000 }]}>
                <Input.TextArea rows={4} maxLength={2000} showCount placeholder="描述需求或问题，请勿填写密码或密钥" />
              </Form.Item>
              <Button type="primary" htmlType="submit" loading={busy}>提交反馈</Button>
            </Form>
          </Card>
          <Card title="反馈记录" extra={<Button onClick={loadFeedback}>刷新状态</Button>}>
            <Table rowKey="id" size="small" dataSource={records} columns={[
              { title: '内容', dataIndex: 'message', ellipsis: true },
              { title: '同步状态', key: 'status', render: (_, row) =>
                row.status === 'synced' && row.github_issue_number
                  ? <a href={`${githubIssues}/${row.github_issue_number}`} target="_blank" rel="noreferrer">GitHub #{row.github_issue_number}</a>
                  : '待同步',
              },
            ]} />
          </Card>
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
