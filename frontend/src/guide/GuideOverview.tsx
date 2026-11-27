import { Collapse, Typography } from 'antd'
import './guide.css'
import { GUIDE_FAQ, GUIDE_MODULES, GUIDE_STEPS, RELEASE_ORDER, readGuideFacts, type GuideSource } from './facts.ts'

export function GuideOverview({ source }: { source: GuideSource }) {
  const facts = readGuideFacts(source)
  const items = [
    ['项目', facts.name],
    ['当前版本', facts.version],
    ['版本 SHA', facts.sha],
    ['环境', facts.environment],
    ['公网地址', facts.publicAddress],
    ['最近核对', facts.checkedAt],
  ] as const

  return <section className="usage-guide" aria-label="使用指南">
    <header className="usage-guide-hero">
      <Typography.Title level={2}>使用指南</Typography.Title>
      <Typography.Paragraph>{facts.summary}</Typography.Paragraph>
      <dl className="usage-guide-facts">
        {items.map(([label, value]) => <div key={label}>
          <dt>{label}</dt>
          <dd>{value}</dd>
        </div>)}
      </dl>
    </header>
    <div className="usage-guide-layout">
      <div className="usage-guide-main">
        <section aria-label="快速开始">
          <Typography.Title level={3}>快速开始</Typography.Title>
          <ol className="usage-guide-steps">
            {GUIDE_STEPS.map((step, index) => <li key={step.title}>
              <strong>{index + 1}. {step.title}</strong>
              <p>{step.detail}</p>
            </li>)}
          </ol>
        </section>
        <section aria-label="五个模块">
          <Typography.Title level={3}>五个模块</Typography.Title>
          <div className="usage-guide-modules">
            {GUIDE_MODULES.map(item => <article key={item.id}>
              <h4>{item.title}</h4>
              <p>{item.detail}</p>
            </article>)}
          </div>
          <Typography.Paragraph type="secondary">这些是说明，不是新的业务模块，也不会切换左侧导航。</Typography.Paragraph>
        </section>
      </div>
      <aside className="usage-guide-side" aria-label="升级说明与常见问题">
        <section>
          <Typography.Title level={3}>基座升级说明</Typography.Title>
          <p>本应用的发布顺序是{RELEASE_ORDER}。</p>
          <p>下游升级的正式来源是 main，preview 不是下游升级目标。版本、SHA 和调度结果仍在「基座版本」栏目；原有操作指南仍在「操作指南」栏目。</p>
        </section>
        <section>
          <Typography.Title level={3}>常见问题</Typography.Title>
          <Collapse
            items={GUIDE_FAQ.map(item => ({
              key: item.key,
              label: item.question,
              children: <p>{item.answer}</p>,
            }))}
          />
        </section>
      </aside>
    </div>
  </section>
}
