import { useEffect, useRef, useState } from 'react'
import { Alert, Button, Card, Empty, Flex, Progress, Space, Table, Tabs, Tag, Typography } from 'antd'
import { diskView, hostState, serviceState } from './hostops-view.mjs'
import './hostops.css'
import { HostOpsTasks } from './HostOpsTasks'
import type { TaskApi } from './HostOpsTasks'
import { HostOpsPolicy, HostOpsModules } from './HostOpsEvidence'
import type { EvidenceApi } from './HostOpsEvidence'

type Disk = { total_bytes: number; used_bytes: number; free_bytes: number }
type Service = { service_id: string; state: string; last_exit_code?: number | null }
type Snapshot = { system: string; architecture: string; disk: Disk | null; services: Service[]; capabilities: string[] }
type Host = { host_id: string; state: string; age_seconds?: number; snapshot: Snapshot | null }
type HostResponse = { hosts: Host[] }
const serviceNames: Record<string,string> = {
  'macci.runner':'MacCI 测试执行器', 'macci.preview':'Preview 合并协调器',
  'macci.publish':'Main 发布协调器', 'macci.receipts':'回执发送器', 'panel.merge':'Panel 合并协调器',
}

export function HostOpsPage({load,taskApi,evidenceApi}: {load: (signal: AbortSignal) => Promise<HostResponse>; taskApi:TaskApi;evidenceApi:EvidenceApi}) {
  const loader = useRef(load)
  loader.current = load
  const controller = useRef<AbortController | null>(null)
  const [hosts,setHosts] = useState<Host[]>([])
  const [error,setError] = useState('')
  const [loading,setLoading] = useState(true)
  const [lastRead,setLastRead] = useState<number | null>(null)
  const [tab,setTab] = useState('overview')
  async function refresh() {
    if (controller.current) return
    const request = new AbortController()
    controller.current = request
    setLoading(true)
    try {
      const result = await loader.current(request.signal)
      if (request.signal.aborted) return
      setHosts(result.hosts)
      setLastRead(Date.now())
      setError('')
    } catch (e) {
      if (!request.signal.aborted) {
        setHosts([])
        setLastRead(null)
        const status = typeof e === 'object' && e !== null && 'status' in e ? e.status : null
        setError(status === 503 ? 'HostOps 尚未启用或配置不完整' : status === 401 ? '登录已失效，请重新登录' : '无法读取宿主状态，请检查连接后重试')
      }
    } finally {
      if (controller.current === request) {
        controller.current = null
        if (!request.signal.aborted) setLoading(false)
      }
    }
  }
  useEffect(() => {
    void refresh()
    const timer = window.setInterval(() => void refresh(), 15000)
    return () => {
      window.clearInterval(timer)
      controller.current?.abort()
      controller.current = null
    }
  }, [])
  const host = hosts[0]
  const state = hostState(host?.state)
  const snapshot = host?.snapshot
  const disk = diskView(snapshot?.disk)
  const serviceTable = <Table<Service> size="small" rowKey="service_id" pagination={false}
    dataSource={snapshot?.services ?? []} scroll={{x:480}}
    locale={{emptyText:<Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="暂无宿主服务回执"/>}}
    columns={[
      {title:'服务名称',dataIndex:'service_id',render:(id:string)=>serviceNames[id] ?? id},
      {title:'状态',dataIndex:'state',render:(value:string)=>{const s=serviceState(value);return <Tag color={s.color}>{s.label}</Tag>}},
      {title:'最近退出码',dataIndex:'last_exit_code',render:(value:number|null|undefined)=>value ?? '未上报'},
    ]}/>
  const unavailable = (title:string, description:string) => <Card title={title}><Alert type="info" showIcon message="功能尚未开放" description={description}/></Card>
  return <Flex vertical gap="middle">
    <Flex align="center" justify="space-between" wrap="wrap" gap={12}>
      <div className="workspace-page-heading"><Typography.Title level={3}>宿主机管理</Typography.Title><Typography.Text type="secondary">HostOps · 宿主状态与受控运维</Typography.Text></div>
      <Space wrap><Tag color={state.color}>{state.label}</Tag><Button onClick={()=>void refresh()} loading={loading}>刷新状态</Button></Space>
    </Flex>
    {error && <Alert type="warning" showIcon message={error}/>}
    <Tabs activeKey={tab} onChange={setTab} items={[
      {key:'overview',label:'运行概览',children:<Flex vertical gap="middle">
        {host?.state === 'stale' && <Alert type="warning" showIcon message="心跳已过期，下列数据为历史快照，并非当前运行证据"/>}
        <div className="hostops-grid">
          <Card size="small" title="宿主连接"><Typography.Title level={4}>{host?.host_id ?? '未连接'}</Typography.Title><Tag color={state.color}>{state.label}</Tag><Typography.Paragraph type="secondary" style={{marginTop:8}}>心跳年龄：{host?.age_seconds == null ? '未上报' : host.age_seconds+' 秒'}</Typography.Paragraph></Card>
          <Card size="small" title="系统环境"><Typography.Title level={4}>{snapshot?.system ?? '未上报'}</Typography.Title><Typography.Text type="secondary">{snapshot?.architecture ?? '架构未上报'}</Typography.Text></Card>
          <Card size="small" title="磁盘容量">{disk ? <><Typography.Title level={4}>{disk.free} GiB 可用</Typography.Title><Progress percent={disk.percent}/><Typography.Text type="secondary">总容量 {disk.total} GiB</Typography.Text></> : <Typography.Text type="secondary">未上报</Typography.Text>}</Card>
          <Card size="small" title="CPU / 内存"><Typography.Title level={4}>未上报</Typography.Title><Typography.Text type="secondary">尚无实时利用率采集证据</Typography.Text></Card>
        </div>
        <Card title="服务状态">{serviceTable}</Card>
        <Card size="small" title="Agent 能力"><Space wrap>{snapshot?.capabilities.length ? snapshot.capabilities.map(c=><Tag key={c}>{c}</Tag>) : '暂无能力上报'}</Space></Card>
      </Flex>},
      {key:'terminal',label:'远程终端',children:tab==='terminal'?<HostOpsTasks api={taskApi} host={host?.host_id} online={host?.state==='online'} mode="terminal"/>:null},
      {key:'services',label:'服务管理',children:<Card title="服务清单"><Alert type="info" showIcon message="当前仅支持读取服务状态；跨用户服务控制需要独立可信 broker，尚未开放" style={{marginBottom:12}}/>{serviceTable}</Card>},
      {key:'upgrade',label:'模块升级',children:tab==='upgrade'?<HostOpsModules api={evidenceApi}/>:null},
      {key:'records',label:'执行记录',children:tab==='records'?<HostOpsTasks api={taskApi} host={host?.host_id} online={host?.state==='online'} mode="records"/>:null},
      {key:'security',label:'安全授权',children:tab==='security'?<HostOpsPolicy api={evidenceApi}/>:null},
    ]}/>
    <Typography.Text type="secondary">最近读取：{lastRead === null ? '未取得数据' : new Date(lastRead).toLocaleString('zh-CN',{hour12:false})} · 每15秒刷新一次</Typography.Text>
  </Flex>
}

function DescriptionsSafe({state,host}:{state:string;host?:string}) {
  return <Space direction="vertical"><Typography.Text>宿主：{host ?? '未配置或未连接'}</Typography.Text><Typography.Text>连接状态：{state}</Typography.Text><Typography.Text type="secondary">当前能力仅限只读诊断。</Typography.Text></Space>
}
