import {useEffect,useRef,useState} from 'react'
import {Alert,Button,Card,Descriptions,Flex,Table,Tag,Typography} from 'antd'
type Policy={host_id:string;policy_version:string;dispatch_configured:boolean;agent_dispatch_active:boolean;operations:{operation:string;risk:string;allowed:boolean}[];service_mutation_allowed:boolean;remote_upgrade_allowed:boolean;arbitrary_shell_allowed:boolean}
type Modules={modules:{module_id:string;observed_digest:string|null;expected_digest:string|null;state:string;fresh:boolean}[];remote_upgrade_allowed:boolean}
export type EvidenceApi={policy:(signal:AbortSignal)=>Promise<Policy>;modules:(signal:AbortSignal)=>Promise<Modules>}
function useEvidence<T>(load:(signal:AbortSignal)=>Promise<T>){
  const ref=useRef(load);ref.current=load
  const [data,setData]=useState<T|null>(null)
  const [error,setError]=useState(false)
  const [loading,setLoading]=useState(true)
  const pending=useRef<AbortController|null>(null)
  async function refresh(){
    if(pending.current)return
    const c=new AbortController();pending.current=c;setLoading(true)
    try{const value=await ref.current(c.signal);if(!c.signal.aborted){setData(value);setError(false)}}
    catch{if(!c.signal.aborted){setData(null);setError(true)}}
    finally{if(pending.current===c){pending.current=null;if(!c.signal.aborted)setLoading(false)}}
  }
  useEffect(()=>{void refresh();const timer=window.setInterval(()=>void refresh(),15000);return()=>{window.clearInterval(timer);pending.current?.abort();pending.current=null}},[])
  return {data,error,loading,refresh}
}
export function HostOpsPolicy({api}:{api:EvidenceApi}){
  const {data,error,loading,refresh}=useEvidence(api.policy)
  return <Flex vertical gap={12}>
    {error&&<Alert type="warning" message="授权状态暂不可用"/>}
    <Card title="有效授权策略" extra={<Button loading={loading} onClick={()=>void refresh()}>刷新</Button>}>
      <Descriptions column={1} items={[
        {key:'host',label:'宿主',children:data?.host_id??'未确认'},
        {key:'policy',label:'策略版本',children:data?.policy_version??'未确认'},
        {key:'gateway',label:'服务端调度配置',children:data?data.dispatch_configured?'已配置':'未启用':'未确认'},
        {key:'agent',label:'Agent 调度心跳',children:data?.agent_dispatch_active?<Tag color="success">已上报启用</Tag>:<Tag>未确认启用</Tag>},
        {key:'service',label:'服务修改',children:<Tag>未开放</Tag>},
        {key:'upgrade',label:'远程升级',children:<Tag>未开放</Tag>},
        {key:'shell',label:'任意 Shell',children:<Tag>禁止</Tag>},
      ]}/>
      <Alert type="info" message="策略为固定只读白名单，不允许通过网页扩大宿主权限。"/>
    </Card>
    <Card title="允许的操作"><Table size="small" pagination={false} rowKey="operation" dataSource={data?.operations??[]} columns={[{title:'操作',dataIndex:'operation'},{title:'风险等级',dataIndex:'risk'},{title:'策略',dataIndex:'allowed',render:(v:boolean)=><Tag color={v?'blue':'default'}>{v?'允许':'拒绝'}</Tag>}]}/></Card>
  </Flex>
}
export function HostOpsModules({api}:{api:EvidenceApi}){
  const {data,error,loading,refresh}=useEvidence(api.modules)
  return <Flex vertical gap={12}>
    {error&&<Alert type="warning" message="模块版本证据暂不可用"/>}
    <Alert type="info" message="运行制品摘要来自 Agent 心跳，目标摘要来自独立本地配置。摘要一致不代表完整安全验收。"/>
    <Card title="模块版本核对" extra={<Button loading={loading} onClick={()=>void refresh()}>刷新</Button>}>
      {(data?.modules??[]).map(m=><Descriptions key={m.module_id} column={1} bordered size="small" items={[
        {key:'id',label:'模块',children:m.module_id},
        {key:'observed',label:'运行摘要',children:<Typography.Text code style={{overflowWrap:'anywhere'}}>{m.observed_digest??'未上报'}</Typography.Text>},
        {key:'expected',label:'目标摘要',children:<Typography.Text code style={{overflowWrap:'anywhere'}}>{m.expected_digest??'未配置'}</Typography.Text>},
        {key:'status',label:'核对结果',children:<Tag color={m.state==='matching'?'success':m.state==='different'?'warning':'default'}>{m.state==='matching'?'摘要一致':m.state==='different'?'摘要不同':'未确认'}</Tag>},
        {key:'fresh',label:'证据时效',children:m.fresh?'心跳有效':'无有效心跳'},
      ]}/>)}
      {!data?.modules.length&&<Typography.Text type="secondary">暂无模块证据</Typography.Text>}
      <Alert type="warning" style={{marginTop:12}} message="远程升级尚未开放；本地可信安装器需要独立校验及明确激活授权。"/>
    </Card>
  </Flex>
}
