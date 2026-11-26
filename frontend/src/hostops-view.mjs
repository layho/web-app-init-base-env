export function hostState(value) {
  return ({online:{label:'在线',color:'success'},stale:{label:'心跳过期',color:'warning'},never_seen:{label:'尚未连接',color:'default'}})[value] ?? {label:'状态未知',color:'default'}
}
export function serviceState(value) {
  return ({running:{label:'运行中',color:'success'},loaded_idle:{label:'已加载 · 空闲',color:'blue'},last_exit_failed:{label:'最近退出异常',color:'error'},not_visible:{label:'当前域不可见',color:'default'},unsupported:{label:'平台不支持',color:'default'},unknown:{label:'状态未知',color:'warning'}})[value] ?? {label:'状态未知',color:'warning'}
}
export function diskView(disk) {
  if (!disk || ![disk.total_bytes,disk.used_bytes,disk.free_bytes].every(Number.isFinite) ||
      disk.total_bytes <= 0 || disk.used_bytes < 0 || disk.free_bytes < 0 ||
      Math.max(disk.used_bytes,disk.free_bytes) > disk.total_bytes) return null
  return {percent:Math.round(100*disk.used_bytes/disk.total_bytes),free:(disk.free_bytes/2**30).toFixed(1),total:(disk.total_bytes/2**30).toFixed(1)}
}
