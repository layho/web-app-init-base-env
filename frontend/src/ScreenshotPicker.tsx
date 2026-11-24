import {useState} from 'react'
import {Alert, Button, Image, Space, Typography, Upload} from 'antd'
export function ScreenshotPicker({value,onChange,disabled=false}:{value:string[];onChange:(v:string[])=>void;disabled?:boolean}){
  const [error,setError]=useState('')
  const [reading,setReading]=useState(false)
  async function add(files:File[]){
    if(disabled||reading)return
    setError('')
    if(value.length+files.length>9){setError('最多9张截图');return}
    if(files.some(f=>!['image/png','image/jpeg','image/webp'].includes(f.type)||f.size>10*1024*1024)){setError('仅支持PNG、JPG、WebP，单张不超过10MB');return}
    const existingBytes=value.reduce((sum,data)=>sum+Math.floor(data.length*3/4)-(data.endsWith('==')?2:data.endsWith('=')?1:0),0)
    if(existingBytes+files.reduce((sum,file)=>sum+file.size,0)>30*1024*1024){setError('截图总大小不得超过30MB');return}
    setReading(true)
    try{
      const encoded=await Promise.all(files.map(f=>new Promise<string>((resolve,reject)=>{
        const r=new FileReader();r.onload=()=>resolve(String(r.result).split(',')[1]);r.onerror=reject;r.readAsDataURL(f)
      })))
      onChange([...value,...encoded])
    }catch{setError('读取图片失败，请重新选择')}finally{setReading(false)}
  }
  return <Space direction="vertical" style={{width:'100%'}} onPaste={e=>{const files=Array.from(e.clipboardData.files);if(files.length){e.preventDefault();void add(files)}}}>
    <Upload.Dragger multiple accept="image/png,image/jpeg,image/webp" showUploadList={false} disabled={disabled||reading}
      beforeUpload={(_,batch)=>{if(_===batch[0])void add(batch);return false}}>
      <Typography.Text>{reading?'读取截图中…':'点击或拖入图片，也可在此粘贴截图'}</Typography.Text>
    </Upload.Dragger>
    {error&&<Alert type="error" message={error}/>}
    <Space wrap>{value.map((data,i)=><Space key={i} direction="vertical"><Image width={96} height={72} style={{objectFit:'contain'}} src={'data:'+ (data.startsWith('iVBOR')?'image/png':data.startsWith('UklGR')?'image/webp':'image/jpeg') + ';base64,'+data}/><Button size="small" danger disabled={disabled||reading} onClick={()=>onChange(value.filter((_,n)=>n!==i))}>删除</Button></Space>)}</Space>
    <Typography.Text type="secondary">最多9张，单张10MB，总计30MB；请先遮盖敏感信息。图片随表单保存，查看需登录或授权。</Typography.Text>
  </Space>
}
