import { useEffect, useRef, useState } from 'react'
import { Alert, Button, Card, Descriptions, Drawer, Flex, Input, Select, Space, Table, Tag, Typography } from 'antd'

export type HostTask = { task_id:string; actor:string; host_id:string; operation:string; reason:string; created_at:number; expires_at:number; state:string }
export type TaskDetail = HostTask & {receipt:null|{outcome:string;result:unknown;received_at:number}}
export type TaskList = {tasks:HostTask[];dispatch_enabled:boolean}
export type TaskInput = {host_id:string;operation:string;parameters:Record<string,never>;timeout_seconds:number;idempotency_key:string;reason:string}
export type TaskApi = {list:(signal:AbortSignal)=>Promise<TaskList>;create:(input:TaskInput)=>Promise<{task:HostTask;created:boolean}>;detail:(id:string,signal:AbortSignal)=>Promise<TaskDetail>}
const operations = [{value:'host.health',label:'宿主健康诊断'},{value:'service.inventory',label:'服务清单诊断'},{value:'agent.snapshot',label:'完整宿主快照'}]
const labels:Record<string,[string,string]> = {QUEUED:['等待领取','blue'],CLAIMED:['已领取 · 等待回执','processing'],SUCCESS:['执行成功','success'],FAILED:['执行失败','error'],TIMED_OUT:['等待超时','warning'],UNKNOWN:['结果未知','warning'],CANCELLED:['已取消','default']}
function TaskState({value}:{value:string}) {const s=labels[value]??['状态未知','default'];return <Tag color={s[1]}>{s[0]}</Tag>}
const date=(value:number)=>new Date(value*1000).toLocaleString('zh-CN',{hour12:false})
export function HostOpsTasks({api,host,online,mode}:{api:TaskApi;host?:string;online:boolean;mode:'terminal'|'records'}) {
  const apiRef=useRef(api);apiRef.current=api
  const [tasks,setTasks]=useState<HostTask[]>([])
  const [enabled,setEnabled]=useState(false)
  const [error,setError]=useState('')
  const [submitError,setSubmitError]=useState('')
  const [loading,setLoading]=useState(true)
  const [busy,setBusy]=useState(false)
  const submitting=useRef(false)
  const [operation,setOperation]=useState('host.health')
  const [reason,setReason]=useState('')
  const [selected,setSelected]=useState<string|null>(null)
  const [detail,setDetail]=useState<TaskDetail|null>(null)
  const [detailError,setDetailError]=useState('')
  const [detailLoading,setDetailLoading]=useState(false)
  const attempt=useRef<{fingerprint:string;key:string}|null>(null)
  const polling=useRef<AbortController|null>(null)
  async function refresh(){
    if(polling.current)return
    const c=new AbortController();polling.current=c;setLoading(true)
    try{const data=await apiRef.current.list(c.signal);if(!c.signal.aborted){setTasks(data.tasks);setEnabled(data.dispatch_enabled);setError('')}}
    catch{if(!c.signal.aborted){setTasks([]);setEnabled(false);setError('任务记录暂不可用，请检查 HostOps 存储配置或登录状态。')}}
    finally{if(polling.current===c){polling.current=null;if(!c.signal.aborted)setLoading(false)}}
  }
  useEffect(()=>{void refresh();const timer=window.setInterval(()=>void refresh(),5000);return()=>{window.clearInterval(timer);polling.current?.abort();polling.current=null}},[host])
  useEffect(()=>{
    setDetail(null);setDetailError('');if(!selected)return
    let active=true;let pending:AbortController|null=null
    async function read(){
      if(!active||pending)return
      const c=new AbortController();pending=c;setDetailLoading(true)
      try{const data=await apiRef.current.detail(selected!,c.signal);if(active){setDetail(data);setDetailError('')}}
      catch{if(active){setDetail(null);setDetailError('执行详情读取失败，请稍后重试。')}}
      finally{pending=null;if(active)setDetailLoading(false)}
    }
    void read();const timer=window.setInterval(()=>void read(),5000)
    return()=>{active=false;window.clearInterval(timer);pending?.abort()}
  },[selected,host])
  async function submit(){
    if(submitting.current||!host||!online||!enabled||!reason.trim())return
    submitting.current=true;setBusy(true);setSubmitError('')
    const fingerprint=JSON.stringify([host,operation,reason.trim()])
    try{
      if(attempt.current?.fingerprint!==fingerprint)attempt.current={fingerprint,key:crypto.randomUUID()}
      const response=await apiRef.current.create({host_id:host,operation,parameters:{},timeout_seconds:120,idempotency_key:attempt.current.key,reason:reason.trim()})
      attempt.current=null;setReason('');setSelected(response.task.task_id);void refresh()
    }catch{setSubmitError('任务提交未确认。原参数重试会复用同一任务键，防止重复执行。')}
    finally{submitting.current=false;setBusy(false)}
  }
  const selectedRow=tasks.find(t=>t.task_id===selected)
  return <Flex vertical gap="middle">
    {error&&<Alert type="warning" showIcon message={error}/>}
    {mode==='terminal'&&<Card title="受控远程终端" extra={<Tag>L1 · 只读</Tag>}>
      <Flex vertical gap={12}>
        <Alert type="info" showIcon message="仅支持三种已授权诊断操作，不接受任意 Shell 命令。"/>
        {(!online||!enabled)&&<Alert type="warning" showIcon message={!online?'宿主未在线或心跳已过期':'远程调度未启用，禁止提交执行任务'}/>}
        <Typography.Text>选择诊断操作</Typography.Text>
        <Select value={operation} options={operations} onChange={setOperation} disabled={busy}/>
        <Typography.Text>执行原因（请勿填写密钥、密码等敏感信息）</Typography.Text>
        <Input.TextArea rows={2} maxLength={240} value={reason} onChange={e=>setReason(e.target.value)} disabled={busy}/>
        <pre className="hostops-terminal">{JSON.stringify({host_id:host??'未连接',operation,parameters:{},timeout_seconds:120},null,2)}</pre>
        {submitError&&<Alert type="error" message={submitError}/>}
        <Button type="primary" loading={busy} disabled={!host||!online||!enabled||!reason.trim()} onClick={()=>void submit()}>执行诊断</Button>
      </Flex>
    </Card>}
    <Card title="执行记录" extra={<Button loading={loading} onClick={()=>void refresh()}>刷新记录</Button>}>
      <Table<HostTask> size="small" rowKey="task_id" dataSource={tasks} loading={loading} scroll={{x:760}} pagination={{pageSize:10}} columns={[
        {title:'任务',dataIndex:'operation',render:(v:string)=>operations.find(x=>x.value===v)?.label??v},
        {title:'操作人',dataIndex:'actor'},
        {title:'创建时间',dataIndex:'created_at',render:date},
        {title:'状态',dataIndex:'state',render:(v:string)=><TaskState value={v}/>},
        {title:'操作',render:(_,row)=><Button type="link" onClick={()=>setSelected(row.task_id)}>查看详情</Button>},
      ]}/>
    </Card>
    <Drawer title="任务执行详情" open={selected!==null} onClose={()=>setSelected(null)} width="min(680px,100vw)">
      {detailError&&<Alert type="error" message={detailError}/>}
      {(detail??selectedRow)&&<Descriptions column={1} size="small" bordered items={[
        {key:'id',label:'任务 ID',children:<Typography.Text copyable>{selected}</Typography.Text>},
        {key:'state',label:'状态',children:<TaskState value={(detail??selectedRow)!.state}/>},
        {key:'actor',label:'操作人',children:(detail??selectedRow)!.actor},
        {key:'reason',label:'执行原因',children:(detail??selectedRow)!.reason},
        {key:'created',label:'创建时间',children:date((detail??selectedRow)!.created_at)},
      ]}/>}
      <Card size="small" title="Agent 执行回执" loading={detailLoading&&!detail} style={{marginTop:12}}>
        {detail?.receipt?<Space direction="vertical" style={{width:'100%'}}><Typography.Text type="secondary">接收时间：{date(detail.receipt.received_at)}</Typography.Text><pre className="hostops-terminal">{JSON.stringify(detail.receipt.result??{outcome:detail.receipt.outcome},null,2)}</pre></Space>:<Typography.Text type="secondary">尚无 Agent 执行回执，不代表执行成功。</Typography.Text>}
      </Card>
    </Drawer>
  </Flex>
}
