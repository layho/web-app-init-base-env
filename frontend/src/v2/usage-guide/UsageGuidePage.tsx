import { Collapse } from 'antd'
import type { ProjectInfo } from '../../ProjectInfoPage'
import {
  GUIDE_FAQ,
  GUIDE_MODULES,
  GUIDE_STEPS,
  LIGHT_NAV,
  RELEASE_LOOP,
  RELEASE_ORDER,
  UPGRADE_STEPS,
} from './content.ts'
import { readGuideFacts, type RunningVersionFacts } from './facts.ts'
import './usage-guide.css'

export function UsageGuidePage({
  project,
  loading = false,
  username,
  running,
}: {
  project: ProjectInfo | null
  loading?: boolean
  username?: string | null
  running?: RunningVersionFacts | null
}) {
  const facts = readGuideFacts(project, running)
  const user = typeof username === 'string' && username.trim() ? username.trim() : '未记录'
  return <div className="v2-guide" data-shell="unwired">
    <header className="v2-guide-top">
      <div className="v2-guide-brand"><strong>基座验收中心</strong><span>Web App Init Base Env</span></div>
      <div className="v2-guide-top-meta">
        <span>当前版本 {facts.version}</span>
        <span>SHA {facts.sha}</span>
        <span>{user}</span>
      </div>
    </header>
    <div className="v2-guide-body">
      <nav className="v2-guide-nav" aria-label="使用指南预览导航">
        {LIGHT_NAV.map(item => <div key={item.key} className={item.key === 'guide' ? 'is-current' : ''} aria-current={item.key === 'guide' ? 'page' : undefined}>{item.label}</div>)}
        <p>浅色导航预览，尚未接入现有壳。</p>
      </nav>
      <main className="v2-guide-main">
        {loading && !project ? <div className="v2-guide-loading" aria-busy="true">正在读取项目信息</div> : <>
          <section className="v2-guide-hero">
            <div>
              <h1>使用指南</h1>
              <p>快速了解系统功能、使用方法与开发闭环流程。</p>
            </div>
          </section>
          <section className="v2-guide-summary" aria-label="项目摘要">
            <div><span>项目</span><strong>{facts.name}</strong><small>{facts.summary}</small></div>
            <div><span>当前版本</span><strong>{facts.version}</strong></div>
            <div><span>运行环境</span><strong>{facts.environment}</strong></div>
            <div><span>公网地址</span><strong>{facts.publicAddress}</strong></div>
            <div><span>最近更新</span><strong>{facts.checkedAt}</strong></div>
          </section>
          <div className="v2-guide-columns">
            <div>
              <section aria-label="快速开始">
                <h2>快速开始</h2>
                <ol className="v2-guide-steps">
                  {GUIDE_STEPS.map((step, index) => <li key={step.title}><b>{index + 1}</b><div><strong>{step.title}</strong><p>{step.detail}</p></div></li>)}
                </ol>
              </section>
              <section aria-label="功能模块说明">
                <h2>功能模块说明</h2>
                <div className="v2-guide-modules">
                  {GUIDE_MODULES.map(item => <article key={item.key}><h3>{item.title}</h3><p>{item.detail}</p></article>)}
                </div>
              </section>
              <section aria-label="开发闭环流程">
                <h2>开发闭环流程</h2>
                <p className="v2-guide-order">发布顺序是{RELEASE_ORDER}。</p>
                <ol className="v2-guide-loop">
                  {RELEASE_LOOP.map(item => <li key={item.title}><strong>{item.title}</strong><span>{item.detail}</span></li>)}
                </ol>
              </section>
            </div>
            <aside>
              <section>
                <h2>基座升级指南</h2>
                <ol className="v2-guide-upgrade">
                  {UPGRADE_STEPS.map((step, index) => <li key={step.title}><b>{index + 1}</b><div><strong>{step.title}</strong><p>{step.detail}</p></div></li>)}
                </ol>
              </section>
              <section>
                <h2>常见问题</h2>
                <Collapse items={GUIDE_FAQ.map(item => ({ key: item.key, label: item.question, children: <p>{item.answer}</p> }))} />
              </section>
            </aside>
          </div>
        </>}
      </main>
    </div>
  </div>
}
