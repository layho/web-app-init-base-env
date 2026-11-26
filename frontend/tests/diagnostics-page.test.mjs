import assert from 'node:assert/strict'
import { after, test } from 'node:test'
import { createServer } from 'vite'
const server = await createServer({server:{middlewareMode:true,hmr:false},appType:'custom'})
after(()=>server.close())
const {diagnosticCategory,diagnosticSeverity} = await server.ssrLoadModule('/src/OperationsPage.tsx')
test('event classification uses actual codes, not fabricated health state',()=>{
  assert.equal(diagnosticCategory('feedback_failed'),'GitHub / 反馈')
  assert.equal(diagnosticCategory('update_test_build_failed'),'测试与构建')
  assert.equal(diagnosticCategory('rollback_ok'),'部署与恢复')
  assert.equal(diagnosticSeverity('rollback_failed'),'error')
  assert.equal(diagnosticSeverity('update_held'),'warning')
  assert.equal(diagnosticSeverity('update_started'),'info')
  assert.equal(diagnosticSeverity('update_ok'),'success')
})
