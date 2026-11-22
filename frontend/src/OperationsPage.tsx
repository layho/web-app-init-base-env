import { useState } from 'react'
import { Alert, Button, Card, Descriptions, Drawer, Empty, Flex, List, Modal, Space, Table, Tag, Typography } from 'antd'

type Operation = { at: string; code: string; sha: string }
type ReleaseNote = { version: string; title: string; changes: string[]; validation?: string; date?: string; summary?: string; acceptance_required?: boolean; acceptance_status?: string; validation_steps?: string[]; validation_note?: string; reference_url?: string }
export type ReleaseHistory = {
  version: string
  running_sha: string
  releases: ReleaseNote[]
  deployments: Operation[]
  acceptance?: { enabled: boolean; state: 'unavailable' | 'not_requested' | 'queued' | 'published' | 'blocked' | 'unknown'; issue?: number | null }
}
export type SystemLogs = { events: Operation[] }

const labels: Record<string, string> = {
  startup_ok: '启动自检通过',
  schema_check_failed: '数据库迁移检查失败',
  frontend_missing: '缺少前端构建产物',
  install_ok: '安装通过本机健康检查',
  install_failed: '安装健康检查失败',
  update_ok: '更新通过本机健康检查',
  update_blocked: '更新等待数据库迁移',
  rollback: '发布失败，已尝试回退',
  rollback_ok: '回滚后服务已恢复',
  rollback_failed: '回滚后服务未恢复',
  feedback_ok: '反馈已同步到 GitHub',
  feedback_failed: '反馈同步失败',
}

function OperationTable({ records, loading, onDetails }: { records: Operation[]; loading: boolean; onDetails?: (row: Operation) => void }) {
  return <Table
    rowKey={(_, index) => String(index)}
    size="small"
    loading={loading}
    dataSource={records}
    locale={{ emptyText: <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="暂无记录" /> }}
    scroll={{ x: 480 }}
    columns={[
      { title: '时间', dataIndex: 'at', width: 180, render: (at: string) => new Date(at).toLocaleString('zh-CN', { hour12: false }) },
      { title: '事件', dataIndex: 'code', render: (code: string) => <Tag color={
        code.endsWith('_ok') ? 'success' : 'warning'
      }>{labels[code] ?? code}</Tag> },
      { title: '关联提交', dataIndex: 'sha', width: 120, render: (sha: string) => <code title={sha}>{sha.slice(0, 10)}</code> },
      ...(onDetails ? [{ title: '操作', key: 'action', width: 80, render: (_: unknown, row: Operation) => <Button type="link" onClick={() => onDetails(row)}>详情</Button> }] : []),
    ]}
  />
}

type RefreshProps = { loading: boolean; onRefresh: () => void }

function acceptanceTag(release: ReleaseNote) {
  if (release.acceptance_required) return <Tag color="warning">需要验收</Tag>
  if (release.acceptance_status === 'observed') return <Tag color="success">已有使用反馈</Tag>
  return <Tag>{release.acceptance_required === false ? '常规更新' : '验收状态未记录'}</Tag>
}

function safeReference(value?: string) {
  if (!value) return undefined
  try {
    const parsed = new URL(value)
    return parsed.protocol === 'https:' && parsed.hostname === 'github.com' && !parsed.username && !parsed.password ? parsed.href : undefined
  } catch { return undefined }
}

