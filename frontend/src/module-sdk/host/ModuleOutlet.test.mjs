import assert from 'node:assert/strict'
import { after, test } from 'node:test'
import { createElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { createServer } from 'vite'
const server = await createServer({server:{middlewareMode:true,hmr:false},appType:'custom'})
after(()=>server.close())
const { registerModules } = await server.ssrLoadModule('/src/module-sdk/index.ts')
const { ModuleOutlet } = await server.ssrLoadModule('/src/module-sdk/host/ModuleOutlet.tsx')
test('missing and hidden routes cannot invoke the loader', () => {
  let calls=0
  const registration=registerModules([{id:'notes-module',routeKey:'notes-module',title:'Notes',order:1,icon:'experiment',sdk:{min:'0.1.0',max:'0.1.0'},permission:'notes.read',load:async()=>{calls++;return {default:()=>null}}}])
  for (const routeKey of ['notes-module','workspace','missing-module']) {
    const html=renderToStaticMarkup(createElement(ModuleOutlet,{registration,routeKey,granted:[]}))
    assert.match(html,/模块不存在或不可访问/)
  }
  assert.equal(calls,0)
})
test('explicitly granted route enters lazy loading without mounting another page', () => {
  const registration=registerModules([{id:'notes-module',routeKey:'notes-module',title:'Notes',order:1,icon:'experiment',sdk:{min:'0.1.0',max:'0.1.0'},permission:'notes.read',load:async()=>({default:()=>null})}])
  const html=renderToStaticMarkup(createElement(ModuleOutlet,{registration,routeKey:'notes-module',granted:['notes.read']}))
  assert.match(html,/正在加载模块/)
})