export function ReleasePage({ data, loading, onRefresh, onAccept }: RefreshProps & { data: ReleaseHistory | null; onAccept: (sha: string, version: string) => Promise<void> }) {
  const [selectedVersion, setSelectedVersion] = useState<string | null>(null)
  const [deployment, setDeployment] = useState<Operation | null>(null)
  const [accepting, setAccepting] = useState(false)
  const [confirm, setConfirm] = useState<{sha: string; version: string} | null>(null)
  const [acceptError, setAcceptError] = useState<string | null>(null)
  const acceptance = data?.acceptance
  const canAccept = Boolean(acceptance?.enabled && acceptance.state === 'not_requested' && data && /^[0-9a-f]{40}$/.test(data.running_sha) && data.releases[0]?.acceptance_required && data.releases[0]?.version === data.version)
  function requestAcceptance() {
    if (!data || !canAccept) return
    setAcceptError(null)
    setConfirm({ sha: data.running_sha, version: data.version })
  }
  async function confirmAcceptance() {
    if (!confirm || accepting) return
    setAccepting(true)
    setAcceptError(null)
    try {
      await onAccept(confirm.sha, confirm.version)
      setConfirm(null)
    } catch (error) {
      setAcceptError(error instanceof Error ? error.message : '验收提交失败，请重试')
    } finally {
      setAccepting(false)
    }
  }
  function acceptanceAction() {
    if (!acceptance?.enabled) return null
    if (acceptance.state === 'published') return <Tag color="success">验收通过 · 已发布 main</Tag>
    if (acceptance.state === 'queued') return <Tag color="processing">验收已提交 · 等待发布 main</Tag>
    if (acceptance.state === 'blocked') return <Tag color="error">发布受阻，请查看验收任务</Tag>
    if (acceptance.state === 'unknown') return <Tag color="warning">发布状态待刷新</Tag>
    return canAccept ? <Button type="primary" onClick={requestAcceptance} disabled={accepting || loading}>验收通过</Button> : null
  }
  const selected = data?.releases.find(release => release.version === selectedVersion)
  const pending = data?.releases.filter(release => release.acceptance_required && release.acceptance_status === 'pending').length ?? 0
  return <Flex vertical gap="middle">
    <Card title="当前运行版本" extra={<Space wrap>{pending > 0 && acceptance?.state !== 'published' && <Tag color="warning">待验收版本 {pending}</Tag>}{acceptanceAction()}<Button onClick={onRefresh} loading={loading}>刷新</Button></Space>}>
      {data ? <Flex wrap gap="small" align="center">
        <Tag color="blue">{data.version}</Tag>
        <Typography.Text copyable code style={{ overflowWrap: 'anywhere' }}>{data.running_sha === 'unknown' ? '未上报 Git SHA' : data.running_sha}</Typography.Text>
      </Flex> : <Typography.Text type="secondary">暂无版本信息</Typography.Text>}
    </Card>
    <Alert type="info" showIcon message="代码已提交不等于线上已验收" description={acceptance?.enabled ? '体验当前版本后点击「验收通过」，系统将提交绑定当前运行 SHA 的正式发布请求；发布完成前不会显示已发布。' : '请核对当前运行版本和 Git SHA，以实际验证结果为准。'} />
    {acceptance?.issue && <Typography.Link href={`https://github.com/layho/web-app-init-base-env/issues/${acceptance.issue}`} target="_blank" rel="noopener noreferrer">查看正式发布任务 #{acceptance.issue}</Typography.Link>}
    <Modal title="确认验收并正式发布" open={confirm !== null} onCancel={() => { if (!accepting) setConfirm(null) }} onOk={() => void confirmAcceptance()} okText="验收通过，提交发布" cancelText="取消" confirmLoading={accepting} closable={!accepting} maskClosable={!accepting} cancelButtonProps={{ disabled: accepting }}>
      <Typography.Paragraph>确认已体验以下候选版本，授权将该版本发布至正式 main 分支。系统会再次核验测试结果和分支 SHA。</Typography.Paragraph>
      <Typography.Paragraph strong>v{confirm?.version}</Typography.Paragraph>
      <Typography.Paragraph code style={{ overflowWrap: 'anywhere' }}>{confirm?.sha}</Typography.Paragraph>
      {acceptError && <Alert type="error" showIcon message={acceptError} />}
    </Modal>
    {data?.releases.length ? data.releases.map(release =>
      <Card key={release.version}
        title={<Space wrap><Typography.Text strong>v{release.version}</Typography.Text><span>{release.title}</span></Space>}
        extra={<Space wrap><Typography.Text type="secondary">{release.date || '日期未记录'}</Typography.Text>{release.version === data?.version && acceptance?.state === 'published' ? <Tag color="success">已验收</Tag> : acceptanceTag(release)}{release.version === data?.version ? acceptanceAction() : null}<Button type="link" onClick={() => setSelectedVersion(release.version)}>详情</Button></Space>}>
        {release.summary && <Typography.Paragraph type="secondary">{release.summary}</Typography.Paragraph>}
        <List size="small" dataSource={release.changes ?? []} renderItem={change =>
          <List.Item><Typography.Text style={{ whiteSpace: 'pre-wrap', overflowWrap: 'anywhere' }}>{change}</Typography.Text></List.Item>} />
        {release.validation_steps?.length ? <div style={{ marginTop: 12, padding: 12, border: '1px solid #f5d591', background: '#fffdf5', borderRadius: 8 }}>
          <Typography.Text strong>验收步骤</Typography.Text>
          <ol>{release.validation_steps.map((step, i) => <li key={i} style={{ whiteSpace: 'pre-wrap', overflowWrap: 'anywhere' }}>{step}</li>)}</ol>
          <Typography.Paragraph type="secondary" style={{ marginBottom: 0 }}>{release.validation_note || release.validation}</Typography.Paragraph>
        </div> : <Typography.Paragraph type="secondary" style={{ margin: '12px 0 0' }}>{release.validation_note || release.validation || '暂无验收说明'}</Typography.Paragraph>}
        {safeReference(release.reference_url) && <Typography.Link href={safeReference(release.reference_url)} target="_blank" rel="noopener noreferrer">查看 GitHub 记录</Typography.Link>}
      </Card>
    ) : <Card><Empty description={loading ? '正在读取更新记录…' : '暂无更新记录'} /></Card>}
    <Card title="本机发布记录">
      <OperationTable records={data?.deployments ?? []} loading={loading} onDetails={setDeployment} />
    </Card>
    <Drawer title={selected ? '版本 ' + selected.version + ' · ' + selected.title : '版本详情'} width={680}
      open={selectedVersion !== null} onClose={() => setSelectedVersion(null)}>
      {selected ? <Flex vertical gap="middle">
        <Descriptions bordered size="small" column={1} items={[
          { key: 'version', label: '版本号', children: selected.version },
          { key: 'date', label: '发布日期', children: selected.date || '未记录' },
          { key: 'acceptance', label: '验收状态', children: selected.version === data?.version && acceptance?.state === 'published' ? <Tag color="success">已验收</Tag> : acceptanceTag(selected) },
        ]} />
        {selected.version === data?.version && acceptanceAction()}
        {selected.summary && <Card size="small" title="版本摘要"><Typography.Paragraph style={{ whiteSpace: 'pre-wrap', overflowWrap: 'anywhere' }}>{selected.summary}</Typography.Paragraph></Card>}
        <Card size="small" title="更新内容"><List size="small" dataSource={selected.changes ?? []} renderItem={change => <List.Item><Typography.Text style={{ whiteSpace: 'pre-wrap', overflowWrap: 'anywhere' }}>{change}</Typography.Text></List.Item>} /></Card>
        <Card size="small" title="测试与验收">
          {selected.validation_steps?.length ? <ol>{selected.validation_steps.map((step, i) => <li key={i} style={{ whiteSpace: 'pre-wrap', overflowWrap: 'anywhere' }}>{step}</li>)}</ol> : null}
          <Typography.Paragraph style={{ whiteSpace: 'pre-wrap', overflowWrap: 'anywhere' }}>{selected.validation_note || selected.validation || '暂无验收说明'}</Typography.Paragraph>
          {safeReference(selected.reference_url) && <a href={safeReference(selected.reference_url)} target="_blank" rel="noopener noreferrer">查看 GitHub 记录</a>}
        </Card>
      </Flex> : <Empty description="该版本暂不在当前记录中" />}
    </Drawer>
    <Drawer title="发布详情" width={580} open={deployment !== null} onClose={() => setDeployment(null)}>
      {deployment && <Descriptions bordered column={1} size="small" items={[
        { key: 'at', label: '运行时间', children: new Date(deployment.at).toLocaleString('zh-CN', { hour12: false }) },
        { key: 'result', label: '发布事件', children: <Tag color={deployment.code.endsWith('_ok') ? 'success' : 'warning'}>{labels[deployment.code] ?? deployment.code}</Tag> },
        { key: 'sha', label: '完整提交 SHA', children: <Typography.Text copyable style={{ overflowWrap: 'anywhere' }}>{deployment.sha}</Typography.Text> },
        { key: 'running', label: '是否当前运行', children: data?.running_sha && data.running_sha !== 'unknown' ? (data.running_sha === deployment.sha ? 'SHA 一致' : 'SHA 不一致') : '未确认' },
      ]} />}
    </Drawer>
  </Flex>
}

export function SystemLogPage({ data, loading, onRefresh }: RefreshProps & { data: SystemLogs | null }) {
  return <Card title="最近 100 条系统事件" extra={<Button onClick={onRefresh} loading={loading}>刷新</Button>}>
    <OperationTable records={data?.events ?? []} loading={loading} />
  </Card>
}
